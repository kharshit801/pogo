"""All-apps capture plumbing that can be tested without root or touching real routes."""
import contextlib
import io
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
            self.assertEqual(tun.MacNet().primary_route(), ("en6", "10.20.30.1"))

    def test_primary_route_without_gateway(self):
        with mock.patch.object(tun, "_out", return_value="default  link#15  UCS  en6\n"):
            self.assertEqual(tun.MacNet().primary_route(), ("en6", None))

    def test_leftover_capture_routes_are_detected(self):
        with mock.patch.object(tun, "_out", return_value=NETSTAT), \
                mock.patch.object(tun.system, "run", return_value=mock.Mock(returncode=1)):
            self.assertIn("capture routes", tun.MacNet().leftovers())

    def test_cleanup_only_runs_well_formed_route_deletes(self):
        net = tun.MacNet()
        self.assertTrue(net.valid_delete(["route", "-n", "delete", "-host", "10.20.30.40"]))
        for evil in (";rm -rf /", "$(id)", "--", "-flush", "a b"):
            self.assertFalse(net.valid_delete(["route", "-n", "delete", evil]), evil)
        self.assertFalse(net.valid_delete(["rm", "-rf", "/"]))


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
        names = ("SIGINT", "SIGTERM", "SIGHUP")
        saved = {getattr(signal, n): signal.getsignal(getattr(signal, n)) for n in names if hasattr(signal, n)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        # the worker is meant to outlive its launcher, which Python flags as a ResourceWarning
        guard = warnings.catch_warnings()
        guard.__enter__()
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(guard.__exit__, None, None, None)

    def test_worker_reports_progress_then_ready_and_cleans_up(self):
        read_end, write_end = os.pipe()
        with mock.patch.object(tun, "Capture", FakeCapture):
            self.assertEqual(tun.run_worker({}, report_fd=write_end), 0)
        with os.fdopen(read_end) as f:
            self.assertEqual(f.read(), "ok\tvirtual network interface created\nready\t\n")
        self.assertTrue(FakeCapture.torn_down)

    def _start_with_worker(self, script: str):
        reports = []
        fake_cmd = lambda *args, **kw: [sys.executable, "-c", script, *args]  # noqa: E731
        with mock.patch.object(tun, "preflight", return_value=None), \
                mock.patch.object(tun, "self_command", fake_cmd):
            code = tun.start_background({}, lambda kind, msg: reports.append((kind, msg)))
        return code, reports

    @unittest.skipIf(sys.platform == "win32", "the pipe relay is the macOS path")
    def test_background_relays_progress_until_ready(self):
        script = ("import os, sys; fd = int(sys.argv[sys.argv.index('--report-fd') + 1]); "
                  "os.write(fd, b'ok\\tstep one\\nready\\t\\n')")
        code, reports = self._start_with_worker(script)
        self.assertEqual(code, 0)
        self.assertEqual(reports, [("ok", "step one")])

    @unittest.skipIf(sys.platform == "win32", "the pipe relay is the macOS path")
    def test_background_reports_failure_when_worker_dies(self):
        script = ("import os, sys; fd = int(sys.argv[sys.argv.index('--report-fd') + 1]); "
                  "os.write(fd, b'fail\\tboom\\n'); sys.exit(1)")
        code, reports = self._start_with_worker(script)
        self.assertEqual(code, 1)
        self.assertEqual(reports[0], ("fail", "boom"))
        self.assertIn("didn't start", reports[-1][1])


PFLAG_HELP = """Usage of tun2socks:
  -d, --device string              Use this device [driver://]name
      --loglevel string            Log level [debug|info|warn|error|silent] (default "info")
  -p, --proxy string               Use this proxy [protocol://]host[:port]
"""


class Tun2socksCommandTests(unittest.TestCase):
    """tun2socks 2.7 switched to POSIX-style flags: `-loglevel` made it exit at once."""

    def test_loglevel_uses_two_dashes(self):
        with mock.patch.object(tun, "_out", return_value=PFLAG_HELP):
            self.assertEqual(tun._loglevel_args("tun2socks"), ["--loglevel", "error"])

    def test_capture_starts_tun2socks_with_two_dash_flags(self):
        cfg = {"upstream": {"type": "http", "host": "10.9.9.9", "port": 3128, "username": ""},
               "listen": {"host": "127.0.0.1", "port": 8899}, "rules": [], "default_action": "proxy"}
        out = io.StringIO()
        with mock.patch.object(tun.MacNet, "primary_route", return_value=("en0", "10.0.0.1")), \
                mock.patch.object(tun.MacNet, "ifindex", return_value=4), \
                mock.patch.object(tun.MacNet, "dns_servers", return_value=[]), \
                mock.patch.object(tun, "resolve_ipv4", return_value=["10.9.9.9"]), \
                mock.patch.object(tun, "find_tun2socks", return_value="/opt/tun2socks"), \
                contextlib.redirect_stdout(out):
            capture = tun.Capture(cfg, lambda kind, msg: None, system_proxy=False, dry_run=True, net=tun.MacNet())
            self.assertTrue(capture.setup())
        self.assertIn("/opt/tun2socks --device utun233 --proxy socks5://127.0.0.1:8899", out.getvalue())


if __name__ == "__main__":
    unittest.main()
