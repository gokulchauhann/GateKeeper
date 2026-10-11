"""
Login and sessions.

Passwords: checked with bcrypt against users.password_hash (hashes are
created by db/seed.py). Sessions: a random token in an httponly cookie,
mapped to the user id in server memory. Restarting the server signs
everyone out, which is acceptable for this project.
"""
import secrets

import bcrypt

from backend.db import system_cursor

SESSION_COOKIE = "gk_session"
SESSION_MAX_AGE = 8 * 60 * 60  # seconds

_sessions: dict[str, int] = {}  # token -> users.id

# Checked when the username does not exist, so a wrong username takes as
# long as a wrong password and does not reveal which usernames exist.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt())


def check_login(username: str, password: str) -> dict | None:
    """Returns the users row if the password is right, otherwise None."""
    with system_cursor() as cur:
        cur.execute("SELECT id, username, password_hash, role, tenant_id "
                    "FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
    stored = row["password_hash"].encode() if row else _DUMMY_HASH
    if bcrypt.checkpw(password.encode(), stored) and row:
        return row
    return None


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = user_id
    return token


def end_session(token: str) -> None:
    _sessions.pop(token, None)


def load_user(token: str) -> dict | None:
    """The signed-in user in the shape the templates expect:
    {username, role, tenant}, where tenant is None for the admin."""
    user_id = _sessions.get(token)
    if user_id is None:
        return None
    with system_cursor() as cur:
        cur.execute("""
            SELECT u.username, u.role, t.id AS tenant_id, t.name AS tenant_name,
                   p.name AS plan, p.cpu_quota_percent, p.memory_limit_mb
            FROM users u
            LEFT JOIN tenants t ON t.id = u.tenant_id
            LEFT JOIN plan_tiers p ON p.id = t.plan_tier_id
            WHERE u.id = %s""", (user_id,))
        row = cur.fetchone()
    if row is None:
        return None
    tenant = None
    if row["tenant_id"] is not None:
        tenant = {"id": row["tenant_id"], "name": row["tenant_name"], "plan": row["plan"],
                  "cpu_quota_percent": row["cpu_quota_percent"],
                  "memory_limit_mb": row["memory_limit_mb"]}
    return {"username": row["username"], "role": row["role"], "tenant": tenant}
