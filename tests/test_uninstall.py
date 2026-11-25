"""`pogo uninstall`: what it removes, what it must never remove, and the command itself. Every
test works in a temporary folder; nothing touches the real installation, PATH or Keychain."""
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("POGO_HOME", tempfile.mkdtemp(prefix="pogo-test-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pogo import cli, uninstall  # noqa: E402

WINDOWS = sys.platform == "win32"


def fake_pogo(folder: Path) -> Path:
    """A folder that looks like a copy of pogo's program."""
    (folder / "pogo").mkdir(parents=True)
    (folder / "pogo" / "cli.py").write_text("# pogo\n")
    (folder / "bin").mkdir()
    (folder / "bin" / "pogo").write_text("#!/usr/bin/env python3\n")
    return folder


def symlink_or_skip(test: unittest.TestCase, link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        test.skipTest("this account can't create symlinks")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pogo-uninstall-"))
        self.home = self.root / "pogo-home"
        self.home.mkdir()
        (self.home / "config.json").write_text("{}")

    def mac(self, running: Path, installed: Path = None, command: Path = None):
        where = uninstall.Locations(self.home, running, installed or self.root / "share" / "pogo",
                                    command or self.root / "bin" / "pogo")
        return uninstall.plan(where, windows=False)

    def deleted(self, plan):
        return [path for path, _ in plan.delete]

    def test_installed_copy_its_command_and_settings_go(self):
        installed = fake_pogo(self.root / "share" / "pogo")
        (self.root / "bin").mkdir()
        symlink_or_skip(self, self.root / "bin" / "pogo", installed / "bin" / "pogo")
        plan = self.mac(running=installed, installed=installed)
        self.assertEqual(self.deleted(plan), [self.root / "bin" / "pogo", installed, self.home])
        self.assertEqual(plan.keep, [])

    def test_a_source_folder_stays_and_only_its_link_goes(self):
        source = fake_pogo(self.root / "my-checkout")
        (self.root / "bin").mkdir()
        symlink_or_skip(self, self.root / "bin" / "pogo", source / "bin" / "pogo")
        plan = self.mac(running=source)
        self.assertEqual(self.deleted(plan), [self.root / "bin" / "pogo", self.home])
        self.assertIn("my-checkout stays", plan.keep[0])

    def test_a_command_that_isnt_pogo_stays(self):
        (self.root / "bin").mkdir()
        other = self.root / "something-else"
        other.write_text("")
        symlink_or_skip(self, self.root / "bin" / "pogo", other)
        plan = self.mac(running=fake_pogo(self.root / "src"))
        self.assertNotIn(self.root / "bin" / "pogo", self.deleted(plan))
        self.assertTrue(any("isn't a link to pogo" in note for note in plan.keep))

    def test_pip_installs_are_left_to_pip(self):
        site = self.root / "lib" / "site-packages"
        site.mkdir(parents=True)
        plan = self.mac(running=site)
        self.assertIn("pip uninstall pogo-cli", " ".join(plan.keep))

    def test_a_shared_settings_folder_is_only_tidied(self):
        shared = self.root / "Documents"
        shared.mkdir()
        where = uninstall.Locations(shared, fake_pogo(self.root / "src"), self.root / "none", self.root / "none" / "pogo")
        plan = uninstall.plan(where, windows=False)
        self.assertEqual(plan.tidy, [shared])
        self.assertNotIn(shared, self.deleted(plan))

    def test_never_deletes_a_folder_holding_the_users_home(self):
        (self.root / "pogo-user" / "me").mkdir(parents=True)  # POGO_HOME set to a parent of the home folder
        with mock.patch.object(uninstall.config, "user_home", return_value=self.root / "pogo-user" / "me"):
            where = uninstall.Locations(self.root / "pogo-user", fake_pogo(self.root / "src"),
                                        self.root / "none", self.root / "none" / "pogo")
            plan = uninstall.plan(where, windows=False)
        self.assertEqual(self.deleted(plan), [])
        self.assertEqual(plan.tidy, [self.root / "pogo-user"])

    def test_windows_removes_the_whole_pogo_folder_after_pogo_exits(self):
        root = self.root / ".pogo"
        fake_pogo(root / "app")
        (root / "bin").mkdir()
        (root / "bin" / "pogo.cmd").write_text("@echo off\n")
        where = uninstall.Locations(root, root / "app", root / "app", root / "bin" / "pogo.cmd")
        plan = uninstall.plan(where, windows=True)
        self.assertEqual(self.deleted(plan), [root])
        self.assertEqual(plan.later, root)
        self.assertEqual(plan.keep, [])


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pogo-uninstall-"))
        self.reports = []

    def report(self, kind, msg):
        self.reports.append((kind, msg))

    def test_folders_and_files_are_deleted(self):
        folder = self.root / "pogo-home"
        (folder / "bin").mkdir(parents=True)
        (folder / "bin" / "tun2socks").write_text("x")
        os.chmod(folder / "bin" / "tun2socks", 0o444)  # read-only, like downloaded helpers can be
        single = self.root / "pogo-link"
        single.write_text("x")
        plan = uninstall.Plan(delete=[(folder, "settings"), (single, "the pogo command")])
        self.assertEqual(uninstall.remove(plan, self.report), 0)
        self.assertFalse(folder.exists() or single.exists())
        self.assertEqual([kind for kind, _ in self.reports], ["ok", "ok"])

    def test_a_shared_folder_keeps_everything_that_isnt_pogos(self):
        shared = self.root / "Documents"
        (shared / "bin").mkdir(parents=True)
        for name in ("config.json", "pogo.log", "tun.log.1", "notes.txt"):
            (shared / name).write_text("x")
        (shared / "bin" / "tun2socks").write_text("x")
        uninstall.remove(uninstall.Plan(tidy=[shared]), self.report)
        self.assertEqual(sorted(p.name for p in shared.iterdir()), ["notes.txt"])

    def test_emptying_a_folder_spares_the_running_command(self):
        root = self.root / ".pogo"
        (root / "app" / "pogo").mkdir(parents=True)
        (root / "app" / "pogo" / "cli.py").write_text("x")
        (root / "bin").mkdir()
        (root / "bin" / "pogo.cmd").write_text("x")
        (root / "bin" / "wintun.dll").write_text("x")
        (root / "config.json").write_text("{}")
        uninstall._empty_except(root, root / "bin" / "pogo.cmd")
        self.assertEqual([p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))], ["bin", "bin/pogo.cmd"])


class PathEntryTests(unittest.TestCase):
    def test_both_spellings_of_pogos_folder_are_removed_and_nothing_else(self):
        home = r"C:\Users\Asha"
        value = r'C:\Tools;%USERPROFILE%\.pogo\bin;C:\Users\asha\.pogo\bin\;"C:\Program Files\Git\cmd";%USERPROFILE%\.pogo'
        self.assertEqual(uninstall.without_pogo(value, home),
                         r'C:\Tools;"C:\Program Files\Git\cmd";%USERPROFILE%\.pogo')

    def test_a_path_without_pogo_is_unchanged(self):
        value = r"C:\Tools;;C:\Python312\Scripts"
        self.assertEqual(uninstall.without_pogo(value, r"C:\Users\Asha"), value)


@unittest.skipUnless(WINDOWS, "Windows registry and cmd.exe")
class WindowsUninstallTests(unittest.TestCase):
    def test_path_entry_is_removed_and_stays_expandable(self):
        import winreg
        subkey = rf"Software\pogo-test-{uuid.uuid4().hex}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, r"C:\Tools;%USERPROFILE%\.pogo\bin;C:\Other")
        self.addCleanup(winreg.DeleteKey, winreg.HKEY_CURRENT_USER, subkey)
        self.assertTrue(uninstall.remove_path_entry(subkey))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            self.assertEqual(winreg.QueryValueEx(key, "Path"), (r"C:\Tools;C:\Other", winreg.REG_EXPAND_SZ))
        self.assertFalse(uninstall.remove_path_entry(subkey))  # nothing left to remove

    def test_the_folder_holding_the_running_command_goes_a_moment_later(self):
        root = Path(tempfile.mkdtemp(prefix="pogo-later-")) / ".pogo"
        fake_pogo(root / "app")
        (root / "bin").mkdir()
        (root / "bin" / "pogo.cmd").write_text("@echo off\n")
        plan = uninstall.Plan(delete=[(root, "everything")], later=root)
        self.assertEqual(uninstall.remove(plan, lambda kind, msg: None), 0)
        self.assertEqual(sorted(p.name for p in root.rglob("*")), ["bin", "pogo.cmd"])
        for _ in range(100):
            if not root.exists():
                break
            time.sleep(0.2)
        self.assertFalse(root.exists())


class UninstallCommandTests(unittest.TestCase):
    """The whole command, run in-process against a temporary layout."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pogo-uninstall-"))
        self.home = self.root / "pogo-home"
        (self.home / "bin").mkdir(parents=True)
        (self.home / "config.json").write_text("{}")
        where = uninstall.Locations(self.home, fake_pogo(self.root / "src"), self.root / "none",
                                    self.root / "none" / ("pogo.cmd" if WINDOWS else "pogo"))
        for patch in (mock.patch.object(uninstall, "locations", return_value=where),
                      mock.patch.object(uninstall, "remove_path_entry", return_value=False),
                      mock.patch.object(cli.config, "delete_password", return_value=True),
                      mock.patch("pogo.winproxy._unregister_restore"),
                      mock.patch.object(cli.tun, "leftovers", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def run_uninstall(self, *args, state=None, off=0):
        state = state or {"pid": None, "tunnel": None, "system": False, "system_other": None}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "capture_state", return_value=state), \
                mock.patch.object(cli.sysproxy, "pointing_to", return_value=[]), \
                mock.patch.object(cli, "cmd_off", return_value=off) as cmd_off, \
                mock.patch("sys.stdin", io.StringIO("")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(["uninstall", *args])
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), cmd_off

    def test_asks_first_and_changes_nothing_without_an_answer(self):
        code, out, _ = self.run_uninstall()
        self.assertEqual(code, 1)
        self.assertIn("Nothing changed.", out)
        self.assertTrue(self.home.exists())

    def test_removes_everything_with_yes(self):
        code, out, cmd_off = self.run_uninstall("--yes")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.home.exists())
        self.assertIn("pogo is uninstalled", out)
        self.assertIn("the source folder", out)  # the running copy isn't an installed one
        cmd_off.assert_not_called()  # pogo was already off

    def test_turns_pogo_off_first_and_keeps_everything_if_that_fails(self):
        running = {"pid": 4242, "tunnel": {"pid": 1}, "system": False, "system_other": None}
        code, out, cmd_off = self.run_uninstall("--yes", state=running, off=1)
        self.assertEqual(code, 1)
        cmd_off.assert_called_once()
        self.assertIn("nothing was removed", out)
        self.assertTrue(self.home.exists())


if __name__ == "__main__":
    unittest.main()
