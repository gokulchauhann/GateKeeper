# Architecture

This document describes Gatekeeper's system architecture in detail: its components, how they interact, the data flow through the system, and the reasoning behind the key design decisions. For the interface contracts between components, see [`interface-contract.md`](interface-contract.md).

## 1. Overview

Gatekeeper is a multi-tenant web application in which per-tenant database resource isolation is enforced by the Linux kernel, not by application-level logic. The system has two user-facing surfaces (a tenant application and an admin dashboard) sitting on top of a shared FastAPI backend, which routes all database access through a Governor module. The Governor is responsible for mapping each tenant's database session to a real operating system process and confining that process to a dedicated Linux cgroup with CPU and memory limits.

The core idea the whole architecture is built around: **PostgreSQL forks a dedicated OS process for every client connection**, and exposes that process's PID via the `pg_backend_pid()` SQL function. This gives the Governor a reliable, real handle to attach kernel-level resource controls to — something that isn't available in the same form on thread-per-connection database engines like MySQL, which is why this project is scoped to PostgreSQL only.

## 2. High-Level Diagram

```
 Tenant Browser (Login)              Admin Browser (Login)
        │                                     │
        └──────────────┬──────────────────────┘
                        ▼
                 FastAPI Backend
        (session auth, tenant routes, admin routes)
                        │
                        ▼
              Governor Module (Python)
   - per-tenant connection pool (psycopg2; a connection never crosses tenants)
   - first connection: read limits from plan_tiers, create the cgroup
   - every checkout: backend PID → tenant's cgroup.procs
   - get_tenant_usage(): cpu.stat / memory.current deltas
                        │
                        ▼
          Linux cgroups v2 (per-tenant group)
        cpu.max · memory.high · cpu.stat · memory.current
                        │
                        ▼
     PostgreSQL backend process (per tenant session,
              confined to its cgroup)
                        │
                        ▼
   Usage & throttle events ──▶ Metadata DB (PostgreSQL)
                        │
                        ▼
              Admin Dashboard (live view)
```

## 3. Components

### 3.1 Tenant Application (Frontend)
Server-rendered HTML pages (Jinja2 templates) representing a small multi-tenant business application — the simulated "product" tenants use day-to-day. Includes:
- A login page (session-based auth).
- Light-weight pages that generate routine, indexed queries (e.g., viewing orders/customers).
- One deliberately heavy action (e.g., a full report export) used to demonstrate the throttling mechanism during the live demo.

### 3.2 Admin Dashboard (Frontend)
A separate login surface for the platform operator. Displays, per tenant:
- Current plan tier and configured CPU/memory quota.
- Live resource usage, refreshed via periodic polling (JavaScript `setInterval` + `fetch`, roughly every 1–2 seconds) against the backend's usage endpoint.
- A timestamped log of throttling events.
- (Phase 3) A control to change a tenant's plan tier, which will update their cgroup limits without a restart.

### 3.3 FastAPI Backend
The single entry point for both frontends. Responsibilities:
- Session-based authentication for both tenant and admin users (passwords checked with bcrypt; a random session token in an `httponly` cookie, mapped to the user server-side).
- Two kinds of database connection. Tenant data (customers, orders, the heavy report) goes through the Governor. Login, admin pages and the usage tables use a separate plain connection pool (`backend/db.py`), so the admin's own queries never run inside a tenant's cgroup.
- Every tenant transaction runs `SET LOCAL max_parallel_workers_per_gather = 0`. Parallel query workers are separate Postgres processes started by the server, so they would not be in the tenant's cgroup; turning them off keeps the tenant's whole query inside its confined backend.
- A background monitor thread (`backend/usage.py`, real mode only) calls `governor.get_tenant_usage()` every 2 seconds and stores samples in `usage_events` and `throttle_events`.
- `GOVERNOR=stub` (default) or `GOVERNOR=real` in `.env` selects the Governor, so the app can be developed on any machine and run with enforcement on Linux.

