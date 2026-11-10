# Phase 3 Real-Mode Results

Repeated measurements of the heavy report with and without cgroup enforcement, plus the full real-mode validation. All numbers below were read from the running system on 10 November 2026. Phase 2's single-run results are in [`phase2-results.md`](phase2-results.md).

## Environment

- Ubuntu 24.04 virtual machine (Multipass) on an Apple M5 MacBook: 2 vCPUs, 4 GB RAM, kernel 6.8.0-147-generic, cgroups v2.
- PostgreSQL 16.15, Python 3.12.3 in a virtual environment, branch `phase3/frontend-live-dashboard`.
- Demo data from `db/seed.py`: 4 tenants, 100,000 orders per tenant.
- Real mode started as root: `sudo ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000`.
- The laptop was on AC power with Low Power Mode off. Earlier runs on battery in Low Power Mode took 2.7 to 6 s for the same unlimited report, so power settings matter for timings.

## 1. Validation (`scripts/validate_real_mode.py`)

After all four tenants had signed in: **18 PASS, 0 FAIL**. For every tenant, `cpu.max` and `memory.high` matched its plan and `cgroup.procs` held only PostgreSQL backend PIDs.

| Tenant | Plan | `cpu.max` | `memory.high` |
|---|---|---|---|
| Acme Corp | Basic | `20000 100000` | 268435456 (256 MB) |
| Globex | Pro | `50000 100000` | 1073741824 (1024 MB) |
| Initech | Basic | `20000 100000` | 268435456 (256 MB) |
| Umbrella Labs | Enterprise | `100000 100000` | 4294967296 (4096 MB) |

## 2. Heavy report: stub vs real, 5 runs each

Measured with an earlier version of `scripts/measure_report.py` (same method; run it as `scripts/measure_report.py acme umbrella` to repeat): one warm-up run per tenant, then 5 runs alternating Acme and Umbrella. In real mode the tenant cgroup's `cpu.stat` was read before and after each run; CPU share is CPU time used divided by the report's duration.

| Tenant | Plan | Stub, no limit | Real mode | CPU used per run (real) | CPU share (real) | Throttled per run (real) |
|---|---|---|---|---|---|---|
| Acme | Basic, 20% | 1,706 ms ± 74 | 31,286 ms ± 286 | 6.22 to 6.33 s | **20.0%** (20.0 to 20.1) | 24.8 to 25.2 s |
| Umbrella | Enterprise, 100% | 1,724 ms ± 45 | 1,334 ms ± 12 | 1.32 to 1.35 s | **99.5%** (99.0 to 99.8) | 0 |

- **Enforcement is exact.** Acme received 20.0% of one core in every run. Umbrella, on a 100% plan, was never throttled.
- **Stub is not a perfect baseline.** Umbrella was faster in real mode than in stub (1.33 s vs 1.72 s), probably because the stub opens a new database connection for every request while real mode reuses pooled connections. A fairer "enforcement off" baseline would be the real Governor with the cgroup writes switched off.

## 3. Why Acme is 23 times slower, not 5 times

Without a limit Acme and Umbrella need the same work (1.71 s and 1.72 s in stub). Yet under the 20% limit Acme's report used about 6.27 s of CPU, against 1.33 s for Umbrella's unthrottled run: about 4.7 times more CPU for the same work.

The same effect appears without PostgreSQL. A plain Python loop (25 million additions) in a temporary cgroup in the same VM:

| `cpu.max` | Wall time | CPU used | Share |
|---|---|---|---|
| `max 100000` (no limit) | 0.85 s, 0.81 s | 0.84 s, 0.81 s | 99% |
| `20000 100000` (20%) | 18.96 s, 18.76 s | 3.80 s, 3.77 s | 20.0% |

So inside this VM, any program throttled to 20% uses about 4.6 times more CPU time for the same work. The slowdown therefore splits into about 5 times from the 20% quota and about 4.7 times from the environment (5 × 4.7 ≈ 23.5; measured 31.3 s ÷ 1.33 s ≈ 23.5). This is a property of the test machine, not of Gatekeeper. A likely cause is the host running a mostly idle virtual CPU on slower cores or at a lower clock; this has not been confirmed. On bare-metal Linux the extra factor should be close to 1.

For this reason the CPU share, which does not depend on CPU speed, is the main measure of enforcement.

## 4. Admin dashboard during a report

While Acme's report was running (real mode, monitor sampling every 2 seconds):

| Tenant | CPU / quota | Memory / limit | Throttled in sample | Status |
|---|---|---|---|---|
| Acme Corp (Basic) | 19.2% / 20% | 28 MB / 256 MB | 1,655 ms | Throttled |
| Globex, Initech, Umbrella Labs | 0.0% | 0 to 13 MB | 0 ms | Healthy |

The CPU chart showed Acme's measured line flat along its dashed 20% quota line for the length of the report.

## Not yet tested

- **Noisy-neighbour comparison.** Another tenant's response time while Acme runs enough concurrent heavy reports to keep both vCPUs busy, with enforcement on and off.
- **The plain-loop test on bare-metal Linux**, to confirm the 4.6 times factor in section 3 comes from the VM.
- **Memory limit behaviour** (`memory.high`) under a memory-heavy query.
- **Plan-tier change without a restart.**
