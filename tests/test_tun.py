"""All-apps capture plumbing that can be tested without root or touching real routes."""
import os
import signal
import sys
import tempfile
import unittest
import warnings
from unittest import mock

os.environ.setdefault("POGO_HOME", tempfile.mkdtemp(prefix="pogo-test-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pogo import config, tun  # noqa: E402

NETSTAT = """Routing tables

Internet:
Destination        Gateway            Flags               Netif Expire
default            192.168.1.1        UGScIg                en0
default            10.20.30.1        UGScg                 en6
0/1                198.18.0.1         UGSc              utun233
127                127.0.0.1          UCS                   lo0
"""


class ParsingTests(unittest.TestCase):
    def test_primary_route_skips_interface_scoped_defaults(self):
        with mock.patch.object(tun, "_out", return_value=NETSTAT):
            self.assertEqual(tun.primary_route(), ("en6", "10.20.30.1"))

    def test_primary_route_without_gateway(self):
        with mock.patch.object(tun, "_out", return_value="default  link#15  UCS  en6\n"):
            self.assertEqual(tun.primary_route(), ("en6", None))

    def test_leftover_capture_routes_are_detected(self):
        with mock.patch.object(tun, "_out", return_value=NETSTAT), \
                mock.patch.object(tun.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertIn("capture routes", tun.leftovers())

    def test_cleanup_only_runs_well_formed_route_deletes(self):
        good = ["route", "-n", "delete", "-host", "10.20.30.40"]
        self.assertTrue(all(tun._ROUTE_ARG.match(a) for a in good[3:]))
        for evil in (";rm -rf /", "$(id)", "--", "-flush", "a b"):
            self.assertIsNone(tun._ROUTE_ARG.match(evil), evil)


class FakeCapture:
    def __init__(self, cfg, report, **kwargs):
        self.report, self.stopping, self.torn_down = report, False, False

    def setup(self):
        self.report("ok", "virtual network interface created")
        return True

    def watch(self):
        return "test finished"

    def teardown(self):
        FakeCapture.torn_down = True


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        # the worker is meant to outlive its launcher, which Python flags as a ResourceWarning
        guard = warnings.catch_warnings()
        guard.__enter__()
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(guard.__exit__, None, None, None)

    def test_worker_reports_progress_then_ready_and_cleans_up(self):
        read_end, write_end = os.pipe()
        with mock.patch.object(tun, "Capture", FakeCapture):
            self.assertEqual(tun.run_worker({}, write_end), 0)
        with os.fdopen(read_end) as f:
            self.assertEqual(f.read(), "ok\tvirtual network interface created\nready\t\n")
        self.assertTrue(FakeCapture.torn_down)

    def _start_with_worker(self, script: str):
        reports = []
        fake_cmd = lambda *args: [sys.executable, "-c", script, *args]  # noqa: E731
        with mock.patch.object(tun, "preflight", return_value=None), \
                mock.patch.object(tun, "self_command", fake_cmd):
            code = tun.start_background({}, lambda kind, msg: reports.append((kind, msg)))
        return code, reports

    def test_background_relays_progress_until_ready(self):
        script = ("import os, sys; fd = int(sys.argv[sys.argv.index('--report-fd') + 1]); "
                  "os.write(fd, b'ok\\tstep one\\nready\\t\\n')")
        code, reports = self._start_with_worker(script)
        self.assertEqual(code, 0)
        self.assertEqual(reports, [("ok", "step one")])

    def test_background_reports_failure_when_worker_dies(self):
        script = ("import os, sys; fd = int(sys.argv[sys.argv.index('--report-fd') + 1]); "
                  "os.write(fd, b'fail\\tboom\\n'); sys.exit(1)")
        code, reports = self._start_with_worker(script)
        self.assertEqual(code, 1)
        self.assertEqual(reports[0], ("fail", "boom"))
        self.assertIn("didn't start", reports[-1][1])


if __name__ == "__main__":
    unittest.main()
