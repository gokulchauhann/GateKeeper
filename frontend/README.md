# GateKeeper frontend

Server-rendered Jinja2 templates, one stylesheet layer per concern, and small vanilla JavaScript files. No build step and no frontend framework.

```
frontend/
├── templates/
│   ├── base.html                 shared layout: sidebar, navigation, logout, dev-mode banner
│   ├── login.html
│   ├── macros/layout.html        icons and brand
│   ├── macros/ui.html            badges, stat cards, usage meters, empty states
│   ├── tenant/dashboard.html
│   ├── tenant/customers.html
│   ├── tenant/orders.html
│   └── admin/dashboard.html
├── static/
│   ├── css/base.css              tokens, app shell, buttons, forms, sign-in page
│   ├── css/dashboard.css         cards, tables, badges, meters, responsive rules
│   └── js/
│       ├── app.js                shared helpers (window.GK)
│       ├── login.js              show/hide password, double-submit guard
│       ├── tenant.js             "Generate heavy report" action
│       ├── admin.js              live polling, tenant table, Chart.js charts, throttle log
│       └── admin-plan.js         plan-change form (loaded only when the backend supports it)
├── dev_server.py                 DEVELOPMENT ONLY: serves the pages on localhost
└── dev_data.py                   DEVELOPMENT ONLY: sample tenants, users, customers, orders
```

## Run it locally

The real backend (`backend/main.py`) now serves these templates; see the main README to run it. The development-only server below still works for frontend work without a database. From the repository root:

```bash
pip install -r requirements.txt
uvicorn frontend.dev_server:app --reload --port 8000
```

Open http://127.0.0.1:8000/login. Test accounts are in `frontend/dev_data.py`.

The dev server takes usage numbers from `governor/stub_governor.py`, so they are random and **no cgroup limits are applied**. Every page shows a banner saying so. Customers and orders are sample data because `db/schema.sql` has no tables for them yet.

## What the real backend needs to provide

To use these templates, `backend/main.py` needs:

```python
templates = Jinja2Templates(directory="frontend/templates")
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")
```

Templates are rendered with `templates.TemplateResponse(request, "<template>", context)`. The routes and context below are what `frontend/dev_server.py` uses. The real backend implements the same routes and context (see `docs/interface-contract.md`).

### Context on every page

| Key | Type | Meaning |
|---|---|---|
| `user` | dict or `None` | `username`, `role` (`"tenant"` / `"admin"`), `tenant` (tenant dict, `None` for admin) |
| `active_page` | str | `dashboard`, `customers`, `orders` or `admin`; highlights the nav link |
| `governor_mode` | str | `"stub"` shows the development banner and "Not enforced" labels. Set `"enforced"` **only** when the real Governor has assigned the session to its cgroup. |
| `data_source` | str | `"development"` shows a "Sample data" label on customers and orders |
| `warn_ratio` | float | Usage share of quota shown as "Near limit" (dev server: `0.9`) |

A **tenant dict** is a `tenants` row joined with its `plan_tiers` row: `id`, `name`, `plan`, `cpu_quota_percent`, `memory_limit_mb`.

A **usage sample** is exactly the `Governor.get_tenant_usage()` shape from `docs/interface-contract.md`: `tenant_id`, `cpu_percent`, `memory_used_mb`, `throttled_ms`, `timestamp`.

### Pages

| Route | Template | Extra context |
|---|---|---|
| `GET /login`, `POST /login` | `login.html` | `error` (str, on failure), `username` (to refill the field). Form fields: `username`, `password`. |
| `POST /logout` | (redirect) | |
| `GET /tenant` | `tenant/dashboard.html` | `tenant`, `usage` (sample or `None` if no open session), `customer_count`, `order_count`, `recent_orders`, `report_url` |
| `GET /tenant/customers` | `tenant/customers.html` | `tenant`, `customers`: `id`, `name`, `email`, `status` (`active`/`inactive`), `created_at` |
| `GET /tenant/orders` | `tenant/orders.html` | `tenant`, `orders`: `id`, `customer`, `amount`, `status`, `created_at`; `summary`: `count`, `revenue`, `open` |
| `GET /admin` | `admin/dashboard.html` | `rows`: `{tenant, usage, status}` where status is `healthy`/`warning`/`throttled`/`idle`; `summary`: `total`, `active`, `throttled`, `avg_cpu`; `updated_at`; `usage_url`. Optional (Phase III, see below): `usage_history_url`, `throttle_events_url`, `plan_change_url`, `plans`. |

### JSON endpoints used by JavaScript

