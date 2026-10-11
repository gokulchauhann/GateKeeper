"""
Tenant usage for the admin dashboard.

GOVERNOR=real: a background thread calls governor.get_tenant_usage() for every
tenant every MONITOR_INTERVAL seconds and stores the samples in usage_events
(and throttle_events when throttled_ms > 0). Both rows are written in one
transaction, so they share the same NOW() timestamp and can be matched up.
The admin page reads the latest stored sample per tenant.

GOVERNOR=stub: nothing is stored. The stub's random numbers are returned
directly, and every page shows the "not enforced" banner.
"""
import logging
import threading
from datetime import timezone

from backend.db import system_cursor
from backend.tenant_data import GOVERNOR_MODE, governor

MONITOR_INTERVAL = 2      # seconds between samples
STALE_AFTER = 10          # a sample older than this means "no open session"

log = logging.getLogger("gatekeeper.monitor")
_stop = threading.Event()
_thread = None


def tenant_ids() -> list:
    with system_cursor() as cur:
        cur.execute("SELECT id FROM tenants ORDER BY id")
        return [r["id"] for r in cur.fetchall()]


def _monitor_loop():
    while not _stop.is_set():
        try:
            samples = [(t, governor.get_tenant_usage(str(t))) for t in tenant_ids()]
            with system_cursor() as cur:   # one transaction, so one NOW() for all rows
                for t, s in samples:
                    if s is None:          # no cgroup yet: tenant has never connected
                        continue
                    cur.execute("INSERT INTO usage_events (tenant_id, cpu_percent, memory_used_mb) "
                                "VALUES (%s, %s, %s)", (t, s["cpu_percent"], s["memory_used_mb"]))
                    if s["throttled_ms"] > 0:
                        cur.execute("INSERT INTO throttle_events (tenant_id, throttled_ms) "
                                    "VALUES (%s, %s)", (t, int(s["throttled_ms"])))
        except Exception:
            log.exception("usage monitor cycle failed")   # keep running; try again next cycle
        _stop.wait(MONITOR_INTERVAL)


def start_monitor():
    """Starts the monitor only with the real Governor and only once it has
    get_tenant_usage() (Rohit's task). Otherwise does nothing."""
    global _thread
    if GOVERNOR_MODE != "enforced" or not hasattr(governor, "get_tenant_usage"):
        log.warning("usage monitor not started (stub mode or no get_tenant_usage yet)")
        return
    _stop.clear()
    _thread = threading.Thread(target=_monitor_loop, name="usage-monitor", daemon=True)
    _thread.start()


def stop_monitor():
    _stop.set()


def latest_samples() -> dict:
    """{tenant_id: sample} in the interface-contract shape, for tenants with a
    recent sample. Tenants without one are idle and are left out."""
    if GOVERNOR_MODE != "enforced":
        return {t: governor.get_tenant_usage(str(t)) for t in tenant_ids()}
    with system_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (u.tenant_id)
                   u.tenant_id, u.cpu_percent, u.memory_used_mb,
                   coalesce(th.throttled_ms, 0) AS throttled_ms, u.recorded_at
            FROM usage_events u
            LEFT JOIN throttle_events th
                   ON th.tenant_id = u.tenant_id AND th.occurred_at = u.recorded_at
            WHERE u.recorded_at > NOW() - make_interval(secs => %s)
            ORDER BY u.tenant_id, u.recorded_at DESC""", (STALE_AFTER,))
        rows = cur.fetchall()
    return {r["tenant_id"]: {
        "tenant_id": str(r["tenant_id"]),
        "cpu_percent": round(r["cpu_percent"], 1),
        "memory_used_mb": round(r["memory_used_mb"], 1),
        "throttled_ms": r["throttled_ms"],
        "timestamp": r["recorded_at"].replace(tzinfo=timezone.utc).isoformat(),
    } for r in rows}


def usage_status(sample, tenant: dict, warn_ratio: float) -> str:
    """healthy / warning / throttled / idle. Same rules as frontend/static/js/admin.js."""
    if sample is None:
        return "idle"
    if sample["throttled_ms"] > 0:
        return "throttled"
    if (sample["cpu_percent"] >= tenant["cpu_quota_percent"] * warn_ratio
            or sample["memory_used_mb"] >= tenant["memory_limit_mb"] * warn_ratio):
        return "warning"
    return "healthy"
