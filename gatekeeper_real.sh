#!/usr/bin/env bash
# Gatekeeper: run with the REAL Governor (cgroups v2 enforcement) in GitHub
# Codespaces (also works on Ubuntu, Debian and WSL2).
#
#   bash gatekeeper_real.sh        limits enforced (GOVERNOR=real)
#   bash gatekeeper_real.sh off    limits off but still measured (GOVERNOR=unlimited),
#                                  the baseline for on/off comparisons
#
# Keep this file out of git; it is only for running the project.
# Safe to run again: it skips what is already done, then (re)starts the server.
# No passwords are stored in this file. A random local database password is
# generated on first run and written only to .env, which git ignores.
set -e

MODE="real"
GOV_VALUE="real"
if [ "$1" = "off" ]; then GOV_VALUE="unlimited"; fi

# ---------- Find the repository, wherever this script is run from ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for candidate in "$SCRIPT_DIR" "$PWD" /workspaces/GateKeeper; do
    if [ -f "$candidate/db/schema.sql" ] && [ -f "$candidate/backend/main.py" ]; then
        REPO="$candidate"
        break
    fi
done
if [ -z "$REPO" ]; then
    echo "ERROR: cannot find the GateKeeper repository (looked for db/schema.sql)."
    echo "Put this script in the repository root, or run it from there."
    exit 1
fi
cd "$REPO"
echo "Repository: $REPO   Mode: GOVERNOR=$GOV_VALUE"

# Run a command as the postgres user. Codespaces allows password-free sudo only
# as root, so become root first and then switch user with runuser.
as_postgres() { sudo runuser -u postgres -- "$@"; }

# ---------- 1. cgroups check (real mode only) ----------
if [ "$MODE" = "real" ]; then
    echo "== Checking cgroups v2 =="
    if ! sudo mkdir -p /sys/fs/cgroup/gk-setup-check 2>/dev/null; then
        echo "ERROR: cannot create a cgroup on this machine, so the real Governor cannot run here."
        exit 1
    fi
    missing=""
    [ -f /sys/fs/cgroup/gk-setup-check/cpu.max ] || missing="$missing cpu.max"
    [ -f /sys/fs/cgroup/gk-setup-check/memory.high ] || missing="$missing memory.high"
    sudo rmdir /sys/fs/cgroup/gk-setup-check
    if [ -n "$missing" ]; then
        echo "ERROR: cgroup controllers not available:$missing. The real Governor cannot run here."
        exit 1
    fi
    echo "cgroups v2 OK."
fi

# ---------- 2. PostgreSQL ----------
echo "== PostgreSQL =="
if ! command -v psql > /dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq postgresql > /dev/null
fi
sudo service postgresql start > /dev/null
echo "PostgreSQL running."

# ---------- 3. .env (and the database password) ----------
echo "== .env =="
# A .env without DATABASE_URL (for example one holding only GOVERNOR=...) is
# useless to the backend, so treat it as missing and write a complete one.
if [ -f .env ] && ! grep -q "^DATABASE_URL=" .env; then
    echo ".env has no DATABASE_URL; replacing it."
    rm .env
fi
if [ ! -f .env ]; then
    DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    as_postgres psql -qc "ALTER USER postgres PASSWORD '$DB_PASSWORD';"
    {
        echo "DATABASE_URL=postgresql://postgres:$DB_PASSWORD@localhost:5432/gatekeeper"
        echo "SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
        echo "GOVERNOR=$GOV_VALUE"
    } > .env
    echo ".env created with a new random database password."
else
    if grep -q "^GOVERNOR=" .env; then
        sed -i "s/^GOVERNOR=.*/GOVERNOR=$GOV_VALUE/" .env
    else
        echo "GOVERNOR=$GOV_VALUE" >> .env
    fi
    echo ".env kept; GOVERNOR=$GOV_VALUE."
fi

# ---------- 4. Database and tables ----------
echo "== Database =="
if as_postgres psql -lqt | cut -d'|' -f1 | grep -qw gatekeeper; then
    echo "Database gatekeeper already exists."
else
    as_postgres createdb gatekeeper
    # Files are read by this shell (<), so the postgres user needs no access to the repo folder.
    as_postgres psql -q -d gatekeeper < db/schema.sql
    as_postgres psql -q -d gatekeeper < db/demo_data.sql
    echo "Tables created."
fi

# ---------- 5. Python environment ----------
echo "== Python environment =="
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
echo "Packages installed."

# ---------- 6. Demo data ----------
echo "== Demo data =="
if ! seed_out=$(./venv/bin/python db/seed.py 2>&1); then
    if echo "$seed_out" | grep -q "Tenants already exist"; then
        echo "(Seed skipped: data already present. Use: ./venv/bin/python db/seed.py --reset)"
    else
        echo "$seed_out"
        echo "ERROR: seeding failed (see above)."
        exit 1
    fi
else
    echo "$seed_out"
fi
as_postgres psql -d gatekeeper -c "SELECT tenant_id, count(*) AS orders FROM orders GROUP BY 1 ORDER BY 1;"

# ---------- 7. Start the server ----------
# Stop a server left over from an earlier run, so port 8000 is free.
sudo pkill -f "uvicorn backend.main:app" 2>/dev/null || true
sleep 1

echo "== Starting the server ($MODE mode) =="
echo "   Open the forwarded port 8000 (Ports tab), then go to /login."
echo "   Accounts: acme/acme-dev, globex/globex-dev, initech/initech-dev, umbrella/umbrella-dev, admin/admin-dev"
echo "   Stop with Ctrl+C."
# --forwarded-allow-ips: trust the Codespaces proxy, so links to CSS/JS use https.
UVICORN_ARGS=(backend.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips "*")
if [ "$MODE" = "real" ]; then
    if [ "$GOV_VALUE" = "unlimited" ]; then
        echo "   Admin page should say 'Governor: limits off'."
    else
        echo "   Admin page should say 'Governor enforcing'."
    fi
    # Root is needed to write to /sys/fs/cgroup.
    exec sudo ./venv/bin/uvicorn "${UVICORN_ARGS[@]}"
else
    echo "   Pages show 'not enforced' in stub mode."
    exec ./venv/bin/uvicorn "${UVICORN_ARGS[@]}"
fi