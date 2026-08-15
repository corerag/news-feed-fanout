import json

from app import config
from app.redis_client import get_client


async def is_backpressured() -> bool:
    client = get_client()
    val = await client.get(config.BACKPRESSURE_KEY)
    return val == "1"


async def enqueue_fanout(post_id: int, author_id: int, content: str, created_at: str, follower_ids: list[int]) -> tuple[int, int]:
    """Pushes one fan-out job per follower onto the Redis queue.

    Real load shedding: once the queue is at/over QUEUE_MAX_LEN, remaining
    jobs for this call are dropped and counted rather than pushed, and the
    backpressure flag is set so the API reports 'throttled' on this and
    subsequent posts until a worker drains the queue back below the resume
    threshold.

    Returns (enqueued_count, dropped_count).
    """
    client = get_client()
    enqueued = 0
    dropped = 0

    # Track depth locally between real LLEN checks so we don't pay a round
    # trip per follower on large fan-outs; re-synced from Redis every batch
    # so concurrent posts pushing into the same queue still get a real,
    # not-too-stale view of depth.
    depth = await client.llen(config.QUEUE_KEY)

    pipe = client.pipeline(transaction=False)
    pending = 0

    for follower_id in follower_ids:
        if depth >= config.QUEUE_MAX_LEN:
            dropped += 1
            continue
        job = json.dumps(
            {
                "follower_id": follower_id,
                "post_id": post_id,
                "author_id": author_id,
                "content": content,
                "created_at": created_at,
            }
        )
        pipe.rpush(config.QUEUE_KEY, job)
        pending += 1
        enqueued += 1
        depth += 1
        if pending >= 200:
            await pipe.execute()
            pipe = client.pipeline(transaction=False)
            pending = 0
            depth = await client.llen(config.QUEUE_KEY)

    if pending:
        await pipe.execute()

    if dropped:
        await client.incrby(config.DROPPED_COUNTER_KEY, dropped)

    depth = await client.llen(config.QUEUE_KEY)
    if depth >= config.QUEUE_MAX_LEN:
        await client.set(config.BACKPRESSURE_KEY, "1")

    return enqueued, dropped
