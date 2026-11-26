"""`pogo setup-helper`: a one-time, admin-authorised setup so that afterwards `pogo on` can start
the all-apps capture **without a password or a UAC prompt** — the way Proxifier and VPNs avoid
prompting on every launch.

- macOS: a tiny root-owned wrapper at /usr/local/libexec/pogo-tun, plus a sudoers rule that lets
  your account run just that one wrapper with `sudo` and no password.
- Windows: a Scheduled Task that runs the capture with the highest privileges; `pogo on` triggers
  it, which an on-demand task does without a UAC prompt.

Set up once per PC (it asks for admin that one time). `pogo setup-helper --remove` undoes it.
`pogo on` still works without any of this — it just prompts each time instead.

Security: this is a deliberate, scoped grant of privilege. On a machine where other people have
their own accounts, remember that anyone who can change pogo's files could then run code as admin
without a password. Install pogo somewhere only an administrator can write (or keep it to PCs you
manage), and read SECURITY.md.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import config, system
from .launch import BOOTSTRAP, self_command

# --- macOS ---------------------------------------------------------------------------------
MAC_WRAPPER = Path("/usr/local/libexec/pogo-tun")
MAC_SUDOERS = Path("/etc/sudoers.d/pogo")

# --- Windows -------------------------------------------------------------------------------
WIN_TASK = "pogo-capture"


def invoking_user() -> str:
    """The account to grant the no-prompt capture to: the real user, even when we're elevated."""
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("USERNAME") or ""


# --- macOS build helpers (pure, so they can be tested anywhere) -----------------------------

def mac_wrapper_script(home: Path, python: str) -> str:
    """A fixed root-owned script that runs the capture. Everything that varies is baked in here
    at setup, so the sudoers rule can allow this one exact command and nothing else."""
    return (
        "#!/bin/sh\n"
        "# pogo privileged helper. Installed by `pogo setup-helper`; managed by pogo, don't edit.\n"
        "# Lets `pogo on` start the all-apps capture without a password. Remove: pogo setup-helper --remove\n"
        f"export POGO_HOME={_shell_quote(str(home))}\n"
        f"exec {_shell_quote(python)} -c {_shell_quote(BOOTSTRAP)} tun --background\n"
    )


def mac_sudoers_line(user: str) -> str:
    # Scoped to exactly the wrapper: this account may run that one command as root, no password.
    return f"{user} ALL=(root) NOPASSWD: {MAC_WRAPPER}\n"


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def mac_ready() -> bool:
    """Is the no-password path set up and actually allowed right now?"""
    if not MAC_WRAPPER.exists():
        return False
    # `sudo -n -l <cmd>` asks "may I run this without a password?" without running it.
    result = system.run(["sudo", "-n", "-l", str(MAC_WRAPPER)])
    return result.returncode == 0


def launch_mac() -> int:
    """Start the capture through the wrapper with no password. The wrapper has POGO_HOME baked in
    and takes no arguments, so the sudoers rule can allow exactly this command. It relays progress
    to this terminal just as the sudo path does."""
    return subprocess.run(["sudo", "-n", str(MAC_WRAPPER)]).returncode


# --- Windows build helpers -----------------------------------------------------------------

def win_task_command(home: Path) -> list[str]:
    """The fixed command the scheduled task runs: the capture worker, writing its progress to the
    files `pogo on` reads."""
    progress = config.HOME_DIR / "tun.progress"
    return self_command("tun", "--worker", "--report-file", str(progress), "--log-file",
                        str(config.TUN_LOG), home=str(home), windowless=True)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def win_task_xml(home: Path, user: str) -> str:
    """Task Scheduler XML for the capture. Registered from XML rather than `schtasks /tr` because
    the worker command is far longer than schtasks's 261-character /tr limit.

    - HighestAvailable + InteractiveToken: runs elevated with the signed-in user's token, no stored
      password, and no UAC prompt when triggered on demand.
    - no triggers, AllowStartOnDemand: it only ever runs when `pogo on` starts it.
    - ExecutionTimeLimit PT0S: never time the long-running capture out."""
    cmd = win_task_command(home)
    command, arguments = cmd[0], subprocess.list2cmdline(cmd[1:])
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo><Description>pogo all-apps capture (no prompt). "
        "Created by pogo setup-helper.</Description></RegistrationInfo>\n"
        '  <Principals><Principal id="Author">\n'
        f"    <UserId>{_xml_escape(user)}</UserId>\n"
        "    <LogonType>InteractiveToken</LogonType>\n"
        "    <RunLevel>HighestAvailable</RunLevel>\n"
        "  </Principal></Principals>\n"
        "  <Settings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "  </Settings>\n"
        '  <Actions Context="Author"><Exec>\n'
        f"    <Command>{_xml_escape(command)}</Command>\n"
        f"    <Arguments>{_xml_escape(arguments)}</Arguments>\n"
        "  </Exec></Actions>\n"
        "</Task>\n"
    )


