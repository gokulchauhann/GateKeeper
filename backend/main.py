"""
Gatekeeper backend.

Run from the repository root:
    uvicorn backend.main:app --reload --port 8000

Login, tenant pages, heavy report, admin dashboard and /admin/usage.
Set GOVERNOR=stub (default) or GOVERNOR=real in .env. GOVERNOR=real needs
Linux with cgroups v2 and root (sudo).
"""
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before anything reads DATABASE_URL

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from backend import auth, db, tenant_data, usage  # noqa: E402

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
WARN_RATIO = 0.9  # usage at or above 90% of quota shows as "Near limit"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    usage.start_monitor()
    yield
    usage.stop_monitor()
    db.close_pool()


app = FastAPI(title="Gatekeeper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
templates = Jinja2Templates(directory=FRONTEND_DIR / "templates")


def _relative_url_for(name: str, **path_params) -> str:
    # Root-relative links ("/static/css/base.css") instead of full http:// URLs.
    # Behind a proxy (GitHub Codespaces) the app only sees http, and the browser
    # blocks http stylesheets on an https page. A relative link keeps the page's scheme.
    return str(app.url_path_for(name, **path_params))


templates.env.globals["url_for"] = _relative_url_for


# ---------- Helpers ----------

def redirect(url: str) -> RedirectResponse:
    # 303 makes the browser follow a form POST with a GET.
    return RedirectResponse(url, status_code=303)


def current_user(request: Request) -> dict | None:
    return auth.load_user(request.cookies.get(auth.SESSION_COOKIE, ""))


def home_for(role: str) -> str:
    return "/admin" if role == "admin" else "/tenant"


def require(request: Request, role: str):
    """(user, None) if signed in with this role, else (None, redirect response)."""
    user = current_user(request)
    if user is None:
        return None, redirect("/login")
    if user["role"] != role:
        return None, redirect(home_for(user["role"]))
    return user, None


def render(request: Request, template: str, status_code: int = 200, **context):
    base = {
        "user": current_user(request),
        "governor_mode": tenant_data.GOVERNOR_MODE,  # "enforced" only with the real Governor
        "data_source": "database",
        "warn_ratio": WARN_RATIO,
        "dev_mode": False,         # never true in the real backend
    }
    return templates.TemplateResponse(request, template, {**base, **context},
                                      status_code=status_code)


# ---------- Routes ----------

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Pages use an inline SVG icon; this stops the browser's default request logging a 404.
    return Response(status_code=204)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/")
def index(request: Request):
    user = current_user(request)
    return redirect(home_for(user["role"]) if user else "/login")


@app.get("/login")
def login_page(request: Request):
    user = current_user(request)
    if user:
        return redirect(home_for(user["role"]))
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    username = username.strip()
    if not username or not password:
        return render(request, "login.html", status_code=400,
                      error="Enter your username and password.", username=username)
    row = auth.check_login(username, password)
    if row is None:
        return render(request, "login.html", status_code=401,
                      error="Incorrect username or password.", username=username)
    response = redirect(home_for(row["role"]))
    response.set_cookie(auth.SESSION_COOKIE, auth.start_session(row["id"]),
                        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE, ""))
    response = redirect("/login")
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# ---------- Tenant pages ----------

@app.get("/tenant")
def tenant_dashboard(request: Request):
    user, response = require(request, "tenant")
    if response:
        return response
    tenant = user["tenant"]
    return render(request, "tenant/dashboard.html", active_page="dashboard", tenant=tenant,
                  usage=usage.latest_samples().get(tenant["id"]),
                  report_url="/tenant/report",
                  **tenant_data.dashboard_data(tenant["id"]))


@app.get("/tenant/customers")
def tenant_customers(request: Request):
    user, response = require(request, "tenant")
    if response:
        return response
    tenant = user["tenant"]
    return render(request, "tenant/customers.html", active_page="customers", tenant=tenant,
                  customers=tenant_data.customers(tenant["id"]))


@app.get("/tenant/orders")
def tenant_orders(request: Request):
    user, response = require(request, "tenant")
    if response:
        return response
    tenant = user["tenant"]
    return render(request, "tenant/orders.html", active_page="orders", tenant=tenant,
                  **tenant_data.orders_page(tenant["id"]))


@app.post("/tenant/report")
def tenant_report(request: Request):
    """The heavy report, run through the Governor. JSON for frontend/static/js/tenant.js."""
    user = current_user(request)
    if user is None or user["role"] != "tenant":
        return JSONResponse({"detail": "Sign in as a tenant to run reports."}, status_code=401)
    return tenant_data.heavy_report(user["tenant"]["id"])


# ---------- Admin ----------

def all_tenants() -> list:
    with db.system_cursor() as cur:
        cur.execute("""SELECT t.id, t.name, p.name AS plan, p.cpu_quota_percent, p.memory_limit_mb
                       FROM tenants t JOIN plan_tiers p ON p.id = t.plan_tier_id
                       ORDER BY t.id""")
        return [dict(r) for r in cur.fetchall()]


@app.get("/admin")
def admin_dashboard(request: Request):
    user, response = require(request, "admin")
    if response:
        return response
    samples = usage.latest_samples()
    rows = []
    for tenant in all_tenants():
        sample = samples.get(tenant["id"])
        rows.append({"tenant": tenant, "usage": sample,
                     "status": usage.usage_status(sample, tenant, WARN_RATIO)})
    active = [r for r in rows if r["usage"]]
    summary = {
        "total": len(rows),
        "active": len(active),
        "throttled": sum(r["status"] == "throttled" for r in rows),
        "avg_cpu": sum(r["usage"]["cpu_percent"] for r in active) / len(active) if active else None,
    }
    return render(request, "admin/dashboard.html", active_page="admin", rows=rows, summary=summary,
                  updated_at=max((r["usage"]["timestamp"] for r in active), default=None),
                  usage_url="/admin/usage")


@app.get("/admin/usage")
def admin_usage(request: Request):
    """One latest sample per active tenant (docs/interface-contract.md)."""
    user = current_user(request)
    if user is None or user["role"] != "admin":
        return JSONResponse({"detail": "Admin access required."}, status_code=403)
    return list(usage.latest_samples().values())
