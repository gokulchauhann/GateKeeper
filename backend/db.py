"""
System database connection for the backend.

Used for login, users/tenants/plan_tiers lookups and the admin pages.
It does NOT go through the Governor, so these queries never run inside a
tenant's cgroup. Tenant data (customers, orders, the report) must use
governor.get_connection() instead. See docs/interface-contract.md.
"""
import os
from contextlib import contextmanager

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# FastAPI runs normal (def) routes in a thread pool, so the pool must be thread-safe.
_pool = None


def init_pool():
    global _pool
    _pool = ThreadedConnectionPool(1, 5, os.environ["DATABASE_URL"])


def close_pool():
    if _pool is not None:
        _pool.closeall()


@contextmanager
def system_cursor():
    """Yields a cursor whose rows are dicts. Commits on success, rolls back on error,
    and always returns the connection to the pool."""
    conn = _pool.getconn()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
    finally:
        _pool.putconn(conn)
