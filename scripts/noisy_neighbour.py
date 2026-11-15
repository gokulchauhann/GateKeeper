"""
Noisy-neighbour test for Gatekeeper: how much does one tenant's heavy load
slow down another tenant, with cgroup limits on and off?

1. Baseline: the victim tenant loads a normal page (its orders page) on a
   quiet system, --samples times.
2. Load: the heavy tenant(s) run the heavy report concurrently and keep
   starting new ones for --duration seconds, while the victim loads the same
   page again.
3. It prints the victim's median and 95th-percentile response time in both
   phases and the slowdown, plus how the heavy reports went.

Run it on the machine that runs the backend, once per mode:
    bash gatekeeper_real.sh off     # GOVERNOR=unlimited: limits off, measured
    ./venv/bin/python scripts/noisy_neighbour.py
    bash gatekeeper_real.sh         # GOVERNOR=real: limits on
    ./venv/bin/python scripts/noisy_neighbour.py

Options:
    --base-url URL      backend address (default http://127.0.0.1:8000)
    --heavy USERS       comma-separated heavy tenant users (default acme)
    --victim USER       victim tenant user (default globex)
    --concurrency N     concurrent heavy reports per heavy user
                        (default: CPU cores, at most 5 = one tenant's pool size)
    --duration S        seconds of heavy load (default 30)
    --samples N         victim requests per phase (default 20)
    --out FILE          also save the results as JSON

One heavy report uses at most one CPU core (parallel query is off), so to make
the machine busy without limits, run at least as many reports as there are
cores. A tenant's pool holds 5 connections, so on machines with more than 5
cores add a second heavy tenant (for example --heavy acme,initech).

Passwords are asked for and never stored. Nothing in the database is changed.
"""
import argparse
import getpass
import http.cookiejar
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 600  # seconds; a capped report can take minutes under heavy load


class Session:
    """One signed-in user, with its own cookie jar."""

    def __init__(self, base_url, username, password):
        self.base = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        data = urllib.parse.urlencode({"username": username, "password": password}).encode()
        failed = f"Login failed for '{username}' (check the password, and that it is a tenant user)."
        try:
            with self.opener.open(self.base + "/login", data=data, timeout=30) as resp:
                # A successful login redirects (303) to /tenant.
                if not resp.geturl().rstrip("/").endswith("/tenant"):
                    raise SystemExit(failed)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                raise SystemExit(failed)
            raise
        except urllib.error.URLError as e:
            raise SystemExit(f"Cannot reach the backend at {self.base}: {e.reason}. Is the server running?")

    def get_page(self, path):
        """Loads a page; returns the response time in milliseconds."""
        start = time.perf_counter()
        with self.opener.open(self.base + path, timeout=TIMEOUT) as resp:
            resp.read()
        return (time.perf_counter() - start) * 1000

    def heavy_report(self):
        with self.opener.open(self.base + "/tenant/report", data=b"", timeout=TIMEOUT) as resp:
            return json.loads(resp.read())


def percentile(values, p):
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summary(values):
    return {"n": len(values), "median_ms": round(statistics.median(values), 1),
            "p95_ms": round(percentile(values, 95), 1),
            "min_ms": round(min(values), 1), "max_ms": round(max(values), 1)}


def victim_samples(session, path, n, pause=0.25):
    times = []
    for _ in range(n):
        times.append(session.get_page(path))
        time.sleep(pause)
    return times


