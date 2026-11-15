"""
memory.high test for Gatekeeper: a tenant that needs more memory than its plan
allows should be slowed down by the kernel, not killed, and PostgreSQL should
not restart.

It takes a connection for the tenant through the real Governor (so the backend
process is in the tenant's cgroup with the plan's memory.high), lets that
session sort a large in-memory set (work_mem is raised for this session only),
and reports:
  - how often the kernel saw the cgroup above memory.high (memory.events "high"),
  - the peak memory.current while the query ran,
  - whether the same backend process survived and PostgreSQL did not restart.

Run from the repository root, as root (the Governor writes cgroups), on the
Linux machine with the database. The web server does not need to run.
    sudo ./venv/bin/python scripts/memory_test.py               # Acme, Basic, 256 MB
    sudo ./venv/bin/python scripts/memory_test.py --tenant 4    # Umbrella, Enterprise, 4096 MB

Options:
    --tenant ID     tenant id (default 1)
    --rows N        rows to sort (default 4,000,000; roughly 400+ MB in memory)
    --timeout S     statement timeout in seconds (default 180)

Above memory.high the kernel reclaims memory and slows the process down. On a
machine without swap it may not be able to free enough, so the query can become
very slow; the statement timeout then cancels it. Either way the backend should
survive, which is the point of using memory.high instead of memory.max.
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402

from governor import governor  # noqa: E402


def read(path):
    with open(path) as f:
        return f.read().strip()


def memory_events(cg):
    """memory.events as {name: count}; 'high' counts times above memory.high."""
    events = {}
    for line in read(os.path.join(cg, "memory.events")).splitlines():
        key, value = line.split()
        events[key] = int(value)
    return events


def swap_total_kb():
    for line in read("/proc/meminfo").splitlines():
        if line.startswith("SwapTotal:"):
            return int(line.split()[1])
    return 0


def main():
    ap = argparse.ArgumentParser(description="memory.high test for Gatekeeper.")
    ap.add_argument("--tenant", default="1")
    ap.add_argument("--rows", type=int, default=4_000_000)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run as root: sudo ./venv/bin/python scripts/memory_test.py")

    gov = governor.TenantGovernor()
    try:
        conn = gov.get_connection(args.tenant)
    except governor.GovernorConnectionError as e:
        reason = e.__cause__ or e
        sys.exit(f"Could not get a connection for tenant {args.tenant}: {str(reason).strip().splitlines()[0]}\n"
                 "Is PostgreSQL running? Start it with: sudo service postgresql start")
    pid = conn.info.backend_pid
    cg = governor.cgroup_path(str(int(args.tenant)))

    with conn.cursor() as cur:
        cur.execute("SELECT pg_postmaster_start_time()")
        started_before = cur.fetchone()[0]
    conn.rollback()

    high = read(os.path.join(cg, "memory.high"))
    high_mb = "no limit" if high == "max" else f"{int(high) // 1048576} MB"
    before = memory_events(cg)
    print(f"Tenant {args.tenant}: backend PID {pid} in {cg}")
    print(f"memory.high = {high} ({high_mb}); swap on this machine: {swap_total_kb() // 1024} MB")
    print(f"Sorting {args.rows:,} rows in memory (work_mem 2GB for this session only)...")

    peak = [0]
    done = threading.Event()

    def sample_peak():
        while not done.is_set():
            try:
                peak[0] = max(peak[0], int(read(os.path.join(cg, "memory.current"))))
            except OSError:
                pass
            time.sleep(0.2)

    sampler = threading.Thread(target=sample_peak, daemon=True)
    sampler.start()

    outcome = "completed"
    start = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL work_mem = '2GB'")
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            cur.execute("SET LOCAL statement_timeout = %s", (f"{args.timeout}s",))
            cur.execute("""SELECT count(*) FROM (
                               SELECT md5(g::text) AS h FROM generate_series(1, %s) AS g ORDER BY h
                           ) AS sorted""", (args.rows,))
            cur.fetchone()
        conn.rollback()
    except psycopg2.errors.QueryCanceled:
        outcome = f"cancelled by the {args.timeout} s statement timeout"
        conn.rollback()
    except psycopg2.Error as e:
        outcome = f"failed: {e.__class__.__name__}: {str(e).strip()}"
    elapsed = time.perf_counter() - start
    done.set()
    sampler.join()
    after = memory_events(cg)

    # Did the backend process and PostgreSQL itself survive?
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid(), pg_postmaster_start_time()")
            pid_after, started_after = cur.fetchone()
        conn.rollback()
        same_backend = pid_after == pid
        restarted = started_after != started_before
    except psycopg2.Error:
        same_backend, restarted = False, None

    print(f"\n=== memory.high test, tenant {args.tenant} ({high_mb}) ===")
    print(f"Query: {outcome} after {elapsed:.1f} s")
    print(f"Peak memory.current while running: {peak[0] // 1048576} MB")
    print(f"memory.events 'high' (times above memory.high): +{after.get('high', 0) - before.get('high', 0)}")
    print(f"memory.events 'oom_kill': +{after.get('oom_kill', 0) - before.get('oom_kill', 0)}")
    print(f"Same backend process afterwards: {'yes' if same_backend else 'NO'}")
    print(f"PostgreSQL restarted: {'unknown' if restarted is None else ('YES' if restarted else 'no')}")

    try:
        gov.release_connection(args.tenant, conn)
    except Exception:
        pass


if __name__ == "__main__":
    main()