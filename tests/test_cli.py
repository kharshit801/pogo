"""CLI behaviour: overview screen, help, typos, rules, import, test, daemon start/stop.
Never touches real network settings or the Keychain."""
import base64
import contextlib
import io
import os
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("POGO_HOME", tempfile.mkdtemp(prefix="pogo-test-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pogo import cli, config, logo, proxifier_import  # noqa: E402

PROFILE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ProxifierProfile version="101" platform="MacOSX" product_id="2" product_minver="200">
  <Options><Resolve><AutoModeDetection enabled="true"/><ViaProxy enabled="false"/></Resolve></Options>
  <ProxyList>
    <Proxy id="100" type="HTTPS"><Address>10.9.8.7</Address><Port>3128</Port><Options>0</Options>
      <Authentication enabled="true"><Username>student</Username><Password>ENCRYPTED</Password></Authentication>
    </Proxy>
    <Proxy id="101" type="SOCKS5"><Address>10.9.8.8</Address><Port>1080</Port><Options>0</Options></Proxy>
  </ProxyList>
  <ChainList/>
  <RuleList>
    <Rule enabled="true"><Name>Docker</Name><Applications>Docker; "Code Helper (Plugin)"</Applications><Action type="Proxy">100</Action></Rule>
    <Rule enabled="true"><Name>Notes</Name><Applications>Notes</Applications><Action type="Direct"/></Rule>
    <Rule enabled="true"><Name>Notes again</Name><Applications>Notes</Applications><Action type="Proxy">100</Action></Rule>
    <Rule enabled="true"><Name>Localhost</Name><Targets>localhost; 127.0.0.1; ::1; %ComputerName%</Targets><Action type="Direct"/></Rule>
    <Rule enabled="true"><Name>Campus</Name><Targets>*.college.edu; 10.1.1.0-10.1.1.255</Targets><Action type="Direct"/></Rule>
    <Rule enabled="true"><Name>Ads</Name><Targets>ads.example.com</Targets><Action type="Block"/></Rule>
    <Rule enabled="true"><Name>Self</Name><Targets>10.9.8.7</Targets><Action type="Proxy">100</Action></Rule>
    <Rule enabled="true"><Name>SSH</Name><Targets>git.example.com</Targets><Ports>22</Ports><Action type="Direct"/></Rule>
    <Rule enabled="false"><Name>Off</Name><Targets>off.example.com</Targets><Action type="Block"/></Rule>
    <Rule enabled="true"><Name>Default</Name><Action type="Proxy">100</Action></Rule>
  </RuleList>
</ProxifierProfile>
"""


def run_cli(*argv, stdin: str = ""):
    """Run pogo in-process; returns (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch("sys.stdin", io.StringIO(stdin)):
        try:
            cli.main(list(argv))
            code = 0
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue(), err.getvalue()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        for f in (config.CONFIG_FILE, config.PID_FILE, config.TUN_FILE):
            f.unlink(missing_ok=True)
        patches = [mock.patch.object(config, "set_password"), mock.patch.object(config, "delete_password"),
                   mock.patch.dict(os.environ, {"POGO_PASSWORD": "secret"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def configure(self, **upstream):
        cfg = config.load()
        cfg["upstream"].update({"type": "http", "host": "10.9.8.7", "port": 3128, "username": "student"}, **upstream)
        config.save(cfg)
        return cfg


class OverviewTests(Base):
    def test_plain_pogo_shows_every_command_and_getting_started(self):
        code, out, _ = run_cli()
        self.assertEqual(code, 0)
        for text in ("Get started", "Everyday", "pogo import", "rules add", "doctor", "reset",
                     "pogo help <command>", "not set up yet"):
            self.assertIn(text, out)

    def home_screen(self, terminal: bool, columns: int = 80) -> str:
        with mock.patch.object(cli.ui, "is_terminal", return_value=terminal), \
                mock.patch.object(cli.shutil, "get_terminal_size", return_value=os.terminal_size((columns, 24))):
            return run_cli()[1]

    def test_home_screen_shows_the_logo_in_a_terminal(self):
        out = self.home_screen(terminal=True)
        for line in logo.LOGO:
            self.assertIn(line, out)
        beside_logo = [line for line in out.splitlines() if logo.LOGO[3] in line or logo.LOGO[4] in line]
        self.assertIn("pogo", beside_logo[0])
        self.assertIn("route every app", beside_logo[1])

    def test_home_screen_skips_the_logo_when_piped_or_narrow(self):
        for out in (self.home_screen(terminal=False), self.home_screen(terminal=True, columns=60)):
            self.assertNotIn("█", out)
            self.assertIn("route every app", out.splitlines()[0])

    def test_overview_shows_off_status_once_configured(self):
        self.configure()
        _, out, _ = run_cli()
        self.assertIn("OFF", out)
        self.assertNotIn("Get started", out)

    def test_typo_suggests_the_right_command(self):
        code, _, err = run_cli("strat")
        self.assertEqual(code, 2)
        self.assertIn("did you mean:  pogo start", err)

    def test_help_for_a_command_has_examples(self):
        code, out, _ = run_cli("help", "on")
        self.assertEqual(code, 0)
        self.assertIn("--basic", out)
        self.assertIn("examples:", out)

    def test_help_for_a_subcommand(self):
        _, out, _ = run_cli("help", "rules", "add")
        self.assertIn("usage: pogo rules add", out)

    def test_missing_argument_prints_error_and_help(self):
        code, _, err = run_cli("rules", "add")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertIn("examples:", err)


class RulesTests(Base):
    def test_add_check_remove(self):
        self.configure()
        run_cli("rules", "add", "https://www.youtube.com/watch?v=1", "block")
        _, out, _ = run_cli("rules", "check", "www.youtube.com")
        self.assertIn("block", out)
        run_cli("rules", "rm", "www.youtube.com")          # alias + pattern
        _, out, _ = run_cli("rules", "check", "www.youtube.com")
        self.assertIn("default", out)

    def test_shadowed_rule_warns_and_suggests_position(self):
        self.configure()
        run_cli("rules", "add", "example.com", "direct")
        _, out, _ = run_cli("rules", "add", "www.example.com", "block")
        self.assertIn("comes first", out)
        self.assertIn("--at", out)

    def test_remove_unknown_number_fails(self):
        self.configure()
        code, _, err = run_cli("rules", "remove", "99")
        self.assertEqual(code, 1)
        self.assertIn("there's no rule 99", err)

    def test_list_is_the_default_subcommand(self):
        self.configure()
        _, out, _ = run_cli("rules")
        self.assertIn("everything else (default)", out)


class ImportTests(Base):
    def setUp(self):
        super().setUp()
        self.profile = Path(tempfile.mkdtemp()) / "campus.ppx"
        self.profile.write_text(PROFILE)

    def test_convert(self):
        result = proxifier_import.convert(self.profile)
        self.assertEqual(result.upstream, {"type": "http", "host": "10.9.8.7", "port": 3128, "username": "student"})
        self.assertEqual(result.default_action, "proxy")
        patterns = {(r["pattern"], r["action"]) for r in result.rules}
        self.assertEqual(patterns, {("localhost", "direct"), ("127.0.0.1", "direct"), ("::1", "direct"),
                                    ("*.college.edu", "direct"), ("10.1.1.0/24", "direct"),
                                    ("ads.example.com", "block")})
        self.assertEqual(result.skipped_apps, [("Docker", "proxy"), ("Code Helper (Plugin)", "proxy"),
                                               ("Notes", "direct")])
        notes = " ".join(result.notes)
        self.assertIn("ports", notes)
        self.assertIn("proxy server itself", notes)
        self.assertIn("2 proxies", notes)

    def test_import_command_writes_config(self):
        code, out, _ = run_cli("import", str(self.profile), "--yes")
        self.assertEqual(code, 0, out)
        cfg = config.load()
        self.assertEqual(cfg["upstream"]["host"], "10.9.8.7")
        self.assertEqual(cfg["rules"][0], {"pattern": "localhost", "action": "direct"})
        self.assertIn({"pattern": "192.168.0.0/16", "action": "direct"}, cfg["rules"])  # defaults kept
        self.assertIn("per-app rules can't be copied", out)


GOOD_AUTH = "Basic " + base64.b64encode(b"student:secret").decode()


class FakeProxy(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        if f"Proxy-Authorization: {GOOD_AUTH}".encode() in data:
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")


class LiveTests(Base):
    def setUp(self):
        super().setUp()
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeProxy)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.configure(host="127.0.0.1", port=self.server.server_address[1])

    def test_test_command_ok(self):
        code, out, _ = run_cli("test")
        self.assertEqual(code, 0, out)
        self.assertIn("logged in", out)

    def test_test_command_wrong_password(self):
        with mock.patch.dict(os.environ, {"POGO_PASSWORD": "wrong"}):
            code, out, _ = run_cli("test")
        self.assertEqual(code, 1)
        self.assertIn("rejected your username or password", out)
        self.assertIn("pogo setup", out)

    def test_test_command_explains_unsupported_login_method(self):
        class NtlmOnly(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                     b"Proxy-Authenticate: NTLM\r\nProxy-Authenticate: Negotiate\r\n\r\n")
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), NtlmOnly)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.configure(host="127.0.0.1", port=server.server_address[1])
        code, out, _ = run_cli("test")
        self.assertEqual(code, 1)
        self.assertIn("NTLM/NEGOTIATE login", out)

    def test_daemon_start_status_stop(self):
        cfg = config.load()
        cfg["listen"]["port"] = free_port()
        config.save(cfg)
        code, out, _ = run_cli("start", "-d")
        self.addCleanup(cli._stop_daemon)
        self.assertEqual(code, 0, out)
        self.assertTrue(config.running_pid())
        _, out, _ = run_cli("status")
        self.assertIn("running on 127.0.0.1", out)
        code, out, _ = run_cli("stop")
        self.assertEqual(code, 0)
        self.assertIsNone(config.running_pid())


if __name__ == "__main__":
    unittest.main()
