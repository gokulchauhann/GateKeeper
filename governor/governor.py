"""
Gatekeeper Governor: per-tenant connection pools whose PostgreSQL backend
processes are confined to a per-tenant Linux cgroup (v2).

Built on Rohit's TenantGovernor. Phase 2 additions (docs/interface-contract.md):
- CPU and memory limits come from the tenant's plan tier (tenants JOIN plan_tiers);
  an unknown tenant is rejected before any cgroup is created.
- memory.high instead of memory.max: the kernel slows the tenant down rather
  than killing a backend (a killed backend makes Postgres restart every connection).
- get_tenant_usage() turns cgroup counters into per-interval usage numbers.

Needs Linux with cgroups v2 and root (sudo).
"""
import os
import threading
import time
from datetime import datetime, timezone

import psycopg2
from dotenv import load_dotenv
from psycopg2 import pool

CGROUP_ROOT = "/sys/fs/cgroup"
CPU_PERIOD_US = 100000
load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")


class GovernorConnectionError(Exception):
    pass


def cgroup_path(tenant_id: str) -> str:
    return os.path.join(CGROUP_ROOT, f"gatekeeper-tenant_{tenant_id}")


def create_cgroup(tenant_id: str, cpu_quota_percent: int, memory_limit_mb: int) -> str:
    """Creates the tenant's cgroup (if needed) and writes its CPU and memory limits."""
    path = cgroup_path(tenant_id)
    os.makedirs(path, exist_ok=True)

    quota_us = cpu_quota_percent * CPU_PERIOD_US // 100
    with open(os.path.join(path, "cpu.max"), "w") as f:
        f.write(f"{quota_us} {CPU_PERIOD_US}")

    with open(os.path.join(path, "memory.high"), "w") as f:
        f.write(str(memory_limit_mb * 1024 * 1024))

    return path


def assign_pid(cgroup_path: str, pid: int) -> None:
    with open(os.path.join(cgroup_path, "cgroup.procs"), "w") as f:
        f.write(str(pid))


def tenant_limits(tenant_id: str):
    """(cpu_quota_percent, memory_limit_mb) from the tenant's plan tier, or None
    if the tenant does not exist. Uses its own short connection, outside any cgroup."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT p.cpu_quota_percent, p.memory_limit_mb
                           FROM tenants t JOIN plan_tiers p ON p.id = t.plan_tier_id
                           WHERE t.id = %s""", (int(tenant_id),))
            return cur.fetchone()
    finally:
        conn.close()


def _read_cpu_stat(path: str) -> dict:
    """cpu.stat is lines of 'key value'; returns them as {key: int}."""
    stats = {}
    with open(os.path.join(path, "cpu.stat")) as f:
        for line in f:
            key, value = line.split()
            stats[key] = int(value)
    return stats


class TenantGovernor:
    def __init__(self, minconn: int = 1, maxconn: int = 5):
        self._minconn = minconn
        self._maxconn = maxconn
        self._pools = {}
        self._cgroups = {}        # tenant_id -> cgroup path, once limits are applied
        self._lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._last_sample = {}    # tenant_id -> (monotonic time, usage_usec, throttled_usec)

    def _get_pool(self, tenant_id: str):
        with self._lock:
            if tenant_id not in self._pools:
                self._pools[tenant_id] = pool.ThreadedConnectionPool(
                    self._minconn, self._maxconn, DATABASE_URL
                )
            return self._pools[tenant_id]

    def _ensure_cgroup(self, tenant_id: str) -> str:
        """On a tenant's first connection: look up its plan tier and create its
        cgroup with those limits. Later calls reuse the result."""
        with self._lock:
            if tenant_id in self._cgroups:
                return self._cgroups[tenant_id]
        limits = tenant_limits(tenant_id)
        if limits is None:
            raise GovernorConnectionError(f"unknown tenant {tenant_id}")
        path = create_cgroup(tenant_id, *limits)
        with self._lock:
            self._cgroups[tenant_id] = path
        return path

    def get_connection(self, tenant_id):
        try:
            tenant_id = str(int(tenant_id))   # rejects things like "../x"
            path = self._ensure_cgroup(tenant_id)
            tenant_pool = self._get_pool(tenant_id)
            conn = tenant_pool.getconn()
        except GovernorConnectionError:
            raise
        except Exception as e:
            raise GovernorConnectionError(f"no connection for tenant {tenant_id}") from e
        try:
            # Every checkout: if Postgres replaced a connection, its new PID is confined too.
            assign_pid(path, conn.info.backend_pid)
        except Exception as e:
            tenant_pool.putconn(conn, close=True)
            raise GovernorConnectionError(f"cgroup assignment failed for tenant {tenant_id}") from e
        return conn

    def release_connection(self, tenant_id, conn):
        self._pools[str(int(tenant_id))].putconn(conn)

    def get_tenant_usage(self, tenant_id):
        """Usage since the previous call, in the interface-contract shape.
        None if the tenant's cgroup does not exist yet (it has never connected).
        cpu.stat holds running totals, so the numbers come from the difference
        between this reading and the last one."""
        tenant_id = str(int(tenant_id))
        path = cgroup_path(tenant_id)
        if not os.path.isdir(path):
            return None
        stats = _read_cpu_stat(path)
        usage_us = stats.get("usage_usec", 0)
        throttled_us = stats.get("throttled_usec", 0)
        with open(os.path.join(path, "memory.current")) as f:
            memory_bytes = int(f.read())
        now = time.monotonic()

        with self._usage_lock:
            previous = self._last_sample.get(tenant_id)
            self._last_sample[tenant_id] = (now, usage_us, throttled_us)

        cpu_percent, throttled_ms = 0.0, 0
        if previous is not None:
            elapsed_us = (now - previous[0]) * 1_000_000
            if elapsed_us > 0:
                cpu_percent = (usage_us - previous[1]) / elapsed_us * 100
            throttled_ms = (throttled_us - previous[2]) // 1000

        return {
            "tenant_id": tenant_id,
            "cpu_percent": round(cpu_percent, 1),
            "memory_used_mb": round(memory_bytes / 1048576, 1),
            "throttled_ms": int(throttled_ms),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def cgroup_of(pid: int) -> str:
    with open(f"/proc/{pid}/cgroup") as f:
        return f.read().strip()


if __name__ == "__main__":
    # Self-test: run as root after seeding (sudo ./venv/bin/python governor/governor.py)
    gov = TenantGovernor()
    connections = [
        ("tenant 1, conn A", "1", gov.get_connection("1")),
        ("tenant 1, conn B", "1", gov.get_connection("1")),
        ("tenant 2, conn A", "2", gov.get_connection("2")),
    ]
    for label, tenant, conn in connections:
        pid = conn.info.backend_pid
        print(f"{label}: PID {pid} -> {cgroup_of(pid)}")
    for tenant in ("1", "2"):
        path = cgroup_path(tenant)
        with open(os.path.join(path, "cpu.max")) as f, open(os.path.join(path, "memory.high")) as g:
            print(f"tenant {tenant}: cpu.max = {f.read().strip()}, memory.high = {g.read().strip()}")
    try:
        gov.get_connection("999")
    except GovernorConnectionError as e:
        print(f"tenant 999 rejected: {e}")
    gov.get_tenant_usage("1")
    time.sleep(1)
    print("usage tenant 1:", gov.get_tenant_usage("1"))
    for _, tenant, conn in connections:
        gov.release_connection(tenant, conn)
