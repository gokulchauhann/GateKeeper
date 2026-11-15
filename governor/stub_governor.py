"""
Stub Governor — usable fake implementation for backend/frontend development
while the real cgroup-based Governor (governor/governor.py) is being built.

"""

import os
import random
from datetime import datetime, timezone

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")


class StubGovernor:
    def get_connection(self, tenant_id: str):
        """Returns a plain psycopg2 connection. No pooling, no cgroup
        assignment — just enough to let real queries run during dev."""
        try:
            conn = psycopg2.connect(DATABASE_URL)
            return conn
        except Exception as e:
            raise GovernorConnectionError(
                f"Stub governor failed to open connection for tenant "
                f"{tenant_id}: {e}"
            )

    def release_connection(self, tenant_id: str, conn):
        """No pool to return to in the stub — just close it."""
        try:
            conn.close()
        except Exception:
            pass

    def discard_connection(self, tenant_id: str, conn):
        """Same as release in the stub: the connection is closed either way."""
        self.release_connection(tenant_id, conn)

    def get_tenant_usage(self, tenant_id: str) -> dict:
        """Returns fake-but-plausible usage data, shaped exactly like the
        real Governor's output, so the admin dashboard can be built and
        tested before real cgroup monitoring exists."""
        return {
            "tenant_id": tenant_id,
            "cpu_percent": round(random.uniform(5, 95), 1),
            "memory_used_mb": round(random.uniform(50, 800), 1),
            "throttled_ms": random.choice([0, 0, 0, 120, 450]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class GovernorConnectionError(Exception):
    """Raised when a Governor (stub or real) cannot provide a connection
    for the requested tenant."""
    pass
