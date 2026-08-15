import time

from app import config
from app.redis_client import get_client

# Atomic token bucket. Reads current (tokens, last_refill_ts) from a Redis
# hash, refills based on elapsed wall-clock time, and — atomically, inside
# Redis — decides whether this request gets a token. Doing the read-refill-
# decide-write cycle in a single EVAL is what makes it safe under concurrent
# callers from multiple API instances; a plain GET-then-SET from Python would
# race.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill_per_sec)

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)

return allowed
"""

_script = None


def _get_script(client):
    global _script
    if _script is None:
        _script = client.register_script(_TOKEN_BUCKET_LUA)
    return _script


async def take_token(account_id: int) -> bool:
    """Returns True if the account had a token available (and it's now spent)."""
    client = get_client()
    script = _get_script(client)
    key = f"ratelimit:bucket:{account_id}"
    now = time.time()
    allowed = await script(
        keys=[key],
        args=[config.RATE_LIMIT_CAPACITY, config.RATE_LIMIT_REFILL_PER_SEC, now],
    )
    if not allowed:
        await client.incr(config.RATE_LIMIT_COUNTER_KEY)
    return bool(allowed)