def main():
    ap = argparse.ArgumentParser(description="Noisy-neighbour test for Gatekeeper.")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--heavy", default="acme")
    ap.add_argument("--victim", default="globex")
    ap.add_argument("--concurrency", type=int, default=0)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--out")
    args = ap.parse_args()

    heavy_users = [u.strip() for u in args.heavy.split(",") if u.strip()]
    cores = os.cpu_count() or 1
    per_user = args.concurrency or min(cores, 5)
    if per_user > 5:
        print("Note: a tenant's pool has 5 connections, so more than 5 concurrent reports per user will fail.")
    total = per_user * len(heavy_users)
    if total < cores:
        print(f"Note: {total} concurrent report(s) on {cores} cores cannot keep every core busy; "
              f"add a heavy user (--heavy acme,initech) for a stronger test.")

    passwords = {}
    for user in [args.victim] + heavy_users:
        if user not in passwords:
            passwords[user] = getpass.getpass(f"Password for {user}: ")

    victim = Session(args.base_url, args.victim, passwords[args.victim])
    page = "/tenant/orders"

    print(f"\nWarming up ({args.victim} {page})...")
    victim_samples(victim, page, 3, pause=0)

    print(f"Phase 1, quiet system: {args.samples} requests...")
    quiet = victim_samples(victim, page, args.samples)

    # ---- Phase 2: heavy load ----
    stop = threading.Event()
    reports, errors, mode = [], [], {}
    lock = threading.Lock()

    def heavy_worker(user):
        try:
            s = Session(args.base_url, user, passwords[user])
        except SystemExit as e:
            with lock:
                errors.append(str(e))
            return
        while not stop.is_set():
            try:
                r = s.heavy_report()
                with lock:
                    reports.append((user, r["duration_ms"]))
                    mode["governor_mode"] = r.get("governor_mode")
            except (urllib.error.URLError, KeyError, ValueError) as e:
                with lock:
                    errors.append(f"{user}: {e}")
                return

    threads = [threading.Thread(target=heavy_worker, args=(u,), daemon=True)
               for u in heavy_users for _ in range(per_user)]
    print(f"Phase 2, heavy load: {total} concurrent heavy report(s) from {', '.join(heavy_users)} "
          f"for {args.duration:.0f} s...")
    load_start = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(2)   # let the reports start before measuring

    loaded = []
    while time.perf_counter() - load_start < args.duration:
        loaded.append(victim.get_page(page))
        time.sleep(0.25)
    stop.set()
    print("Load period over; waiting for the heavy reports still running to finish...")
    for t in threads:
        t.join()

    if errors:
        print("\nErrors during the heavy load:")
        for e in errors[:10]:
            print("  ", e)
    if not loaded:
        sys.exit("No victim samples were taken under load; increase --duration.")

    q, l = summary(quiet), summary(loaded)
    durations = [d for _, d in reports]
    result = {
        "governor_mode": mode.get("governor_mode", "unknown (no report finished)"),
        "cores": cores, "heavy_users": heavy_users, "concurrent_reports": total,
        "duration_s": args.duration, "victim": args.victim, "victim_page": page,
        "victim_quiet": q, "victim_under_load": l,
        "slowdown_median": round(l["median_ms"] / q["median_ms"], 2),
        "slowdown_p95": round(l["p95_ms"] / q["p95_ms"], 2),
        "heavy_reports_finished": len(durations),
        "heavy_report_median_ms": round(statistics.median(durations)) if durations else None,
    }

    print(f"\n=== Results (governor_mode: {result['governor_mode']}, {cores} cores, "
          f"{total} concurrent heavy reports) ===")
    print(f"{'Victim ' + args.victim + ' ' + page:34s} {'median':>10s} {'p95':>10s} {'n':>5s}")
    print(f"{'  quiet system':34s} {q['median_ms']:>8.1f}ms {q['p95_ms']:>8.1f}ms {q['n']:>5d}")
    print(f"{'  under heavy load':34s} {l['median_ms']:>8.1f}ms {l['p95_ms']:>8.1f}ms {l['n']:>5d}")
    print(f"Slowdown under load: {result['slowdown_median']}x (median), {result['slowdown_p95']}x (p95)")
    if durations:
        print(f"Heavy reports finished: {len(durations)}, median {result['heavy_report_median_ms']:,} ms each")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Saved to {args.out}")


if __name__ == "__main__":
    main()
