"""
Tenant usage for the admin dashboard.

GOVERNOR=real: a background thread calls governor.get_tenant_usage() for every
tenant every MONITOR_INTERVAL seconds and stores the samples in usage_events
(and throttle_events when throttled_ms > 0). Both rows are written in one
transaction, so they share the same NOW() timestamp and can be matched up.
The admin page reads the latest stored sample per tenant.

GOVERNOR=unlimited works the same way (real cgroups, limits set to "max").

GOVERNOR=stub: nothing is stored. The stub's random numbers are returned
directly, and every page shows the "not enforced" banner.
"""
import logging
import threading

from backend.db import system_cursor
from backend.tenant_data import MEASURED, governor

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
    if not MEASURED or not hasattr(governor, "get_tenant_usage"):
        log.warning("usage monitor not started (stub mode or no get_tenant_usage yet)")
        return
    _stop.clear()
    _thread = threading.Thread(target=_monitor_loop, name="usage-monitor", daemon=True)
    _thread.start()


def stop_monitor():
    _stop.set()


# Columns for one usage sample: a usage_events row and, if the tenant was
# throttled in that cycle, the throttle_events row with the same timestamp
# (both are written in one transaction, so they share NOW()).
# Timestamps are stored without a time zone, so AT TIME ZONE attaches the
# database's own zone; the result is correct on servers not set to UTC.
_SAMPLE_SELECT = """
    SELECT u.tenant_id, u.cpu_percent, u.memory_used_mb,
           coalesce(th.throttled_ms, 0) AS throttled_ms,
           u.recorded_at AT TIME ZONE current_setting('TimeZone') AS sampled_at
    FROM usage_events u
    LEFT JOIN throttle_events th
           ON th.tenant_id = u.tenant_id AND th.occurred_at = u.recorded_at
"""


def _sample(r) -> dict:
    """One row in the interface-contract usage shape."""
    return {
        "tenant_id": str(r["tenant_id"]),
        "cpu_percent": round(r["cpu_percent"], 1),
        "memory_used_mb": round(r["memory_used_mb"], 1),
        "throttled_ms": r["throttled_ms"],
        "timestamp": r["sampled_at"].isoformat(),
    }


def latest_samples() -> dict:
    """{tenant_id: sample} in the interface-contract shape, for tenants with a
    recent sample. Tenants without one are idle and are left out."""
    if not MEASURED:
        return {t: governor.get_tenant_usage(str(t)) for t in tenant_ids()}
    with system_cursor() as cur:
        cur.execute("SELECT DISTINCT ON (u.tenant_id) * FROM (" + _SAMPLE_SELECT + """
                        WHERE u.recorded_at > NOW() - make_interval(secs => %s)
                     ) u
                     ORDER BY u.tenant_id, u.sampled_at DESC""", (STALE_AFTER,))
        rows = cur.fetchall()
    return {r["tenant_id"]: _sample(r) for r in rows}


def usage_history(minutes: int) -> list:
    """Every stored sample from the last `minutes` minutes, oldest first, for
    the admin charts. Empty in stub mode, where nothing is stored."""
    if not MEASURED:
        return []
    with system_cursor() as cur:
        cur.execute(_SAMPLE_SELECT + """
            WHERE u.recorded_at > NOW() - make_interval(mins => %s)
            ORDER BY u.recorded_at, u.tenant_id""", (minutes,))
        return [_sample(r) for r in cur.fetchall()]


def throttle_events(limit: int) -> list:
    """The most recent throttle events, newest first, for the admin log.
    Timestamps match the usage samples, so the page does not list one twice."""
    if not MEASURED:
        return []
    with system_cursor() as cur:
        cur.execute("""
            SELECT tenant_id, throttled_ms,
                   occurred_at AT TIME ZONE current_setting('TimeZone') AS occurred_at
            FROM throttle_events
            ORDER BY occurred_at DESC
            LIMIT %s""", (limit,))
        return [{"tenant_id": str(r["tenant_id"]), "throttled_ms": r["throttled_ms"],
                 "occurred_at": r["occurred_at"].isoformat()} for r in cur.fetchall()]


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
