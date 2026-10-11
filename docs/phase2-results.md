# Phase 2 Results

First end-to-end run of the full system: login, backend, Governor, Linux cgroup and admin dashboard. All numbers below were read from the running system on 11 October 2026. Earlier manual validations are in [`cgroups-notes.md`](cgroups-notes.md) (Gokul) and [`cgroup-validation.md`](cgroup-validation.md) (Rohit).

## Environment

- GitHub Codespaces (Linux container), PostgreSQL 16, Python 3.14 in a virtual environment.
- Backend started as root: `sudo ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips "*"`.
- Demo data from `db/seed.py`: 4 tenants, 5 users, 500 customers and 100,000 orders per tenant (400,000 orders in total).

## 1. Data and login (stub mode)

| Check | Result |
|---|---|
| `db/schema.sql` and `db/demo_data.sql` load | Pass |
| Seed: orders per tenant | 100,000 for each of tenants 1 to 4 |
| Login with correct password | `POST /login` 303, then `GET /tenant` 200 |
| Login with wrong password | `POST /login` 401 |
| Tenant opening `/admin` | 303 redirect back to `/tenant` |
| Admin opening `/tenant` | 303 redirect back to `/admin` |
| Signed-out user opening `/tenant` | 303 redirect to `/login` |
| Tenant dashboard, customers and orders pages | 200, with each tenant's own data |

## 2. Heavy report: without and with the cgroup

The report (`POST /tenant/report`) is a deliberately CPU-bound query: monthly totals plus a self-join of the tenant's orders on customer. Parallel query workers are disabled for tenant queries, so the whole query runs in the tenant's one backend process.

| Mode | Limit | Report time |
|---|---|---|
| `GOVERNOR=stub` (no cgroup) | none | 4,224 ms |
| `GOVERNOR=real` (Acme, Basic plan) | `cpu.max = 20000 100000` (20%) | 17,365 ms |

The capped run was about 4.1 times slower. A 20% cap stretches only the CPU part of the work: 17.4 s at 20% is about 3.5 s of CPU, so roughly 0.7 s of the 4.2 s baseline was work the cap does not slow (planning, reading data, returning rows). Each figure is a single run; repeated runs are planned for Phase 3.

## 3. Kernel evidence for tenant 1 (Acme)

Read from `/sys/fs/cgroup/gatekeeper-tenant_1/` after the capped report:

| File | Value |
|---|---|
| `cpu.max` | `20000 100000` |
| `cgroup.procs` | `72767` (Acme's PostgreSQL backend PID) |
| `cpu.stat` `usage_usec` | 7,246,614 |
| `cpu.stat` `nr_periods` / `nr_throttled` | 371 / 359 |
| `cpu.stat` `throttled_usec` | 28,371,263 |

These counters cover everything the cgroup has run since it was created, not only the report. The tenant was throttled in 359 of 371 scheduling periods (96.8%), and it used 7.25 s of CPU over 371 × 0.1 s = 37.1 s, about 19.5%, which matches the 20% limit.

## 4. Admin dashboard during the report

Two samples taken while Acme's report was running (`GOVERNOR=real`, monitor sampling every 2 seconds):

| Tenant | CPU / quota | Memory / limit | Throttled in sample | Status |
|---|---|---|---|---|
| Acme Corp (Basic) | 20.0% / 20%, then 20.1% / 20% | 41 MB / 256 MB | 1,582 ms, then 1,577 ms | Throttled |
| Globex (Pro) | 0.0% / 50% | 20 MB / 1024 MB | 0 ms | Healthy |
| Initech, Umbrella Labs | No session | No session | n/a | Idle |

About 1.58 s of each 2 s sample was throttled time (about 79%), which is what a query that wants a full core gets under a 20% cap. After the report finished, Acme returned to 0.0% CPU and Healthy.

## Not yet tested

- **Noisy-neighbour comparison.** We have not yet measured another tenant's response time while a heavy tenant runs, with enforcement on and off. A single report uses at most one CPU core, so on a machine with several cores the comparison needs enough concurrent heavy reports to keep every core busy. Planned for Phase 3.
- **Repeated measurements** (several runs per mode) and automated tests.
- **Plan-tier change without a restart** (Phase 3).
