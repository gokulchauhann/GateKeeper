"""
Seed the Gatekeeper database with demo tenants, users, customers and orders.

Run from the repository root, after db/schema.sql and db/demo_data.sql:
    python db/seed.py            # refuses to run if tenants already exist
    python db/seed.py --reset    # DELETES all tenants, users, customers,
                                 # orders and usage/throttle rows, then reseeds

Demo accounts (local demo only, never real credentials):
    acme / acme-dev, globex / globex-dev, initech / initech-dev,
    umbrella / umbrella-dev, admin / admin-dev
These match frontend/dev_data.py, so the same logins work on the real backend.
"""
import os
import sys

import bcrypt
import psycopg2
from dotenv import load_dotenv

ORDERS_PER_TENANT = 100_000     # 4 tenants -> 400,000 orders in total
CUSTOMERS_PER_TENANT = 500

# (tenant name, plan tier name, login username)
TENANTS = [
    ("Acme Corp", "basic", "acme"),
    ("Globex", "pro", "globex"),
    ("Initech", "basic", "initech"),
    ("Umbrella Labs", "enterprise", "umbrella"),
]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def main():
    load_dotenv()
    reset = "--reset" in sys.argv
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn, conn.cursor() as cur:   # one transaction: all or nothing
            cur.execute("SELECT count(*) FROM tenants")
            if cur.fetchone()[0] > 0:
                if not reset:
                    sys.exit("Tenants already exist. Use --reset to wipe and reseed.")
                cur.execute("TRUNCATE orders, customers, usage_events, throttle_events, "
                            "users, tenants RESTART IDENTITY")

            for name, plan, username in TENANTS:
                cur.execute(
                    "INSERT INTO tenants (name, plan_tier_id) "
                    "SELECT %s, id FROM plan_tiers WHERE name = %s RETURNING id",
                    (name, plan),
                )
                tenant_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO users (tenant_id, username, password_hash, role) "
                    "VALUES (%s, %s, %s, 'tenant')",
                    (tenant_id, username, hash_password(f"{username}-dev")),
                )

                # Customers and orders are generated inside Postgres with
                # generate_series, which is much faster than inserting
                # 100,000 rows one by one from Python.
                cur.execute(
                    """
                    INSERT INTO customers (tenant_id, name, email, status, created_at)
                    SELECT %(t)s, 'Customer ' || g, 'customer' || g || '@tenant' || %(t)s || '.example',
                           CASE WHEN random() < 0.85 THEN 'active' ELSE 'inactive' END,
                           NOW() - random() * INTERVAL '365 days'
                    FROM generate_series(1, %(n)s) AS g
                    """,
                    {"t": tenant_id, "n": CUSTOMERS_PER_TENANT},
                )
                cur.execute(
                    """
                    INSERT INTO orders (tenant_id, customer_id, amount, status, created_at)
                    SELECT %(t)s,
                           (SELECT min(id) FROM customers WHERE tenant_id = %(t)s)
                             + floor(random() * %(c)s)::int,
                           round((100 + random() * 9900)::numeric, 2),
                           (ARRAY['pending','paid','shipped','delivered','delivered','refunded'])
                             [1 + floor(random() * 6)::int],
                           NOW() - random() * INTERVAL '365 days'
                    FROM generate_series(1, %(n)s)
                    """,
                    {"t": tenant_id, "c": CUSTOMERS_PER_TENANT, "n": ORDERS_PER_TENANT},
                )
                print(f"Seeded {name} ({plan}): login {username}")

            cur.execute(
                "INSERT INTO users (tenant_id, username, password_hash, role) "
                "VALUES (NULL, 'admin', %s, 'admin')",
                (hash_password("admin-dev"),),
            )
            print("Seeded admin: login admin")
        # Fresh statistics so the query planner knows the tables are large.
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ANALYZE customers; ANALYZE orders;")
    finally:
        conn.close()
    print(f"Done: {len(TENANTS)} tenants, {len(TENANTS) * ORDERS_PER_TENANT:,} orders.")


if __name__ == "__main__":
    main()
