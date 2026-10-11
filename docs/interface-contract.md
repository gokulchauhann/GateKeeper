# Interface Contract

---

## Governor.get_connection(tenant_id: str)

Returns a `psycopg2` connection object from that tenant's own pool. Pooling is **per tenant**: each tenant has its own pool, so a connection (and therefore its PostgreSQL backend process) is only ever used by that one tenant. A request borrows a connection and returns it; the tenant's next request may get a different connection from the same pool (up to 5), but every one of them is in that tenant's cgroup. A connection is never handed to another tenant, unlike PgBouncer's transaction pooling, which would break the PID-to-cgroup mapping.

On first connection for a tenant, the Governor looks up the tenant's plan tier and CPU/memory limits from the `tenants` and `plan_tiers` tables (see [`db/schema.sql`](../db/schema.sql)), and assigns the connection's real PostgreSQL backend PID (via `pg_backend_pid()`) into that tenant's Linux cgroup accordingly.

**On failure:** raises an exception (`GovernorConnectionError` or similar) if the tenant cannot be found, the database is unreachable, or the cgroup assignment fails. Callers (the backend) must catch this and respond with an appropriate error rather than letting it propagate as an unhandled 500.

---

## Governor.release_connection(tenant_id: str, conn)

Returns a connection to the tenant's pool once the backend is done using it for a request. Must be called after every `get_connection` to avoid exhausting the pool. Does not remove the PID from its cgroup — the cgroup assignment persists for the connection's lifetime, not just a single request.

---

## Governor.get_tenant_usage(tenant_id: str) -> dict

Reads the tenant's current `cpu.stat` and `memory.current` from its cgroup and returns:
```json
{
  "tenant_id": "string",
  "cpu_percent": 0.0,
  "memory_used_mb": 0.0,
  "throttled_ms": 0,
  "timestamp": "ISO8601 string"
}
```
Called every 2 seconds by the backend's monitor thread (`backend/usage.py`, real mode only), which writes rows into the `usage_events` and `throttle_events` tables (see [`db/schema.sql`](../db/schema.sql)), which the admin dashboard reads from.

---

## GET /admin/usage

Returns JSON, sourced from the `usage_events` and `throttle_events` tables:
```json
[
  {
    "tenant_id": "string",
    "cpu_percent": 0.0,
    "memory_used_mb": 0.0,
    "throttled_ms": 0,
    "timestamp": "ISO8601 string"
  }
]
```

---

## Notes

- Thread-safety: the Governor's internal per-tenant pool registry must be safe for concurrent access, since FastAPI handles multiple tenants' requests concurrently. Callers do not need to add their own locking.
- Schema reference: all tenant, plan-tier, and usage data referenced above lives in [`db/schema.sql`](../db/schema.sql) — `tenants`, `plan_tiers`, `users`, `usage_events`, `throttle_events`.

---

## Governor: rules added in Phase 2

- **Limits come from the database.** On a tenant's first connection, `get_connection` reads `cpu_quota_percent` and `memory_limit_mb` from `tenants` joined with `plan_tiers`. If the tenant does not exist, it raises `GovernorConnectionError` and creates no cgroup. The fixed 20% limit is removed.
- **Memory:** use `memory.high` (the kernel slows the process down), not `memory.max` (the kernel can kill a backend, and Postgres then restarts every connection, which hurts all tenants).
- **How `get_tenant_usage` computes its numbers.** `cpu.stat` holds running totals, so the Governor keeps the previous reading per tenant and uses the difference:
  - `cpu_percent` = (change in `usage_usec`) / (elapsed wall-clock microseconds) × 100. 100 means one full core.
  - `throttled_ms` = (change in `throttled_usec`) / 1000, for the same interval.
  - `memory_used_mb` = `memory.current` / 1,048,576.
  - On the first call for a tenant there is no previous reading, so it returns `cpu_percent` 0 and `throttled_ms` 0.
  - If the tenant's cgroup does not exist yet (no session opened), it returns `None`.

---

## Backend

**Two kinds of database connection:**
- **System connection** (a plain `psycopg2` connection or small pool, *not* through the Governor): login, reading `users`/`tenants`/`plan_tiers`, writing and reading `usage_events`/`throttle_events`, all admin pages. This keeps the admin's own queries out of any tenant's cgroup.
- **Tenant connection** (`governor.get_connection(tenant_id)` / `release_connection`): every query on `customers` and `orders`, including the heavy report. Always release in a `finally:` block. Every query must filter by `tenant_id`.

**Authentication:** form login (fields `username`, `password`), checked with `bcrypt.checkpw` against `users.password_hash`. Session is a random token in an `httponly`, `samesite=lax` cookie, mapped server-side to the user id (in memory, so a restart signs everyone out). No signing secret is needed for this, so `SESSION_SECRET` in `.env` is currently unused. Tenant routes require role `tenant`; admin routes require role `admin`.

**Routes** (templates and context exactly as in `frontend/README.md`):

| Route | Returns |
|---|---|
| `GET /login`, `POST /login` | `login.html`; on success 303 redirect to `/tenant` or `/admin` |
| `POST /logout` | 303 redirect to `/login` |
| `GET /tenant` | `tenant/dashboard.html` |
| `GET /tenant/customers` | `tenant/customers.html` (latest customers; paginate or limit, there are 500 per tenant) |
| `GET /tenant/orders` | `tenant/orders.html` (latest 50 orders; `summary` computed in SQL over all of the tenant's orders) |
| `POST /tenant/report` | JSON `{"status": "completed", "duration_ms": int, "rows": [{"month", "orders", "revenue", "repeat_orders"}], "governor_mode": str, "cgroup_enforced": bool}` (`repeat_orders` is extra; the page ignores it) |
| `GET /admin` | `admin/dashboard.html` |
| `GET /admin/usage` | JSON list, **one latest sample per tenant**, shape as in `GET /admin/usage` above |

- `governor_mode` is `"enforced"` and `cgroup_enforced` is `true` **only** when the request used the real Governor. With the stub they are `"stub"` and `false`.
- `dev_mode` is never true in the real backend.
- The monitor writes a cycle's `usage_events` and `throttle_events` rows in one transaction, so they share the same `NOW()` timestamp; `/admin/usage` joins them on `tenant_id` and that timestamp. A tenant with no sample in the last 10 seconds is idle and left out.
- Errors: non-2xx status with `{"detail": "message"}`. A `GovernorConnectionError` becomes HTTP 503 with a readable message, never an unhandled 500.

**Choosing the Governor:** set `GOVERNOR=real` or `GOVERNOR=stub` in `.env` (default `stub`), so the backend can be built and tested on any laptop and switched to the real Governor on a WSL/Linux machine running as root.

**Phase 3 (not needed now):** `GET /admin/usage/history` for the chart, and `POST /admin/tenants/{id}/plan` for plan changes. Shapes to be added here before they are built.
