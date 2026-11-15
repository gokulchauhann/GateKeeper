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


def _alive(conn) -> bool:
    """conn.closed is free; SELECT 1 also catches a server that restarted."""
    if conn.closed:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.rollback()
        return True
    except Exception:
        return False


def _checkout():
    """A live connection from the pool; dead ones are closed and replaced."""
    for _ in range(6):   # pool size 5, plus one fresh connection
        conn = _pool.getconn()
        if _alive(conn):
            return conn
        _pool.putconn(conn, close=True)
    raise RuntimeError("database unreachable: no live connection")


@contextmanager
def system_cursor():
    """Yields a cursor whose rows are dicts. Commits on success, rolls back on error,
    and always returns the connection to the pool (or closes it if it broke)."""
    conn = _checkout()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
    finally:
        _pool.putconn(conn, close=bool(conn.closed))
