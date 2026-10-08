import os
import sys

import psycopg2

CGROUP_ROOT = "/sys/fs/cgroup"
CPU_PERIOD_US = 100000
DATABASE_URL = os.environ.get("DATABASE_URL")


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


def get_connection(tenant_id: str):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        conn.commit()

        cgroup_path = create_cgroup(tenant_id, 20)
        assign_pid(cgroup_path, pid)
    except Exception:
        conn.close()
        raise
    return conn


if __name__ == "__main__":
    conn = get_connection("1")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        print("backend PID:", cur.fetchone()[0])
    input("Connection open. Verify from another terminal, then press Enter...")
    conn.close()
