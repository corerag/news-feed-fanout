# News Feed Fan-out — Real System

A running implementation of the fan-out/sharding/backpressure/rate-limiting
architecture from [`newsfeed-fanout-demo`](https://github.com/corerag/newsfeed-fanout-demo)
(a single-file HTML/JS simulation of the *System Design Primer* news feed).
This is the same architecture built as an actual FastAPI + Postgres + Redis
system: real SQL tables, a real Redis queue, real worker processes, and a
load test against the real thing.

**Live:** https://api-production-ee8f.up.railway.app (`/health`, `/metrics`,
`/feed/{id}`, etc. — see [DEPLOY.md](DEPLOY.md) for how it was deployed and
why Railway instead of Fly.io). Nothing here is animated — every number
in the "Real load test results" section below came from hitting a running
instance with concurrent HTTP requests and reading real Postgres/Redis
state back out.

## Architecture

- **API** (FastAPI, `app/`) — `POST /users`, `POST /follow`, `POST /posts`,
  `GET /feed/{user_id}`, `GET /metrics`, plus `POST /admin/seed` for load-test
  data generation.
- **Postgres** — `users`, `follows`, `posts` tables, plus `feed_shard_0 .. feed_shard_{N-1}`,
  one real table per shard. `follower_id % N_SHARDS` picks the shard, exactly
  like the simulation's diagram — except these are actual tables with actual
  indexes, created by `app/db.py::ensure_schema()` on startup.
- **Redis** — the fan-out job queue (`fanout:queue`, a Redis list drained with
  `BLPOP`), the backpressure flag, drop/rate-limit counters, per-shard write
  counters, and the token-bucket rate limiter. `GET /metrics` reads these
  live — it is not derived from in-process state, so it is correct across
  multiple API instances too.
- **Worker** (`worker/worker.py`) — a standalone process, run as many
  instances as you want, that pops jobs and writes into the correct shard
  table. Multiple workers share the queue naturally because Redis `BLPOP`
  is atomic across consumers.
- **Fan-out-on-write** — regular accounts: one job per follower is pushed to
  Redis; workers write `(follower_id, post_id, ...)` rows into the follower's
  shard.
- **Fan-out-on-read** — accounts with `follower_count >= CELEBRITY_THRESHOLD`
  (default 1000) skip the queue entirely: one row in `posts`, nothing else.
  `GET /feed/{user_id}` merges the follower's shard rows (push) with a live
  query for posts from any celebrity they follow (pull), sorted by time.
- **Backpressure** — `fanout:queue` is capped at `QUEUE_MAX_LEN` (real
  `LLEN`). Once at cap, further fan-out jobs for that post are dropped and
  counted (`fanout:dropped`), and a `fanout:backpressure` flag is set. A
  worker clears the flag once depth drains back below
  `QUEUE_MAX_LEN * QUEUE_RESUME_RATIO`.
- **Rate limiting** — a Lua-script token bucket per account
  (`ratelimit:bucket:{account_id}`), atomic via a single `EVAL`, applied to
  `POST /posts`. Survives multiple API instances because the state lives in
  Redis, not memory.

## Running it locally

### Docker (one command)

```
docker compose up --build --scale worker=3
```

Brings up Postgres, Redis, the API (`:8000`), and 3 worker containers.
Override shard/worker/queue/rate-limit knobs via `.env` (copy `.env.example`)
or inline: `N_SHARDS=8 docker compose up --build --scale worker=6`.

> Note: this repo's actual local validation and load-test numbers below were
> produced *without* Docker (no Docker daemon was available in the dev
> environment) — Postgres and Redis were run as native local processes and
> the FastAPI/worker code run directly with `uvicorn`/`python`. The
> docker-compose topology is the same code, same schema, same queue; it
> just wasn't the harness used to produce these specific numbers. If you
> reproduce this with docker-compose you should expect the same *shape* of
> results, with somewhat higher latency from the extra container networking
> hop.

### Without Docker

```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
# start your own Postgres + Redis, then:
export DATABASE_URL=postgresql://feed@127.0.0.1:5433/feed
export REDIS_URL=redis://127.0.0.1:6380/0
.venv/Scripts/python -m uvicorn app.main:app --port 8000
.venv/Scripts/python worker/worker.py   # run N times for N workers
```

## Load test

```
python scripts/load_test.py --base-url http://localhost:8000 --label "shards=4 workers=3"
```

