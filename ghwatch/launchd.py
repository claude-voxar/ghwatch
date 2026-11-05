"""Run the poller as a launchd LaunchAgent so it survives crashes, reboots
and periods when no session is watching.

The agent runs `ghwatch poll --wait-lock` with KeepAlive: if a watcher has
already started a poller, the agent's process stands by on the lock and
takes over when that poller exits.
"""
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time

from ghwatch import poller

LABEL = "com.github.claude-voxar.ghwatch.poller"
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plist_path(agents_dir=None):
    agents_dir = agents_dir or os.path.expanduser("~/Library/LaunchAgents")
    return os.path.join(agents_dir, f"{LABEL}.plist")


def service():
    return f"gui/{os.getuid()}/{LABEL}"


def build_plist(home, python=sys.executable, pkg_root=PKG_ROOT, gh_path=None):
    gh_path = gh_path or shutil.which("gh")
    path_dirs = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if gh_path:
        # launchd's PATH is minimal; the poller shells out to `gh auth token`.
        path_dirs.insert(0, os.path.dirname(gh_path))
    log = os.path.join(home, "poller.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [python, os.path.join(pkg_root, "bin", "ghwatch"), "poll", "--wait-lock"],
        "EnvironmentVariables": {"GHWATCH_HOME": home, "PATH": ":".join(dict.fromkeys(path_dirs))},
        "WorkingDirectory": pkg_root,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def launchctl(*args, run=subprocess.run):
    return run(["launchctl", *args], capture_output=True, text=True)


def is_installed(agents_dir=None):
    return os.path.exists(plist_path(agents_dir))


def install(home, agents_dir=None, run=subprocess.run):
    """Write the plist and (re)load it. Returns the plist path."""
    path = plist_path(agents_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(home, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(home), fh)
    launchctl("bootout", service(), run=run)  # fine if it wasn't loaded
    r = launchctl("bootstrap", f"gui/{os.getuid()}", path, run=run)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {(r.stderr or r.stdout).strip()}")
    return path


def uninstall(agents_dir=None, run=subprocess.run):
    """Unload and remove the agent. Returns True if it was installed."""
    launchctl("bootout", service(), run=run)
    path = plist_path(agents_dir)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def agent_pid(run=subprocess.run):
    """PID of the agent's current process, or None if not loaded/running."""
    r = launchctl("print", service(), run=run)
    if r.returncode != 0:
        return None
    m = re.search(r"^\s*pid = (\d+)", r.stdout, re.MULTILINE)
    return int(m.group(1)) if m else None


def restart(home, run=subprocess.run, kill=os.kill, sleep=time.sleep, timeout=20.0):
    """Restart onto new code: relaunch the agent, then retire any other poller.

    kickstart -k replaces the agent process (new code). If a watcher-started
    poller holds the lock, it is sent SIGTERM so the fresh agent takes over.
    Returns True once the agent's process holds the lock.
    """
    r = launchctl("kickstart", "-k", service(), run=run)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl kickstart failed: {(r.stderr or r.stdout).strip()}")
    waited = 0.0
    while waited < timeout:
        holder, agent = poller.running_pid(home), agent_pid(run=run)
        if holder is not None and holder == agent:
            return True
        if holder is not None and agent is not None:
            try:
                kill(holder, signal.SIGTERM)
            except ProcessLookupError:
                pass
        sleep(0.5)
        waited += 0.5
    return False
