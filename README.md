![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![PostgreSQL](https://img.shields.io/badge/postgresql-14+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-in%20development-yellow.svg)

# Gatekeeper

**A cgroup-aware, multi-tenant database connection governor for kernel-level resource isolation.**

Gatekeeper is a multi-tenant database governor that solves the "noisy neighbor" problem using Linux cgroups v2 instead of software-level timeouts or rate limits. Each tenant's PostgreSQL session is mapped to its real operating system backend process and confined to a dedicated cgroup, so CPU and memory limits are enforced directly by the kernel scheduler — not guessed at by application code. Built as a small multi-tenant web application with tenant and admin logins, it demonstrates the mechanism live: one tenant can hammer the database with a heavy query while every other tenant's performance stays unaffected.

---

## Table of Contents

- [The Problem](#the-problem)
- [How It Works](#how-it-works)
- [Demo](#demo)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Getting Started](#getting-started)
- [Project Status](#project-status)
- [Scope & Limitations](#scope--limitations)
- [Future Work](#future-work)
- [Team](#team)
- [References](#references)
- [License](#license)

---

## The Problem

Multi-tenant SaaS applications often share a single database instance across many customers to keep infrastructure costs down. This exposes the classic "noisy neighbor" problem: one tenant's heavy query — a large report export, an unindexed join, a bulk import — can starve every other tenant sharing that database, even though they've done nothing wrong.

Most existing mitigations (query timeouts, connection limits, application-level rate limiting) are **soft** — they react after a query has already started consuming resources, and rely on the application correctly predicting bad behavior. None of them physically cap what a tenant's database session can consume at the OS level.

## How It Works

PostgreSQL creates a dedicated operating system process for every client connection, and exposes that process's PID via `pg_backend_pid()`. Gatekeeper's **Governor module** uses this to:

1. Pool each tenant's connections using **session-based pooling** (a tenant keeps the same backend process for their whole session — not shared mid-session across tenants, unlike transaction-level pooling).
2. On connection, look up the tenant's plan tier and assign their PostgreSQL backend process's real PID into a dedicated **Linux cgroup v2** group with CPU/memory limits.
3. Let the **kernel scheduler** — not application code — enforce those limits directly on the running database process.
4. Read live usage and throttling stats (`cpu.stat`, `memory.current`) back out of the cgroup filesystem and log them for the admin dashboard.

See [`docs/architecture.md`](docs/architecture.md) for the full breakdown and diagram.

## Demo

> 🎬 Demo video/GIF coming soon.

The demo scenario: multiple simulated tenants share one PostgreSQL instance through Gatekeeper. One tenant triggers a deliberately heavy query (e.g., a large report generation). The admin dashboard shows that tenant being throttled in real time, while every other tenant's queries stay fast and unaffected.

## Architecture

```
 Tenant Browser (Login)        Admin Browser (Login)
        │                              │
        └──────────► FastAPI Backend ◄──────────┘
                          │
                          ▼
                 Governor Module (Python)
        - per-tenant connection pool (psycopg2, session pooling)
        - on new connection: get pg_backend_pid()
        - assign PID → tenant's cgroup
                          │
                          ▼
           Linux cgroups v2 (CPU / memory limits)
                          │
                          ▼
        PostgreSQL backend process (per tenant, confined)
                          │
                          ▼
     Usage / throttle events → Metadata DB → Admin Dashboard
```

Full diagram: [`docs/architecture.md`](docs/architecture.md)

## Tech Stack

| Technology | Role |
|---|---|
| Python 3.10+ | Core language for backend and Governor module |
| FastAPI | Web framework — auth, tenant & admin API routes |
| psycopg2 | PostgreSQL driver, connection pooling |
| PostgreSQL 14+ | Primary multi-tenant database + metadata store |
| Linux cgroups v2 | Kernel-level CPU/memory enforcement per tenant |
| Jinja2 | Server-rendered HTML templates |
| Chart.js | Live usage graphs on the admin dashboard |

## Getting Started

> ⚠️ Setup instructions are still being finalized as the project develops. This section will be updated with exact steps.

**Prerequisites (planned):**
- Linux with cgroups v2 enabled and delegated (root/sudo access required)
- Python 3.10+
- PostgreSQL 14+

**Planned setup:**
```bash
git clone https://github.com/<your-username>/gatekeeper.git
cd gatekeeper
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Database setup and environment variable instructions — TODO
# Run instructions — TODO
```

## Project Status

Currently in **Phase 1 — Design & Core Validation**.

- [x] Problem definition and architecture design
- [x] Phase 1 proposal submitted
- [ ] Manual cgroup + PID validation (Phase 1)
- [ ] Governor module core implementation (Phase 2)
- [ ] Backend API + authentication (Phase 2)
- [ ] Tenant + admin UI (Phase 2)
- [ ] Live admin dashboard with usage/throttle logging (Phase 3)
- [ ] Dynamic plan-tier quota adjustment (Phase 3)
- [ ] End-to-end testing & demo rehearsal (Phase 3)

Track detailed progress on the [Issues](../../issues) / [Projects](../../projects) board.

## Scope & Limitations

- **PostgreSQL only.** MySQL uses a thread-per-connection model rather than a process-per-connection model, so per-tenant cgroup assignment doesn't map cleanly onto it.
- **No transparent wire-protocol proxy (yet).** Client apps talk to the FastAPI backend, which internally routes through the Governor — a tenant's own external application cannot yet connect directly to Gatekeeper as a drop-in Postgres replacement. See [Future Work](#future-work).
- **Single-machine demo.** The project demonstrates the mechanism with simulated tenants on one machine, not a distributed production deployment.
- **Session pooling required.** The isolation guarantee depends on a tenant retaining the same backend process for their session; transaction-level pooling would break the PID-to-tenant mapping.

## Future Work

- **Transparent Postgres wire-protocol proxy** — allow any existing application to point at Gatekeeper as a drop-in replacement for a normal Postgres connection string, with zero application-side changes (similar in spirit to PgBouncer, but with kernel-level enforcement underneath).
- **Predictive query-cost throttling** using `EXPLAIN (FORMAT JSON)` to pre-classify heavy queries before execution.
- **I/O weight enforcement** via the cgroups v2 `io` controller, in addition to CPU/memory.
- **Automatic tenant migration** to isolated instances for tenants consistently over quota.

## Team

**Team Cosmos**

| Name | Role |
|---|---|
| Gokul Singh | Team Lead |
| Yuvraj Singh | Member |
| Kanak Bhatia | Member |
| Rohit Singh | Member |

## References

- [PostgreSQL Documentation — System Administration Functions (`pg_backend_pid`)](https://www.postgresql.org/docs/current/functions-info.html)
- [The Linux Kernel Documentation — Control Group v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)

## License

This project is licensed under the [MIT License](LICENSE).
