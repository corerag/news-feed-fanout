import os


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://feed:feed@postgres:5432/feed"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Sharding
N_SHARDS = _int("N_SHARDS", 4)

# Postgres connection pool (deliberately modest by default so pool exhaustion
# is something the load test can actually surface, not something hidden
# behind an unrealistically large pool).
POOL_MIN_SIZE = _int("POOL_MIN_SIZE", 2)
POOL_MAX_SIZE = _int("POOL_MAX_SIZE", 10)

# Fan-out queue / backpressure
QUEUE_KEY = "fanout:queue"
QUEUE_MAX_LEN = _int("QUEUE_MAX_LEN", 500)
QUEUE_RESUME_RATIO = _float("QUEUE_RESUME_RATIO", 0.6)
BACKPRESSURE_KEY = "fanout:backpressure"
DROPPED_COUNTER_KEY = "fanout:dropped"
SHARD_COUNTER_PREFIX = "fanout:shard:writes:"

# Fan-out-on-read threshold: users with follower_count >= this are flagged
# is_celebrity and are switched from fan-out-on-write to fan-out-on-read.
CELEBRITY_THRESHOLD = _int("CELEBRITY_THRESHOLD", 1000)

# Token bucket rate limiter (per account, applied to POST /posts)
RATE_LIMIT_CAPACITY = _float("RATE_LIMIT_CAPACITY", 5)
RATE_LIMIT_REFILL_PER_SEC = _float("RATE_LIMIT_REFILL_PER_SEC", 0.5)
RATE_LIMIT_COUNTER_KEY = "ratelimit:blocked"

FEED_DEFAULT_LIMIT = _int("FEED_DEFAULT_LIMIT", 30)

WORKER_POP_TIMEOUT_SEC = _int("WORKER_POP_TIMEOUT_SEC", 1)

# Required header (X-Admin-Token) to hit the bulk-seed endpoint used by the
# load test script. Not a real auth system -- just enough to keep the seed
# endpoint from being wide open on a public deployment.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-seed-token")
