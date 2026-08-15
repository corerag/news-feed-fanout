import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db  # noqa: E402
from app.redis_client import get_client  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s worker[%(process)d] %(levelname)s %(message)s",
)
log = logging.getLogger("worker")

_running = True


def _handle_stop(signum, frame):
    global _running
    log.info("received signal %s, shutting down after current job", signum)
    _running = False


async def process_job(pool, client, raw: str) -> None:
    job = json.loads(raw)
    follower_id = job["follower_id"]
    post_id = job["post_id"]
    author_id = job["author_id"]
    content = job["content"]
    created_at = datetime.fromisoformat(job["created_at"])

    shard = db.shard_of(follower_id)
    table = db.shard_table(shard)

    await pool.execute(
        f"""
        INSERT INTO {table} (follower_id, post_id, author_id, content, created_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT DO NOTHING
        """,
        follower_id,
        post_id,
        author_id,
        content,
        created_at,
    )
    await client.incr(f"{config.SHARD_COUNTER_PREFIX}{shard}")


async def maybe_clear_backpressure(client) -> None:
    flagged = await client.get(config.BACKPRESSURE_KEY)
    if flagged != "1":
        return
    depth = await client.llen(config.QUEUE_KEY)
    resume_at = int(config.QUEUE_MAX_LEN * config.QUEUE_RESUME_RATIO)
    if depth <= resume_at:
        await client.delete(config.BACKPRESSURE_KEY)
        log.info("backpressure cleared: queue depth %d <= resume threshold %d", depth, resume_at)


async def main() -> None:
    await db.ensure_schema()
    pool = await db.get_pool()
    client = get_client()

    log.info(
        "worker starting: n_shards=%d queue_max_len=%d resume_ratio=%.2f",
        config.N_SHARDS,
        config.QUEUE_MAX_LEN,
        config.QUEUE_RESUME_RATIO,
    )

    processed = 0
    while _running:
        item = await client.blpop([config.QUEUE_KEY], timeout=config.WORKER_POP_TIMEOUT_SEC)
        if item is None:
            continue
        _, raw = item
        try:
            await process_job(pool, client, raw)
            processed += 1
            if processed % 500 == 0:
                log.info("processed %d jobs", processed)
        except Exception:
            log.exception("failed to process job: %s", raw)
        await maybe_clear_backpressure(client)

    log.info("worker exiting, processed %d jobs total", processed)
    await db.close_pool()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    asyncio.run(main())
