"""Isolate tests from the developer's real config, data dir and browser (set before deja_browser is imported).

- Config and data dir live in a fresh temp dir whose name starts with TEST_PREFIX; the config sets
  [browser] port to a random free port, never 9222, so a test can never touch the user's agent browser.
- Unit tests must not start a real browser: any Popen that looks like a browser launch is refused and
  the test fails at teardown (the code under test may swallow the error, so raising alone is not enough).
- Session end (and interpreter exit, e.g. after Ctrl-C) kills any browser still running on a profile
  under the temp dir and removes the dir, so a run can not leave an orphan holding a port.
"""
import atexit
import os
import shutil
import socket
import subprocess
import tempfile

import pytest

TEST_PREFIX = "deja-browser-test-"   # browser.py recognises leftovers by this prefix (TEST_PROFILE_PREFIX)


def _free_port():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != 9222:
            return port


_TMP = tempfile.mkdtemp(prefix=TEST_PREFIX)
TEST_PORT = _free_port()
_CFG = os.path.join(_TMP, "config.toml")
with open(_CFG, "w", encoding="utf-8") as f:
    f.write(f"[browser]\nport = {TEST_PORT}\n")
os.environ["DEJA_BROWSER_CONFIG"] = _CFG
os.environ["DEJA_BROWSER_HOME"] = os.path.join(_TMP, "data")

# ---------- no real browser from a unit test ----------

LAUNCHES = []
_RealPopen = subprocess.Popen


def _looks_like_browser(args):
    s = " ".join(map(str, args)) if isinstance(args, (list, tuple)) else str(args)
    return "--remote-debugging-port" in s or "--user-data-dir" in s


class _GuardedPopen(_RealPopen):
    def __init__(self, args, *a, **kw):
        if _looks_like_browser(args):
            LAUNCHES.append(args)
            raise OSError("tests/conftest.py: a unit test tried to start a real browser")
        super().__init__(args, *a, **kw)


subprocess.Popen = _GuardedPopen


@pytest.fixture(autouse=True)
def _no_real_browser():
    before = len(LAUNCHES)
    yield
    new = LAUNCHES[before:]
    if new:
        pytest.fail(f"test tried to start a real browser (mock the tier / launch): {new[0]}", pytrace=False)


# ---------- teardown: never leave a browser behind ----------

def _browsers_under(root):
    """PIDs of processes whose command line names `root` (the test temp dir)."""
    needle = os.path.normcase(root)
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process -Filter \"CommandLine like '%" + TEST_PREFIX + "%'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    else:
        out = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True, text=True).stdout
        out = "\n".join("\t".join(line.strip().split(None, 1)) for line in out.splitlines() if line.strip())
    pids = []
    for line in out.splitlines():
        pid, _, cl = line.partition("\t")
        if needle in os.path.normcase(cl) and pid.strip().isdigit() and int(pid) != os.getpid():
            pids.append(int(pid))
    return pids


_cleaned = False


def _cleanup():
    global _cleaned
    if _cleaned:
        return
    _cleaned = True
    try:
        for pid in _browsers_under(_TMP):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
    except Exception:
        pass
    shutil.rmtree(_TMP, ignore_errors=True)


atexit.register(_cleanup)


def pytest_sessionfinish(session, exitstatus):
    _cleanup()
