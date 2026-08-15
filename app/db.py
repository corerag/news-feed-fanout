import asyncpg

from app import config

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=config.POOL_MIN_SIZE,
            max_size=config.POOL_MAX_SIZE,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


SHARD_TABLE_TEMPLATE = "feed_shard_{i}"


def shard_table(shard_id: int) -> str:
    return SHARD_TABLE_TEMPLATE.format(i=shard_id)


def shard_of(follower_id: int) -> int:
    return follower_id % config.N_SHARDS


async def ensure_schema() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                follower_count BIGINT NOT NULL DEFAULT 0,
                is_celebrity BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS follows (
                follower_id BIGINT NOT NULL REFERENCES users(id),
                followee_id BIGINT NOT NULL REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (follower_id, followee_id)
            );
            CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows(followee_id);
            CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_id);

            CREATE TABLE IF NOT EXISTS posts (
                id BIGSERIAL PRIMARY KEY,
                author_id BIGINT NOT NULL REFERENCES users(id),
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_posts_author_created
                ON posts(author_id, created_at DESC);
            """
        )
        for i in range(config.N_SHARDS):
            table = shard_table(i)
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    follower_id BIGINT NOT NULL,
                    post_id BIGINT NOT NULL,
                    author_id BIGINT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (follower_id, post_id)
                );
                CREATE INDEX IF NOT EXISTS idx_{table}_follower_created
                    ON {table}(follower_id, created_at DESC);
                """
            )