def win_ready() -> bool:
    return system.run(["schtasks", "/query", "/tn", WIN_TASK]).returncode == 0


def launch_windows() -> int:
    return system.run(["schtasks", "/run", "/tn", WIN_TASK]).returncode


# --- install / remove ----------------------------------------------------------------------

def ready() -> bool:
    """Is `pogo on` able to skip the prompt?"""
    return win_ready() if system.IS_WINDOWS else mac_ready()


def install(report) -> int:
    """Set up the no-prompt capture. Must run with admin rights (cmd_setup_helper arranges that)."""
    if system.IS_WINDOWS:
        return _install_windows(report)
    return _install_mac(report)


def remove(report) -> int:
    if system.IS_WINDOWS:
        return _remove_windows(report)
    return _remove_mac(report)


def _install_mac(report) -> int:
    if os.geteuid() != 0:
        report("fail", "setting up the helper needs admin rights")
        return 1
    user = invoking_user()
    if not user or user == "root":
        report("fail", "couldn't tell which account to set up for; run: sudo pogo setup-helper")
        return 1
    home = config.HOME_DIR
    try:
        MAC_WRAPPER.parent.mkdir(parents=True, exist_ok=True)
        _write_root_file(MAC_WRAPPER, mac_wrapper_script(home, _real_python()), 0o755)
        # validate the sudoers snippet before installing it: a bad file can lock everyone out of sudo
        if not _sudoers_ok(mac_sudoers_line(user)):
            report("fail", "the sudoers rule failed validation, so nothing was changed")
            return 1
        _write_root_file(MAC_SUDOERS, mac_sudoers_line(user), 0o440)
    except OSError as e:
        report("fail", f"couldn't install the helper: {e}")
        return 1
    report("ok", f"done. {user} can now run 'pogo on' without a password on this Mac")
    return 0


def _remove_mac(report) -> int:
    if os.geteuid() != 0:
        report("fail", "removing the helper needs admin rights")
        return 1
    for path in (MAC_SUDOERS, MAC_WRAPPER):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            report("fail", f"couldn't remove {path}: {e}")
            return 1
    report("ok", "removed. 'pogo on' will ask for your password again")
    return 0


def _install_windows(report) -> int:
    if not system.is_admin():
        report("fail", "setting up the helper needs admin rights")
        return 1
    import tempfile
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    xml = win_task_xml(config.HOME_DIR, invoking_user())
    fd, path = tempfile.mkstemp(suffix=".xml", dir=str(config.HOME_DIR))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(xml.encode("utf-16"))   # schtasks wants the file encoded to match its header
        result = system.run(["schtasks", "/create", "/tn", WIN_TASK, "/xml", path, "/f"])
    finally:
        system.remove_file(path)
    if result.returncode != 0 or not win_ready():
        report("fail", "couldn't create the scheduled task: " + (result.stdout or result.stderr or "").strip())
        return 1
    report("ok", "done. 'pogo on' will no longer show a permission prompt on this PC")
    return 0


def _remove_windows(report) -> int:
    if not system.is_admin():
        report("fail", "removing the helper needs admin rights")
        return 1
    result = system.run(["schtasks", "/delete", "/tn", WIN_TASK, "/f"])
    if result.returncode != 0 and win_ready():
        report("fail", "couldn't remove the scheduled task: " + (result.stdout or result.stderr or "").strip())
        return 1
    report("ok", "removed. 'pogo on' will show the permission prompt again")
    return 0


# --- small privileged file helpers (macOS) -------------------------------------------------

def _real_python() -> str:
    import sys
    return sys.executable


def _write_root_file(path: Path, content: str, mode: int) -> None:
    """Write a root-owned file atomically (temp file + rename), then set its mode."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pogo-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sudoers_ok(content: str) -> bool:
    """Check a sudoers snippet with `visudo -cf -` before trusting it."""
    try:
        check = subprocess.run(["visudo", "-cf", "-"], input=content, text=True, capture_output=True)
        return check.returncode == 0
    except (OSError, ValueError):
        return False
