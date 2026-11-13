"""
Real-mode validation for Gatekeeper (Linux, cgroups v2).

Checks, against the running system, that the Governor's cgroup enforcement is
really in place. Every check prints PASS, FAIL, BLOCKED (the environment does
not allow it), SKIP (nothing to check yet) or INFO. Nothing is assumed: if a
file cannot be read or a step is not allowed, the script says so.

Run from the repository root, as root, while the backend runs with GOVERNOR=real:
    sudo ./venv/bin/python scripts/validate_real_mode.py
    sudo ./venv/bin/python scripts/validate_real_mode.py --report-user acme --runs 3

Options:
    --base-url URL      backend address (default http://127.0.0.1:8000)
    --report-user NAME  also time the heavy report as this tenant user; the
                        password is asked for and never stored
    --runs N            heavy report runs (default 1)
    --interval S        seconds between the two cpu.stat readings (default 3)

The only change it makes is a temporary cgroup (gk-validate-<pid>) to test
write permission, which it removes again.
"""
import argparse
import getpass
import http.cookiejar
import json
import os
import platform
import sys
import time
import urllib.parse
import urllib.request

import psycopg2
from dotenv import load_dotenv

CGROUP_ROOT = "/sys/fs/cgroup"
CPU_PERIOD_US = 100000
results = []


def report(status, check, detail=""):
    results.append(status)
    print(f"[{status:7}] {check}" + (f"\n          {detail}" if detail else ""))


def read(path):
    with open(path) as f:
        return f.read().strip()


def cpu_stat(path):
    return {k: int(v) for k, v in (line.split() for line in read(os.path.join(path, "cpu.stat")).splitlines())}


def tenant_cgroup(tenant_id):
    return os.path.join(CGROUP_ROOT, f"gatekeeper-tenant_{tenant_id}")


# ---------- Environment ----------

def check_environment():
    if platform.system() != "Linux":
        report("BLOCKED", "Operating system", f"{platform.system()}: cgroups v2 exist only on Linux.")
        return False
    report("PASS", "Operating system", f"Linux {platform.release()}")

    mounts = read("/proc/mounts").splitlines()
    if not any(line.split()[1:3] == [CGROUP_ROOT, "cgroup2"] for line in mounts):
        report("BLOCKED", "cgroups v2 mounted at /sys/fs/cgroup", "No cgroup2 filesystem there (cgroups v1 or none).")
        return False
    report("PASS", "cgroups v2 mounted at /sys/fs/cgroup")

    controllers = read(os.path.join(CGROUP_ROOT, "cgroup.controllers")).split()
    subtree = read(os.path.join(CGROUP_ROOT, "cgroup.subtree_control")).split()
    missing = [c for c in ("cpu", "memory") if c not in controllers]
    if missing:
        report("BLOCKED", "cpu and memory controllers available", f"Missing: {', '.join(missing)}. Available: {' '.join(controllers)}")
    else:
        report("PASS", "cpu and memory controllers available")
    not_delegated = [c for c in ("cpu", "memory") if c not in subtree]
    if not_delegated:
        report("FAIL", "cpu and memory enabled for child cgroups",
               f"cgroup.subtree_control is '{' '.join(subtree)}'; without {', '.join(not_delegated)} "
               "child cgroups have no cpu.max / memory.high.")
    else:
        report("PASS", "cpu and memory enabled for child cgroups")

    euid = os.geteuid()
    test = os.path.join(CGROUP_ROOT, f"gk-validate-{os.getpid()}")
    try:
        os.mkdir(test)
        try:
            has_cpu_max = os.path.exists(os.path.join(test, "cpu.max"))
        finally:
            os.rmdir(test)
        report("PASS" if has_cpu_max else "FAIL", "Can create a cgroup with cpu.max",
               f"Running as uid {euid}." + ("" if has_cpu_max else " The cgroup had no cpu.max file."))
    except OSError as e:
        report("BLOCKED", "Can create a cgroup with cpu.max",
               f"mkdir {test} failed as uid {euid}: {e.strerror}. "
               + ("Run with sudo." if euid != 0 else "This environment does not allow creating cgroups "
                  "(for example a restricted container); real mode needs Linux/WSL2 or a Codespace that permits it."))
    return True


