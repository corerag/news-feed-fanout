import random
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app import config, db

router = APIRouter()


class SeedRequest(BaseModel):
    n_users: int = 500
    avg_follows_per_user: int = 20
    n_celebrities: int = 2
    celebrity_followers: int = 300


def _check_token(x_admin_token: str | None):
    if x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="bad or missing X-Admin-Token")


@router.post("/admin/seed")
async def seed(body: SeedRequest, x_admin_token: str | None = Header(default=None)):
    _check_token(x_admin_token)

    pool = await db.get_pool()
    run_id = uuid4().hex[:8]
    regular_usernames = [f"lt_{run_id}_u{i}" for i in range(body.n_users)]
    celeb_usernames = [f"lt_{run_id}_c{i}" for i in range(body.n_celebrities)]
    rng = random.Random(run_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            regular_rows = await conn.fetch(
                "INSERT INTO users (username) SELECT unnest($1::text[]) RETURNING id",
                regular_usernames,
            )
            regular_ids = [r["id"] for r in regular_rows]

            celeb_ids = []
            if celeb_usernames:
                celeb_rows = await conn.fetch(
                    "INSERT INTO users (username, is_celebrity) SELECT unnest($1::text[]), TRUE RETURNING id",
                    celeb_usernames,
                )
                celeb_ids = [r["id"] for r in celeb_rows]

            follow_records: list[tuple[int, int]] = []
            k_regular = min(body.avg_follows_per_user, max(len(regular_ids) - 1, 0))
            for uid in regular_ids:
                pool_ids = [i for i in regular_ids if i != uid]
                for followee in rng.sample(pool_ids, min(k_regular, len(pool_ids))):
                    follow_records.append((uid, followee))

            k_celeb = min(body.celebrity_followers, len(regular_ids))
            for cid in celeb_ids:
                for uid in rng.sample(regular_ids, k_celeb):
                    follow_records.append((uid, cid))

            if follow_records:
                await conn.copy_records_to_table(
                    "follows", records=follow_records, columns=["follower_id", "followee_id"]
                )

            await conn.execute(
                """
                UPDATE users u SET
                    follower_count = COALESCE(sub.cnt, 0),
                    is_celebrity = COALESCE(sub.cnt, 0) >= $1
                FROM (
                    SELECT followee_id, count(*) AS cnt FROM follows GROUP BY followee_id
                ) sub
                WHERE u.id = sub.followee_id
                """,
                config.CELEBRITY_THRESHOLD,
            )

    return {
        "run_id": run_id,
        "regular_user_ids": regular_ids,
        "celebrity_user_ids": celeb_ids,
        "follow_edges": len(follow_records),
    }


@router.post("/admin/reset-metrics")
async def reset_metrics(x_admin_token: str | None = Header(default=None)):
    _check_token(x_admin_token)
    from app.redis_client import get_client

    client = get_client()
    keys = [config.DROPPED_COUNTER_KEY, config.RATE_LIMIT_COUNTER_KEY, config.BACKPRESSURE_KEY]
    keys += [f"{config.SHARD_COUNTER_PREFIX}{i}" for i in range(config.N_SHARDS)]
    if keys:
        await client.delete(*keys)
    return {"ok": True}
