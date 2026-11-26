"""Windows support. The pure logic runs on every platform; the tests that talk to Windows itself
(Credential Manager, routing table, proxy settings) run on Windows, e.g. GitHub's Windows runners."""
import contextlib
import hashlib
import io
import os
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("POGO_HOME", tempfile.mkdtemp(prefix="pogo-test-"))
os.environ.setdefault("POGO_NO_TELEMETRY", "1")  # never emit a real event from tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pogo import cli, config, helper, system, tun, ui, winproxy  # noqa: E402

WINDOWS = sys.platform == "win32"
ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"


class BypassListTests(unittest.TestCase):
    def test_default_rules_become_wildcards(self):
        entries = winproxy.bypass_string(config.DEFAULT_DIRECT + ["::1", "*"]).split(";")
        for expected in ("localhost", "*.local", "127.*", "10.*", "192.168.*", "169.254.*", "<local>"):
            self.assertIn(expected, entries)
        self.assertEqual([e for e in entries if e.startswith("172.")], [f"172.{n}.*" for n in range(16, 32)])
        self.assertNotIn("*", entries)       # would bypass the proxy for everything
        self.assertNotIn("::1", entries)     # IPv6 entries aren't reliable in WinINet
        self.assertEqual(entries[-1], "<local>")

    def test_domains_include_subdomains(self):
        self.assertEqual(winproxy.bypass_string(["example.com"]), "example.com;*.example.com;<local>")
        self.assertEqual(winproxy.bypass_string(["*.example.com"]), "*.example.com;example.com;<local>")

    def test_ranges(self):
        self.assertEqual(winproxy.bypass_string(["10.1.2.3/32"]), "10.1.2.3;<local>")
        self.assertEqual(winproxy.bypass_string(["10.1.2.0/24"]), "10.1.2.*;<local>")
        self.assertEqual(winproxy.bypass_string(["10.1.2.0/30"]).count("10.1.2."), 4)
        self.assertEqual(winproxy.bypass_string(["0.0.0.0/1"]), "<local>")  # too broad: leave it to pogo's rules
        self.assertEqual(winproxy.bypass_string(["host?.lan"]), "<local>")   # WinINet only understands *

    def test_server_string_never_sets_socks(self):
        # Windows apps read "socks=" as SOCKS4, which pogo doesn't speak
        self.assertEqual(winproxy.server_string("127.0.0.1", 8899), "http=127.0.0.1:8899;https=127.0.0.1:8899")

    def test_first_address_and_points_at(self):
        self.assertEqual(winproxy.first_address("http=a:1;https=b:2"), "b:2")
        self.assertEqual(winproxy.first_address("proxy.campus:3128"), "proxy.campus:3128")
        self.assertTrue(winproxy.points_at({"flags": 3, "server": "http=127.0.0.1:8899"}, "127.0.0.1:8899"))
        self.assertFalse(winproxy.points_at({"flags": 1, "server": "http=127.0.0.1:8899"}, "127.0.0.1:8899"))


class FakeWinApi:
    """Stands in for pogo.winnet (the IP Helper API) so WinNet's logic runs anywhere."""

    def __init__(self):
        self.checked = []

    def primary_route(self):
        return 7, "10.0.0.1"

    def interface_name(self, index):
        return "Wi-Fi"

    def dns_servers(self):
        return ["10.0.0.53"]

    def via_gateway(self, ip):
        return True

    def route_exists(self, prefix, ifindex):
        self.checked.append((prefix, ifindex))
        return True


