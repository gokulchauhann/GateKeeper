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
import time
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
            # Parallel query workers are separate Postgres processes started by the
            # server, so they would NOT be in the tenant's cgroup. Turning them off
            # keeps all of this tenant's work inside the one confined backend.
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            yield cur
    finally:
        # These queries only read, so roll back: a pooled connection must not
        # sit "idle in transaction" while it waits for the next request.
        conn.rollback()
        governor.release_connection(str(tenant_id), conn)


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


# The deliberately heavy report. Besides monthly orders and revenue it counts,
# for every order, how many earlier orders the same customer had placed
# ("repeat orders"). That is a self-join of the tenant's orders on customer:
# with about 200 orders per customer it compares millions of pairs, so it is
# CPU-bound and runs for seconds. That is the point: it is the noisy neighbour.
_REPORT_SQL = """
WITH earlier AS (
    SELECT o1.id, count(*) AS earlier_orders
    FROM orders o1
    JOIN orders o2
      ON o2.tenant_id = o1.tenant_id
     AND o2.customer_id = o1.customer_id
     AND o2.created_at < o1.created_at
    WHERE o1.tenant_id = %(t)s AND o2.tenant_id = %(t)s
    GROUP BY o1.id
)
SELECT to_char(date_trunc('month', o.created_at), 'YYYY-MM') AS month,
       count(*) AS orders,
       sum(o.amount) AS revenue,
       count(e.id) AS repeat_orders
FROM orders o
LEFT JOIN earlier e ON e.id = o.id
WHERE o.tenant_id = %(t)s
GROUP BY 1
ORDER BY 1
"""


def heavy_report(tenant_id: int) -> dict:
    started = time.perf_counter()
    with tenant_cursor(tenant_id) as cur:
        cur.execute(_REPORT_SQL, {"t": tenant_id})
        rows = cur.fetchall()
    duration_ms = round((time.perf_counter() - started) * 1000)
    return {
        "status": "completed",
        "duration_ms": duration_ms,
        "rows": [{"month": r["month"], "orders": r["orders"], "revenue": float(r["revenue"]),
                  "repeat_orders": r["repeat_orders"]} for r in rows],
        "governor_mode": GOVERNOR_MODE,
        "cgroup_enforced": GOVERNOR_MODE == "enforced",
    }
