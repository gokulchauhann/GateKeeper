# GateKeeper frontend

Server-rendered Jinja2 templates, one stylesheet layer per concern, and small vanilla JavaScript files. No build step and no frontend framework.

```
frontend/
├── templates/
│   ├── base.html                
│   ├── login.html
│   ├── macros/layout.html        
│   ├── macros/ui.html           
│   ├── tenant/dashboard.html
│   ├── tenant/customers.html
│   ├── tenant/orders.html
│   └── admin/dashboard.html
├── static/
│   ├── css/base.css             
│   ├── css/dashboard.css         
│   └── js/
│       ├── app.js               
│       ├── login.js              
│       ├── tenant.js             
│       └── admin.js              
├── dev_server.py                
└── dev_data.py                   

## Run it locally

`backend/main.py` has no login, tenant or admin routes yet, so the pages are served by a development-only server. From the repository root:

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

Templates are rendered with `templates.TemplateResponse(request, "<template>", context)`. The routes and context below are what `frontend/dev_server.py` uses; they are a proposal to agree with the backend owner, not an existing API.

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
| `GET /admin` | `admin/dashboard.html` | `rows`: `{tenant, usage, status}` where status is `healthy`/`warning`/`throttled`/`idle`; `summary`: `total`, `active`, `throttled`, `avg_cpu`; `updated_at`; `usage_url` |

### JSON endpoints used by JavaScript

- **`GET /admin/usage`**: list of usage samples, as defined in `docs/interface-contract.md`. Used by the admin page's Refresh button and, in Phase III, by polling.
- **Heavy report** (`POST` to `report_url`; the dev server uses `/tenant/report`): returns
  `{"status": "completed", "duration_ms": int, "rows": [{"month", "orders", "revenue"}], "cgroup_enforced": bool}`.
  The UI only says the query was limited by a cgroup when `cgroup_enforced` is `true`.

Errors should return a non-2xx status with `{"detail": "message"}`; the pages show that message.

## Still waiting on the backend

- Real login with bcrypt-hashed passwords from the `users` table, and server-side sessions.
- `customers` and `orders` tables (not in `db/schema.sql` yet) and the routes that read them through `Governor.get_connection()`.
- The heavy report query, run through the Governor.
- `GET /admin/usage` reading `usage_events` / `throttle_events`.
- Plan tier and quota per tenant on the admin page (`tenants` joined with `plan_tiers`).
