# Deploying (Railway)

Live instance: **https://api-production-ee8f.up.railway.app**
(`/admin/*` requires an `X-Admin-Token` header — value lives in the `api`
service's Railway variables, not published here or in git history)

This was deployed with the Railway CLI, no dashboard clicking required.
Fly.io was tried first but consistently rejected app creation with a billing
error even after a card was added to the account — see the note at the
bottom. Railway worked cleanly.

## Steps actually run

```
npm install -g @railway/cli
railway login                      # interactive browser OAuth
railway init --name news-feed-fanout

railway add --database postgres    # managed Postgres service
railway add --database redis       # managed Redis service
railway add --service api          # empty service, source pushed via `up`
railway add --service worker

railway variables --service api --set "DATABASE_URL=\${{Postgres.DATABASE_URL}}" \
  --set "REDIS_URL=\${{Redis.REDIS_URL}}" \
  --set "N_SHARDS=4" --set "QUEUE_MAX_LEN=500" --set "QUEUE_RESUME_RATIO=0.6" \
  --set "CELEBRITY_THRESHOLD=1000" --set "RATE_LIMIT_CAPACITY=5" \
  --set "RATE_LIMIT_REFILL_PER_SEC=0.5" --set "POOL_MIN_SIZE=2" \
  --set "POOL_MAX_SIZE=10" --set "ADMIN_TOKEN=<generate your own, don't reuse a published one>" --set "PORT=8000"

railway variables --service worker --set "DATABASE_URL=\${{Postgres.DATABASE_URL}}" \
  --set "REDIS_URL=\${{Redis.REDIS_URL}}" --set "N_SHARDS=4" \
  --set "QUEUE_MAX_LEN=500" --set "QUEUE_RESUME_RATIO=0.6" \
  --set "POOL_MIN_SIZE=2" --set "POOL_MAX_SIZE=10" --set "SERVICE_ROLE=worker"

railway up --service api --ci
railway up --service worker --ci

railway domain --service api
railway service scale --service worker us-west=3
```

## One image, two roles

Railway's CLI-driven flow doesn't give a clean way to set a different start
command per service when both deploy from the same source directory without
a dashboard visit. Rather than duplicate the Dockerfile, `entrypoint.sh`
branches on a `SERVICE_ROLE` env var:

```sh
if [ "$SERVICE_ROLE" = "worker" ]; then
    exec python worker/worker.py
else
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
```

The `api` service has no `SERVICE_ROLE` set (defaults to the API); `worker`
has `SERVICE_ROLE=worker`. Same image, same build, two roles — and it means
`railway up --service worker` doesn't need any special build config at all.

## Scaling workers

```
railway service scale --service worker us-west=3
```

Railway scales by region+replica-count pairs, not a flat instance count —
`railway service scale` output is `{region: replicas}`. The initial
single-instance deploy landed in a default region; scaling explicitly to
`us-west=3` and zeroing the old region (`sfo=0`) is what actually produced 3
running worker instances.

Later scaled to 6 (`railway service scale --service worker us-west2=6`),
along with shrinking each worker's own Postgres pool
(`POOL_MIN_SIZE=1`/`POOL_MAX_SIZE=3` — a single worker loop only ever holds
one connection at a time, so a bigger per-replica pool was pure waste). The
result wasn't a clean win: drain time improved ~13% but feed-read p99 got
noticeably worse, consistent with 6 workers adding real write contention on
Railway's single shared Postgres instance. See the README's load test
section for the numbers — worth reading before assuming "more workers" is
the right lever on a given deployment.

## Why not Fly.io

`fly apps create` failed repeatedly with `We need your payment information
to continue`, even after a card was confirmed saved on the account's billing
page, across 6+ attempts and a 3-minute wait for propagation. Whatever the
underlying cause (account-level verification beyond just a card, a stuck
billing-status cache, something else), the CLI gave no path to diagnose it
further. Railway's `add --database` / `add --service` / `up` flow worked on
the first real attempt once logged in.

## Admin token rotation

An earlier commit published the literal `ADMIN_TOKEN` value in this file.
It has been rotated on Railway (the old value now returns 403) and scrubbed
from this doc going forward — but it's still visible in git history for
this repo, so treat that specific string as permanently burned, not secret.
If you fork or reuse this code, generate your own token; don't copy one
that's ever been committed anywhere.

## Live vs local load test

See the README's load test results table — the live deployment numbers
(`run5_live_railway.json`) were captured against this exact URL, not a
simulation of it.