The backend does not implement any database wire protocol itself — it is a normal web application server. It does not currently expose Gatekeeper as a transparent proxy that external applications could connect to directly (see [Future Work](#6-known-limitations--future-work)).

### 3.4 Governor Module
The core of the system, and the component that bridges the application layer and the operating system. Responsibilities:
1. **Connection pooling per tenant**, using psycopg2's `ThreadedConnectionPool` (thread-safe, because FastAPI serves requests on several threads). Pooling is **per tenant**: each tenant has its own pool, so a connection (and therefore its PostgreSQL backend process) is only ever used by that one tenant. A request borrows a connection and returns it; the tenant's next request may get a different connection from the same pool (up to 5), but every one of them is in that tenant's cgroup. A connection is never handed to another tenant, unlike PgBouncer's transaction pooling, which would break the PID-to-cgroup mapping.
2. **PID discovery.** The Governor reads `conn.info.backend_pid`, which psycopg2 gets from libpq. It is the same number `SELECT pg_backend_pid();` returns, without an extra query.
3. **Cgroup assignment.** On a tenant's first connection the Governor reads `cpu_quota_percent` and `memory_limit_mb` from `tenants` joined with `plan_tiers` (an unknown tenant is rejected before any cgroup exists), creates `/sys/fs/cgroup/gatekeeper-tenant_<id>`, and writes `cpu.max` and `memory.high`. On every checkout it writes the connection's PID into `cgroup.procs`, so a connection that Postgres replaced is confined too. All of a tenant's backends share one cgroup, so the limit applies to the tenant as a whole.
4. **Usage numbers.** `get_tenant_usage()` reads `cpu.stat` and `memory.current`. `cpu.stat` holds running totals, so it keeps the previous reading per tenant and reports the difference: CPU % = Δ`usage_usec` ÷ elapsed time × 100, throttled ms = Δ`throttled_usec` ÷ 1000. The backend's monitor stores these samples.

### 3.5 Linux cgroups v2
The actual enforcement mechanism, provided by the kernel. Each tenant is assigned one cgroup, configured with:
- `cpu.max` — a hard CPU time ceiling per scheduling period (e.g., `20000 100000` caps the group to 20% of one core).
- `memory.high` — a memory threshold in bytes. Above it the kernel reclaims memory and slows the group down. We use it instead of `memory.max` (a hard limit), because hitting `memory.max` can get a backend killed by the OOM killer, and Postgres then restarts every connection, which would hurt all tenants.

Once a process's PID is written into a cgroup's `cgroup.procs` file, the kernel scheduler enforces these limits directly on that process. No application code monitors or intervenes at query-execution time — the enforcement is physical, not advisory.

### 3.6 PostgreSQL
Serves two roles in this architecture:
- **Primary multi-tenant database** — holds the actual application data (orders, customers, etc.) that tenants query through the application.
- **Metadata store** — a separate schema/set of tables recording tenant plan tiers, quota configuration, and logged usage/throttle events, read by the admin dashboard.

PostgreSQL's process-per-connection model (as opposed to a threaded model) is the foundational assumption the entire Governor design depends on.

## 4. Data Flow: A Single Request, End to End

Using the example of a tenant clicking "Generate Report":

1. The tenant's browser sends the request to the FastAPI backend, with their session cookie.
2. The backend validates the session and identifies the tenant.
3. The backend calls `governor.get_connection(tenant_id)`.
4. The Governor takes a connection from that tenant's pool (opening one if needed), reads its backend PID and writes it into the tenant's cgroup.
5. The backend executes the report query on the returned connection.
6. If the query's CPU/memory usage exceeds the tenant's cgroup limits, the kernel throttles the underlying PostgreSQL backend process directly — the query simply takes longer to complete; no application-level intervention occurs.
7. The backend's monitor thread calls `get_tenant_usage()` on its next 2-second cycle, sees the throttling in `cpu.stat`, and stores a usage sample and a throttle event in the metadata database.
8. The admin dashboard, polling the usage endpoint, reflects the updated CPU/memory usage and the new throttle event on its next refresh — typically within a couple of seconds.

Meanwhile, other tenants' connections live in their own separate cgroups and are entirely unaffected by tenant's heavy query — this is the core property the live demo is built to make visible.

## 5. Key Design Decisions

| Decision | Reasoning |
|---|---|
| PostgreSQL only, no MySQL | MySQL's default threaded connection model doesn't expose a clean per-connection OS process to attach cgroup controls to. |
| Per-tenant pools, not shared transaction pooling | A connection is only ever used by one tenant, so every backend PID belongs to exactly one tenant's cgroup. Transaction pooling across tenants would break that mapping. |
| `memory.high`, not `memory.max` | Slowing a tenant down is safe; an OOM kill of one backend makes Postgres restart every connection. |
| Parallel workers off for tenant queries | Workers are separate processes outside the tenant's cgroup and would let a heavy query escape its limit. |
| Separate system connection for admin and login | Keeps the operator's own queries out of tenant cgroups. |
| Governor embedded in the backend, not a standalone wire-protocol proxy | Building a protocol-level Postgres proxy is significant additional systems work; embedding the Governor as an internal module lets the team focus development time on the core isolation mechanism within the project timeline. A standalone proxy is documented as future work. |
| Polling instead of WebSockets for the admin dashboard | The usage data changes on the order of seconds, not milliseconds; polling every 1–2 seconds is simple to implement correctly and sufficient for the demo's purposes. |
| Cgroups v2 (not v1) | v2's unified hierarchy is the current standard interface on modern Linux kernels and is simpler to reason about than v1's per-controller trees. |

## 6. Known Limitations & Future Work

- **No transparent proxy.** External applications cannot yet point directly at Gatekeeper using a standard Postgres connection string; all access currently goes through the FastAPI backend's own routes. A wire-protocol-level proxy (accepting real `psql`/driver connections via the Postgres frontend/backend protocol) is planned as a post-submission extension.
- **Single-machine deployment.** The system is demonstrated on one machine with simulated tenants, not a distributed, production-scale deployment.
- **CPU and memory only.** I/O-weight enforcement (via the cgroups v2 `io` controller) is not yet implemented.
- **Reactive, not predictive, throttling.** Query cost is not currently estimated before execution; a planned extension uses PostgreSQL's `EXPLAIN (FORMAT JSON)` planner output to pre-classify expensive queries and adjust limits proactively.

## 7. Related Documents

- [`interface-contract.md`](interface-contract.md) — exact function signatures and API response shapes between components.
- [`cgroups-notes.md`](cgroups-notes.md) — raw exploration notes and terminal commands from manual cgroup validation.