class WinNetTests(unittest.TestCase):
    def test_routes_go_to_the_non_persistent_store(self):
        add = tun.WinNet.route_cmd("add", "0.0.0.0/1", 12, "198.18.0.1")
        self.assertEqual(add, ["netsh", "interface", "ipv4", "add", "route", "prefix=0.0.0.0/1", "interface=12",
                               "nexthop=198.18.0.1", "metric=1", "store=active"])

    def test_cleanup_only_accepts_well_formed_deletes(self):
        net = tun.WinNet()
        self.assertTrue(net.valid_delete(tun.WinNet.route_cmd("delete", "10.0.0.53/32", 7, "10.0.0.1")))
        self.assertFalse(net.valid_delete(["netsh", "interface", "ipv4", "delete", "route", "prefix=1.2.3.4/32",
                                           "interface=7 & calc"]))
        self.assertFalse(net.valid_delete(["netsh", "advfirewall", "reset"]))

    def test_route_added_checks_the_routing_table(self):
        net, api = tun.WinNet(), FakeWinApi()
        with mock.patch.object(tun.WinNet, "_api", staticmethod(lambda: api)):
            self.assertTrue(net.route_added(tun.WinNet.route_cmd("add", "128.0.0.0/1", 12, "198.18.0.1"), None))
        self.assertEqual(api.checked, [("128.0.0.0/1", 12)])

    def test_routes_that_already_existed_are_never_removed(self):
        api = FakeWinApi()
        cfg = {"upstream": {"host": "10.9.9.9"}, "listen": {"host": "127.0.0.1", "port": 8899}}
        capture = tun.Capture(cfg, lambda k, m: None, system_proxy=False, net=tun.WinNet())
        capture.net.tun_index = 12  # set by wait_device() once the adapter exists
        with mock.patch.object(tun.WinNet, "_api", staticmethod(lambda: api)), \
                mock.patch.object(tun, "_write_state"), mock.patch.object(tun.system, "run") as run:
            add, delete = capture.net.exception_route("168.63.129.16", "14", "10.1.0.1")
            self.assertTrue(capture._route(add, delete))           # already there (FakeWinApi says so)
            add, delete = capture.net.capture_routes()[0]
            self.assertTrue(capture._route(add, delete, adopt=True))  # pogo's own leftover: adopt it
        run.assert_not_called()                                    # nothing was added
        self.assertEqual(len(capture.undo), 1)                     # only the capture route gets removed
        self.assertIn("prefix=0.0.0.0/1", capture.undo[0])

    def test_exception_route_without_gateway_is_on_link(self):
        add, delete = tun.WinNet().exception_route("10.9.9.9", "7", None)
        self.assertIn("nexthop=0.0.0.0", add)
        self.assertTrue(tun.WinNet().valid_delete(delete))

    def test_dry_run_plan(self):
        cfg = {"upstream": {"type": "http", "host": "10.9.9.9", "port": 3128, "username": ""},
               "listen": {"host": "127.0.0.1", "port": 8899}, "rules": [], "default_action": "proxy"}
        out = io.StringIO()
        with mock.patch.object(tun.WinNet, "_api", staticmethod(lambda: FakeWinApi())), \
                mock.patch.object(tun, "find_tun2socks", return_value=r"C:\pogo\bin\tun2socks.exe"), \
                contextlib.redirect_stdout(out):
            self.assertTrue(tun.Capture(cfg, lambda k, m: None, system_proxy=False, dry_run=True,
                                        net=tun.WinNet()).setup())
        plan = out.getvalue()
        self.assertIn("--device tun://pogo --proxy socks5://127.0.0.1:8899", plan)
        self.assertIn("store=active", plan)
        self.assertIn("prefix=10.9.9.9/32 interface=7 nexthop=10.0.0.1", plan)