- **`GET /admin/usage`**: list of usage samples, as defined in `docs/interface-contract.md`. The admin page polls it every 2 seconds (one request at a time, paused while the tab is hidden, stopped on 401/403) and feeds the table, stat cards, charts and throttle log from it.
- **Heavy report** (`POST` to `report_url`; the dev server uses `/tenant/report`): returns
  `{"status": "completed", "duration_ms": int, "rows": [{"month", "orders", "revenue"}], "cgroup_enforced": bool}`.
  The UI only says the query was limited by a cgroup when `cgroup_enforced` is `true`.

Errors should return a non-2xx status with `{"detail": "message"}`; the pages show that message.

## Phase III admin dashboard

**What the numbers mean.** `cpu_percent` is a share of **one core** (100 = one full core), computed by the Governor from the change in `usage_usec` between two samples; it is the same unit as `cpu_quota_percent`, so "20.0% / 20%" means the tenant is at its `cpu.max` quota. Memory is `memory.current` against the plan's `memory.high` (a soft limit). `throttled_ms` is CPU time the kernel withheld during one monitor interval, not a running total. The page never computes or invents usage values; it only shows what the backend returns.

**Live charts.** Built from the polled `/admin/usage` samples, so they start empty and cover the time since the page was opened (last 5 minutes kept). A sample with an unchanged `timestamp` is not plotted twice. In stub mode the chart and throttle log are labelled as simulated.

**Waiting on the backend.** These are not implemented in `backend/` yet (they are listed as Phase 3 in `docs/interface-contract.md`). The page already handles them when the backend passes the matching context key; until then it shows what it can and says what is missing. The shapes below are a proposal to agree with the backend owner and move into the interface contract.

| Context key | Endpoint | Proposed response |
|---|---|---|
| `usage_history_url` | `GET /admin/usage/history?minutes=5` | List of usage samples (same shape as `/admin/usage`), from `usage_events` joined with `throttle_events`, oldest first. Fills the charts on page load. |
| `throttle_events_url` | `GET /admin/throttle-events?limit=100` | `[{"tenant_id", "throttled_ms", "occurred_at"}]` from `throttle_events`. Without it, the log only shows throttling seen since the page opened. |
| `plan_change_url` + `plans` | `POST /admin/tenants/{tenant_id}/plan`, body `{"plan": "pro"}` | `{"tenant_id", "plan", "cpu_quota_percent", "memory_limit_mb", "limits_applied": bool, "cpu_max": "50000 100000"}`. `plan_change_url` is a template with `{tenant_id}`; `plans` is a list of `plan_tiers` rows (`name`, `cpu_quota_percent`, `memory_limit_mb`). |

For plan changes the Governor also needs a method that re-reads the tenant's plan and rewrites `cpu.max` and `memory.high` on the existing cgroup; today it reads the limits once per tenant (`_ensure_cgroup`), so a change only applies after a restart. The page reports success only when the response has `limits_applied: true` and `cpu_max` matches the new quota; otherwise it shows a warning. Without `plan_change_url` it shows a "not available yet" notice instead of the form.

The development server implements the plan-change endpoint for testing the form only. It has no cgroups, so it always answers `limits_applied: false`.

## Real-mode validation

`scripts/validate_real_mode.py` checks a running real-mode system on Linux: cgroups v2 and controllers, permission to create cgroups, `cpu.max` and `memory.high` against each tenant's plan, the PIDs in `cgroup.procs` against PostgreSQL's client backends, `cpu.stat` deltas, and (optionally) heavy-report timings with the CPU share and throttling measured during each run. Each check prints PASS, FAIL, BLOCKED, SKIP or INFO, and the exit code is 0 (all checks that could run passed), 2 (a FAIL) or 3 (blocked by the environment).

Run it from the repository root, as root, while the backend runs with `GOVERNOR=real`, after at least one tenant has opened a page (so its cgroup exists):

```bash
sudo ./venv/bin/python scripts/validate_real_mode.py --report-user acme --runs 3
```

`--report-user` is optional; the tenant's password is asked for and never stored. Run `--help` for all options.

## Backend status (Phase 2)

Done in `backend/`: bcrypt login and server-side sessions, customers/orders pages from the database through the Governor, the heavy report, `GET /admin/usage`, and plan tiers and quotas on the admin page. Phase 3 frontend (above): live polling, charts and the throttle log are done on top of `/admin/usage`; stored history, the full throttle-event history and plan changes need the backend endpoints listed above.

<details><summary>Original list (kept for history)</summary>


- Real login with bcrypt-hashed passwords from the `users` table, and server-side sessions.
- `customers` and `orders` tables (not in `db/schema.sql` yet) and the routes that read them through `Governor.get_connection()`.
- The heavy report query, run through the Governor.
- `GET /admin/usage` reading `usage_events` / `throttle_events`.
- Plan tier and quota per tenant on the admin page (`tenants` joined with `plan_tiers`).

</details>
