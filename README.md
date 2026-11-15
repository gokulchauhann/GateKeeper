![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![PostgreSQL](https://img.shields.io/badge/postgresql-14+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-phase%203-yellow.svg)

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
- [Testing](#testing)
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
- **Dead connections are replaced.** Every checkout checks the connection (`SELECT 1`), so after a PostgreSQL restart the next request gets a fresh connection, whose new PID goes into the cgroup as usual.
- **Plans can change while tenants are running.** `apply_limits()` rewrites `cpu.max` and `memory.high` on the existing cgroup; the kernel applies it from the next 100 ms period.

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
| `unlimited` | Same as `real` | Same Governor, pools and cgroups, but `cpu.max` and `memory.high` are set to `max`: tenants are measured, not limited. The baseline for on/off comparisons. |

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

The admin charts use Chart.js from `frontend/static/vendor/chart.umd.min.js`, which is in the repository. To restore it if it is ever missing:
```bash
mkdir -p frontend/static/vendor
curl -L -o frontend/static/vendor/chart.umd.min.js https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js
```

Open http://127.0.0.1:8000/login. Demo accounts (local demo data only): `acme` / `acme-dev`, `globex` / `globex-dev`, `initech` / `initech-dev`, `umbrella` / `umbrella-dev`, and `admin` / `admin-dev`.

**Governor self-test (real mode, after seeding):**
```bash
sudo ./venv/bin/python governor/governor.py
```
It prints each connection's PID and cgroup, each tenant's `cpu.max` and `memory.high`, rejects an unknown tenant, and prints one usage sample.

**Frontend only, without a database:** `uvicorn frontend.dev_server:app --reload --port 8000` (see [`frontend/README.md`](frontend/README.md)).

## Testing

**Unit tests** (no root, cgroups or database needed; a temporary folder stands in for `/sys/fs/cgroup`):
```bash
python -m unittest discover -s tests -v
```
They cover plan-tier limits, PID assignment on every checkout, rejection of unknown and malformed tenant IDs, pool reuse, live plan changes (`apply_limits`), the `unlimited` mode, the usage calculation from `cpu.stat`, and dead-connection replacement.

**Real-mode scripts** (Linux, PostgreSQL seeded, backend running unless noted):

| Script | What it checks |
|---|---|
| `sudo ./venv/bin/python scripts/validate_real_mode.py` | Every tenant's cgroup has the right `cpu.max`, `memory.high` and only PostgreSQL backend PIDs |
| `./venv/bin/python scripts/measure_report.py acme umbrella` | Heavy-report time and CPU share per tenant, repeated runs |
| `./venv/bin/python scripts/noisy_neighbour.py` | Another tenant's page time under heavy load; run once with `bash gatekeeper_real.sh off` and once with limits on |
| `sudo ./venv/bin/python scripts/memory_test.py` | A tenant above its `memory.high` is slowed, not killed, and PostgreSQL does not restart (no web server needed) |

The demo itself is written up in [`docs/demo-script.md`](docs/demo-script.md).

## Project Status

Currently in **Phase 3: Evaluation and Completion**.

- [x] Problem definition and architecture design
- [x] Manual cgroup + PID validation, done twice on real PostgreSQL backends ([`docs/cgroups-notes.md`](docs/cgroups-notes.md), [`docs/cgroup-validation.md`](docs/cgroup-validation.md))
- [x] Metadata schema, demo data tables and seed script
- [x] Interface contract and stub Governor
- [x] Tenant and admin UI
- [x] Backend: bcrypt login and sessions, tenant pages, heavy report, admin page, `/admin/usage` (tested in stub mode)
- [x] Governor: per-tenant pools, plan-tier limits, `memory.high`, PID assignment, `get_tenant_usage()`
- [x] End-to-end run in real mode: the heavy report took 4,224 ms without a limit and 17,365 ms under a 20% cgroup; the admin page showed 20.0% / 20% CPU while it ran ([`docs/phase2-results.md`](docs/phase2-results.md))
- [x] Live admin dashboard: CPU and memory charts with plan quota lines, throttle-event log, plan-change form (Phase 3, frontend)
- [x] Real-mode validation on Linux: `scripts/validate_real_mode.py` 18/18 checks; 5 runs per mode, Acme held at 20.0% of a core in every run, Umbrella (100%) never throttled ([`docs/phase3-real-mode-results.md`](docs/phase3-real-mode-results.md))
- [x] Usage-history and throttle-event endpoints: the admin charts and throttle log show stored data from before the page opened (Phase 3)
- [x] Plan-tier changes from the admin page without a restart: `apply_limits()` rewrites `cpu.max` and `memory.high` on the running cgroup; a running report sped up from 20% to about 50% of a core after a Basic-to-Pro change (Phase 3)
- [x] Enforcement-off mode (`GOVERNOR=unlimited`): same Governor and cgroups, limits set to `max`, for fair on/off comparisons (Phase 3)
- [x] Noisy-neighbour comparison: under the same load, the other tenant's median page time was 45.9 ms with limits on against 87.5 ms with limits off (1.28× vs. 2.35× its quiet time) ([`docs/phase3-real-mode-results.md`](docs/phase3-real-mode-results.md), section 5)
- [x] Dead-connection handling: health check on checkout, broken connections closed, 503 instead of a crash (Phase 3)
- [x] Unit tests: 15 tests for the Governor and the measurement helpers (`tests/`)
- [x] Demo script ([`docs/demo-script.md`](docs/demo-script.md)); Chart.js served from the repository, so the demo works offline
- [x] `memory.high` test: the same 4,000,000-row sort ran in 10.8 s for a 4 GB tenant, while a 256 MB tenant was held at about 269 MB and slowed past a 180 s timeout, with no OOM kill and no PostgreSQL restart ([`docs/phase3-real-mode-results.md`](docs/phase3-real-mode-results.md), section 7)
- [ ] Repeated noisy-neighbour runs

## Scope & Limitations

- **PostgreSQL only.** MySQL runs connections as threads inside one process, so there is no per-connection process to put in a cgroup.
- **No transparent wire-protocol proxy (yet).** Clients use the FastAPI backend, which routes tenant queries through the Governor. An external application cannot yet connect to Gatekeeper as a drop-in Postgres server.
- **Single-machine demo** with simulated tenants, not a production deployment.
- **Per-tenant pools are required.** Isolation depends on a connection never being shared between tenants; cross-tenant transaction pooling (as in PgBouncer's transaction mode) would break the PID-to-tenant mapping.
- **`cpu.max` is a ceiling, not a reservation.** It stops one tenant taking too much, but does not add capacity. Proportional sharing with `cpu.weight` is future work.
- **Cgroups limit CPU time per tenant, not every shared resource.** Tenants still share PostgreSQL's buffer cache, its background processes and disk I/O, so a small slowdown remains under load even with limits on (1.28× in our test).
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
| Yuvraj Singh | Frontend: login, tenant and admin pages, live admin dashboard, development server. Real-mode validation and measurement scripts, Phase 3 real-mode results |
| Kanak Bhatia | Member |

## References

- [PostgreSQL Documentation: System Information Functions (`pg_backend_pid`)](https://www.postgresql.org/docs/current/functions-info.html)
- [The Linux Kernel Documentation: Control Group v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)

## License

This project is licensed under the [MIT License](LICENSE).