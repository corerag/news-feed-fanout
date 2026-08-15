from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import config, db, fanout
from app.admin import router as admin_router
from app.rate_limit import take_token
from app.redis_client import get_client, close_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.ensure_schema()
    yield
    await db.close_pool()
    await close_client()


app = FastAPI(title="News Feed Fan-out Demo", lifespan=lifespan)
app.include_router(admin_router)


class CreateUser(BaseModel):
    username: str


class CreateFollow(BaseModel):
    follower_id: int
    followee_id: int


class CreatePost(BaseModel):
    author_id: int
    content: str


@app.post("/users")
async def create_user(body: CreateUser):
    pool = await db.get_pool()
    try:
        row = await pool.fetchrow(
            "INSERT INTO users (username) VALUES ($1) RETURNING id, username, follower_count, is_celebrity",
            body.username,
        )
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"could not create user: {e}")
    return dict(row)


@app.post("/follow")
async def follow(body: CreateFollow):
    if body.follower_id == body.followee_id:
        raise HTTPException(status_code=400, detail="cannot follow yourself")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.execute(
                """
                INSERT INTO follows (follower_id, followee_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                body.follower_id,
                body.followee_id,
            )
            if inserted.endswith("0"):
                return {"ok": True, "already_following": True}
            row = await conn.fetchrow(
                """
                UPDATE users SET
                    follower_count = follower_count + 1,
                    is_celebrity = (follower_count + 1) >= $2
                WHERE id = $1
                RETURNING id, follower_count, is_celebrity
                """,
                body.followee_id,
                config.CELEBRITY_THRESHOLD,
            )
    return {"ok": True, "followee": dict(row)}


@app.post("/posts")
async def create_post(body: CreatePost):
    allowed = await take_token(body.author_id)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate limited: no tokens available")

    pool = await db.get_pool()
    author = await pool.fetchrow(
        "SELECT id, is_celebrity, follower_count FROM users WHERE id = $1", body.author_id
    )
    if author is None:
        raise HTTPException(status_code=404, detail="unknown author")

    post = await pool.fetchrow(
        "INSERT INTO posts (author_id, content) VALUES ($1, $2) RETURNING id, created_at",
        body.author_id,
        body.content,
    )
    post_id = post["id"]
    created_at = post["created_at"].isoformat()

    if author["is_celebrity"]:
        # Fan-out-on-read: single write to `posts` above, no queue, no
        # per-follower shard writes. Followers pick this up at feed-read time.
        return {
            "post_id": post_id,
            "mode": "fanout-on-read",
            "follower_count": author["follower_count"],
            "enqueued": 0,
            "dropped": 0,
            "throttled": await fanout.is_backpressured(),
        }

    follower_rows = await pool.fetch(
        "SELECT follower_id FROM follows WHERE followee_id = $1", body.author_id
    )
    follower_ids = [r["follower_id"] for r in follower_rows]

    enqueued, dropped = await fanout.enqueue_fanout(
        post_id, body.author_id, body.content, created_at, follower_ids
    )

    return {
        "post_id": post_id,
        "mode": "fanout-on-write",
        "follower_count": len(follower_ids),
        "enqueued": enqueued,
        "dropped": dropped,
        "throttled": await fanout.is_backpressured(),
    }


@app.get("/feed/{user_id}")
async def get_feed(user_id: int, limit: int = config.FEED_DEFAULT_LIMIT):
    pool = await db.get_pool()
    shard = db.shard_of(user_id)
    table = db.shard_table(shard)

    pushed_rows = await pool.fetch(
        f"""
        SELECT post_id, author_id, content, created_at
        FROM {table}
        WHERE follower_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )

    celeb_rows = await pool.fetch(
        """
        SELECT p.id AS post_id, p.author_id, p.content, p.created_at
        FROM posts p
        JOIN follows f ON f.followee_id = p.author_id
        JOIN users u ON u.id = p.author_id
        WHERE f.follower_id = $1 AND u.is_celebrity = TRUE
        ORDER BY p.created_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )

    items = [
        {**dict(r), "source": "push", "shard": shard} for r in pushed_rows
    ] + [{**dict(r), "source": "pull", "shard": None} for r in celeb_rows]
    items.sort(key=lambda r: r["created_at"], reverse=True)
    items = items[:limit]
    for item in items:
        item["created_at"] = item["created_at"].isoformat()

    return {"user_id": user_id, "shard": shard, "items": items}


@app.get("/metrics")
async def metrics():
    client = get_client()
    depth = await client.llen(config.QUEUE_KEY)
    dropped = await client.get(config.DROPPED_COUNTER_KEY)
    rate_limited = await client.get(config.RATE_LIMIT_COUNTER_KEY)
    backpressure = await client.get(config.BACKPRESSURE_KEY)

    shard_writes = {}
    for i in range(config.N_SHARDS):
        v = await client.get(f"{config.SHARD_COUNTER_PREFIX}{i}")
        shard_writes[i] = int(v) if v else 0

    return {
        "queue_depth": depth,
        "queue_max_len": config.QUEUE_MAX_LEN,
        "resume_threshold": int(config.QUEUE_MAX_LEN * config.QUEUE_RESUME_RATIO),
        "backpressure": backpressure == "1",
        "dropped_total": int(dropped) if dropped else 0,
        "rate_limited_total": int(rate_limited) if rate_limited else 0,
        "shard_writes": shard_writes,
        "n_shards": config.N_SHARDS,
    }


@app.get("/health")
async def health():
    pool = await db.get_pool()
    await pool.fetchval("SELECT 1")
    client = get_client()
    await client.ping()
    return {"status": "ok"}
