"""
Load test for the news feed fan-out demo.

Fires concurrent POST /posts (regular + celebrity accounts) and concurrent
GET /feed/{user_id} reads against a *running* deployment (local docker-compose
or a live deploy), and reports real p50/p99 latency, throughput, queue drain
rate, and drop/rate-limit counts pulled from the live /metrics endpoint.

Usage:
    python scripts/load_test.py --base-url http://localhost:8000 --label "shards=4 workers=3"
"""

import argparse
import asyncio
import json
import random
import statistics
import string
import sys
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Sample:
    ok: bool
    status: int
    latency_ms: float


@dataclass
class PhaseResult:
    name: str
    samples: list = field(default_factory=list)
    wall_seconds: float = 0.0

    def summary(self) -> dict:
        oks = [s for s in self.samples if s.ok]
        latencies = sorted(s.latency_ms for s in oks)
        errors = len(self.samples) - len(oks)

        def pct(p):
            if not latencies:
                return None
            idx = min(len(latencies) - 1, int(len(latencies) * p))
            return round(latencies[idx], 2)

        return {
            "phase": self.name,
            "requests": len(self.samples),
            "errors": errors,
            "wall_seconds": round(self.wall_seconds, 3),
            "throughput_rps": round(len(oks) / self.wall_seconds, 2) if self.wall_seconds > 0 else None,
            "p50_ms": pct(0.50),
            "p90_ms": pct(0.90),
            "p99_ms": pct(0.99),
            "max_ms": round(latencies[-1], 2) if latencies else None,
        }


def random_content(n=60):
    return "".join(random.choices(string.ascii_letters + " ", k=n))


async def run_batch(coro_factory, count: int, concurrency: int) -> PhaseResult:
    sem = asyncio.Semaphore(concurrency)
    samples: list[Sample] = []

    async def worker(i):
        async with sem:
            t0 = time.perf_counter()
            try:
                status = await coro_factory(i)
                ok = 200 <= status < 300
            except Exception:
                status = -1
                ok = False
            dt_ms = (time.perf_counter() - t0) * 1000
            samples.append(Sample(ok=ok, status=status, latency_ms=dt_ms))

    t_start = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(count)))
    wall = time.perf_counter() - t_start

    result = PhaseResult(name="", samples=samples, wall_seconds=wall)
    return result


async def seed(client: httpx.AsyncClient, args) -> dict:
    resp = await client.post(
        "/admin/seed",
        json={
            "n_users": args.n_users,
            "avg_follows_per_user": args.avg_follows,
            "n_celebrities": args.n_celebrities,
            "celebrity_followers": args.celebrity_followers,
        },
        headers={"X-Admin-Token": args.admin_token},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


async def reset_metrics(client: httpx.AsyncClient, args) -> None:
    resp = await client.post(
        "/admin/reset-metrics", headers={"X-Admin-Token": args.admin_token}, timeout=30
    )
    resp.raise_for_status()


async def wait_for_drain(client: httpx.AsyncClient, timeout_s: float) -> dict:
    t0 = time.perf_counter()
    last = None
    while time.perf_counter() - t0 < timeout_s:
        resp = await client.get("/metrics", timeout=10)
        last = resp.json()
        if last["queue_depth"] == 0:
            break
        await asyncio.sleep(0.25)
    return {"drain_seconds": round(time.perf_counter() - t0, 3), "final_metrics": last}


async def main_async(args):
    async with httpx.AsyncClient(base_url=args.base_url) as client:
        print(f"Seeding: {args.n_users} users, {args.n_celebrities} celebrities...")
        seeded = await seed(client, args)
        await reset_metrics(client, args)
        regular_ids = seeded["regular_user_ids"]
        celeb_ids = seeded["celebrity_user_ids"]
        print(
            f"  -> {len(regular_ids)} regular users, {len(celeb_ids)} celebrities, "
            f"{seeded['follow_edges']} follow edges"
        )

        results = []

        # Phase 1: regular-account posts (fan-out-on-write, hits the queue)
        async def post_regular(i):
            author = random.choice(regular_ids)
            resp = await client.post(
                "/posts", json={"author_id": author, "content": random_content()}, timeout=30
            )
            return resp.status_code

        phase = await run_batch(post_regular, args.num_posts, args.post_concurrency)
        phase.name = "post_regular (fan-out-on-write)"
        results.append(phase)
        print(f"  {phase.name}: {phase.summary()}")

        # Phase 2: celebrity posts (fan-out-on-read, no queue)
        if celeb_ids:
            async def post_celeb(i):
                author = random.choice(celeb_ids)
                resp = await client.post(
                    "/posts", json={"author_id": author, "content": random_content()}, timeout=30
                )
                return resp.status_code

            phase = await run_batch(post_celeb, min(50, args.num_posts), args.post_concurrency)
            phase.name = "post_celebrity (fan-out-on-read)"
            results.append(phase)
            print(f"  {phase.name}: {phase.summary()}")

        # Drain the queue and measure how long real workers take
        print("Waiting for queue to drain...")
        drain = await wait_for_drain(client, timeout_s=120)
        print(f"  drain: {drain}")

        # Phase 3: feed reads (push shard + pull celebrity merge)
        async def read_feed(i):
            uid = random.choice(regular_ids)
            resp = await client.get(f"/feed/{uid}", timeout=30)
            return resp.status_code

        phase = await run_batch(read_feed, args.num_reads, args.read_concurrency)
        phase.name = "get_feed (push+pull merge)"
        results.append(phase)
        print(f"  {phase.name}: {phase.summary()}")

        final_metrics = (await client.get("/metrics", timeout=10)).json()

        report = {
            "label": args.label,
            "base_url": args.base_url,
            "config": {
                "n_users": args.n_users,
                "avg_follows": args.avg_follows,
                "n_celebrities": args.n_celebrities,
                "celebrity_followers": args.celebrity_followers,
                "num_posts": args.num_posts,
                "post_concurrency": args.post_concurrency,
                "num_reads": args.num_reads,
                "read_concurrency": args.read_concurrency,
            },
            "phases": [r.summary() for r in results],
            "drain": drain,
            "final_metrics": final_metrics,
        }

        print("\n=== SUMMARY ===")
        print(json.dumps(report, indent=2))

        if args.out:
            with open(args.out, "w") as f:
                json.dump(report, f, indent=2)
            print(f"\nWrote {args.out}")

        return report


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--admin-token", default="dev-seed-token")
    p.add_argument("--label", default="")
    p.add_argument("--n-users", type=int, default=500)
    p.add_argument("--avg-follows", type=int, default=20)
    p.add_argument("--n-celebrities", type=int, default=1)
    p.add_argument("--celebrity-followers", type=int, default=300)
    p.add_argument("--num-posts", type=int, default=200)
    p.add_argument("--post-concurrency", type=int, default=20)
    p.add_argument("--num-reads", type=int, default=1000)
    p.add_argument("--read-concurrency", type=int, default=50)
    p.add_argument("--out", default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except httpx.HTTPStatusError as e:
        print(f"HTTP error: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
