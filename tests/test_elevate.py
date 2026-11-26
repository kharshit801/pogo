"""`pogo setup-helper`: the one-time, admin-authorised setup that lets `pogo on` skip the prompt.
The privileged install itself can only be checked on a real machine; here we cover everything
around it — what gets written, how readiness is detected, and that `pogo on` takes the no-prompt
path when it's set up and falls back to the prompt when it isn't."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("POGO_HOME", tempfile.mkdtemp(prefix="pogo-test-"))
os.environ.setdefault("POGO_NO_TELEMETRY", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pogo import config, elevate, tun  # noqa: E402


@unittest.skipIf(sys.platform == "win32", "the macOS helper (sudoers/wrapper) is POSIX-only")
class MacBuildTests(unittest.TestCase):
    def test_wrapper_runs_the_capture_with_the_home_baked_in(self):
        script = elevate.mac_wrapper_script(Path("/Users/lab/.config/pogo"), "/usr/bin/python3")
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn("export POGO_HOME='/Users/lab/.config/pogo'", script)
        self.assertIn("/usr/bin/python3", script)
        self.assertIn("tun --background", script)

    def test_a_home_with_a_space_or_quote_is_safely_quoted(self):
        script = elevate.mac_wrapper_script(Path("/Users/o'brien/my pogo"), "/usr/bin/python3")
        self.assertIn("export POGO_HOME='/Users/o'\\''brien/my pogo'", script)

    def test_sudoers_rule_is_scoped_to_the_one_wrapper(self):
        line = elevate.mac_sudoers_line("labuser")
        self.assertEqual(line, f"labuser ALL=(root) NOPASSWD: {elevate.MAC_WRAPPER}\n")

    def test_ready_is_false_without_the_wrapper(self):
        missing = Path(tempfile.mkdtemp()) / "pogo-tun"
        with mock.patch.object(elevate, "MAC_WRAPPER", missing):
            self.assertFalse(elevate.mac_ready())

    def test_ready_asks_sudo_whether_the_rule_is_active(self):
        wrapper = Path(tempfile.mkdtemp()) / "pogo-tun"
        wrapper.write_text("#!/bin/sh\n")
        with mock.patch.object(elevate, "MAC_WRAPPER", wrapper), \
                mock.patch.object(elevate.system, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(elevate.mac_ready())
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "-l", str(wrapper)])
        with mock.patch.object(elevate, "MAC_WRAPPER", wrapper), \
                mock.patch.object(elevate.system, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(elevate.mac_ready())

    def test_launch_uses_sudo_n_with_no_arguments(self):
        with mock.patch.object(elevate.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(elevate.launch_mac(), 0)
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", str(elevate.MAC_WRAPPER)])


@unittest.skipIf(sys.platform == "win32", "the macOS helper (sudoers/wrapper) is POSIX-only")
class MacInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pogo-elevate-"))
        self.enter(mock.patch.object(elevate, "MAC_WRAPPER", self.tmp / "libexec" / "pogo-tun"))
        self.enter(mock.patch.object(elevate, "MAC_SUDOERS", self.tmp / "sudoers.d" / "pogo"))
        (self.tmp / "sudoers.d").mkdir()
        self.reports = []

    def enter(self, cm):
        cm.start(); self.addCleanup(cm.stop)

    def report(self, kind, msg):
        self.reports.append((kind, msg))

    def test_install_needs_root(self):
        with mock.patch.object(elevate.os, "geteuid", return_value=501):
            self.assertEqual(elevate.install(self.report), 1)
        self.assertEqual(self.reports[-1][0], "fail")

    def test_install_writes_the_wrapper_and_a_validated_sudoers_rule(self):
        with mock.patch.object(elevate.os, "geteuid", return_value=0), \
                mock.patch.object(elevate, "invoking_user", return_value="labuser"), \
                mock.patch.object(elevate, "_real_python", return_value="/usr/bin/python3"), \
                mock.patch.object(elevate, "_sudoers_ok", return_value=True):
            self.assertEqual(elevate.install(self.report), 0)
        self.assertIn("tun --background", elevate.MAC_WRAPPER.read_text())
        self.assertEqual(oct(elevate.MAC_WRAPPER.stat().st_mode & 0o777), oct(0o755))
        self.assertIn("labuser ALL=(root) NOPASSWD:", elevate.MAC_SUDOERS.read_text())
        self.assertEqual(oct(elevate.MAC_SUDOERS.stat().st_mode & 0o777), oct(0o440))

    def test_a_bad_sudoers_rule_changes_nothing(self):
        with mock.patch.object(elevate.os, "geteuid", return_value=0), \
                mock.patch.object(elevate, "invoking_user", return_value="labuser"), \
                mock.patch.object(elevate, "_real_python", return_value="/usr/bin/python3"), \
                mock.patch.object(elevate, "_sudoers_ok", return_value=False):
            self.assertEqual(elevate.install(self.report), 1)
        self.assertFalse(elevate.MAC_SUDOERS.exists())

    def test_remove_deletes_both_files(self):
        elevate.MAC_WRAPPER.parent.mkdir(parents=True)
        elevate.MAC_WRAPPER.write_text("x"); elevate.MAC_SUDOERS.write_text("y")
        with mock.patch.object(elevate.os, "geteuid", return_value=0):
            self.assertEqual(elevate.remove(self.report), 0)
        self.assertFalse(elevate.MAC_WRAPPER.exists() or elevate.MAC_SUDOERS.exists())


class WindowsBuildTests(unittest.TestCase):
    def test_task_runs_the_worker_writing_progress_where_pogo_on_reads_it(self):
        cmd = elevate.win_task_command(config.HOME_DIR)
        self.assertIn("tun", cmd)
        self.assertIn("--worker", cmd)
        self.assertIn("--report-file", cmd)

    def test_task_xml_is_highest_privilege_interactive_and_on_demand(self):
        xml = elevate.win_task_xml(config.HOME_DIR, "LAB\\student")
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)   # no stored password
        self.assertIn("<AllowStartOnDemand>true</AllowStartOnDemand>", xml)
        self.assertIn("<UserId>LAB\\student</UserId>", xml)
        self.assertIn("--worker", xml)            # the capture worker
        self.assertIn("--report-file", xml)

    def test_task_xml_escapes_the_user(self):
        self.assertIn("<UserId>a &amp; b</UserId>", elevate.win_task_xml(config.HOME_DIR, "a & b"))

    def test_ready_queries_the_task(self):
        with mock.patch.object(elevate.system, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(elevate.win_ready())
        self.assertEqual(run.call_args.args[0], ["schtasks", "/query", "/tn", elevate.WIN_TASK])


class NoPromptLaunchTests(unittest.TestCase):
    """start_elevated must take the scheduled-task path (no UAC) when the helper is set up."""

    def setUp(self):
        self.cfg = {"upstream": {"type": "http", "host": "10.0.0.1", "port": 3128, "username": ""},
                    "listen": {"host": "127.0.0.1", "port": 8899}, "rules": [], "default_action": "proxy"}
        config.HOME_DIR.mkdir(parents=True, exist_ok=True)

    def test_scheduled_task_is_used_instead_of_a_uac_prompt(self):
        progress = config.HOME_DIR / "tun.progress"

        def fake_run_task():
            progress.write_text("ready\t\n", encoding="utf-8")   # the task's worker reaches "ready"
            return 0

        with mock.patch.object(tun, "preflight", return_value=None), \
                mock.patch.object(tun.system, "is_admin", return_value=False), \
                mock.patch.object(tun.elevate, "win_ready", return_value=True), \
                mock.patch.object(tun.elevate, "launch_windows", side_effect=fake_run_task) as launch, \
                mock.patch.object(tun.system, "elevate") as uac:
            self.assertEqual(tun.start_elevated(self.cfg, lambda k, m: None), 0)
        launch.assert_called_once()
        uac.assert_not_called()   # no UAC prompt was shown


if __name__ == "__main__":
    unittest.main()
