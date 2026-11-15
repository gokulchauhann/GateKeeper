# Demo Script

A step-by-step live demo of Gatekeeper, about 10 minutes. Any team member should be able to run it. Measured results are in [`phase3-real-mode-results.md`](phase3-real-mode-results.md).

## Before the evaluation (15 minutes ahead)

1. Start the GitHub Codespace (or the Linux VM) and open a terminal in the repository.
2. Start the backend with limits on: `bash gatekeeper_real.sh` (or `sudo ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips "*"` with `GOVERNOR=real` in `.env`). Wait for `Uvicorn running`.
3. Open two browser windows on port 8000: a normal one for the tenant and an incognito one for the admin.
4. Make sure every tenant is on its seeded plan (Acme Basic, Globex Pro, Initech Basic, Umbrella Enterprise). Change any back on the admin page if needed.
5. Run the heavy report once as Acme as a warm-up, so the first demo run is not slowed by a cold cache.
6. Keep the screenshots from `phase3-real-mode-results.md` open in another tab as a fallback.

## The demo

| # | Do | Show and say |
|---|---|---|
| 1 | Admin window: sign in as `admin` / `admin-dev` | "Governor enforcing". Four tenants, each with its plan: Basic 20% CPU, Pro 50%, Enterprise 100%. "Each tenant's PostgreSQL backend processes go into that tenant's own Linux cgroup." |
| 2 | Tenant window: sign in as `acme` / `acme-dev` | Acme's dashboard: its own customers and orders only, plan Basic, "Protected". |
| 3 | Acme: click **Generate heavy report** | Switch to the admin window. Acme's CPU line rises to its dashed 20% quota line and stays flat there, status "Throttled", throttled time about 1.6 s in every 2 s sample. "The kernel scheduler gives Acme 20 ms of every 100 ms; the rest of the time it waits." |
| 4 | While it runs: Plan tiers form, Acme Corp to **Pro** | "The backend confirmed cpu.max = 50000 100000." Within a few seconds Acme's line jumps to about 50%. "We rewrote cpu.max on the running cgroup; the same query speeds up without a restart." |
| 5 | Change Acme back to **Basic** | The line drops back toward 20%. |
| 6 | Optional, terminal: `cat /sys/fs/cgroup/gatekeeper-tenant_1/cpu.max` and `cat /sys/fs/cgroup/gatekeeper-tenant_1/cgroup.procs` | The quota and Acme's real backend PIDs, straight from the kernel. |
| 7 | Show the noisy-neighbour result (section 5 of the results) | "Under the same load from Acme, Globex's page took 87.5 ms with limits off and 45.9 ms with limits on." |

## Questions to be ready for

- **Why PostgreSQL?** One OS process per connection, so a connection can be put in a cgroup. MySQL uses threads in one process.
- **Why not session pooling?** One pool per tenant means a connection never serves another tenant, so every PID belongs to one cgroup, and idle browser sessions do not hold connections.
- **What if the database restarts?** Every checkout checks the connection (`SELECT 1`) and replaces dead ones; a request that loses its connection mid-way gets a 503, the next one works.
- **Why `memory.high`, not `memory.max`?** Going over `memory.max` can get a backend killed, and PostgreSQL then restarts every connection. `memory.high` slows the tenant down instead (`scripts/memory_test.py`).
- **Why is a small slowdown left with limits on (1.28×)?** Cgroups limit CPU time per tenant; tenants still share the buffer cache, PostgreSQL's background processes and disk I/O.
- **What does not work yet?** Disk I/O limits, and the backend runs as root in real mode (see Future Work in the README).

## If something goes wrong

| Problem | Fix |
|---|---|
| Admin badge says "stub mode" | `.env` has `GOVERNOR=stub`. Set `GOVERNOR=real` and restart the server with `sudo`. |
| Pages unstyled in Codespaces | Start uvicorn with `--forwarded-allow-ips "*"`. |
| Tenant page says "Database unavailable" | The server is not running as root, so cgroup writes fail. Restart with `sudo`. |
| Charts empty, "Chart.js could not be loaded" | `frontend/static/vendor/chart.umd.min.js` is missing (see the README). The table still updates. |
| Nothing works | Show the screenshots and numbers in `phase3-real-mode-results.md` and explain the flow from them. |
