"""
Tenant data access through the Governor.

Every query on customers and orders goes through governor.get_connection(),
so with the real Governor it runs in the tenant's backend process, inside
that tenant's cgroup. Every query also filters by tenant_id, so a tenant
only ever sees its own rows.

GOVERNOR=stub (default) works on any machine: plain connections, no cgroups.
GOVERNOR=real needs Linux with cgroups v2 and root (sudo).
"""
import os
from contextlib import contextmanager

from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from governor import stub_governor

GOVERNOR_MODE = "enforced" if os.environ.get("GOVERNOR", "stub") == "real" else "stub"

if GOVERNOR_MODE == "enforced":
    from governor import governor as real_governor
    governor = real_governor.TenantGovernor()
else:
    governor = stub_governor.StubGovernor()

# Each module defines its own error class, so catch whichever one is raised.
_GOVERNOR_ERRORS = (stub_governor.GovernorConnectionError,)
if GOVERNOR_MODE == "enforced":
    _GOVERNOR_ERRORS += (real_governor.GovernorConnectionError,)


@contextmanager
def tenant_cursor(tenant_id: int):
    """A dict cursor on this tenant's Governor connection. Always ends the
    transaction and gives the connection back to the tenant's pool."""
    try:
        conn = governor.get_connection(str(tenant_id))
    except _GOVERNOR_ERRORS as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable for this tenant: {e}")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
    finally:
        # These queries only read, so roll back: a pooled connection must not
        # sit "idle in transaction" while it waits for the next request.
        conn.rollback()
        governor.release_connection(str(tenant_id), conn)


def tenant_usage(tenant_id: int):
    """Latest usage sample, or None. The real Governor has no
    get_tenant_usage() yet (Rohit's task), so this returns None until then."""
    if hasattr(governor, "get_tenant_usage"):
        return governor.get_tenant_usage(str(tenant_id))
    return None


def _order_rows(rows):
    # Templates expect plain numbers and YYYY-MM-DD dates, like frontend/dev_data.py.
    return [{**r, "amount": float(r["amount"]), "created_at": r["created_at"].strftime("%Y-%m-%d")}
            for r in rows]


_ORDER_COLUMNS = """
    SELECT o.id, c.name AS customer, o.amount, o.status, o.created_at
    FROM orders o JOIN customers c ON c.id = o.customer_id
    WHERE o.tenant_id = %s
    ORDER BY o.created_at DESC
    LIMIT %s
"""


def dashboard_data(tenant_id: int) -> dict:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT count(*) AS n FROM customers WHERE tenant_id = %s", (tenant_id,))
        customer_count = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM orders WHERE tenant_id = %s", (tenant_id,))
        order_count = cur.fetchone()["n"]
        cur.execute(_ORDER_COLUMNS, (tenant_id, 5))
        recent = _order_rows(cur.fetchall())
    return {"customer_count": customer_count, "order_count": order_count, "recent_orders": recent}


def customers(tenant_id: int) -> list:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("""SELECT id, name, email, status, created_at FROM customers
                       WHERE tenant_id = %s ORDER BY id""", (tenant_id,))
        rows = cur.fetchall()
    return [{**r, "created_at": r["created_at"].strftime("%Y-%m-%d")} for r in rows]


def orders_page(tenant_id: int, limit: int = 50) -> dict:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(_ORDER_COLUMNS, (tenant_id, limit))
        rows = _order_rows(cur.fetchall())
        # Summary over ALL of the tenant's orders, computed in SQL.
        cur.execute("""
            SELECT count(*) AS count,
                   coalesce(sum(amount) FILTER (WHERE status <> 'refunded'), 0) AS revenue,
                   count(*) FILTER (WHERE status IN ('pending', 'paid', 'shipped')) AS open
            FROM orders WHERE tenant_id = %s""", (tenant_id,))
        s = cur.fetchone()
    return {"orders": rows,
            "summary": {"count": s["count"], "revenue": float(s["revenue"]), "open": s["open"]}}