def _zip(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


class HelperTests(unittest.TestCase):
    def test_pinned_builds_are_complete(self):
        for (os_name, arch), (asset, checksum, member) in helper.TUN2SOCKS_BUILDS.items():
            self.assertRegex(checksum, r"^[0-9a-f]{64}$")
            self.assertEqual(asset, f"tun2socks-{os_name}-{arch}.zip")
            self.assertTrue(member.startswith(f"tun2socks-{os_name}-{arch}"))
            if os_name == "windows":
                self.assertIn(arch, helper.WINTUN_DLL)

    def test_machine_names(self):
        with mock.patch.object(helper.system, "IS_WINDOWS", False), mock.patch.object(helper.system, "IS_MAC", True):
            for raw, arch in (("arm64", "arm64"), ("x86_64", "amd64")):
                with mock.patch.object(helper.platform, "machine", return_value=raw):
                    self.assertEqual(helper.machine(), ("darwin", arch))
        with mock.patch.object(helper.system, "IS_WINDOWS", True), \
                mock.patch.object(helper, "_windows_native_arch", return_value=None):
            for raw, arch in (("AMD64", "amd64"), ("ARM64", "arm64"), ("x86", "386")):
                with mock.patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": raw, "PROCESSOR_ARCHITEW6432": ""}):
                    self.assertEqual(helper.machine(), ("windows", arch))

    def test_a_tampered_download_is_refused(self):
        with mock.patch.object(helper, "machine", return_value=("darwin", "arm64")), \
                mock.patch.object(helper.system, "IS_WINDOWS", False), \
                mock.patch.object(helper, "_download", return_value=_zip("tun2socks-darwin-arm64", b"evil")):
            with self.assertRaises(helper.HelperError) as caught:
                helper.install()
        self.assertIn("checksum", str(caught.exception))

    def test_install_writes_the_verified_binary(self):
        data = _zip("tun2socks-darwin-arm64", b"binary")
        builds = {("darwin", "arm64"): ("tun2socks-darwin-arm64.zip", hashlib.sha256(data).hexdigest(),
                                        "tun2socks-darwin-arm64")}
        with mock.patch.object(helper, "TUN2SOCKS_BUILDS", builds), \
                mock.patch.object(helper, "machine", return_value=("darwin", "arm64")), \
                mock.patch.object(helper.system, "IS_WINDOWS", False), \
                mock.patch.object(helper, "_download", return_value=data), \
                mock.patch.object(helper.time, "sleep"):
            path = helper.install()
        self.assertEqual(path.read_bytes(), b"binary")


class ProgressRelayTests(unittest.TestCase):
    """The Windows admin worker reports progress through a file (UAC can't share pipes)."""

    def test_only_complete_lines_are_read(self):
        path = Path(tempfile.mkdtemp()) / "progress"
        path.write_bytes(b"ok\tone\ninf")
        lines, offset = tun.read_progress(path, 0)
        self.assertEqual((lines, offset), ([("ok", "one")], 7))
        with open(path, "ab") as f:
            f.write(b"o\ttwo\n")
        self.assertEqual(tun.read_progress(path, offset)[0], [("info", "two")])

    def _start(self, script: str):
        reports = []
        fake = lambda *args, **kw: [sys.executable, "-c", script, *args]  # noqa: E731
        with mock.patch.object(tun, "preflight", return_value=None), \
                mock.patch.object(tun.system, "is_admin", return_value=True), \
                mock.patch.object(tun, "self_command", fake):
            code = tun.start_elevated({}, lambda kind, msg: reports.append((kind, msg)))
        return code, reports

    SCRIPT = ("import sys; path = sys.argv[sys.argv.index('--report-file') + 1]; "
              "open(path, 'a', encoding='utf-8').write({lines!r}); sys.exit({code})")

    def test_relays_until_ready(self):
        code, reports = self._start(self.SCRIPT.format(lines="ok\tadapter created\nready\t\n", code=0))
        self.assertEqual(code, 0)
        self.assertEqual(reports, [("ok", "adapter created")])

    def test_reports_failure_when_the_worker_dies(self):
        code, reports = self._start(self.SCRIPT.format(lines="fail\tboom\n", code=1))
        self.assertEqual(code, 1)
        self.assertEqual(reports[0], ("fail", "boom"))
        self.assertIn("didn't start", reports[-1][1])


class ShellAndConsoleTests(unittest.TestCase):
    def setUp(self):
        config.CONFIG_FILE.unlink(missing_ok=True)

    def _env(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO("")):
            with self.assertRaises(SystemExit):
                cli.main(["env", *args])
        return out.getvalue().splitlines()

    def test_env_syntaxes(self):
        self.assertTrue(all(line.startswith("$env:") for line in self._env("--shell", "powershell")))
        self.assertTrue(all(line.startswith("set ") for line in self._env("--shell", "cmd")))
        self.assertTrue(all(line.startswith("export ") for line in self._env("--shell", "sh")))
        self.assertTrue(all(line.startswith("Remove-Item Env:") for line in self._env("--unset", "--shell", "powershell")))

    def test_classic_console_symbols(self):
        out = io.StringIO()
        ui._LegacyConsole(out).write("✔ ● ↳ ← ok")
        self.assertEqual(out.getvalue(), "√ * -> <- ok")


class SystemTests(unittest.TestCase):
    def test_process_helpers(self):
        self.assertTrue(system.pid_alive(os.getpid()))
        self.assertFalse(system.pid_alive(2 ** 22 + 12345))
        self.assertIn("python", system.process_command(os.getpid()).lower())
        self.assertTrue(any("python" in name for name in system.process_names()))

    def test_process_start_time(self):
        now = time.time()
        started = system.process_started(os.getpid())
        self.assertIsNotNone(started)
        self.assertTrue(now - 3600 < started <= now + 1, started)
        self.assertIsNone(system.process_started(2 ** 22 + 12345))
        self.assertTrue(system.same_process(os.getpid(), now))
        self.assertTrue(system.same_process(os.getpid(), None))
        self.assertFalse(system.same_process(os.getpid(), started - 60))  # PID reused since
        self.assertFalse(system.same_process(2 ** 22 + 12345, now))

    def test_replace_and_remove(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "a").write_text("1")
        system.replace_file(folder / "a", folder / "b")
        self.assertEqual((folder / "b").read_text(), "1")
        system.remove_file(folder / "b")
        system.remove_file(folder / "b")  # already gone: fine
        self.assertFalse((folder / "b").exists())


@unittest.skipUnless(WINDOWS, "talks to Windows")
class WindowsLiveTests(unittest.TestCase):
    def test_credential_manager_round_trip(self):
        from pogo import wincred
        account = f"test-{uuid.uuid4().hex}@example.invalid:1"
        self.addCleanup(wincred.delete, account)
        self.assertEqual(wincred.read(account), "")
        wincred.write(account, "student", "pässwörd ✔")
        self.assertEqual(wincred.read(account), "pässwörd ✔")
        wincred.delete(account)
        self.assertEqual(wincred.read(account), "")

    def test_routing_table_and_adapters(self):
        from pogo import winnet
        self.assertTrue(winnet.routes())
        index, _ = winnet.primary_route()
        self.assertTrue(index)
        self.assertTrue(any(a.up for a in winnet.adapters()))
        self.assertIsInstance(winnet.dns_servers(), list)
        self.assertTrue(winnet.route_exists("127.0.0.0/8") or winnet.best_route("127.0.0.1"))

    def test_proxy_settings_can_be_read(self):
        settings = winproxy.query()
        self.assertEqual(set(settings), {"flags", "server", "bypass", "autoconfig"})

    @unittest.skipUnless(ON_CI, "changes this computer's proxy settings")
    def test_proxy_settings_round_trip(self):
        before = winproxy.query()
        self.assertEqual(winproxy.enable("127.0.0.1", 8899, config.DEFAULT_DIRECT), [])
        try:
            self.assertEqual(winproxy.effective(), (True, "127.0.0.1:8899"))
            self.assertIn("10.*", winproxy.query()["bypass"])
        finally:
            self.assertEqual(winproxy.disable(match="127.0.0.1:8899"), [])
        after = winproxy.query()
        self.assertEqual((after["flags"], after["server"]), (before["flags"], before["server"]))


if __name__ == "__main__":
    unittest.main()
