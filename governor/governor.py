import os
import threading

from dotenv import load_dotenv
from psycopg2 import pool

CGROUP_ROOT = "/sys/fs/cgroup"
CPU_PERIOD_US = 100000
load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")

class GovernorConnectionError(Exception):
    pass
    
def create_cgroup(tenant_id: str, cpu_quota_percent: int) -> str:
    path = os.path.join(CGROUP_ROOT, f"gatekeeper-tenant_{tenant_id}")
    os.makedirs(path, exist_ok=True)

    quota_us = cpu_quota_percent * CPU_PERIOD_US // 100
    with open(os.path.join(path, "cpu.max"), "w") as f:
        f.write(f"{quota_us} {CPU_PERIOD_US}")

    return path


def assign_pid(cgroup_path: str, pid: int) -> None:
    with open(os.path.join(cgroup_path, "cgroup.procs"), "w") as f:
        f.write(str(pid))


class TenantGovernor:
    def __init__(self, minconn: int = 1, maxconn: int = 5):
        self._minconn = minconn
        self._maxconn = maxconn
        self._pools = {}
        self._lock = threading.Lock()

    def _get_pool(self, tenant_id: str):
        with self._lock:
            if tenant_id not in self._pools:
                self._pools[tenant_id] = pool.ThreadedConnectionPool(
                    self._minconn, self._maxconn, DATABASE_URL
                )
            return self._pools[tenant_id]

    def get_connection(self, tenant_id):
    try:
        tenant_id = str(int(tenant_id))   # rejects things like "../x"
        tenant_pool = self._get_pool(tenant_id)
        conn = tenant_pool.getconn()
    except Exception as e:
        raise GovernorConnectionError(f"no connection for tenant {tenant_id}") from e
    try:
        assign_pid(create_cgroup(tenant_id, 20), conn.info.backend_pid)
    except Exception as e:
        tenant_pool.putconn(conn, close=True)
        raise GovernorConnectionError(f"cgroup assignment failed for tenant {tenant_id}") from e
    return conn

    def release_connection(self, tenant_id, conn):
    self._pools[str(int(tenant_id))].putconn(conn)


def cgroup_of(pid: int) -> str:
    with open(f"/proc/{pid}/cgroup") as f:
        return f.read().strip()


if __name__ == "__main__":
    gov = TenantGovernor()
    connections = [
        ("tenant 1, conn A", gov.get_connection("1")),
        ("tenant 1, conn B", gov.get_connection("1")),
        ("tenant 2, conn A", gov.get_connection("2")),
    ]
    for label, conn in connections:
        pid = conn.info.backend_pid
        print(f"{label}: PID {pid} -> {cgroup_of(pid)}")
