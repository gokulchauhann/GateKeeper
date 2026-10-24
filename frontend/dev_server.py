"""
DEVELOPMENT-ONLY server for the GateKeeper frontend.

backend/main.py does not have login, tenant or admin routes yet, so this small
FastAPI app renders the frontend templates on localhost. It is not the
production backend and must not be deployed.

- Usage numbers come from the team's StubGovernor (governor/stub_governor.py),
  so they follow docs/interface-contract.md. They are random, and no cgroup
  limits are applied.
- Customers and orders come from frontend/dev_data.py, because db/schema.sql
  has no tables for them yet.
- Login uses the development accounts in frontend/dev_data.py and an
  in-memory session store.

The template context each page expects is documented in frontend/README.md,
so the real backend can render the same templates.

Run from the repository root:
    uvicorn frontend.dev_server:app --reload --port 8000
"""
import hmac
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from frontend import dev_data
from governor.stub_governor import StubGovernor

FRONTEND_DIR = Path(__file__).resolve().parent
SESSION_COOKIE = "gk_dev_session"
SESSION_MAX_AGE = 8 * 60 * 60
# Usage at or above this share of a tenant's quota is shown as "Near limit".
WARN_RATIO = 0.9
# How long the simulated heavy report takes on the dev server.
REPORT_DELAY_SECONDS = 2.5

app = FastAPI(title="GateKeeper frontend (development server)")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
templates = Jinja2Templates(directory=FRONTEND_DIR / "templates")
governor = StubGovernor()

# token -> username. In memory, so restarting the server signs everyone out.
_sessions: dict[str, str] = {}


# ---------- Helpers ----------

def _redirect(url: str) -> RedirectResponse:
    # 303 makes the browser follow up with a GET after a form POST.
    return RedirectResponse(url, status_code=303)


def tenant_view(tenant_id: int) -> dict:
    """A tenant row joined with its plan tier, in the shape the templates expect."""
    tenant = dev_data.TENANTS[tenant_id]
    return {"id": tenant_id, "name": tenant["name"], "plan": tenant["plan"], **dev_data.PLAN_TIERS[tenant["plan"]]}


def tenant_usage(tenant_id: int) -> dict | None:
    """Latest usage sample (interface-contract shape), or None if the tenant has no open session."""
    if tenant_id not in dev_data.ACTIVE_TENANT_IDS:
        return None
    return governor.get_tenant_usage(str(tenant_id))


def usage_status(usage: dict | None, tenant: dict) -> str:
    """healthy / warning / throttled / idle. Mirrored in frontend/static/js/admin.js."""
    if usage is None:
        return "idle"
    if usage["throttled_ms"] > 0:
        return "throttled"
    if (usage["cpu_percent"] >= tenant["cpu_quota_percent"] * WARN_RATIO
            or usage["memory_used_mb"] >= tenant["memory_limit_mb"] * WARN_RATIO):
        return "warning"
    return "healthy"


def current_user(request: Request) -> dict | None:
    username = _sessions.get(request.cookies.get(SESSION_COOKIE, ""))
    account = dev_data.DEV_USERS.get(username)
    if account is None:
        return None
    tenant = tenant_view(account["tenant_id"]) if account["tenant_id"] else None
    return {"username": username, "role": account["role"], "tenant": tenant}


def home_for(role: str) -> str:
    return "/admin" if role == "admin" else "/tenant"


def require(request: Request, role: str):
    """Return (user, None) if the signed-in user has this role, else (None, redirect)."""
    user = current_user(request)
    if user is None:
        return None, _redirect("/login")
    if user["role"] != role:
        return None, _redirect(home_for(user["role"]))
    return user, None


def render(request: Request, template: str, status_code: int = 200, **context):
    base = {
        "user": current_user(request),
        "governor_mode": "stub",        # "enforced" only when the real Governor confirms cgroup assignment
        "data_source": "development",   # shows the "Sample data" label on tenant pages
        "warn_ratio": WARN_RATIO,
    }
    return templates.TemplateResponse(request, template, {**base, **context}, status_code=status_code)


# ---------- Sign in / out ----------

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Pages use an inline SVG icon; this stops browsers logging a 404 for the default request.
    return Response(status_code=204)


@app.get("/")
def index(request: Request):
    user = current_user(request)
    return _redirect(home_for(user["role"]) if user else "/login")