Seeds users/follows via `POST /admin/seed` (bulk `COPY`, not one HTTP call
per follow), fires concurrent `POST /posts` (regular + celebrity accounts)
and concurrent `GET /feed/{id}`, and reports real p50/p90/p99 latency,
throughput, queue-drain time, and final `/metrics` (drops, rate limits,
per-shard write counts) — all read from the live system, not computed
client-side.

## Real load test results

Environment: native Windows processes, loopback network (127.0.0.1),
Postgres 17, standalone Redis. 800 users, ~15 follows/user (12,000 edges),
1 celebrity account with 500 followers. 150 concurrent posts
(concurrency 20), 1500 concurrent feed reads (concurrency 60), per run.

| Config | post p50/p99 (ms) | feed p50/p99 (ms) | queue drain | dropped jobs | rate-limited |
|---|---|---|---|---|---|
| 4 shards / 3 workers / pool=10 | 29.8 / 148.0 | 63.9 / 416.1 | 0.255s | 3,318 | 45 |
| 8 shards / 3 workers / pool=10 | 28.4 / 135.1 | 62.2 / 414.1 | 0.263s | 3,843 | 45 |
| 4 shards / 1 worker  / pool=10 | 24.4 / 99.6  | 66.8 / 407.2 | 0.265s | 3,890 | 45 |
| 4 shards / 3 workers / pool=60 | 32.6 / 166.7 | 61.3 / 440.5 | 0.265s | 3,173 | 45 |
| **LIVE** on Railway, 3 workers, pool_min=2/max=10 | 136.0 / 301.2 | 150.6 / 375.8 | 4.11s | 1,117 | 45 |
| **LIVE** on Railway, 6 workers, pool_min=1/max=3 | 122.2 / 344.4 | 149.5 / 816.8 | **3.58s** | 1,003 | 45 |

Full JSON output for each run is in `load_test_results/`. The live run used a
smaller seed (300 users vs 800) since it's a shared low-tier deployment, not
a benchmark rig — so drop counts aren't directly comparable to the local
runs, but the latency and drain numbers are the real story:

**Local loopback numbers are not a preview of live numbers.** Post p50 went
from ~30ms to 136ms, feed p50 from ~64ms to 151ms, and — most notably —
queue drain time went from ~0.26s to **4.11s**, a >15x jump, despite only
675 jobs actually being written (vs ~1,300-1,400 locally) — working out to
roughly 6-18ms per job depending on how you attribute it across the 3
workers, versus sub-millisecond locally. Locally, worker-to-Postgres round
trips are effectively free because everything is on `127.0.0.1`. On
Railway, the worker, Redis, and Postgres are three separate services
talking over the platform's internal network, and each `BLPOP` + `INSERT`
cycle pays real (if small) per-job network latency, done sequentially with
no batching. This is the exact mechanism the local-only testing in this
repo could never have caught: at local scale, worker count essentially
didn't matter (finding #1 above); on real infrastructure, per-job network
latency is the real cost, and worker count should matter far more than the
loopback tests suggested — a real deployment is the only way that shows up.

Shard writes came out even in both configs (e.g. 8-shard run: 99, 102, 98,
115, 97, 115, 100, 105 — `follower_id % N` really does spread load evenly
once you have enough distinct follower ids).

**Doubling live workers (3 → 6) to compensate for the network-latency finding
above got a real but unglamorous result.** Drain time improved 4.11s →
3.58s — about 13%, nowhere near the ~2x you'd hope for from doubling worker
count. Worse, `get_feed` p99 got noticeably *worse* (375.8ms → 816.8ms) at
the same read concurrency. The most plausible read, without deeper Postgres
instrumentation than this repo has: Railway's managed Postgres here is a
single small shared instance, and 6 workers hammering it with `INSERT`s
concurrently with 25 concurrent feed-reader connections creates more
contention on that one instance than 3 workers did — so "scale out the
workers" isn't a free win once the bottleneck has moved to a shared,
resource-constrained database rather than per-job network latency. Fixing
*that* would mean a bigger Postgres plan or read replicas, not more
workers — a different lever than the one this task started with, and only
visible because the scale-up was actually tested against the live system
instead of assumed to help.

## Where the real system differed from the HTML simulation

The simulation (`newsfeed-fanout-demo`) models: a worker "tick" every 380ms
that drains exactly one job, a queue capacity slider (6–20), a resume ratio
of 0.6, and a token bucket refilling one token per 3s. Building the real
thing surfaced several things that model doesn't (and structurally can't)
show:

