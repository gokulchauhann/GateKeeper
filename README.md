![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![PostgreSQL](https://img.shields.io/badge/postgresql-14+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-phase%202-yellow.svg)

# Gatekeeper

**A cgroup-aware, multi-tenant database connection governor for kernel-level resource isolation.**

Gatekeeper tackles the "noisy neighbour" problem on a shared PostgreSQL database with Linux cgroups v2 instead of software-level timeouts or rate limits. Each tenant's database connections are tied to their real operating system backend processes, and those processes are placed in a cgroup for that tenant, so the kernel scheduler enforces the tenant's CPU and memory limits. It is built as a small multi-tenant web application with tenant and admin logins, so the mechanism can be shown live: one tenant runs a heavy report while the other tenants keep working normally.

---

## Table of Contents

- [The Problem](#the-problem)
- [How It Works](#how-it-works)
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

Multi-tenant SaaS applications often share one database instance across many customers to keep costs down. This exposes the "noisy neighbour" problem: one tenant's heavy query, such as a large report or an unindexed join, can starve every other tenant on that database.

Common mitigations (query timeouts, connection limits, application-level rate limiting) are **soft**. They react after a query has already started using resources, and they rely on the application predicting bad behaviour. None of them caps what a tenant's database work can consume at the operating system level.

## How It Works

PostgreSQL starts a dedicated operating system process for every client connection, and psycopg2 exposes that process's PID (`conn.info.backend_pid`, the same value as `SELECT pg_backend_pid()`). Gatekeeper's **Governor** uses this:

1. **Per-tenant pools.** Each tenant has its own connection pool, so a connection, and its backend process, is only ever used by one tenant. A request borrows a connection and returns it; the next request may get a different connection from the same pool, but every one of them belongs to that tenant.
2. **Limits from the plan tier.** On a tenant's first connection, the Governor reads the tenant's plan (`basic` 20% CPU / 256 MB, `pro` 50% / 1024 MB, `enterprise` 100% / 4096 MB) and creates `/sys/fs/cgroup/gatekeeper-tenant_<id>` with `cpu.max` and `memory.high`.
3. **PID into the cgroup.** On every checkout, the connection's backend PID is written into the tenant's `cgroup.procs`. From then on the **kernel scheduler** throttles that process, whatever the application does.
4. **Usage.** `get_tenant_usage()` turns the cgroup's `cpu.stat` and `memory.current` counters into CPU %, memory and throttled time, which the admin dashboard shows.

Two details matter for correctness:
- **Parallel query is turned off for tenant queries** (`SET LOCAL max_parallel_workers_per_gather = 0`). Parallel workers are separate Postgres processes outside the tenant's cgroup and would let a heavy query escape its limit.
- **Admin and login queries use a separate plain connection**, so the operator's own queries never run inside a tenant's cgroup.

See [`docs/architecture.md`](docs/architecture.md) for the full design and [`docs/interface-contract.md`](docs/interface-contract.md) for the exact interfaces.

## Architecture

```
Tenant Browser (Login)        Admin Browser (Login)
       │                              │
       └──────────► FastAPI Backend ◄─┘
                 (sessions, tenant + admin routes)
          │ tenant data                   │ login, admin, usage tables
          ▼                               ▼
   Governor (TenantGovernor)        plain connection pool
   - one psycopg2 pool per tenant
   - limits from plan_tiers
   - backend PID → tenant cgroup
          │
          ▼
   Linux cgroups v2: gatekeeper-tenant_<id>
   (cpu.max, memory.high, cpu.stat, memory.current)
          │
          ▼
   PostgreSQL backend processes (confined per tenant)

   Monitor thread (real mode): get_tenant_usage() every 2 s
   → usage_events / throttle_events → admin dashboard
```

## Tech Stack

| Technology | Role |
|---|---|
| Python 3.10+ | Backend and Governor |
| FastAPI + uvicorn | Web framework: auth, tenant and admin routes |
| psycopg2 | PostgreSQL driver and per-tenant connection pools |
| bcrypt | Password hashing |
| PostgreSQL 14+ | Tenant data (customers, orders) and metadata (plans, usage) |
| Linux cgroups v2 | Kernel-level CPU and memory limits per tenant |
| Jinja2 + vanilla JavaScript | Server-rendered pages; Chart.js planned for Phase 3 |

## Getting Started

### Two modes

| `GOVERNOR=` in `.env` | Runs on | What happens |
|---|---|---|
| `stub` (default) | Any machine with Python and PostgreSQL | Plain connections, no cgroups. Usage numbers are random, and every page shows a "not enforced" banner. For development. |
| `real` | Linux, WSL2 or GitHub Codespaces with cgroups v2, run as **root** | Tenant connections go into their cgroups and limits are enforced by the kernel. |

cgroups v2 does not exist on native Windows or macOS. Our Phase 2 real-mode run worked in a GitHub Codespace with `sudo`; other containers may not allow creating cgroups, so check with `sudo mkdir /sys/fs/cgroup/gk-test` (and `rmdir` it afterwards).

### Setup (Ubuntu, WSL2 or GitHub Codespaces)

```bash
git clone https://github.com/gokulchauhann/GateKeeper.git
cd GateKeeper

# PostgreSQL
sudo apt-get update && sudo apt-get install -y postgresql
sudo service postgresql start
sudo runuser -u postgres -- psql -c "ALTER USER postgres PASSWORD 'choose-a-local-password';"
sudo runuser -u postgres -- createdb gatekeeper
sudo runuser -u postgres -- psql -d gatekeeper < db/schema.sql
sudo runuser -u postgres -- psql -d gatekeeper < db/demo_data.sql

# Python
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configuration (never commit .env)
cp .env.example .env
# edit .env: DATABASE_URL=postgresql://postgres:<your-password>@localhost:5432/gatekeeper

# Demo data: 4 tenants, 5 users, 500 customers and 100,000 orders per tenant
python db/seed.py            # add --reset to wipe and reseed
```

`sudo runuser -u postgres --` is used instead of `sudo -u postgres` because some environments (Codespaces) allow password-free sudo only as root.

### Run

**Stub mode (any machine):**
```bash
uvicorn backend.main:app --port 8000
```

**Real mode (Linux/WSL2):** set `GOVERNOR=real` in `.env`, then run as root, because writing to `/sys/fs/cgroup` needs it:
```bash
sudo ./venv/bin/uvicorn backend.main:app --port 8000
```

Behind a proxy that serves the app over https (for example Codespaces port forwarding), add `--host 0.0.0.0 --forwarded-allow-ips "*"`.

Open http://127.0.0.1:8000/login. Demo accounts (local demo data only): `acme` / `acme-dev`, `globex` / `globex-dev`, `initech` / `initech-dev`, `umbrella` / `umbrella-dev`, and `admin` / `admin-dev`.

**Governor self-test (real mode, after seeding):**
```bash
sudo ./venv/bin/python governor/governor.py
```
It prints each connection's PID and cgroup, each tenant's `cpu.max` and `memory.high`, rejects an unknown tenant, and prints one usage sample.

**Frontend only, without a database:** `uvicorn frontend.dev_server:app --reload --port 8000` (see [`frontend/README.md`](frontend/README.md)).

## Project Status

Currently in **Phase 2: Core Development**.

- [x] Problem definition and architecture design
- [x] Manual cgroup + PID validation, done twice on real PostgreSQL backends ([`docs/cgroups-notes.md`](docs/cgroups-notes.md), [`docs/cgroup-validation.md`](docs/cgroup-validation.md))
- [x] Metadata schema, demo data tables and seed script
- [x] Interface contract and stub Governor
- [x] Tenant and admin UI
- [x] Backend: bcrypt login and sessions, tenant pages, heavy report, admin page, `/admin/usage` (tested in stub mode)
- [x] Governor: per-tenant pools, plan-tier limits, `memory.high`, PID assignment, `get_tenant_usage()`
- [x] End-to-end run in real mode: the heavy report took 4,224 ms without a limit and 17,365 ms under a 20% cgroup; the admin page showed 20.0% / 20% CPU while it ran ([`docs/phase2-results.md`](docs/phase2-results.md))
- [ ] Noisy-neighbour comparison: another tenant's response time with enforcement on and off (Phase 3)
- [ ] Live admin chart and throttle-event log (Phase 3)
- [ ] Plan-tier changes from the admin page without a restart (Phase 3)
- [ ] Noisy-neighbour demo with and without enforcement, testing and rehearsal (Phase 3)

## Scope & Limitations

- **PostgreSQL only.** MySQL runs connections as threads inside one process, so there is no per-connection process to put in a cgroup.
- **No transparent wire-protocol proxy (yet).** Clients use the FastAPI backend, which routes tenant queries through the Governor. An external application cannot yet connect to Gatekeeper as a drop-in Postgres server.
- **Single-machine demo** with simulated tenants, not a production deployment.
- **Per-tenant pools are required.** Isolation depends on a connection never being shared between tenants; cross-tenant transaction pooling (as in PgBouncer's transaction mode) would break the PID-to-tenant mapping.
- **`cpu.max` is a ceiling, not a reservation.** It stops one tenant taking too much, but does not add capacity. Proportional sharing with `cpu.weight` is future work.
- **Limits are read once per tenant.** A plan change takes effect after a restart until the Phase 3 "re-apply limits" step is built.
- **Real mode runs the backend as root**, because of the cgroup writes. Moving those writes into a small privileged helper or a delegated cgroup subtree is future work.

## Future Work

- **Transparent Postgres wire-protocol proxy**, so any application can point at Gatekeeper with a normal connection string.
- **Predictive throttling** using `EXPLAIN (FORMAT JSON)` cost estimates.
- **I/O limits** with the cgroups v2 `io` controller.
- **Least privilege:** cgroup writes through a delegated subtree or a small helper instead of running the web app as root.

## Team

**Team Cosmos (T060)**: Graphic Era (Deemed to be University), Dehradun. Mentor: Dr. Neeraj Kumar Pandey.

| Name | Role |
|---|---|
| Gokul Singh | Team lead. Schema and demo data, interface contract, backend, Governor Phase 2 extensions, manual cgroup validation, documentation |
| Rohit Singh | Governor core (cgroup creation, PID assignment, `TenantGovernor` with per-tenant pools), independent cgroup validation |
| Yuvraj Singh | Frontend: login, tenant and admin pages, development server |
| Kanak Bhatia | Member |

## References

- [PostgreSQL Documentation: System Information Functions (`pg_backend_pid`)](https://www.postgresql.org/docs/current/functions-info.html)
- [The Linux Kernel Documentation: Control Group v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)

## License

This project is licensed under the [MIT License](LICENSE).
