"""
Unit tests for governor/governor.py.

They need no root, no cgroups and no PostgreSQL: a temporary folder stands in
for /sys/fs/cgroup, and fake connections stand in for psycopg2. Run from the
repository root with:
    python -m unittest discover -s tests -v
"""
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The Governor imports psycopg2 and python-dotenv. On a machine without them
# (for example a laptop without the venv), install small stand-ins first.
try:
    import psycopg2  # noqa: F401
except ImportError:
    fake = types.ModuleType("psycopg2")
    fake.connect = lambda url: None
    fake.pool = types.ModuleType("psycopg2.pool")
    fake.pool.ThreadedConnectionPool = object
    sys.modules["psycopg2"] = fake
    sys.modules["psycopg2.pool"] = fake.pool
try:
    import dotenv  # noqa: F401
except ImportError:
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = fake_dotenv

governor = importlib.import_module("governor.governor")

PLANS = {"1": (20, 256), "2": (50, 1024), "4": (100, 4096)}   # tenant -> (cpu %, memory MB)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args):
        if self.conn.dead:
            raise RuntimeError("server closed the connection unexpectedly")

    def fetchone(self):
        return (1,)


class FakeConnection:
    next_pid = 1000

    def __init__(self, dead=False):
        FakeConnection.next_pid += 1
        self.info = types.SimpleNamespace(backend_pid=FakeConnection.next_pid)
        self.dead = dead          # server side gone, but conn.closed still 0
        self.closed = 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        if self.dead:
            raise RuntimeError("connection already closed")

    def close(self):
        self.closed = 1


class FakePool:
    """Stands in for psycopg2's ThreadedConnectionPool."""

    def __init__(self, minconn, maxconn, dsn):
        self.maxconn = maxconn
        self.idle = []
        self.in_use = 0
        self.opened = 0
        self.dead_next = 0        # how many newly opened connections are dead

    def getconn(self):
        if self.idle:
            conn = self.idle.pop()
        else:
            if self.in_use >= self.maxconn:
                raise RuntimeError("connection pool exhausted")
            conn = FakeConnection(dead=self.dead_next > 0)
            self.dead_next = max(0, self.dead_next - 1)
            self.opened += 1
        self.in_use += 1
        return conn

    def putconn(self, conn, close=False):
        self.in_use -= 1
        if close:
            conn.close()
        else:
            self.idle.append(conn)


class GovernorTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        patches = [
            mock.patch.object(governor, "CGROUP_ROOT", self.root),
            mock.patch.object(governor, "tenant_limits", lambda t: PLANS.get(str(t))),
            mock.patch.object(governor.pool, "ThreadedConnectionPool", FakePool),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def cg(self, tenant, name):
        with open(os.path.join(self.root, f"gatekeeper-tenant_{tenant}", name)) as f:
            return f.read().strip()


class TestLimitsAndPids(GovernorTestCase):
    def test_limits_come_from_the_plan(self):
        gov = governor.TenantGovernor()
        gov.get_connection("1")
        gov.get_connection("2")
        self.assertEqual(self.cg(1, "cpu.max"), "20000 100000")
        self.assertEqual(self.cg(1, "memory.high"), str(256 * 1024 * 1024))
        self.assertEqual(self.cg(2, "cpu.max"), "50000 100000")
        self.assertEqual(self.cg(2, "memory.high"), str(1024 * 1024 * 1024))

    def test_backend_pid_is_written_on_every_checkout(self):
        gov = governor.TenantGovernor()
        a = gov.get_connection("1")
        self.assertEqual(self.cg(1, "cgroup.procs"), str(a.info.backend_pid))
        b = gov.get_connection("1")       # a second connection, same tenant
        self.assertEqual(self.cg(1, "cgroup.procs"), str(b.info.backend_pid))
        self.assertNotEqual(a.info.backend_pid, b.info.backend_pid)

    def test_unknown_tenant_is_rejected_before_any_cgroup_exists(self):
        gov = governor.TenantGovernor()
        with self.assertRaises(governor.GovernorConnectionError):
            gov.get_connection("999")
        self.assertFalse(os.path.exists(os.path.join(self.root, "gatekeeper-tenant_999")))

    def test_invalid_tenant_ids_are_rejected(self):
        gov = governor.TenantGovernor()
        for bad in ["../x", "1; rm -rf /", "", "abc"]:
            with self.subTest(tenant_id=bad):
                with self.assertRaises(governor.GovernorConnectionError):
                    gov.get_connection(bad)
        self.assertEqual(os.listdir(self.root), [])

    def test_released_connections_are_reused(self):
        gov = governor.TenantGovernor()
        for _ in range(8):     # more cycles than the pool size of 5
            gov.release_connection("1", gov.get_connection("1"))
        self.assertEqual(gov._pools["1"].opened, 1)


class TestPlanChangesAndModes(GovernorTestCase):
    def test_apply_limits_changes_a_running_cgroup(self):
        gov = governor.TenantGovernor()
        conn = gov.get_connection("1")
        with mock.patch.dict(PLANS, {"1": (50, 1024)}):
            result = gov.apply_limits("1")
        self.assertEqual(result["cpu_max"], "50000 100000")
        self.assertEqual(self.cg(1, "cpu.max"), "50000 100000")
        self.assertEqual(self.cg(1, "cgroup.procs"), str(conn.info.backend_pid))  # same process

    def test_apply_limits_rejects_unknown_tenant(self):
        with self.assertRaises(governor.GovernorConnectionError):
            governor.TenantGovernor().apply_limits("999")

    def test_enforcement_off_writes_max_but_still_confines(self):
        gov = governor.TenantGovernor(enforce=False)
        conn = gov.get_connection("1")
        self.assertEqual(self.cg(1, "cpu.max"), "max 100000")
        self.assertEqual(self.cg(1, "memory.high"), "max")
        self.assertEqual(self.cg(1, "cgroup.procs"), str(conn.info.backend_pid))


class TestUsage(GovernorTestCase):
    def write_stats(self, usage_us, throttled_us, memory_bytes):
        path = os.path.join(self.root, "gatekeeper-tenant_1")
        with open(os.path.join(path, "cpu.stat"), "w") as f:
            f.write(f"usage_usec {usage_us}\nnr_periods 10\nnr_throttled 5\nthrottled_usec {throttled_us}\n")
        with open(os.path.join(path, "memory.current"), "w") as f:
            f.write(str(memory_bytes))

    def test_no_cgroup_means_no_usage(self):
        self.assertIsNone(governor.TenantGovernor().get_tenant_usage("1"))

    def test_usage_is_the_change_between_two_readings(self):
        gov = governor.TenantGovernor()
        gov.get_connection("1")
        clock = iter([100.0, 102.0])     # two readings, 2 seconds apart
        with mock.patch.object(governor.time, "monotonic", lambda: next(clock)):
            self.write_stats(5_000_000, 1_000_000, 50 * 1048576)
            first = gov.get_tenant_usage("1")
            self.write_stats(5_400_000, 2_600_000, 60 * 1048576)   # +0.4 s CPU, +1.6 s throttled
            second = gov.get_tenant_usage("1")
        self.assertEqual((first["cpu_percent"], first["throttled_ms"]), (0.0, 0))
        self.assertEqual(second["cpu_percent"], 20.0)       # 0.4 s / 2 s
        self.assertEqual(second["throttled_ms"], 1600)
        self.assertEqual(second["memory_used_mb"], 60.0)
        self.assertEqual(second["tenant_id"], "1")


class TestDeadConnections(GovernorTestCase):
    def test_dead_connection_is_replaced_and_new_pid_confined(self):
        gov = governor.TenantGovernor()
        first = gov.get_connection("1")
        gov.release_connection("1", first)
        first.dead = True                  # e.g. PostgreSQL restarted
        conn = gov.get_connection("1")
        self.assertIsNot(conn, first)
        self.assertEqual(first.closed, 1)  # the dead one was closed, not reused
        self.assertEqual(self.cg(1, "cgroup.procs"), str(conn.info.backend_pid))

    def test_unreachable_database_raises(self):
        gov = governor.TenantGovernor()
        gov.get_connection("2")            # creates tenant 2's pool
        gov._pools["2"].idle.clear()
        gov._pools["2"].in_use = 0
        gov._pools["2"].dead_next = 100    # every new connection is dead
        with self.assertRaises(governor.GovernorConnectionError):
            gov.get_connection("2")

    def test_connection_that_broke_during_a_request_is_not_reused(self):
        gov = governor.TenantGovernor()
        conn = gov.get_connection("1")
        conn.closed = 1                    # broke during the request
        gov.release_connection("1", conn)
        self.assertNotIn(conn, gov._pools["1"].idle)


if __name__ == "__main__":
    unittest.main()
