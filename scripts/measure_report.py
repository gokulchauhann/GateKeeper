"""
Heavy-report timing for Gatekeeper, per tenant, with the kernel's CPU accounting.

Signs in as each tenant user, runs one warm-up report each, then runs the
heavy report N times per user, alternating users so drift affects all of them
equally. For every run it prints the server-side duration and, when the run was
enforced (GOVERNOR=real), the CPU time the tenant's cgroup used, the CPU share
(CPU used / duration) and the time the kernel throttled it, read from the
cgroup's cpu.stat before and after the run.

Run from the repository root on the Linux machine that runs the backend:
    ./venv/bin/python scripts/measure_report.py acme umbrella
    ./venv/bin/python scripts/measure_report.py acme --runs 10

Run it once with GOVERNOR=stub and once with GOVERNOR=real to compare. Use the
same machine and power settings for both: on a laptop, battery power or a
low-power mode can change the timings by several times.

Options:
    --base-url URL   backend address (default http://127.0.0.1:8000)
    --runs N         measured runs per user (default 5)
    --warmup N       unmeasured runs per user first (default 1)

Passwords are asked for and never stored. Reading cpu.stat does not need root.
"""
import argparse
import getpass
import http.cookiejar
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

import psycopg2
from dotenv import load_dotenv

CGROUP_ROOT = "/sys/fs/cgroup"


def tenant_id_for(username):
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
    return row[0] if row else None


def cpu_stat(tenant_id):
    """(usage_usec, throttled_usec) of the tenant's cgroup, or (0, 0) if it does not exist."""
    try:
        with open(os.path.join(CGROUP_ROOT, f"gatekeeper-tenant_{tenant_id}", "cpu.stat")) as f:
            stat = dict(line.split() for line in f if line.strip())
    except FileNotFoundError:
        return 0, 0
    return int(stat.get("usage_usec", 0)), int(stat.get("throttled_usec", 0))


def sign_in(base_url, username, password):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    try:
        opener.open(base_url + "/login", data, timeout=10)
    except urllib.error.HTTPError as e:
        sys.exit(f"Sign-in failed for {username} (HTTP {e.code}).")
    return opener


def run_report(opener, base_url):
    req = urllib.request.Request(base_url + "/tenant/report", method="POST")
    return json.load(opener.open(req, timeout=600))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("users", nargs="+", help="tenant usernames, for example acme umbrella")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    load_dotenv()

    users = []
    for name in args.users:
        tid = tenant_id_for(name)
        if tid is None:
            sys.exit(f"{name} is not a tenant user.")
        users.append((name, tid, sign_in(base_url, name, getpass.getpass(f"Password for {name}: "))))

    for _ in range(args.warmup):
        for _, _, opener in users:
            run_report(opener, base_url)

    results = {name: [] for name, _, _ in users}
    for i in range(1, args.runs + 1):
        for name, tid, opener in users:
            before = cpu_stat(tid)
            r = run_report(opener, base_url)
            after = cpu_stat(tid)
            ms = r["duration_ms"]
            line = f"{name:10} run {i}: {ms:7,d} ms   mode={r['governor_mode']}"
            if r.get("cgroup_enforced"):
                cpu_ms = (after[0] - before[0]) / 1000
                share = 100 * cpu_ms / ms
                line += f"   CPU {cpu_ms:7,.0f} ms   share {share:5.1f}%   throttled {(after[1] - before[1]) / 1000:7,.0f} ms"
                results[name].append((ms, share))
            else:
                results[name].append((ms, None))
            print(line)

    print()
    for name, rows in results.items():
        times = [ms for ms, _ in rows]
        sd = statistics.stdev(times) if len(times) > 1 else 0
        line = f"{name:10} mean {statistics.mean(times):7,.0f} ms   sd {sd:5,.0f} ms   min {min(times):,}   max {max(times):,}"
        shares = [s for _, s in rows if s is not None]
        if shares:
            line += f"   CPU share mean {statistics.mean(shares):.1f}%"
        print(line)


if __name__ == "__main__":
    main()