@app.get("/login")
def login_page(request: Request):
    user = current_user(request)
    if user:
        return _redirect(home_for(user["role"]))
    return render(request, "login.html", dev_mode=True)


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    username = username.strip()
    if not username or not password:
        return render(request, "login.html", status_code=400, dev_mode=True,
                      error="Enter your username and password.", username=username)
    account = dev_data.DEV_USERS.get(username)
    if account is None or not hmac.compare_digest(account["password"].encode(), password.encode()):
        return render(request, "login.html", status_code=401, dev_mode=True,
                      error="Incorrect username or password.", username=username)

    token = secrets.token_urlsafe(32)
    _sessions[token] = username
    response = _redirect(home_for(account["role"]))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request):
    _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response = _redirect("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------- Tenant pages ----------

@app.get("/tenant")
def tenant_dashboard(request: Request):
    user, redirect = require(request, "tenant")
    if redirect:
        return redirect
    tenant = user["tenant"]
    orders = dev_data.ORDERS[tenant["id"]]
    return render(
        request, "tenant/dashboard.html",
        active_page="dashboard",
        tenant=tenant,
        usage=tenant_usage(tenant["id"]),
        customer_count=len(dev_data.CUSTOMERS[tenant["id"]]),
        order_count=len(orders),
        recent_orders=sorted(orders, key=lambda o: o["created_at"], reverse=True)[:5],
        report_url="/tenant/report",
    )


@app.get("/tenant/customers")
def tenant_customers(request: Request):
    user, redirect = require(request, "tenant")
    if redirect:
        return redirect
    tenant = user["tenant"]
    return render(request, "tenant/customers.html", active_page="customers",
                  tenant=tenant, customers=dev_data.CUSTOMERS[tenant["id"]])


@app.get("/tenant/orders")
def tenant_orders(request: Request):
    user, redirect = require(request, "tenant")
    if redirect:
        return redirect
    tenant = user["tenant"]
    orders = sorted(dev_data.ORDERS[tenant["id"]], key=lambda o: o["created_at"], reverse=True)
    summary = {
        "count": len(orders),
        "revenue": sum(o["amount"] for o in orders if o["status"] != "refunded"),
        "open": sum(o["status"] in ("pending", "paid", "shipped") for o in orders),
    }
    return render(request, "tenant/orders.html", active_page="orders",
                  tenant=tenant, orders=orders, summary=summary)


@app.post("/tenant/report")
def generate_report(request: Request):
    """Simulated heavy report. The real backend runs the expensive query
    through governor.get_connection(tenant_id)."""
    user = current_user(request)
    if user is None or user["role"] != "tenant":
        return JSONResponse({"detail": "Sign in as a tenant to run reports."}, status_code=401)

    started = time.perf_counter()
    time.sleep(REPORT_DELAY_SECONDS)  # stands in for the heavy query

    months = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    for order in dev_data.ORDERS[user["tenant"]["id"]]:
        month = months[order["created_at"][:7]]
        month["orders"] += 1
        month["revenue"] += order["amount"]

    return {
        "status": "completed",
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "rows": [{"month": m, **totals} for m, totals in sorted(months.items())],
        "governor_mode": "stub",
        "cgroup_enforced": False,
    }


# ---------- Admin ----------

@app.get("/admin")
def admin_dashboard(request: Request):
    user, redirect = require(request, "admin")
    if redirect:
        return redirect

    rows = []
    for tenant_id in dev_data.TENANTS:
        tenant = tenant_view(tenant_id)
        usage = tenant_usage(tenant_id)
        rows.append({"tenant": tenant, "usage": usage, "status": usage_status(usage, tenant)})

    active = [row for row in rows if row["usage"]]
    summary = {
        "total": len(rows),
        "active": len(active),
        "throttled": sum(row["status"] == "throttled" for row in rows),
        "avg_cpu": sum(row["usage"]["cpu_percent"] for row in active) / len(active) if active else None,
    }
    return render(
        request, "admin/dashboard.html",
        active_page="admin",
        rows=rows,
        summary=summary,
        updated_at=max((row["usage"]["timestamp"] for row in active), default=None),
        usage_url="/admin/usage",
        plan_change_url="/admin/tenants/{tenant_id}/plan",
        plans=[{"name": name, **limits} for name, limits in dev_data.PLAN_TIERS.items()],
    )


@app.get("/admin/usage")
def admin_usage(request: Request):
    """Same JSON shape as GET /admin/usage in docs/interface-contract.md."""
    user = current_user(request)
    if user is None or user["role"] != "admin":
        return JSONResponse({"detail": "Admin access required."}, status_code=403)
    return [usage for usage in map(tenant_usage, dev_data.TENANTS) if usage]


@app.post("/admin/tenants/{tenant_id}/plan")
async def change_plan(request: Request, tenant_id: int):
    """Exercises the plan-change form with the proposed response shape
    (frontend/README.md). There are no cgroups here, so limits_applied is
    always false and the page must not claim the limits changed."""
    user = current_user(request)
    if user is None or user["role"] != "admin":
        return JSONResponse({"detail": "Admin access required."}, status_code=403)
    if tenant_id not in dev_data.TENANTS:
        return JSONResponse({"detail": f"Unknown tenant {tenant_id}."}, status_code=404)
    try:
        plan = (await request.json()).get("plan")
    except ValueError:
        plan = None
    if plan not in dev_data.PLAN_TIERS:
        return JSONResponse({"detail": "Unknown plan."}, status_code=422)
    dev_data.TENANTS[tenant_id]["plan"] = plan   # in memory only
    return {"tenant_id": tenant_id, "plan": plan, **dev_data.PLAN_TIERS[plan],
            "limits_applied": False, "cpu_max": None}