# ---------- Tenants: limits and PIDs ----------

def check_tenants(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT t.id, t.name, p.name, p.cpu_quota_percent, p.memory_limit_mb
                       FROM tenants t JOIN plan_tiers p ON p.id = t.plan_tier_id ORDER BY t.id""")
        tenants = cur.fetchall()
        cur.execute("""SELECT pid FROM pg_stat_activity
                       WHERE backend_type = 'client backend' AND datname = current_database()""")
        backends = {r[0] for r in cur.fetchall()}

    found = 0
    for tid, name, plan, quota, mem_mb in tenants:
        path = tenant_cgroup(tid)
        label = f"Tenant {tid} {name} ({plan})"
        if not os.path.isdir(path):
            report("SKIP", f"{label}: cgroup", f"{path} does not exist: the tenant has not connected since the "
                   "server started. Sign in as this tenant and open a page, then run again.")
            continue
        found += 1

        expected_cpu = f"{quota * CPU_PERIOD_US // 100} {CPU_PERIOD_US}"
        actual_cpu = read(os.path.join(path, "cpu.max"))
        report("PASS" if actual_cpu == expected_cpu else "FAIL", f"{label}: cpu.max matches plan ({quota}% of one core)",
               f"expected '{expected_cpu}', found '{actual_cpu}'")

        expected_mem = str(mem_mb * 1024 * 1024)
        actual_mem = read(os.path.join(path, "memory.high"))
        report("PASS" if actual_mem == expected_mem else "FAIL", f"{label}: memory.high matches plan ({mem_mb} MB)",
               f"expected {expected_mem}, found {actual_mem}")

        pids = [int(p) for p in read(os.path.join(path, "cgroup.procs")).split()]
        if not pids:
            report("SKIP", f"{label}: PIDs in cgroup.procs", "Empty: no backend process in the cgroup right now.")
            continue
        not_backend = [p for p in pids if p not in backends]
        wrong_cgroup = [p for p in pids if not _in_cgroup(p, path)]
        detail = f"cgroup.procs: {pids}; PostgreSQL client backends on this database: {len(backends)}"
        if not_backend:
            detail += f"; not PostgreSQL backends of this database: {not_backend}"
        if wrong_cgroup:
            detail += f"; /proc/<pid>/cgroup disagrees for: {wrong_cgroup}"
        report("PASS" if not not_backend and not wrong_cgroup else "FAIL",
               f"{label}: cgroup.procs holds only PostgreSQL backend PIDs", detail)
    if found == 0:
        report("SKIP", "Tenant cgroups", "No tenant cgroup exists yet. Is the backend running with GOVERNOR=real?")
    return tenants


def _in_cgroup(pid, path):
    try:
        return read(f"/proc/{pid}/cgroup").endswith(path[len(CGROUP_ROOT):])
    except OSError:
        return False


# ---------- cpu.stat ----------

def check_cpu_stat(tenants, interval):
    paths = {tid: tenant_cgroup(tid) for tid, *_ in tenants if os.path.isdir(tenant_cgroup(tid))}
    if not paths:
        return
    before = {tid: (time.monotonic(), cpu_stat(p)) for tid, p in paths.items()}
    time.sleep(interval)
    for tid, path in paths.items():
        t0, s0 = before[tid]
        s1, elapsed_us = cpu_stat(path), (time.monotonic() - t0) * 1e6
        cpu = (s1["usage_usec"] - s0["usage_usec"]) / elapsed_us * 100
        report("INFO", f"Tenant {tid}: cpu.stat over {interval}s",
               f"CPU {cpu:.1f}% of one core; periods +{s1['nr_periods'] - s0['nr_periods']}, "
               f"throttled periods +{s1['nr_throttled'] - s0['nr_throttled']}, "
               f"throttled time +{(s1['throttled_usec'] - s0['throttled_usec']) / 1000:.0f} ms "
               f"(totals since creation: usage {s1['usage_usec'] / 1e6:.1f} s, throttled {s1['throttled_usec'] / 1e6:.1f} s)")


# ---------- Heavy report ----------

def run_report(base_url, username, runs, tenants):
    password = os.environ.get("GK_REPORT_PASSWORD") or getpass.getpass(f"Password for {username}: ")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(f"{base_url}/login", urllib.parse.urlencode({"username": username, "password": password}).encode())
    except OSError as e:
        report("FAIL", "Heavy report: sign in", f"{base_url} not reachable: {e}")
        return
    if not any(c.name == "gk_session" for c in jar):
        report("FAIL", "Heavy report: sign in", "No session cookie: wrong username or password?")
        return

    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
    tid = row[0] if row else None
    if tid is None:
        report("FAIL", "Heavy report: sign in", f"{username} is not a tenant user, so it cannot run the report.")
        return
    quota = next(q for t, _, _, q, _ in tenants if t == tid)
    path = tenant_cgroup(tid)

    for run in range(1, runs + 1):
        before = cpu_stat(path) if os.path.isdir(path) else None
        started = time.monotonic()
        try:
            with opener.open(urllib.request.Request(f"{base_url}/tenant/report", method="POST", data=b""), timeout=600) as r:
                body = json.load(r)
        except OSError as e:
            report("FAIL", f"Heavy report run {run}", f"Request failed: {e}")
            continue
        wall = time.monotonic() - started
        detail = (f"duration_ms {body.get('duration_ms')} (wall {wall:.1f}s), governor_mode {body.get('governor_mode')}, "
                  f"cgroup_enforced {body.get('cgroup_enforced')}")
        if before is None:
            report("INFO", f"Heavy report run {run}", detail + "; no cgroup before the run, so no cpu.stat delta.")
            continue
        after = cpu_stat(path)
        cpu_share = (after["usage_usec"] - before["usage_usec"]) / (wall * 1e6) * 100
        throttled_ms = (after["throttled_usec"] - before["throttled_usec"]) / 1000
        detail += f"; tenant cgroup used {cpu_share:.1f}% of one core during the run, throttled {throttled_ms:.0f} ms"
        enforced = body.get("cgroup_enforced") is True and throttled_ms > 0 and cpu_share <= quota * 1.1
        report("PASS" if enforced else "FAIL", f"Heavy report run {run}: kernel enforced the {quota}% quota", detail)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--report-user")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()

    load_dotenv()
    print("Gatekeeper real-mode validation\n")
    mode = os.environ.get("GOVERNOR", "stub")
    report("PASS" if mode == "real" else "FAIL", "GOVERNOR=real in .env", f"found GOVERNOR={mode}")

    if check_environment():
        try:
            conn = psycopg2.connect(os.environ["DATABASE_URL"])
        except (KeyError, psycopg2.Error) as e:
            report("FAIL", "Database connection", str(e).strip())
        else:
            with conn:
                tenants = check_tenants(conn)
            conn.close()
            check_cpu_stat(tenants, args.interval)
            if args.report_user:
                run_report(args.base_url.rstrip("/"), args.report_user, args.runs, tenants)

    counts = {s: results.count(s) for s in ("PASS", "FAIL", "BLOCKED", "SKIP", "INFO")}
    print("\nSummary: " + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
    if counts["FAIL"]:
        print("Result: real-mode enforcement NOT confirmed (see FAIL lines).")
        sys.exit(2)
    if counts["BLOCKED"]:
        print("Result: could not test, the environment blocked required cgroup operations.")
        sys.exit(3)
    print("Result: all checks that could run passed." + (" Some checks were skipped." if counts["SKIP"] else ""))


if __name__ == "__main__":
    main()
