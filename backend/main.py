"""
Gatekeeper backend.

Run from the repository root:
    uvicorn backend.main:app --reload --port 8000

Step 2: login, logout and tenant pages. The admin page and the heavy report
come in step 3. Set GOVERNOR=stub (default) or GOVERNOR=real in .env.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before anything reads DATABASE_URL

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import PlainTextResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from backend import auth, db, tenant_data  # noqa: E402

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
WARN_RATIO = 0.9  # usage at or above 90% of quota shows as "Near limit"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    yield
    db.close_pool()


app = FastAPI(title="Gatekeeper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
templates = Jinja2Templates(directory=FRONTEND_DIR / "templates")


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
                  usage=tenant_data.tenant_usage(tenant["id"]),
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


# ---------- Temporary placeholder (replaced in step 3) ----------

@app.get("/admin")
def admin_placeholder(request: Request):
    user, response = require(request, "admin")
    if response:
        return response
    return PlainTextResponse(f"Signed in as {user['username']} (admin).")
