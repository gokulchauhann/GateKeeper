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
   - per-tenant connection pool (psycopg2, session pooling)
   - on new connection: call pg_backend_pid()
   - assign/confirm PID → tenant's cgroup
   - poll cgroup stat files for usage/throttle data
                        │
                        ▼
          Linux cgroups v2 (per-tenant group)
        cpu.max · memory.max · cpu.stat · memory.current
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
- A control to change a tenant's plan tier, which updates their cgroup limits dynamically.

### 3.3 FastAPI Backend
The single entry point for both frontends. Responsibilities:
- Session-based authentication for both tenant and admin users (password hashing via bcrypt; session token stored in a cookie, validated server-side on each request).
- Tenant-facing routes that need database access call into the Governor rather than opening database connections directly.
- Admin-facing routes expose aggregated usage/throttle data sourced from the Governor and the metadata database.

The backend does not implement any database wire protocol itself — it is a normal web application server. It does not currently expose Gatekeeper as a transparent proxy that external applications could connect to directly (see [Future Work](#6-known-limitations--future-work)).

### 3.4 Governor Module
The core of the system, and the component that bridges the application layer and the operating system. Responsibilities:
1. **Connection pooling per tenant**, using `psycopg2.pool`. Pooling is **session-based**: once a tenant's request checks out a connection, that same connection (and therefore the same PostgreSQL backend process) is retained for the tenant's session, rather than being reused by a different tenant mid-session as transaction-level pooling would allow. This is a deliberate, necessary design choice — if a connection's underlying PID could be reassigned to a different tenant mid-session, the PID-to-cgroup mapping would silently become incorrect.
2. **PID discovery.** On establishing a new pooled connection for a tenant, the Governor executes `SELECT pg_backend_pid();` on that connection to obtain the real OS process ID handling it.
3. **Cgroup assignment.** The Governor writes that PID into the tenant's designated cgroup (`echo <PID> > /sys/fs/cgroup/tenant_<id>/cgroup.procs`), and ensures the cgroup's `cpu.max` and `memory.max` reflect the tenant's current plan tier.
4. **Usage/throttle monitoring.** The Governor periodically reads each tenant's `cpu.stat` (for `nr_throttled` / `throttled_usec`) and `memory.current`, and writes usage snapshots and throttle events to the metadata database for the admin dashboard to display.

### 3.5 Linux cgroups v2
The actual enforcement mechanism, provided by the kernel. Each tenant is assigned one cgroup, configured with:
- `cpu.max` — a hard CPU time ceiling per scheduling period (e.g., `20000 100000` caps the group to 20% of one core).
- `memory.max` — a hard memory ceiling in bytes.

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
4. The Governor either returns an existing pooled connection for that tenant, or opens a new one — and if new, immediately retrieves its backend PID and ensures that PID is placed in the tenant's cgroup.
5. The backend executes the report query on the returned connection.
6. If the query's CPU/memory usage exceeds the tenant's cgroup limits, the kernel throttles the underlying PostgreSQL backend process directly — the query simply takes longer to complete; no application-level intervention occurs.
7. The Governor's background monitor detects the throttling (via `cpu.stat`) on its next poll and logs the event, with tenant ID and timestamp, to the metadata database.
8. The admin dashboard, polling the usage endpoint, reflects the updated CPU/memory usage and the new throttle event on its next refresh — typically within a couple of seconds.

Meanwhile, other tenants' connections live in their own separate cgroups and are entirely unaffected by tenant's heavy query — this is the core property the live demo is built to make visible.

## 5. Key Design Decisions

| Decision | Reasoning |
|---|---|
| PostgreSQL only, no MySQL | MySQL's default threaded connection model doesn't expose a clean per-connection OS process to attach cgroup controls to. |
| Session pooling, not transaction pooling | Transaction pooling can reassign a connection's underlying process across different tenants mid-session, which would break the PID-to-cgroup mapping this project depends on. |
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