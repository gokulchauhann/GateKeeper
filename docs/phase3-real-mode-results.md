# Phase 3 Real-Mode Results

Repeated measurements of the heavy report with and without cgroup enforcement, the full real-mode validation, and the noisy-neighbour test (section 5). Sections 1 to 4 were read from the running system on 10 November 2026 (Ubuntu VM), sections 5 to 7 on 15 November 2026 (GitHub Codespace). Phase 2's single-run results are in [`phase2-results.md`](phase2-results.md).

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

## 5. Noisy-neighbour test: limits on vs. off

Measured with `scripts/noisy_neighbour.py` in a GitHub Codespace with 2 CPU cores, three runs per mode on 15 November 2026 (run 1 in an earlier session that day, runs 2 and 3 back to back). The "off" runs used `GOVERNOR=unlimited`: the real Governor, the same pools and the same cgroups, with `cpu.max` and `memory.high` set to `max`. So the two modes differ only in the limit.

**Method.** The victim tenant, Globex (Pro plan), loads its orders page (a summary over its 100,000 orders) 20 times on a quiet system. Then the heavy tenant, Acme (Basic plan, 20%), runs 2 heavy reports at a time, one per core, and keeps starting new ones for 30 seconds, while Globex loads the same page again.

**Globex's orders page, every run:**

| Mode | Run | Quiet, median / p95 | Under Acme's load, median / p95 | Median slowdown | Samples under load |
|---|---|---|---|---|---|
| Limits off | 1 | 37.3 / 44.1 ms | 87.5 / 121.6 ms | 2.35× | 82 |
| Limits off | 2 | 43.3 / 51.6 ms | 96.2 / 141.6 ms | 2.22× | 80 |
| Limits off | 3 | 45.3 / 77.3 ms | 96.3 / 137.6 ms | 2.13× | 80 |
| Limits on | 1 | 35.9 / 98.9 ms | 45.9 / 75.1 ms | 1.28× | 94 |
| Limits on | 2 | 39.8 / 63.4 ms | 45.4 / 76.5 ms | 1.14× | 94 |
| Limits on | 3 | 40.2 / 67.6 ms | 48.8 / 71.1 ms | 1.21× | 94 |

**Averages of the three runs:**

| | Limits off | Limits on |
|---|---|---|
| Globex median under load | **93.3 ms** (87.5 to 96.3) | **46.7 ms** (45.4 to 48.8) |
| Globex p95 under load | 133.6 ms (121.6 to 141.6) | 74.2 ms (71.1 to 76.5) |
| Median slowdown vs. quiet | **2.23×** (2.13 to 2.35) | **1.21×** (1.14 to 1.28) |
| Acme reports finished per run | 8 to 10, about 7.5 s each | 2, 52 to 79 s each |

- **Enforcement protected the other tenant in every run.** Under the same load, Globex's median page time was 45.4 to 48.8 ms with limits on and 87.5 to 96.3 ms with limits off; the ranges do not overlap. On average, limits on halved Globex's response time under load (46.7 ms against 93.3 ms).
- **The heavy tenant paid for its own load.** With limits off, Acme's two reports took both cores and finished 8 to 10 reports per run. With limits on, its two concurrent reports shared its 20% of one core (about 10% each), and only 2 finished per run.
- **A small slowdown remains with limits on (1.14× to 1.28×).** Acme still uses its 20% of a core, and the tenants share PostgreSQL's buffer cache, its background processes and the backend process. Cgroups limit CPU time per tenant, not every shared resource.
- **Acme's capped reports took 79.4 s in run 1 but 52.5 s in runs 2 and 3.** Run 1 was in an earlier session, and a Codespace can be placed on different cloud hardware between sessions (section 3 shows how much the machine affects throttled runs). Globex's numbers, the measure that matters here, were consistent across all three runs.
- **The quiet-phase p95 of run 1 with limits on (98.9 ms) was raised by an outlier**, so p95 slowdown ratios are not compared; the under-load values are the fair comparison.

## 6. Plan change while a report runs

In real mode, while Acme's heavy report was running at about 20% of a core, the admin changed Acme from Basic to Pro on the admin page. The page confirmed `cpu.max = 50000 100000`, read back from the cgroup, and within a few seconds Acme's line on the CPU chart rose to about 50%, without a restart or a new connection. The new value applies from the kernel's next scheduling period to the processes already in the cgroup. (Observed on the chart; exact samples were not saved.)

The same run also confirmed the usage-history and throttle-event endpoints: after reloading the admin page, the charts and the throttle log showed the report from before the reload, with times matching the local clock.

## 7. Memory limit (`memory.high`)

Measured with `scripts/memory_test.py` in the same Codespace (no swap). The script takes a connection through the real Governor, so the backend process is in the tenant's cgroup, then sorts 4,000,000 rows in memory in that session (`work_mem` raised to 2 GB for the session only, statement timeout 180 s).

| | Acme (Basic, `memory.high` 256 MB) | Umbrella (Enterprise, `memory.high` 4096 MB) |
|---|---|---|
| Query | Cancelled by the 180 s statement timeout (180.7 s) | Completed in 10.8 s |
| Peak `memory.current` | 269 MB | 544 MB |
| `memory.events` "high" (times above `memory.high`) | +30,423 | 0 |
| `memory.events` "oom_kill" | 0 | 0 |
| Same backend process afterwards | yes | yes |
| PostgreSQL restarted | no | no |

- **The limit held.** The sort needs about 544 MB (Umbrella's peak). Acme's cgroup stayed at about 269 MB, close to its 256 MB `memory.high`, and the kernel recorded 30,423 times above the limit, each one followed by reclaim and a slowdown.
- **The tenant was slowed, not killed.** Without swap, the kernel could not free enough memory, so Acme's query became too slow to finish within 180 s, while the same query took 10.8 s for Umbrella. There was no OOM kill, the backend process survived, and PostgreSQL did not restart.
- **This is why we use `memory.high` and not `memory.max`.** A hard `memory.max` limit could have killed the backend, and PostgreSQL then restarts every connection, which would have hurt every tenant.

## Not yet tested

- **The plain-loop test on bare-metal Linux**, to confirm the 4.6 times factor in section 3 comes from the VM.