1. **Worker count barely mattered at this scale, and that's itself the
   finding.** 1 worker vs 3 workers produced statistically indistinguishable
   drain times (0.255s vs 0.265s) and dropped-job counts. The simulation's
   fixed 380ms-per-job tick makes worker count feel like the dominant lever
   on drain speed — visually, more worker circles obviously drain the queue
   faster. On a real local Postgres, a single `INSERT ... ON CONFLICT DO
   NOTHING` takes sub-millisecond, so even one worker clears a 500-deep
   queue before the next load-test phase even starts. Worker count only
   starts to matter once each write costs real network/IO time (a real
   remote Postgres, not loopback) or the queue depth is sustained rather
   than a single burst — see the live-deploy notes below.

2. **The `throttled` flag is a genuinely racy signal, not a reliable one.**
   In a deterministic single-request test (one account, 300 followers,
   `QUEUE_MAX_LEN=50`), the fan-out correctly capped at exactly 50 enqueued
   / 250 dropped — but by the time the same HTTP response was being
   assembled, the single local worker had *already* drained the queue back
   below the resume threshold and cleared the flag, so the API's own
   response reported `"throttled": false` for the request that had just
   shed 250 jobs. The simulation's backpressure state is deliberately
   sticky so you can click around and inspect it; the real Redis flag can
   flip on and off within a single request lifecycle. The dropped-job
   counter is the trustworthy signal here, not the boolean.

3. **Concurrent posts can overshoot `QUEUE_MAX_LEN`, because backpressure
   isn't one atomic global gate.** `enqueue_fanout()` re-checks real Redis
   `LLEN` only once per call (and every 200 pushes within a large fan-out),
   not before every single `RPUSH`, to avoid a Redis round-trip per
   follower. Under concurrent posts this is a genuine TOCTOU race: several
   requests can each see room under the cap and all push, overshooting it
   before anyone notices. That's why dropped-job totals across runs (3,173
   – 3,890) don't cleanly match a hand-computed "sum of follower counts
   minus cap" estimate — real concurrent producers racing a shared queue
   behave differently from a single-threaded JS loop that can never race
   itself.

4. **The read-latency tail is not what it looks like, and only testing
   found that.** `GET /feed/{id}` p99 sat around 410–440ms at 60 concurrent
   readers across every config tested — including shard count (4 vs 8),
   worker count (1 vs 3), *and* Postgres pool size (10 vs 60). The pool-size
   test was specifically run to confirm a hypothesis that the default
   `POOL_MAX_SIZE=10` was starving 60 concurrent readers; widening it to 60
   changed nothing (p99 440.5ms vs 416.1ms, if anything slightly worse).
   The tail is far more consistent with single-process, single-event-loop
   request queueing — this repo runs one `uvicorn` worker process, so 60
   concurrent requests share one Python event loop thread. The simulation
   has no notion of a process/event-loop boundary at all, so it can't show
   you this; a naive read of "add more Postgres connections" (a very
   reasonable-sounding fix) would have been the wrong fix, and only running
   the real load test with a real control group revealed that.

5. **The rate limiter makes single-account load tests look broken if you
   don't know it's there.** Hammering the one celebrity account
   concurrently rate-limited 45 of 50 posts (429s) — correct behavior for a
   5-token bucket refilling at 0.5/s, not a bug, but worth knowing before
   you assume your load generator found a real failure. The simulation's
   separate "push lane" / "pull lane" per-account buckets read the same way
   in a demo, but a real test harness posting from a small pool of accounts
   will hit this immediately in a way a slider-driven demo never forces you
   to confront.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `N_SHARDS` | 4 | Number of `feed_shard_i` tables |
| `QUEUE_MAX_LEN` | 500 | Backpressure cap on `fanout:queue` |
| `QUEUE_RESUME_RATIO` | 0.6 | Resume threshold as a fraction of cap |
| `CELEBRITY_THRESHOLD` | 1000 | `follower_count` at which an account flips to fan-out-on-read |
| `RATE_LIMIT_CAPACITY` | 5 | Token bucket capacity per account |
| `RATE_LIMIT_REFILL_PER_SEC` | 0.5 | Token bucket refill rate |
| `POOL_MIN_SIZE` / `POOL_MAX_SIZE` | 2 / 10 | asyncpg pool bounds |
| `ADMIN_TOKEN` | `dev-seed-token` | Required `X-Admin-Token` header for `/admin/*` |

## Live deployment

See [DEPLOY.md](DEPLOY.md).
