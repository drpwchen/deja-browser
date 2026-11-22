# -*- coding: utf-8 -*-
"""Kimi-WebBridge-compatible `wb(action, args)` on a dedicated agent browser over CDP.

  Dedicated profile ([browser] profile, default <data dir>/profile), CDP on 127.0.0.1 only, port
  [browser] port (default 9222). If that port is taken by another browser, launch() never hands it out:
  a browser left over by this project's own test suite (profile in a temp deja-browser-test-* dir, its
  test runner gone) is closed; anything else is left alone and the agent browser starts on the first
  free port after it, recorded in <profile>/.deja-cdp-port so later runs find it.
  Never touches the user's everyday browser profile. Any Chromium browser works: [browser]
  executable / env DEJA_BROWSER_BROWSER, else the first of Edge, Chrome, Chromium, Brave found on
  this machine. Started on demand and left running afterwards — close_session only closes this
  session's tabs.

  from deja_browser.browser import wb
  wb("navigate", {"url": "https://example.com"}, session="my-task")   -> {success, url, tabId, title, status}
  wb("evaluate", {"code": "document.title"}, session="my-task")       -> {type, value}
  wb("close_session", {}, session="my-task")

Actions, their args and reply shapes, and the cross-cutting topics (refs, frames, dialogs, popups,
then_snapshot, the CLI daemon): clihelp.py, printed by `deja-cdp help [<action | topic>]`. Add a new
action there too (CliHelpTest checks that every action has an entry).

session -> tab mapping is keyed by CDP targetId in [browser] sessions_file, so a later
process finds the same tab again. One CDP connection per thread (lazy, reused until exit), so a
single-threaded caller (`import edge_cdp`; wb()) has one connection per process, as before.
Driver: playwright (default) or patchright (EDGE_CDP_DRIVER=patchright or use_driver()).
CLI: `deja-cdp <action>` goes through the daemon (daemon.py, client.py) unless DEJA_BROWSER_DAEMON=0;
wb() never does.
"""
import atexit, json, os, shutil, signal, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request
from pathlib import Path

from . import config, driverpipe, guard


def _candidates():
    """Chromium browsers in preference order for this OS (Edge first: the project grew up on it)."""
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chrome.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        return [str(Path(r) / rel) for rel in rels for r in roots if r]
    if sys.platform == "darwin":
        return [f"/Applications/{a}.app/Contents/MacOS/{a}" for a in
                ("Microsoft Edge", "Google Chrome", "Chromium", "Brave Browser")]
    return [w for n in ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                        "chromium", "chromium-browser", "brave-browser") if (w := shutil.which(n))]


def find_browser():
    """[browser] executable > env DEJA_BROWSER_BROWSER > first installed candidate > None."""
    want = config.get("browser", "executable") or config.env("BROWSER")
    if want:
        return str(config.expand(want))
    return next((c for c in _candidates() if os.path.exists(c)), None)


EDGE = find_browser()
PROFILE = config.path("browser", "profile", config.data_dir() / "profile")
SESSIONS_FILE = config.path("browser", "sessions_file", config.data_dir() / "sessions.json")
CONFIG_PORT = int(config.get("browser", "port", 9222))
PORT = CONFIG_PORT          # the port in effect: CONFIG_PORT, or the spare one recorded in PROFILE/PORT_FILE
ENDPOINT = f"http://127.0.0.1:{PORT}"
SHOT_DIR = Path(tempfile.gettempdir()) / "deja-cdp"
PORT_FILE = ".deja-cdp-port"                  # in PROFILE: spare port the agent browser moved to
TEST_PROFILE_PREFIX = "deja-browser-test-"    # temp dirs made by this project's own test suite
SPARE_PORTS = 50                              # spare port = first free one in CONFIG_PORT+1 .. +SPARE_PORTS


def _use_port(port):
    global PORT, ENDPOINT
    PORT = int(port)
    ENDPOINT = f"http://127.0.0.1:{PORT}"


def launch_args():
    return [f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check"]


class WbError(RuntimeError):
    pass


class NoTab(WbError):
    """The session has no live tab (never opened, or closed by the user / an Edge restart)."""


class ForeignBrowser(WbError):
    """PORT is held by a browser on another profile (pid, profile = its process id and --user-data-dir)."""

    def __init__(self, msg, pid=None, profile=None):
        super().__init__(msg)
        self.pid, self.profile = pid, profile


# ---------- the browser process ----------

def version():
    try:
        with urllib.request.urlopen(ENDPOINT + "/json/version", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


ID_FILE = ".deja-cdp-browser"   # in PROFILE: id of the live browser last confirmed to run on this profile


def _browser_id(v):
    """The per-process GUID in /json/version's webSocketDebuggerUrl; changes on every browser start."""
    return ((v or {}).get("webSocketDebuggerUrl") or "").rstrip("/").rsplit("/", 1)[-1]


def _remember(bid):
    if bid:
        try:
            (PROFILE / ID_FILE).write_text(bid, encoding="utf-8")
        except OSError:
            pass


def _user_data_dir(cmdline):
    """--user-data-dir from a process command line (quoted or not, spaces allowed) -> str | None."""
    i = cmdline.find("--user-data-dir=")
    if i < 0:
        return None
    rest = cmdline[i + len("--user-data-dir="):]
    if rest.startswith('"'):
        rest = rest[1:].split('"', 1)[0]
    else:
        rest = rest.split(" --", 1)[0].split('" ', 1)[0]
    return rest.strip().strip('"') or None


def _same_dir(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def check_profile(v):
    """Refuse a browser on PORT that is not running PROFILE (e.g. another tool's, or one started with a
    config that could not be read). Fast path: its id matches the one recorded in PROFILE — a string
    compare, no process lookup. Only a browser we have not seen yet costs the netstat + command-line
    query, once; then its id is recorded. [browser] check_profile = false turns this off."""
    bid = _browser_id(v)
    try:
        if bid and (PROFILE / ID_FILE).read_text(encoding="utf-8").strip() == bid:
            return
    except OSError:
        pass
    if not config.get("browser", "check_profile", True):
        return
    pid = listener_pid()
    other = _user_data_dir(_cmdline(pid)) if pid else None
    if other is None:
        return   # cannot tell (no permission, unusual platform): do not block, do not record
    if not _same_dir(other, PROFILE):
        raise ForeignBrowser(f"port {PORT} is held by a browser on another profile ({other}), not the agent "
                             f"profile {PROFILE}. Close that browser, or use another [browser] port.",
                             pid=pid, profile=other)
    _remember(bid)


# ---------- the port is taken: whose browser is it? ----------

def _saved_port():
    try:
        return int((PROFILE / PORT_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _save_port():
    try:
        if PORT == CONFIG_PORT:
            (PROFILE / PORT_FILE).unlink(missing_ok=True)
        else:
            (PROFILE / PORT_FILE).write_text(str(PORT), encoding="utf-8")
    except OSError:
        pass


def _adopt_saved_port():
    """If an earlier launch moved the agent browser to a spare port (PROFILE/PORT_FILE) and it still runs
    there, use that port. A stale record (nothing there, or not our profile) is deleted. No file: one stat."""
    saved = _saved_port()
    if saved is None or saved == PORT:
        return
    old = PORT
    _use_port(saved)
    v = version()
    if v:
        try:
            check_profile(v)
            return
        except WbError:
            pass
    _use_port(old)
    try:
        (PROFILE / PORT_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def is_test_leftover(profile):
    """True only for a profile this project's test suite made: under the system temp dir, inside a
    directory named TEST_PROFILE_PREFIX*. Never the agent PROFILE."""
    if not profile or _same_dir(profile, PROFILE):
        return False
    p = Path(os.path.abspath(profile))
    tmp = Path(os.path.abspath(tempfile.gettempdir()))
    try:
        rel = p.relative_to(tmp) if os.name != "nt" else Path(os.path.normcase(str(p))).relative_to(
            Path(os.path.normcase(str(tmp))))
    except ValueError:
        return False
    return any(part.startswith(TEST_PROFILE_PREFIX) for part in rel.parts)


def _parent_state(pid):
    """-> ("alive", parent command line) | ("dead", "") | (None, "") when it can not be told.
    A parent counts only if it started before the child (a recycled PID is not the parent)."""
    try:
        if os.name == "nt":
            ps = (f"$c = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
                  "if (-not $c) { 'UNKNOWN'; exit }; "
                  "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.ParentProcessId)\"; "
                  "if ($p -and $p.CreationDate -le $c.CreationDate) { \"ALIVE`t$($p.CommandLine)\" } else { 'DEAD' }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=20).stdout.strip()
        else:
            ppid = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
            if not ppid.isdigit():
                return None, ""
            if int(ppid) <= 1:
                return "dead", ""
            def ps(field, p):
                return subprocess.run(["ps", "-o", f"{field}=", "-p", str(p)], capture_output=True, text=True,
                                      timeout=10).stdout.strip()
            pcl = ps("command", ppid)
            if not pcl:
                return "dead", ""
            child_age, parent_age = ps("etimes", pid), ps("etimes", ppid)
            older = not (child_age.isdigit() and parent_age.isdigit()) or int(parent_age) >= int(child_age)
            out = "ALIVE\t" + pcl if older else "DEAD"
    except Exception:
        return None, ""
    if out.startswith("ALIVE"):
        return "alive", out.partition("\t")[2]
    if out == "DEAD":
        return "dead", ""
    return None, ""


def is_orphaned_test_browser(pid, profile):
    """A browser our own test suite started whose test runner is gone: safe to kill. Anything we can not
    be sure about (unknown parent, a live python parent = a test still running) is False."""
    if not is_test_leftover(profile):
        return False
    state, parent = _parent_state(pid)
    if state == "dead":
        return True
    return state == "alive" and "python" not in parent.lower() and "pytest" not in parent.lower()


def _kill_tree(pid):
    if os.name == "nt":
        return subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True).returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _wait_port_free(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listener_pid() is None:
            return True
        time.sleep(0.3)
    return False


def _port_free(port):
    import socket
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _spare_port():
    for p in range(CONFIG_PORT + 1, CONFIG_PORT + 1 + SPARE_PORTS):
        if _port_free(p):
            return p
    raise WbError(f"port {CONFIG_PORT} is taken and no free port in {CONFIG_PORT + 1}-{CONFIG_PORT + SPARE_PORTS}")


def _clear_port(why, pid=None, profile=None):
    """PORT is held by something that is not the agent browser. A browser left over by our own tests is
    killed and PORT reused; anything else is left alone and the agent browser moves to a spare port."""
    if pid and is_orphaned_test_browser(pid, profile):
        print(f"deja-cdp: port {PORT} held by a leftover deja-browser test browser (pid {pid}, {profile}); "
              "closing it", file=sys.stderr)
        if _kill_tree(pid) and _wait_port_free():
            return
    spare = _spare_port()
    print(f"deja-cdp: port {PORT} is {why}; leaving it alone and starting the agent browser on port {spare}",
          file=sys.stderr)
    _use_port(spare)


def launch(timeout=20, url="about:blank"):
    """Start headed Edge on the agent profile unless CDP is already up. -> (version_json, launched_by_us).
    If PORT is taken by another browser: our own orphaned test browser is closed; anything else is left
    running and the agent browser starts on a spare port, recorded in PROFILE/PORT_FILE for later runs."""
    _adopt_saved_port()
    v = version()
    if v:
        try:
            check_profile(v)
            return v, False
        except ForeignBrowser as ex:
            _clear_port(f"held by a browser on another profile ({ex.profile})", ex.pid, ex.profile)
    else:
        pid = listener_pid()
        if pid is not None:
            _clear_port(f"held by a non-CDP process (pid {pid})", pid, _user_data_dir(_cmdline(pid)) or None)
    if not EDGE or not os.path.exists(EDGE):
        raise WbError(f"no Chromium browser found ({EDGE or 'none detected'}); "
                      f"set [browser] executable in {config.config_path()}")
    PROFILE.mkdir(parents=True, exist_ok=True)
    guard.harden_profile(PROFILE)   # browser is down: safe to edit Preferences
    for eid, name in guard.password_managers(PROFILE):
        print(f"deja-cdp: warning: password manager extension {name!r} ({eid}) is installed in the agent "
              "profile; while unlocked it can fill any saved password for the agent. Remove it, or keep it "
              "logged out.", file=sys.stderr)
    # detached: the browser must outlive the script that woke it
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0
    subprocess.Popen([EDGE, *launch_args(), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags)
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = version()
        if v:
            _remember(_browser_id(v))   # we started it on PROFILE: no lookup needed later
            _save_port()                # spare port recorded for later runs; back on CONFIG_PORT: record removed
            return v, True
        time.sleep(0.5)
    # another process may have started the agent browser on a spare port meanwhile (our start was then
    # handed to that browser, which stays on its own port): look where the record says
    port = PORT
    _adopt_saved_port()
    if PORT != port and (v := version()):
        return v, False
    raise WbError(f"CDP endpoint {ENDPOINT} not up after {timeout}s")


def listener_pid():
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{PORT}"):
                return int(parts[4])
        return None
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True).stdout
    return int(out.split()[0]) if out.split() else None


def _cmdline(pid):
    if os.name == "nt":
        ps = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout


def stop():
    """Kill only the agent-profile browser (the PID owning PORT, if its command line names PROFILE)."""
    _adopt_saved_port()
    pid = listener_pid()
    if pid is None:
        return {"killed": None, "reason": f"no listener on {PORT}"}
    cl = _cmdline(pid)
    if str(PROFILE).lower() not in cl.lower():
        return {"killed": None, "pid": pid, "reason": "listener is not the agent profile; refusing"}
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return {"killed": pid, "taskkill_rc": r.returncode}
    os.kill(pid, signal.SIGTERM)
    return {"killed": pid}


# ---------- one CDP connection per thread ----------
# Per THREAD, not per process (v0.4): the daemon runs one worker thread per (session, driver), each with
# its own sync Playwright driver and connection (a sync Playwright object can only be used from the
# thread that started it). A single-threaded caller (`import edge_cdp`, the in-process CLI) sees exactly
# the old behaviour: one lazy connection, reused until exit.

_DRIVER = os.environ.get("EDGE_CDP_DRIVER", "playwright")   # process default; use_driver() on the main thread
CONNECT_TIMEOUT_MS = 10000
_LAUNCH_LOCK = threading.Lock()


class _ThreadState(threading.local):
    def __init__(self):
        self.pw = None           # sync_playwright() instance
        self.browser = None      # connect_over_cdp Browser
        self.tid = {}            # id(page) -> (page, targetId); the page is kept so a recycled id() can't match
        self.driver = None       # per-thread driver override (daemon workers); None = _DRIVER
        self.bcdp = None         # (context, browser-level CDP session) for Target.getTargets
        self.on_connect = None   # callback(browser) after a (re)connect
        self.hooked = None       # the context our dialog listener is on
        self.dialog_hook = None  # daemon worker: callback(dialog) replacing the in-process handler
        self.session = None      # session of the action running / last run on this thread
        self.scope = None        # _Scope of the action running (stacked: batch steps)
        self.sink = []           # dialog records not yet reported (in-process: next wb() result "dialogs")
        self.late_in_result = True   # in-process: put sink into the outermost wb() result; daemon: envelope
        self.recent = {}         # session -> last dialog records (the `dialog` action)
        self.pending = []        # dialog records whose handling failed
        self.adopted = []        # (session, record) popups adopted but not reported yet
        self.resolved = {}       # ref id -> selector / frame prefix it resolved to (daemon recording, learn)


_tls = _ThreadState()


def __getattr__(name):
    """Read-only compat for code that looked at the old module globals."""
    if name == "_pw":
        return _tls.pw
    if name == "_browser":
        return _tls.browser
    if name == "_tid":
        return {k: v[1] for k, v in _tls.tid.items()}
    raise AttributeError(name)


def current_driver():
    """The driver this thread uses: its override (daemon worker), else the process default _DRIVER."""
    return _tls.driver or _DRIVER


def use_driver(name):
    """'playwright' or 'patchright' (same API; patchright hides Runtime.enable). Call before the first wb().
    On the main thread it sets the process default; on any other thread only that thread's driver."""
    global _DRIVER
    if _tls.browser is not None and name != current_driver():
        raise WbError(f"already connected with {current_driver()}")
    if threading.current_thread() is threading.main_thread():
        _DRIVER = name
        _tls.driver = None
    else:
        _tls.driver = name


def _sync_playwright():
    if current_driver() == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright
    return sync_playwright


def open_tabs(timeout=2):
    """[(id, url)] of the browser's page targets from /json/list (plain HTTP: works while a JavaScript
    dialog blocks every CDP client)."""
    try:
        with urllib.request.urlopen(ENDPOINT + "/json/list", timeout=timeout) as r:
            return [(t.get("id"), t.get("url")) for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return []


def _connect_timeout_error(secs):
    tabs = open_tabs()
    listing = "; ".join(f"{i} {u}" for i, u in tabs[:20]) or "(could not list them)"
    return WbError(f"connecting to the browser at {ENDPOINT} timed out after {secs:g} s. A JavaScript dialog "
                   f"(alert/confirm/prompt) may be open in one of these tabs: {listing}; close it or dismiss it "
                   f"in the browser window (or close that tab: PUT {ENDPOINT}/json/close/<id>), then retry.")


def _connect():
    st = _tls
    if st.browser is not None and st.browser.is_connected():
        return st.browser
    if st.browser is not None:            # the browser went away (restart / crash): drop, then reconnect
        _drop_connection()
    with _LAUNCH_LOCK:                    # daemon workers: one launch / port decision at a time
        launch()
    # the node driver prints a DEP0169 url.parse() warning on every connect; it is noise
    os.environ.setdefault("NODE_NO_WARNINGS", "1")
    if st.pw is None:
        cm = _sync_playwright()()
        driverpipe.install(cm)            # an idle caller must not freeze its driver (driverpipe.py)
        st.pw = cm.start()
    try:
        st.browser = st.pw.chromium.connect_over_cdp(ENDPOINT, timeout=CONNECT_TIMEOUT_MS)
    except Exception as ex:
        if type(ex).__name__ == "TimeoutError":
            raise _connect_timeout_error(CONNECT_TIMEOUT_MS / 1000) from None
        raise
    if st.on_connect is not None:
        st.on_connect(st.browser)
    return st.browser


def _drop_connection():
    """Forget this thread's browser connection (and what was cached for it); keep the driver running."""
    st = _tls
    try:
        if st.browser is not None:
            st.browser.close()
    except Exception:
        pass
    st.browser, st.bcdp, st.tid, st.hooked = None, None, {}, None


@atexit.register
def disconnect():
    """Drop this thread's CDP connection and driver. On connect_over_cdp, browser.close() only
    disconnects — Edge stays up."""
    st = _tls
    _drop_connection()
    try:
        if st.pw is not None:
            st.pw.stop()
    except Exception:
        pass
    st.pw = None


def _ctx():
    b = _connect()
    ctx = b.contexts[0] if b.contexts else b.new_context()
    if _tls.hooked is not ctx:
        # a fresh function per registration: the driver caches its argument-wrapping shim ON the handler
        # object, so one shared function would hand a patchright worker playwright's shim (raw, unwrapped
        # impl objects; dialog.accept() then never runs) when both drivers live in one process
        ctx.on("dialog", lambda dialog: _on_ctx_dialog(dialog))
        _tls.hooked = ctx
    return ctx


# ---------- JavaScript dialogs ----------
# One context listener per connection (in-process and daemon alike). A dialog on one of the session's
# own tabs (or on a popup one of them opened) is handled at once: by the running action's `dialog`
# policy, else the default (alert / beforeunload accepted, confirm / prompt dismissed), and recorded.
# A page with its own "dialog" listener (a library caller's) is left to that listener. Anyone else's
# tab: in-process, what the driver would do without a listener (playwright: dismiss, beforeunload
# accept; patchright: leave it); daemon workers leave it (its session's worker or the user handles it).

RECENT_DIALOGS = 20


class _Scope:
    """One running action: its session, dialog policy and the dialogs that fired during it."""

    def __init__(self, session, policy):
        self.session, self.policy, self.dialogs = session, policy, []


def parse_dialog_policy(v):
    """Action arg `dialog` -> None (default) | ("accept", prompt_text | None) | ("dismiss", None).
    "accept" / "dismiss" / true / false / any other string = accept, answering a prompt with it /
    {"accept": bool, "text": str} (to answer a prompt with the literal word "accept")."""
    if v is None or v == "":
        return None
    if v is True:
        return ("accept", None)
    if v is False:
        return ("dismiss", None)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("accept", "dismiss"):
            return (low, None)
        return ("accept", v)
    if isinstance(v, dict) and isinstance(v.get("accept"), bool):
        t = v.get("text")
        if t is not None and not isinstance(t, str):
            raise WbError("dialog: text must be a string")
        return ("accept", t) if v["accept"] else ("dismiss", None)
    raise WbError('dialog: use "accept", "dismiss", the text to answer a prompt with, or {"accept": bool, "text": str}')


def dialog_decision(kind, policy):
    """-> ("accept", text | None) | ("dismiss", None) for a dialog of type `kind` under `policy`."""
    if policy is None:
        return ("accept", None) if kind in ("alert", "beforeunload") else ("dismiss", None)
    if policy[0] == "dismiss":
        return ("dismiss", None)
    return ("accept", policy[1] if kind == "prompt" else None)


def _page_tid(page):
    """targetId from this thread's cache, also when the event hands over another wrapper object for the
    same page (seen with patchright when a playwright driver runs in the same process)."""
    if page is None:
        return None
    tid = cached_target_id(page)
    impl = getattr(page, "_impl_obj", None)
    if tid is None and impl is not None:
        tid = next((t for p, t in list(_tls.tid.values()) if getattr(p, "_impl_obj", None) is impl), None)
    return tid


def _owns(page, session):
    """Is `page` one of the session's tabs, or a popup opened by one (no CDP: the page may be blocked)?"""
    if page is None or not session:
        return False
    tabs = set((_load().get(session) or {}).get("tabs") or [])
    if _page_tid(page) in tabs:
        return True
    impl = getattr(page, "_impl_obj", None)
    if impl is not None:                 # no sync API call inside an event handler: read the impl's field
        op = getattr(impl, "_opener", None)
        if op is None:
            return False
        return any(getattr(p, "_impl_obj", None) is op and t in tabs for p, t in list(_tls.tid.values()))
    try:
        op = page.opener()
    except Exception:
        op = None
    return op is not None and _page_tid(op) in tabs


def _has_own_listener(page):
    """Did the caller put its own "dialog" listener on this page (page.on)? The sync Page has no
    listeners(); its impl object is the event emitter."""
    impl = getattr(page, "_impl_obj", None)
    try:
        ls = impl.listeners("dialog") if impl is not None else page.listeners("dialog")
    except Exception:
        return False
    return isinstance(ls, list) and len(ls) > 0


def _driver_default(dialog):
    """What the driver does to a dialog nobody listens for (playwright; patchright leaves it)."""
    if current_driver() == "patchright":
        return
    if dialog.type == "beforeunload":
        dialog.accept()
    else:
        dialog.dismiss()


def handle_dialog(dialog, session, leave_foreign=False, sink=None):
    """Handle one dialog event (see the section comment). -> the record, or None if it was not ours."""
    st = _tls
    try:
        page = dialog.page
        if page is not None and _has_own_listener(page):
            return None
        scope = st.scope
        if scope is not None:
            session = scope.session
        if not _owns(page, session):
            if not leave_foreign:
                _driver_default(dialog)
            return None
        kind = dialog.type
        how, text = dialog_decision(kind, scope.policy if scope is not None else None)
        rec = {"type": kind, "message": (dialog.message or "")[:1000], "url": page.url,
               "tabId": _page_tid(page), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            if how == "accept":
                dialog.accept(text) if text is not None else dialog.accept()
            else:
                dialog.dismiss()
            rec["handled"] = "accepted" if how == "accept" else "dismissed"
        except Exception as ex:
            rec["handled"] = "failed"
            rec["error"] = (str(ex).splitlines() or [""])[0][:200]
            st.pending.append({**rec, "session": session})
            del st.pending[:-RECENT_DIALOGS]
        if scope is not None:
            scope.dialogs.append(rec)
        (st.sink if sink is None else sink).append(rec)
        rl = st.recent.setdefault(session, [])
        rl.append(rec)
        del rl[:-RECENT_DIALOGS]
        return rec
    except Exception:
        return None     # already handled elsewhere / the page went away


def _on_ctx_dialog(dialog):
    hook = _tls.dialog_hook
    if hook is not None:
        hook(dialog)
    else:
        handle_dialog(dialog, _tls.session)


def _receipt(recs):
    return [{"type": r["type"], "message": r["message"], "handled": r.get("handled")} for r in recs]


def _dialog_note(recs):
    return "; ".join(f"{r['type']} {r['message'][:80]!r} {r.get('handled')}" for r in recs)


def cached_target_id(page):
    """targetId of a page this thread already resolved, else None (no CDP traffic)."""
    ent = _tls.tid.get(id(page))
    return ent[1] if ent is not None and ent[0] is page else None


def target_id(page):
    tid = cached_target_id(page)
    if tid is None:
        s = page.context.new_cdp_session(page)
        try:
            tid = s.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        finally:
            s.detach()
        if len(_tls.tid) > 200:           # long-lived worker: forget closed pages
            _tls.tid = {k: v for k, v in _tls.tid.items() if not _is_closed(v[0])}
        _tls.tid[id(page)] = (page, tid)
    return tid


def _is_closed(page):
    try:
        return page.is_closed()
    except Exception:
        return True


# ---------- session map (persisted across processes) ----------

_SAVE_LOCK = threading.Lock()


def _load():
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _replace(tmp, dst, tries=10):
    """os.replace with retries: on Windows an antivirus / indexer briefly holding the file makes it fail
    with PermissionError (EPERM)."""
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.02 * (i + 1))


def _save(m, session=None):
    """Write the session map. With `session`, only that session's entry is written (merged into the file
    as it is now, so a concurrent writer's other sessions are kept); a session missing from m is deleted."""
    with _SAVE_LOCK:
        if session is not None:
            cur = _load()
            if session in m:
                cur[session] = m[session]
            else:
                cur.pop(session, None)
            m = cur
        tmp = SESSIONS_FILE.with_name(f"{SESSIONS_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            _replace(tmp, SESSIONS_FILE)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _entry(m, session):
    # {"tabs": [targetId...], "borrowed": [targetId...], "current": targetId,
    #  "ids": {targetId: "t1"...}, "next_id": 2}   (ids / next_id since v0.4; old entries get them lazily)
    return m.setdefault(session, {"tabs": [], "borrowed": [], "current": None})


def assign_tab_id(e, tid):
    """Short stable id ("t1", "t2"...) of a session tab; never reused within the session."""
    ids = e.setdefault("ids", {})
    if tid not in ids:
        n = int(e.get("next_id") or 1)
        ids[tid] = f"t{n}"
        e["next_id"] = n + 1
    return ids[tid]


def resolve_tab_ref(e, ref):
    """A tabId or a short id ("t2") -> targetId (a short id the session does not know -> None)."""
    if isinstance(ref, str) and len(ref) > 1 and ref[0] == "t" and ref[1:].isdigit():
        return next((t for t, i in (e.get("ids") or {}).items() if i == ref), None)
    return ref


def adopt_popups(m, e, live):
    """Targets opened by one of the session's own (not borrowed) tabs, directly or via another popup,
    that no session has registered yet -> registered as the session's own tabs. -> [new targetIds]"""
    owned = [t for t in e["tabs"] if t not in e["borrowed"]]
    taken = {t for x in m.values() if isinstance(x, dict) for t in (x.get("tabs") or [])}
    new, grew = [], True
    while grew:
        grew = False
        for t, info in live.items():
            if t not in taken and info.get("openerId") and info["openerId"] in owned:
                e["tabs"].append(t)
                owned.append(t)
                taken.add(t)
                new.append(t)
                assign_tab_id(e, t)
                grew = True
    return new


def _pages_by_tid(ctx):
    """{targetId: page} for EVERY page (one CDP round trip per page not resolved yet: use sparingly)."""
    out = {}
    for p in ctx.pages:
        try:
            out[target_id(p)] = p
        except Exception:
            pass
    return out


def _live_targets(ctx):
    """{targetId: targetInfo} of the browser's page targets in ONE Target.getTargets call (~3 ms), or None
    when that is not possible (then callers fall back to resolving every page)."""
    st = _tls
    try:
        if st.bcdp is None or st.bcdp[0] is not ctx:
            st.bcdp = (ctx, ctx.browser.new_browser_cdp_session())
        infos = st.bcdp[1].send("Target.getTargets")["targetInfos"]
    except Exception:
        st.bcdp = None
        return None
    return {t["targetId"]: t for t in infos if t.get("type") == "page"}


def _page_of(ctx, tid, url=None):
    """The page object for targetId `tid`: from this thread's cache, else resolving pages one by one
    (those whose URL equals `url` first) and stopping at the hit. None if no page has it (yet)."""
    pages = ctx.pages
    for p in pages:
        if cached_target_id(p) == tid:
            return p
    todo = [p for p in pages if cached_target_id(p) is None]
    if url:
        todo.sort(key=lambda p: p.url != url)
    for p in todo:
        try:
            if target_id(p) == tid:
                return p
        except Exception:
            pass
    return None


def _session_pages(ctx, session, m=None, current_only=False):
    """-> (map, entry, [(tid, page)...]) with dead tabs pruned from the entry. Liveness comes from the
    browser's target list; page objects are resolved lazily (current_only: just the current tab)."""
    m = m if m is not None else _load()
    e = _entry(m, session)
    live = targets = _live_targets(ctx)
    if live is None:
        by = _pages_by_tid(ctx)
        live, resolve = by, by.get
    else:
        resolve = lambda t: _page_of(ctx, t, live[t].get("url"))  # noqa: E731
    e["tabs"] = [t for t in e["tabs"] if t in live]
    e["borrowed"] = [t for t in e["borrowed"] if t in e["tabs"]]
    if e["current"] not in e["tabs"]:
        e["current"] = e["tabs"][-1] if e["tabs"] else None
    if "ids" in e:
        e["ids"] = {t: i for t, i in e["ids"].items() if t in e["tabs"]}
    if targets is not None:
        _note_adopted(m, e, session, targets)
    for t in e["tabs"]:
        assign_tab_id(e, t)
    pages = []
    for t in e["tabs"]:
        if current_only and t != e["current"]:
            continue
        p = resolve(t)
        if p is not None:
            pages.append((t, p))
    return m, e, pages


def _note_adopted(m, e, session, targets):
    """adopt_popups, then save at once and queue the new tabs for the running / next action's `popups`."""
    new = adopt_popups(m, e, targets)
    if new:
        _save(m, session)
        for t in new:
            _tls.adopted.append((session, {"tabId": t, "tab": assign_tab_id(e, t),
                                           "url": targets[t].get("url") or ""}))
    return new


def _adopt_now(ctx, session):
    """After an action that may have opened a window: register its popups (one Target.getTargets)."""
    targets = _live_targets(ctx)
    if targets is not None:
        m = _load()
        _note_adopted(m, _entry(m, session), session, targets)


def _take_adopted(session):
    st = _tls
    mine = [r for s, r in st.adopted if s == session]
    if mine:
        st.adopted = [(s, r) for s, r in st.adopted if s != session]
    return mine


def _add(m, e, tid, session, borrowed=False):
    assign_tab_id(e, tid)
    if tid not in e["tabs"]:
        e["tabs"].append(tid)
    if borrowed and tid not in e["borrowed"]:
        e["borrowed"].append(tid)
    e["current"] = tid
    _save(m, session)


def _current(ctx, session):
    m, e, pages = _session_pages(ctx, session, current_only=True)
    _save(m, session)
    for t, p in pages:
        if t == e["current"]:
            return p
    raise NoTab(f"session {session!r} has no tab — navigate first")


def _tab_info(t, p, cur=None, e=None):
    try:
        title = p.title()
    except Exception:
        title = ""
    out = {"tabId": t, "url": p.url, "title": title, "active": t == cur}
    if e is not None and t in (e.get("ids") or {}):
        out["tab"] = e["ids"][t]
    return out


# ---------- actions ----------

def _guard(url):
    """Refuse mail / sign-in / bank / broker hosts (guard.py). A guard rail against accidents, not a
    lock: the lock is that the agent profile is never logged in to them."""
    try:
        guard.check_host(url, tickets=False)
    except guard.HostRefused as ex:
        raise WbError(str(ex)) from None


def _navigate(ctx, session, a):
    url = a.get("url")
    if not url:
        raise WbError("navigate: url required")
    _guard(url)
    m, e, pages = _session_pages(ctx, session, current_only=True)
    page = None
    if not a.get("newTab"):
        page = next((p for t, p in pages if t == e["current"]), None)
    if page is None:
        page = ctx.new_page()
        # registered before the load: a dialog the page raises while loading belongs to this session
        # (the daemon handles it), and a failed load leaves no tab outside the session
        _add(m, e, target_id(page), session)
    resp = page.goto(url, wait_until=a.get("wait_until", "domcontentloaded"), timeout=a.get("timeout", 45000))
    tid = target_id(page)
    _add(m, e, tid, session)
    out = {"success": True, "url": page.url, "tabId": tid, "tab": e["ids"][tid], "title": page.title(),
           "status": resp.status if resp else None}
    rec = saved_recipes(page.url) or saved_recipes(url)
    if rec:
        out["recipes"] = rec
    return out


RECIPES_DIR = config.path("recipes", "dir", config.data_dir() / "recipes")   # same default as recipe.py
MAX_RECIPE_HINTS = 10


def saved_recipes(url, root=None):
    """["<host>/<name>", ...] of saved recipes for url's host (a directory listing, no JSON read), at most
    MAX_RECIPE_HINTS; [] when there are none."""
    import urllib.parse
    try:
        host = urllib.parse.urlsplit(url or "").hostname or ""
    except ValueError:
        return []
    if not host:
        return []
    d = Path(root or RECIPES_DIR) / host
    try:
        names = sorted(p.stem for p in d.glob("*.json") if p.is_file())
    except OSError:
        return []
    return [f"{host}/{n}" for n in names[:MAX_RECIPE_HINTS]]


def _evaluate(page, a):
    code = a.get("code")
    if code is None:
        raise WbError("evaluate: code required")
    target = _resolve_frame(page, a["frame"], a.get("timeout", 10000))[0] if a.get("frame") else page
    pos = (a["arg"],) if "arg" in a else ()     # a function's argument (steps recipes pass their params)
    if current_driver() == "patchright":   # patchright defaults to an isolated world; Kimi ran in the page's own
        v = target.evaluate(code, *pos, isolated_context=False)
    else:
        v = target.evaluate(code, *pos)
    t = "undefined" if v is None else {bool: "boolean", int: "number", float: "number", str: "string"}.get(type(v), "object")
    return {"type": t, "value": v}


_JS_FILL_EL = """(e, val) => { const tag = e.tagName.toLowerCase();
if (tag === 'select') return {select: Array.from(e.options).map(o => [o.value, o.label])};
if (e.isContentEditable) {e.focus(); e.textContent = val; e.dispatchEvent(new InputEvent('input', {bubbles: true}));
  return [tag, 'contenteditable'];}
const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, val);
e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
return [tag, 'value']; }"""
_JS_OPTIONS = "e => Array.from(e.options).map(o => [o.value, o.label])"
_JS_SELECTED = """e => { const s = Array.from(e.selectedOptions).map(o => ({value: o.value, label: o.label}));
return e.multiple ? s : (s[0] || null); }"""
_JS_SET_SELECT = """(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles: true}));
e.dispatchEvent(new Event('change', {bubbles: true})); return e.value === v; }"""


def select_choice(options, value):
    """fill on a <select>: options [[value, label]...] -> ("value", v) when an option has that value,
    else ("label", label) when one has that visible label (trimmed), else None (not an option)."""
    v = str(value)
    if any(o[0] == v for o in options):
        return ("value", v)
    for o in options:
        if (o[1] or "").strip() == v.strip():
            return ("label", o[1])
    return None


def _options_hint(options, n=12):
    shown = ", ".join(repr(o[0]) if o[0] == o[1] else f"{o[0]!r}={o[1]!r}" for o in options[:n])
    return shown + (f", ... ({len(options)} options)" if len(options) > n else "")


def _main_world():
    """kwargs for evaluate calls that must run in the page's own world (patchright isolates by default)."""
    return {"isolated_context": False} if current_driver() == "patchright" else {}


def _fill_select(loc, sel, val, options, timeout):
    ch = select_choice(options, val)
    if ch is None:
        raise WbError(f"fill: {val!r} is neither a value nor a label of an option of the <select> {sel!r} "
                      f"(options: {_options_hint(options)}); nothing was selected")
    out = {"success": True, "tag": "select", "mode": "value", "matched": ch[0]}
    try:
        loc.select_option(**{ch[0]: ch[1]}, timeout=timeout)
    except Exception:                    # hidden / custom-styled <select>: set it like the old fallback did
        v = ch[1] if ch[0] == "value" else next(o[0] for o in options if o[1] == ch[1])
        loc.evaluate(_JS_SET_SELECT, v, **_main_world())
        out["fallback"] = "js"
    out["selected"] = loc.evaluate(_JS_SELECTED)
    return out


def _fill(page, a):
    given, val = a.get("selector"), a.get("value", "")
    if not given:
        raise WbError("fill: selector required")
    sel = _fsel(page, a, given)
    multi = _multi_match(page, sel, given)
    return {**_fill_one(page, sel, val, a), **multi}


def _fill_one(page, sel, val, a):
    timeout = a.get("timeout", 5000)
    try:
        page.fill(sel, str(val), timeout=timeout)
        tag, ce = page.eval_on_selector(sel, "e => [e.tagName.toLowerCase(), !!e.isContentEditable]")
        return {"success": True, "tag": tag, "mode": "contenteditable" if ce else "value"}
    except Exception as ex:
        # readonly date pickers / hidden inputs: Playwright refuses, Kimi just set .value — do the same.
        # Through a locator, so a frame-piercing selector works too; a <select> takes an option.
        loc = page.locator(sel).first
        if not loc.count():
            raise WbError(f"fill: no element matches {sel!r} ({_first_line(ex)[:160]})") from None
        try:
            r = loc.evaluate(_JS_FILL_EL, str(val), timeout=2000, **_main_world())
        except Exception as ex2:
            raise WbError(f"fill: {sel!r}: {_first_line(ex2)[:160]}") from None
        if isinstance(r, dict) and "select" in r:
            return _fill_select(loc, sel, str(val), r["select"], timeout)
        return {"success": True, "tag": r[0], "mode": r[1], "fallback": "js"}


def _select(page, a):
    """Choose an option of a <select> (frame-piercing selectors work) through Playwright select_option:
    value (an option's value, or else its label) | label | index; lists for a multiple select. Waits up
    to timeout (default 5000 ms) for the option to exist, so a cascading list that is still loading is
    fine. -> {success, selected: {value, label}} as now in effect ([...] for a multiple select)."""
    sel = a.get("selector")
    if not sel:
        raise WbError("select: selector required")
    given = [k for k in ("value", "label", "index") if a.get(k) is not None]
    if len(given) != 1:
        raise WbError("select: give exactly one of value, label, index")
    key, want = given[0], a[given[0]]
    sel, raw_sel = _fsel(page, a, sel), sel
    multi = _multi_match(page, sel, raw_sel)
    if key == "index":
        want = [int(x) for x in want] if isinstance(want, list) else int(want)
    timeout = a.get("timeout", 5000)
    loc = page.locator(sel).first
    try:
        loc.select_option(**{key: want}, timeout=timeout)
    except Exception as ex:
        try:
            opts = loc.evaluate(_JS_OPTIONS, timeout=1000)
        except Exception:
            raise WbError(f"select: {sel!r}: {_first_line(ex)}") from None
        raise WbError(f"select: no option with {key} {want!r} in {sel!r} within {timeout} ms "
                      f"(options: {_options_hint(opts)})") from None
    return {"success": True, "selected": loc.evaluate(_JS_SELECTED, timeout=2000), **multi}


def _click(page, a):
    given = a.get("selector")
    if not given:
        raise WbError("click: selector required")
    sel = _fsel(page, a, given)
    multi = _multi_match(page, sel, given)
    loc = page.locator(sel).first
    if ref_id(given) and " >> nth=" in sel and "internal:role=option" in sel:
        try:
            native = loc.evaluate("e => e.tagName === 'OPTION'", timeout=2000)
        except Exception:
            native = False
        if native:
            raise WbError(f"click: {given} is an <option> of a <select>; use select {{selector: <its combobox "
                          "ref or selector>, label: <the option text>}")
    loc.click(timeout=a.get("timeout", 10000))
    try:
        tag, text = loc.evaluate("e => [e.tagName.toLowerCase(), (e.innerText||e.value||'').trim().slice(0,100)]",
                                 timeout=1000)
    except Exception:   # the click navigated away — the element is gone, the click still happened
        tag, text = "", ""
    return {"success": True, "tag": tag, "text": text, **multi}


def _out_path(a, ext):
    p = a.get("path")
    if p:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    return str(SHOT_DIR / f"{int(time.time() * 1000)}.{ext}")


def _screenshot(page, a):
    fmt = (a.get("format") or "png").lower()
    fmt = "jpeg" if fmt in ("jpg", "jpeg") else "png"
    path = _out_path(a, "jpg" if fmt == "jpeg" else "png")
    kw = {"path": path, "type": fmt}
    if fmt == "jpeg" and a.get("quality") is not None:
        kw["quality"] = int(a["quality"])
    if a.get("selector"):
        page.locator(_sel(page, a["selector"])).first.screenshot(**kw)
    else:
        page.screenshot(**kw)
    return {"format": fmt, "path": path, "sizeBytes": os.path.getsize(path), "mimeType": f"image/{fmt}"}


def _save_as_pdf(page, a):
    path = _out_path(a, "pdf")
    fmt = a.get("paper_format") or "A4"
    landscape, bg = bool(a.get("landscape", False)), bool(a.get("print_background", True))
    scale = float(a.get("scale", 1))
    try:
        page.pdf(path=path, format=fmt, landscape=landscape, print_background=bg, scale=scale)
    except Exception:   # fallback: raw CDP (both paths verified headed in r1029-09)
        import base64
        s = page.context.new_cdp_session(page)
        try:
            r = s.send("Page.printToPDF", {"landscape": landscape, "printBackground": bg, "scale": scale})
        finally:
            s.detach()
        Path(path).write_bytes(base64.b64decode(r["data"]))
    return {"path": path, "sizeBytes": os.path.getsize(path), "mimeType": "application/pdf",
            "pageTitle": page.title()}


_INTERACTIVE = ("button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox",
                "option", "menuitem", "tab", "switch", "slider", "spinbutton")
_ROLE = None


def _role(line):
    global _ROLE
    if _ROLE is None:
        import re
        _ROLE = re.compile(r"^\s*-\s+'?([a-z]+)")      # a YAML-quoted key: - 'button "a: b"'
    m = _ROLE.match(line)
    return m.group(1) if m else ""


def _with_ancestors(lines, keep):
    """keep[i] -> also keep every ancestor (lower indentation above it), so a hit has its path."""
    return [lines[i] for i in _ancestor_idx(lines, keep)]


def _ancestor_idx(lines, keep):
    """Sorted indices of the kept lines and all their ancestors."""
    out, stack = set(), []   # stack = [(indent, line index)] of the open ancestors
    for i, ln in enumerate(lines):
        ind = len(ln) - len(ln.lstrip())
        while stack and stack[-1][0] >= ind:
            stack.pop()
        if keep[i]:
            out.update(j for _, j in stack)
            out.add(i)
        stack.append((ind, i))
    return sorted(out)


# ---------- frames ----------
# read / snapshot / wait {text} see inside <iframe> and <frameset> documents (same- and cross-origin) by
# walking page.frames: each visible child frame's content follows its parent's, indented under a
# header line that gives a ready-to-use piercing selector prefix. Pages without child frames take the
# old single-document path (no extra round trip).

FRAME_HOP = " >> internal:control=enter-frame >> "
MAX_FRAMES = 50              # child frames walked per call
CHILD_TIMEOUT = 3000         # ms for one child frame's aria snapshot
_JS_BODY_TEXT = "() => document.body ? document.body.innerText : ''"
_JS_HAS_BODY = "() => !!document.body && document.body.tagName === 'BODY'"
_JS_FRAME_INFO = """e => { const tag = e.tagName.toLowerCase(), d = e.ownerDocument;
const same = Array.from(d.querySelectorAll(tag)), name = e.getAttribute('name') || '';
const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
return {tag, id: e.id || '', name, index: same.indexOf(e),
  idUnique: !!e.id && d.querySelectorAll('#' + CSS.escape(e.id)).length === 1,
  nameUnique: !!name && same.filter(x => x.getAttribute('name') === name).length === 1,
  visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'}; }"""
_CSS_IDENT = None


def _q(v):
    """Attribute value for a single-quoted CSS string (single quotes keep the prefix free of '"', so it
    pastes into a JSON argument as is)."""
    return str(v).replace("\\", "\\\\").replace("'", "\\'")


def frame_hop(info):
    """Selector of a frame element within its parent document (from _JS_FRAME_INFO): #id when the id is
    unique, tag[name='...'] when the name is, else tag >> nth=<index among that tag>."""
    global _CSS_IDENT
    if _CSS_IDENT is None:
        import re
        _CSS_IDENT = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_-]*$")
    tag = info.get("tag") or "iframe"
    fid, name = info.get("id") or "", info.get("name") or ""
    if fid and info.get("idUnique"):
        return f"#{fid}" if _CSS_IDENT.match(fid) else f"{tag}[id='{_q(fid)}']"
    if name and info.get("nameUnique"):
        return f"{tag}[name='{_q(name)}']"
    return f"{tag} >> nth={int(info.get('index') or 0)}"


def frame_header(hop, prefix):
    return f'[frame {hop} \u2192 selector prefix: "{prefix}"]'


def frame_arg_selector(arg):
    """`frame` arg (the frame element's selector, or a prefix ending in enter-frame >>) ->
    (element selector, prefix)."""
    s = str(arg or "").strip()
    for suf in ("internal:control=enter-frame >>", "internal:control=enter-frame"):
        if s.endswith(suf):
            s = s[:-len(suf)].rstrip()
            if s.endswith(">>"):
                s = s[:-2].rstrip()
            break
    if not s:
        raise WbError("frame: give the <iframe>/<frame> selector (or its selector prefix)")
    return s, s + FRAME_HOP


def assemble_sections(sections):
    """[{depth, header, lines}] in tree order -> text. depth 0 (the root document) has no header; a child
    frame's header is indented 2*(depth-1) and its lines 2*depth. Child sections without lines are left
    out (their own children carry the full prefix in their headers)."""
    out = []
    for sec in sections:
        d, lines = sec["depth"], sec["lines"]
        if d == 0:
            out.extend(lines)
        elif lines:
            out.append("  " * (d - 1) + sec["header"])
            out.extend("  " * d + ln for ln in lines)
    return "\n".join(out)


def _first_line(ex):
    return (str(ex).splitlines() or [""])[0][:200]


def _prefix_of(frame):
    hops, cur = [], frame
    while cur.parent_frame is not None:
        hops.append(frame_hop(cur.frame_element().evaluate(_JS_FRAME_INFO)))
        cur = cur.parent_frame
    return "".join(h + FRAME_HOP for h in reversed(hops))


def _resolve_frame(page, arg, timeout=10000):
    """`frame` arg -> (Frame, selector prefix). Accepts a frame name, the frame element's selector, or a
    prefix as printed in read / snapshot headers (nested prefixes too)."""
    if not isinstance(arg, str) or not arg.strip():
        raise WbError("frame: give the <iframe>/<frame> selector (or its selector prefix)")
    if ref_id(arg):
        rid, rec = _ref_record(page, arg)
        if rec.get("k") != "frame":
            raise WbError(f"frame: @{rid} is not a frame (use the [ref=eN] on a [frame ...] header line)")
        arg = rec["p"]
        _tls.resolved[rid] = arg
    try:
        f = page.frame(name=arg.strip())
    except Exception:
        f = None
    if f is not None and f is not page.main_frame:
        return f, _prefix_of(f)
    elsel, pfx = frame_arg_selector(arg)
    try:
        f = page.locator(elsel).first.element_handle(timeout=timeout).content_frame()
    except Exception as ex:
        raise WbError(f"frame: no frame element matches {elsel!r} ({_first_line(ex)})") from None
    if f is None:
        raise WbError(f"frame: {elsel!r} is not an <iframe> / <frame>")
    return f, pfx


def _frame_scope(page, a):
    """-> (root frame, prefix, walk children?) or None for the single-document path (no frame arg and
    a non-default selector, or a page without child frames)."""
    sel = a.get("selector")
    default_sel = sel in (None, "", "body")
    if a.get("frame"):
        f, pfx = _resolve_frame(page, a["frame"], a.get("timeout", 10000))
        return f, pfx, default_sel
    if not default_sel:
        return None
    frames = getattr(page, "frames", None)
    if not isinstance(frames, list) or len(frames) <= 1:
        return None
    return page.main_frame, "", True


def _walk_frames(root, prefix, collect, out, depth=1):
    """Append a section per visible child frame of `root` (depth-first, document order)."""
    for child in root.child_frames:
        if len(out) > MAX_FRAMES:
            return
        try:
            info = child.frame_element().evaluate(_JS_FRAME_INFO)
        except Exception:
            continue                     # detached meanwhile
        if not info.get("visible"):
            continue
        hop = frame_hop(info)
        pfx = prefix + hop + FRAME_HOP
        sec = {"depth": depth, "header": frame_header(hop, pfx), "raw": "", "prefix": pfx, "furl": child.url}
        try:
            sec["raw"] = collect(child) or ""
        except Exception as ex:
            sec["error"] = _first_line(ex)
        out.append(sec)
        _walk_frames(child, pfx, collect, out, depth + 1)


def _frame_aria(frame, timeout):
    """Aria tree of a frame's <body>; "" for a <frameset> document (it has no body)."""
    if not frame.evaluate(_JS_HAS_BODY):
        return ""
    return frame.locator("body").aria_snapshot(timeout=timeout)


# ---------- element refs ----------
# snapshot {refs: true} tags lines with [ref=eN]. A ref is stored per session (<sessions file>.refs/
# <session>.json, so it survives between CLI processes with or without the daemon) as what a Playwright
# role locator needs to find the element again: the frame's selector prefix, an optional scope selector,
# role, accessible name, nth among the same role+name in that scope and how many matched, plus the tab
# and URL. Using @eN re-checks all of it (same tab, same page URL, same frame URL, same match count) and
# fails with "stale ref" rather than pick another element. Nothing is written into the page. Ids are
# session-wide (e1, e2, ... never reused, so a ref from another tab can't hit this one); the same element
# on an unchanged page keeps its id across snapshots, so a delta diff shows only what changed.

REF_ROLES = frozenset(_INTERACTIVE) | {"menuitemcheckbox", "menuitemradio", "treeitem"}
REF_NAMED_ROLES = frozenset({"heading", "img", "cell", "gridcell", "columnheader", "rowheader", "dialog",
                             "alertdialog", "tabpanel"})   # only when they have a name
MAX_REFS = 10000           # per tab and page; nodes beyond it stay untagged (the reply says so)
MAX_TREES = 4              # stored snapshot texts per tab (delta)
MAX_TREE_CHARS = 400000
DELTA_MAX_CHANGE = 0.5     # a diff is returned while changed lines < this share of old + new lines
STALE = "page changed — take a new snapshot"
_ARIA_KEY = _REF_ARG = None


def _ref_rx():
    global _ARIA_KEY, _REF_ARG
    if _ARIA_KEY is None:
        import re
        _ARIA_KEY = re.compile(r'^(?P<role>[a-z]+)(?: (?P<name>"(?:[^"\\]|\\.)*"|/.*/))?(?P<attrs>(?: \[[^\]]*\])*)$')
        _REF_ARG = re.compile(r"^@?(e[0-9]+)$")
    return _ARIA_KEY, _REF_ARG


def parse_aria_line(line):
    """One line of a Playwright aria snapshot -> {role, name, at} (`at` = where " [ref=eN]" goes: the end
    of the key, inside the quotes of a YAML-quoted key) or None (text, /url props, anything else).
    name is None when the line shows none (unnamed, or a name over 900 chars that Playwright leaves out);
    a name the renderer shows as /.../ is literal (we never ask for regex rendering)."""
    key_rx = _ref_rx()[0]
    i = len(line) - len(line.lstrip(" "))
    if not line.startswith("- ", i):
        return None
    j = i + 2
    if line.startswith("'", j):                  # yamlEscapeKeyIfNeeded: '...' with '' for '
        k, buf = j + 1, []
        while k < len(line):
            if line[k] == "'":
                if line.startswith("''", k):
                    buf.append("'")
                    k += 2
                    continue
                break
            buf.append(line[k])
            k += 1
        else:
            return None
        key, at = "".join(buf), k
    else:
        rest = line[j:]
        c = rest.find(": ")
        key = rest[:c] if c >= 0 else rest[:-1] if rest.endswith(":") else rest
        at = j + len(key)
    m = key_rx.match(key)
    if not m:
        return None
    name = m.group("name")
    if name is not None and name.startswith('"'):
        try:
            name = json.loads(name)
        except ValueError:
            return None
    return {"role": m.group("role"), "name": name, "at": at}


def ref_records(lines, base):
    """Per line of ONE document's (or scope's) aria tree: (record, insert-at) for a ref-worthy line, else
    None. base = the fields every ref of this section shares (p frame prefix, s scope, fu frame url). nth / count come from the whole tree, so filters applied afterwards don't change them. A named
    node: nth among the same role+name; an unnamed one: nth among every node of that role (what a role
    locator without a name matches)."""
    from collections import Counter
    parsed = [parse_aria_line(ln) for ln in lines]
    by_name, by_role = Counter(), Counter()
    for p in parsed:
        if p:
            by_name[(p["role"], p["name"])] += 1
            by_role[p["role"]] += 1
    seen_n, seen_r, out = Counter(), Counter(), []
    for p in parsed:
        if p is None:
            out.append(None)
            continue
        role, name = p["role"], p["name"]
        i_n, i_r = seen_n[(role, name)], seen_r[role]
        seen_n[(role, name)] += 1
        seen_r[role] += 1
        if not (role in REF_ROLES or (name and role in REF_NAMED_ROLES)):
            out.append(None)
            continue
        nth, count = (i_r, by_role[role]) if name is None else (i_n, by_name[(role, name)])
        out.append(({**base, "r": role, "n": name, "i": nth, "c": count}, p["at"]))
    return out


def tag_line(line, at, rid):
    return f"{line[:at]} [ref={rid}]{line[at:]}"


def annotate_refs(lines, base, alloc):
    """Every ref-worthy line tagged with [ref=alloc(record)] (an alloc returning None leaves it as is)."""
    out = []
    for ln, c in zip(lines, ref_records(lines, base)):
        rid = alloc(c[0]) if c else None
        out.append(tag_line(ln, c[1], rid) if rid else ln)
    return out


def ref_key(rec):
    return json.dumps([rec.get(k) for k in ("k", "fu", "p", "s", "r", "n", "i", "c")], ensure_ascii=False)


def ref_allocator(store, tid, url, cap=MAX_REFS):
    """-> alloc(record) for tab `tid` now at `url`: the id an identical record already has, else the next
    session-wide id; None once the tab holds `cap` refs (alloc.full is then True). A tab's refs belong to
    one URL (tab["u"]); a snapshot of another URL drops them (they could only be stale now)."""
    tab = store["tabs"].setdefault(tid, {"u": url, "refs": {}})
    if tab.get("u") != url or not isinstance(tab.get("refs"), dict):
        tab["u"], tab["refs"] = url, {}
    index = {ref_key(r): rid for rid, r in tab["refs"].items()}

    def alloc(rec):
        rec = {k: v for k, v in rec.items() if v not in (None, "") or k == "n"}
        k = ref_key(rec)
        rid = index.get(k)
        if rid is None:
            if len(tab["refs"]) >= cap:
                alloc.full = True
                return None
            rid = f"e{int(store.get('next') or 1)}"
            store["next"] = int(rid[1:]) + 1
            tab["refs"][rid] = rec
            index[k] = rid
        return rid
    alloc.full = False
    return alloc


def _attr_q(v):
    """Playwright's escapeForAttributeSelector (exact)."""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"s'


def ref_locator(rec):
    """Selector matching every element of the ref's role (+ name) in its frame / scope (without nth)."""
    sel = f"internal:role={rec['r']}"
    if rec.get("n") is not None:
        sel += f"[name={_attr_q(rec['n'])}]"
    if rec.get("s"):
        sel = f"{rec['s']} >> nth=0 >> {sel}"
    return (rec.get("p") or "") + sel


def ref_id(v):
    """'@e5' / 'e5' -> 'e5'; anything else -> None."""
    if not isinstance(v, str):
        return None
    m = _ref_rx()[1].match(v.strip())
    return m.group(1) if m else None


def _refs_path(session, kind=""):
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session))[:60]
    if safe != session:
        import hashlib
        safe += "-" + hashlib.sha1(str(session).encode("utf-8")).hexdigest()[:8]
    return refs_dir() / f"{safe}{kind}.json"


def refs_dir():
    """<sessions file name>.refs/ next to the sessions file (its directory may be the home directory)."""
    return SESSIONS_FILE.with_name(SESSIONS_FILE.stem + ".refs")


def _read_json(p):
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write_json(p, obj):
    """Atomic write with a unique temp name; os.replace retried on Windows EPERM (like _save)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    try:
        _replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _ref_store(session):
    s = _read_json(_refs_path(session))
    if not isinstance(s.get("tabs"), dict):
        s["tabs"] = {}
    s.setdefault("next", 1)
    return s


def prune_ref_store(store, live_tabs):
    """Drop the refs of tabs that are no longer the session's (closed); the id counter never goes back."""
    store["tabs"] = {t: v for t, v in store["tabs"].items() if t in live_tabs}
    return store


def _save_ref_store(session, store):
    live = set((_load().get(session) or {}).get("tabs") or [])
    _write_json(_refs_path(session), prune_ref_store(store, live))


def _forget_refs(session):
    for kind in ("", ".trees"):
        try:
            _refs_path(session, kind).unlink(missing_ok=True)
        except OSError:
            pass


def _stale(rid, why):
    return WbError(f"stale ref @{rid}: {STALE} ({why})")


def _ref_record(page, v):
    """-> (id, record) of a ref the current tab may use; WbError when unknown / from another tab / the page
    or frame navigated since the snapshot."""
    rid = ref_id(v)
    session = _tls.session
    store = _ref_store(session)
    tid = target_id(page)
    tab = store["tabs"].get(tid) or {}
    rec = (tab.get("refs") or {}).get(rid)
    if rec is None:
        other = next((t for t, tr in store["tabs"].items() if rid in (tr.get("refs") or {})), None)
        if other is not None:
            short = (_entry(_load(), session).get("ids") or {}).get(other, other)
            raise _stale(rid, f"it is from tab {short}, not the current tab; attach_tab "
                              f"{{\"tabId\": \"{short}\"}} to use it there")
        if int(rid[1:]) < int(store.get("next") or 1):
            raise _stale(rid, "it was given for an earlier page of this tab, or for a tab that has closed")
        raise WbError(f"unknown ref @{rid}: no snapshot of this session gave it — take a snapshot with "
                      "refs: true")
    if page.url != tab.get("u"):
        raise _stale(rid, f"the tab is now at {page.url[:200]}, the snapshot was of {str(tab.get('u'))[:200]}")
    if rec.get("p") and rec.get("fu"):
        try:
            furl = _resolve_frame(page, rec["p"], 3000)[0].url
        except WbError:
            furl = None
        if furl != rec["fu"]:
            raise _stale(rid, f"its frame is gone or now shows {furl or 'nothing'}")
    return rid, rec


def resolve_ref(page, v):
    """@eN -> a selector for exactly that element, after checking it is still the one the snapshot saw."""
    rid, rec = _ref_record(page, v)
    if rec.get("k") == "frame":
        raise WbError(f"@{rid} is a frame: pass it as frame: \"@{rid}\", or use a ref of an element inside it")
    loc = ref_locator(rec)
    try:
        n = page.locator(loc).count()
    except Exception as ex:
        raise _stale(rid, _first_line(ex)) from None
    if n != rec.get("c"):
        what = f"{rec['r']} {json.dumps(rec['n'], ensure_ascii=False)}" if rec.get("n") is not None else rec["r"]
        raise _stale(rid, f"{n} elements now match {what}, the snapshot saw {rec.get('c')}")
    out = f"{loc} >> nth={int(rec.get('i') or 0)}"
    _tls.resolved[rid] = out
    return out


def _sel(page, v):
    """A selector argument: @eN / eN resolved (and checked), anything else as given."""
    return resolve_ref(page, v) if ref_id(v) else v


def _fsel(page, a, given):
    """click / fill / select: the selector, put inside a["frame"] when one is given (a ref already
    carries its frame; a selector that already pierces frames is left alone)."""
    sel = _sel(page, given)
    if a.get("frame") and not ref_id(given) and "internal:control=enter-frame" not in sel:
        sel = _resolve_frame(page, a["frame"], a.get("timeout", 10000))[1] + sel
    return sel


def _multi_match(page, sel, given):
    """{matches, warning} when a CSS / text selector (not a ref) matches more than one element."""
    if ref_id(given):
        return {}
    try:
        n = page.locator(sel).count()
    except Exception:
        return {}
    if not isinstance(n, int) or n <= 1:
        return {}
    return {"matches": n, "warning": f"{n} elements match {given!r}; acted on the first. Use a ref from "
                                     "snapshot {refs: true} or a narrower selector"}


def snapshot_delta(old, new, max_change=DELTA_MAX_CHANGE):
    """-> ("unchanged", "") | ("diff", compact unified diff, 1 line of context) | ("full", "")."""
    if old == new:
        return "unchanged", ""
    import difflib
    a, b = old.splitlines(), new.splitlines()
    diff = list(difflib.unified_diff(a, b, lineterm="", n=1))[2:]
    changed = sum(1 for d in diff if d[:1] in "+-")
    if changed >= max_change * max(len(a) + len(b), 1):
        return "full", ""
    text = "\n".join(diff)
    return ("diff", text) if len(text) < len(new) else ("full", "")


def delta_key(a):
    return json.dumps({k: a.get(k) for k in ("selector", "interactive_only", "query", "frame", "refs")},
                      sort_keys=True, ensure_ascii=False)


def _snapshot(page, a):
    """Aria tree, optionally scoped. selector = subtree root; interactive_only = only controls/links
    (with their ancestor path); query = only lines containing the text (case-insensitive) + ancestors.
    Child frames: see the frames section; frame = scope to one frame. refs / delta: see the refs section."""
    given = a.get("selector")
    sel = _sel(page, given) if given else "body"
    a = {**a, "selector": sel}
    timeout = a.get("timeout", 10000)
    fs = _frame_scope(page, a)
    scope = None if sel == "body" else sel
    if fs is None:
        sections = [{"depth": 0, "raw": page.locator(sel).first.aria_snapshot(timeout=timeout), "scope": scope}]
    else:
        root, pfx, walk = fs
        raw = _frame_aria(root, timeout) if sel == "body" else root.locator(sel).first.aria_snapshot(timeout=timeout)
        sections = [{"depth": 0, "raw": raw, "scope": scope, "prefix": pfx, "furl": root.url if pfx else None}]
        if walk:
            _walk_frames(root, pfx, lambda f: _frame_aria(f, CHILD_TIMEOUT), sections)
    refs, delta = bool(a.get("refs")), bool(a.get("delta"))
    tid = target_id(page) if (refs or delta) else None
    if refs:
        store = _ref_store(_tls.session)
        alloc = ref_allocator(store, tid, page.url)
    q = str(a["query"]).lower() if a.get("query") else ""
    for sec in sections:
        raw = sec["raw"].splitlines()
        if sec.get("error"):
            sec["lines"] = [] if (q or a.get("interactive_only")) else [f"(unreadable: {sec['error']})"]
            continue
        idx = _ancestor_idx(raw, [q in ln.lower() for ln in raw]) if q else range(len(raw))
        if a.get("interactive_only"):
            idx = [i for i in idx if _role(raw[i]) in _INTERACTIVE]
        if not refs:
            sec["lines"] = [raw[i] for i in idx]
            continue
        if sec["depth"] > 0 and sec.get("prefix"):     # the header first: ids read in tree order
            fr = alloc({"k": "frame", "p": sec["prefix"], "fu": sec.get("furl")})
            if fr:
                sec["header"] += f" [ref={fr}]"
        base = {"p": sec.get("prefix") or "", "s": sec.get("scope"),
                "fu": sec.get("furl") if sec.get("prefix") else None}
        cands = ref_records(raw, base)
        if sec.get("scope"):
            _drop_scope_root(page, raw, cands)
        sec["lines"] = []
        for i in idx:
            c = cands[i]
            rid = alloc(c[0]) if c else None
            sec["lines"].append(tag_line(raw[i], c[1], rid) if rid else raw[i])
    if refs:
        _save_ref_store(_tls.session, store)
    full = sum(len(sec["raw"]) for sec in sections)
    text = assemble_sections(sections)
    out = {"url": page.url, "title": page.title()}
    if refs and alloc.full:
        out["refs_note"] = (f"only the first {MAX_REFS} nodes got a ref; narrow with selector / query / "
                            "interactive_only for refs further down")
    if refs or delta:
        kind = _delta_step(tid, a, page.url, text)      # also stored for refs: a later delta diffs against it
        if not delta or a.get("offset"):
            kind = None
        if kind is not None and kind[0] == "unchanged":
            return {**out, "unchanged": True, "chars": len(text)}
        if kind is not None and kind[0] == "diff":
            piece, nxt = _clip(kind[1], a.get("max_chars"), None)
            out.update({"delta": "diff", "diff": piece, "chars": len(text)})
            if nxt is not None:
                out.update({"truncated": True, "hint": "the diff was cut; take snapshot without delta for the full tree"})
            return out
        if delta:
            out["delta"] = "full"
    piece, nxt = _clip(text, a.get("max_chars"), a.get("offset"))
    out.update({"tree_text": piece, "chars": len(text), "full_chars": full, "truncated": nxt is not None})
    if nxt is not None:
        out["next_offset"] = nxt
        out["hint"] = _trunc_hint(nxt, len(text), "snapshot")
    return out


def _drop_scope_root(page, raw, cands):
    """A scoped snapshot shows the scope element itself as a top-level line when it has a role, but a role
    locator under the scope only finds descendants: such a line gets no ref (it is the selector you
    passed). Checked live, top-level candidates only (usually one)."""
    for i, ln in enumerate(raw):
        if cands[i] and not ln.startswith(" "):
            try:
                n = page.locator(ref_locator(cands[i][0])).count()
            except Exception:
                n = None
            if n != cands[i][0]["c"]:
                cands[i] = None


def _delta_step(tid, a, url, text):
    """Store this tree as the latest for (tab, args); -> snapshot_delta against the previous one of the
    same URL, or None when there is none."""
    session = _tls.session
    path = _refs_path(session, ".trees")
    st = _read_json(path)
    tabs = st.setdefault("tabs", {})
    live = set((_load().get(session) or {}).get("tabs") or [])
    for t in [t for t in tabs if t not in live]:
        del tabs[t]
    mine = tabs.setdefault(tid, {})
    key = delta_key(a)
    prev = mine.pop(key, None)
    if len(text) <= MAX_TREE_CHARS:
        mine[key] = {"u": url, "t": text}
        for k in list(mine)[:-MAX_TREES]:
            del mine[k]
    _write_json(path, st)
    if not prev or prev.get("u") != url:
        return None
    return snapshot_delta(prev.get("t") or "", text)


def _clip(text, max_chars=None, offset=None):
    """text[offset:] cut to at most max_chars (default 20000) on a LINE boundary -> (piece, next_offset).
    next_offset is where the next page starts (just past the newline we cut at), None when nothing is
    left. A single line longer than the cap is cut mid-line so paging always advances."""
    n = int(max_chars or 20000)
    start = min(max(int(offset or 0), 0), len(text))
    end = start + n
    if end >= len(text):
        return text[start:], None
    cut = end if text[end] == "\n" else text.rfind("\n", start, end)
    if cut <= start:                       # no usable newline in the window
        return text[start:end], end
    return text[start:cut], cut + 1


def _trunc_hint(nxt, total, action):
    again = ("read the next part with offset=%d" % nxt)
    extra = "" if action == "read" else ", or use read {query}"
    return (f"truncated at {nxt:,} of {total:,} chars; narrow with query=\"<text>\" or selector=\"<css>\", "
            f"{again}{extra}")


def query_lines(lines, query, ctx=2):
    """Lines containing `query` (case-insensitive) plus `ctx` lines around each, '…' between gaps."""
    q = query.lower()
    keep = set()
    for i, ln in enumerate(lines):
        if q in ln.lower():
            keep.update(range(max(i - ctx, 0), min(i + ctx + 1, len(lines))))
    out, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("…")
        out.append(lines[i])
        prev = i
    return out


def _read(page, a):
    """Page text (innerText of selector, default body) by lines, blank lines dropped. query = only the
    lines containing it (case-insensitive) plus context_lines (default 2) before/after, '…' where the kept
    runs are not adjacent. Cut like snapshot: line boundary, offset / next_offset, hint."""
    sel = _sel(page, a["selector"]) if a.get("selector") else "body"
    a = {**a, "selector": sel}
    timeout = a.get("timeout", 10000)
    fs = _frame_scope(page, a)
    try:
        if fs is None:
            sections = [{"depth": 0, "raw": page.locator(sel).first.inner_text(timeout=timeout)}]
        else:
            root, pfx, walk = fs
            raw = root.evaluate(_JS_BODY_TEXT) if sel == "body" else root.locator(sel).first.inner_text(timeout=timeout)
            sections = [{"depth": 0, "raw": raw}]
            if walk:
                _walk_frames(root, pfx, lambda f: f.evaluate(_JS_BODY_TEXT), sections)
    except WbError:
        raise
    except Exception as ex:
        raise WbError(f"read: {sel!r}: {_first_line(ex)}") from None
    query = str(a["query"]) if a.get("query") else ""
    for sec in sections:
        lines = [ln for ln in sec["raw"].splitlines() if ln.strip()]
        if sec.get("error"):
            lines = [] if query else [f"(unreadable: {sec['error']})"]
        if query:
            lines = query_lines(lines, query, max(int(a.get("context_lines", 2)), 0))
        sec["lines"] = lines
    text = assemble_sections(sections)
    piece, nxt = _clip(text, a.get("max_chars"), a.get("offset"))
    res = {"url": page.url, "title": page.title(), "text": piece, "chars": len(text),
           "full_chars": sum(len(sec["raw"]) for sec in sections), "truncated": nxt is not None}
    if nxt is not None:
        res["next_offset"] = nxt
        res["hint"] = _trunc_hint(nxt, len(text), "read")
    elif query and not text:
        res["hint"] = (f"no line contains {query!r}; try another spelling, a broader selector=\"<css>\", "
                       "or snapshot {query}")
    return res


_STATIC_TYPES = {"image", "stylesheet", "font", "media", "script", "manifest", "texttrack", "other"}
_REDACT = ("authorization", "cookie", "set-cookie", "x-csrf-token", "x-xsrf-token", "csrf", "token", "api-key",
           "x-api-key", "proxy-authorization")


def _safe_headers(h):
    """Header names + values, but credential-looking values are replaced by their length only."""
    out = {}
    for k, v in (h or {}).items():
        kl = k.lower()
        out[k] = f"<redacted len={len(v)}>" if any(r in kl for r in _REDACT) else v[:300]
    return out


def _network_capture(page, a):
    """Capture the requests one trigger causes, in ONE call (a CLI process cannot span two calls).
    trigger: {"navigate": url} | {"reload": true} | {"click": selector} | {"evaluate": code} | omitted
    url_filter: substring; types: resource types (default xhr+fetch, "all" = everything non-static);
    wait_ms: settle time after the trigger (page.wait_for_timeout — time.sleep would starve the events);
    body_dir: write each response body to a file there (default SHOT_DIR/net-<ts>); bodies are never inline.
    Credential headers come back as <redacted len=N>; their NAMES tell you what a replay needs."""
    types = a.get("types") or ["xhr", "fetch"]
    flt = a.get("url_filter") or ""
    body_dir = Path(a.get("body_dir") or SHOT_DIR / f"net-{int(time.time() * 1000)}")
    body_dir.mkdir(parents=True, exist_ok=True)
    caps = []

    def on_resp(r):
        try:
            req = r.request
            rt = req.resource_type
            if types == "all":
                if rt in _STATIC_TYPES:
                    return
            elif rt not in types:
                return
            if flt and flt not in r.url:
                return
            caps.append(r)
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        trig = a.get("trigger") or {}
        if not isinstance(trig, dict):
            raise WbError('network_capture: trigger must be an object, e.g. {"navigate": url} | '
                          '{"reload": true} | {"click": selector} | {"evaluate": js}')
        if trig.get("navigate"):
            _guard(trig["navigate"])
            page.goto(trig["navigate"], wait_until="domcontentloaded", timeout=a.get("timeout", 45000))
        elif trig.get("reload"):
            page.reload(wait_until="domcontentloaded", timeout=a.get("timeout", 45000))
        elif trig.get("click"):
            page.locator(trig["click"]).first.click(timeout=a.get("timeout", 10000))
        elif trig.get("evaluate"):   # in the page's main world, like the evaluate action (patchright isolates)
            kw = {"isolated_context": False} if current_driver() == "patchright" else {}
            page.evaluate(trig["evaluate"], **kw)
        page.wait_for_timeout(int(a.get("wait_ms", 3000)))
    finally:
        page.remove_listener("response", on_resp)
    out = []
    for i, r in enumerate(caps):
        req = r.request
        item = {"i": i, "method": req.method, "url": r.url[:2000], "status": r.status,
                "type": req.resource_type, "mime": (r.headers.get("content-type") or "")[:80],
                "req_headers": _safe_headers(req.headers)}
        pd = req.post_data
        if pd:
            item["post_data"] = pd[:4000]
            item["post_data_truncated"] = len(pd) > 4000
        try:
            b = r.body()
            ext = "json" if "json" in item["mime"] else "txt"
            p = body_dir / f"{i:03d}.{ext}"
            p.write_bytes(b)
            item["body_path"], item["body_bytes"] = str(p), len(b)
        except Exception as ex:   # redirects / evicted bodies
            item["body_error"] = str(ex).splitlines()[0][:120]
        out.append(item)
    return {"url": page.url, "count": len(out), "body_dir": str(body_dir), "requests": out}


def _network_capture_action(ctx, session, a):
    """network_capture needs a tab to record on. With trigger.navigate it is "open the page and record
    it", so a session without a live tab gets a new one (registered, so close_session closes it);
    any other trigger acts on the current tab and still raises NoTab if there is none."""
    trig = a.get("trigger") or {}
    url = trig.get("navigate") if isinstance(trig, dict) else None
    if not url:
        return _network_capture(_current(ctx, session), a)
    _guard(url)                                  # before any tab is opened
    m, e, pages = _session_pages(ctx, session, current_only=True)
    page = next((p for t, p in pages if t == e["current"]), None)
    if page is None:
        page = ctx.new_page()
        _add(m, e, target_id(page), session)
    else:
        _save(m, session)
    return _network_capture(page, a)


def _cdp(page, a):
    s = page.context.new_cdp_session(page)
    try:
        return s.send(a["method"], a.get("params") or {})
    finally:
        s.detach()


def _list_tabs(ctx, session, a):
    m, e, pages = _session_pages(ctx, session)
    _save(m, session)
    return {"success": True, "tabs": [_tab_info(t, p, e["current"], e) for t, p in pages]}


def _list_browser_tabs(ctx, session, a):
    return {"success": True, "tabs": [_tab_info(t, p) for t, p in _pages_by_tid(ctx).items()]}


def _find_tab(ctx, session, a):
    url = a.get("url") or ""
    m, e, pages = _session_pages(ctx, session)
    hit = next(((t, p) for t, p in pages if p.url.startswith(url)), None)
    borrowed = False
    if hit is None and a.get("active"):   # borrow a tab this session did not open
        hit = _find_any(ctx, lambda u: u.startswith(url))
        borrowed = hit is not None
    if hit is None:
        _save(m, session)
        raise NoTab(f"find_tab: no tab matching {url!r}")
    _add(m, e, hit[0], session, borrowed=borrowed or hit[0] in e["borrowed"])
    return {"success": True, "url": hit[1].url, "tabId": hit[0], "tab": e["ids"][hit[0]],
            "borrowed": hit[0] in e["borrowed"]}


def _find_any(ctx, match):
    """(targetId, page) of the first page in the whole browser whose URL satisfies match(url), or None.
    Uses the target list to pick the candidate, so only that page is resolved."""
    live = _live_targets(ctx)
    if live is None:
        return next(((t, p) for t, p in _pages_by_tid(ctx).items() if match(p.url)), None)
    for p in ctx.pages:                      # page order (as before), cheap ones first: already resolved
        t = cached_target_id(p)
        if t is not None and t in live and match(p.url):
            return t, p
    for t, info in live.items():
        if match(info.get("url") or ""):
            p = _page_of(ctx, t, info.get("url"))
            if p is not None and match(p.url):
                return t, p
    return None


def _attach_tab(ctx, session, a):
    m, e, _ = _session_pages(ctx, session, current_only=True)
    tid = resolve_tab_ref(e, a.get("tabId"))
    if tid is None:
        _save(m, session)
        raise NoTab(f"attach_tab: session {session!r} has no tab {a.get('tabId')!r}")
    live = _live_targets(ctx)
    if live is None:
        page = _pages_by_tid(ctx).get(tid)
    else:
        page = _page_of(ctx, tid, live[tid].get("url")) if tid in live else None
    if page is None:
        _save(m, session)
        raise NoTab(f"attach_tab: no tab {tid!r}")
    mine = tid in e["tabs"] and tid not in e["borrowed"]
    _add(m, e, tid, session, borrowed=not mine)
    return {"success": True, "tabId": tid, "tab": e["ids"][tid], "url": page.url, "title": page.title(),
            "borrowed": not mine}


def _close_session(ctx, session, a):
    m, e, pages = _session_pages(ctx, session)
    closed = 0
    for t, p in pages:
        if t in e["borrowed"]:
            continue
        try:
            p.close()
            closed += 1
        except Exception:
            pass
    m.pop(session, None)
    _save(m, session)
    _forget_refs(session)
    return {"success": True, "closed": closed}


_JS_TEXT_PRESENT = "t => !!document.body && document.body.innerText.includes(t)"


def _wait_text_any_frame(page, text, timeout):
    """Poll every frame of the page (re-read each round: frames come and go) for `text`. -> found?"""
    end = time.monotonic() + timeout / 1000
    while True:
        for f in page.frames:
            try:
                if f.evaluate(_JS_TEXT_PRESENT, text):
                    return True
            except Exception:
                pass                     # navigating / detached: next round
        if time.monotonic() >= end:
            return False
        page.wait_for_timeout(100)


WAIT_KEYS = ("text", "selector", "js", "url")


def wait_condition(a, keys=WAIT_KEYS, what="wait"):
    """The one condition of a wait / expect / until object -> (key, value). url_contains is a synonym of
    url (a substring of the tab's URL or of one of its frames' URLs)."""
    if not isinstance(a, dict):
        raise WbError(f"{what}: give an object with one of {', '.join(keys)}")
    a = dict(a)
    if a.get("url_contains") not in (None, "") and a.get("url") in (None, ""):
        a["url"] = a["url_contains"]
    given = [k for k in keys if a.get(k) not in (None, "")]
    if len(given) != 1:
        raise WbError(f"{what}: give exactly one of {', '.join(keys)}")
    return given[0], a[given[0]]


def _url_in(page, sub):
    try:
        if sub in page.url:
            return True
        return any(sub in (f.url or "") for f in page.frames)
    except Exception:
        return False


def condition_now(page, key, val):
    """One non-blocking look at a wait condition (handoff's until). Errors (a page between documents) = False."""
    try:
        if key == "url":
            return _url_in(page, str(val))
        if key == "text":
            for f in page.frames:
                try:
                    if f.evaluate(_JS_TEXT_PRESENT, str(val)):
                        return True
                except Exception:
                    pass
            return False
        if key == "selector":
            loc = page.locator(_sel(page, val)).first
            return bool(loc.count()) and loc.is_visible()
        return bool(page.evaluate(val, **_main_world()))
    except WbError:
        raise
    except Exception:
        return False


def _wait(page, a):
    """Wait for ONE condition: text (in document.body.innerText), selector (state, default visible), js
    (an expression / function that becomes truthy) or url (a substring of the tab's URL or of one of its
    frames' URLs; url_contains is the same). A timeout is an answer, not an exception, so a batch can decide
    whether to go on; a broken selector or JS is a WbError."""
    key, val = wait_condition(a)
    given = [key]
    timeout = a.get("timeout", 15000)
    t0 = time.monotonic()
    if key == "url":                     # page.url / frame urls: no page JS, survives navigations
        end = t0 + timeout / 1000
        while not _url_in(page, str(val)):
            if time.monotonic() >= end:
                return {"success": False, "waited_ms": int((time.monotonic() - t0) * 1000)}
            page.wait_for_timeout(100)
        return {"success": True, "waited_ms": int((time.monotonic() - t0) * 1000)}
    target = page
    if a.get("frame"):
        try:
            target = _resolve_frame(page, a["frame"], timeout)[0]
        except WbError as ex:
            return {"success": False, "waited_ms": int((time.monotonic() - t0) * 1000), "hint": str(ex)}
        timeout = max(timeout - (time.monotonic() - t0) * 1000, 1)
    try:
        if given[0] == "text" and target is page and isinstance(getattr(page, "frames", None), list):
            if not _wait_text_any_frame(page, str(a["text"]), timeout):
                return {"success": False, "waited_ms": int((time.monotonic() - t0) * 1000)}
        elif given[0] == "text":
            target.wait_for_function(_JS_TEXT_PRESENT, arg=str(a["text"]), timeout=timeout, polling=100)
        elif given[0] == "selector":
            wsel = _sel(page, a["selector"]) if target is page else a["selector"]
            target.wait_for_selector(wsel, state=a.get("state", "visible"), timeout=timeout)
        else:
            target.wait_for_function(a["js"], timeout=timeout, polling=100)
    except Exception as ex:
        waited = int((time.monotonic() - t0) * 1000)
        if type(ex).__name__ == "TimeoutError":
            return {"success": False, "waited_ms": waited}
        raise WbError(f"wait: {str(ex).splitlines()[0][:200]}") from None
    return {"success": True, "waited_ms": int((time.monotonic() - t0) * 1000)}


def _batch(ctx, session, a):
    """Run steps [{action, args, quiet?}] in this one process (one driver start, one CDP connection)
    through wb(), so guards and session mapping behave exactly as for single calls."""
    steps = a.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WbError("batch: steps must be a non-empty list of {action, args}")
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not isinstance(s.get("action"), str):
            raise WbError(f"batch: step {i} needs an action")
        if s["action"] == "batch":
            raise WbError(f"batch: step {i}: nested batch is not allowed")
        if s.get("args") is not None and not isinstance(s["args"], dict):
            raise WbError(f"batch: step {i}: args must be an object")
    stop = a.get("stop_on_error", True)
    default_dialog = a.get("dialog")          # batch-level policy for steps without their own
    results, failed = [], False
    for s in steps:
        act = s["action"]
        if failed and stop:
            results.append({"action": act, "ok": False, "skipped": True})
            continue
        t0 = time.monotonic()
        sargs = s.get("args") or {}
        if default_dialog is not None and "dialog" not in sargs:
            sargs = {**sargs, "dialog": default_dialog}
        try:
            data, err = wb(act, sargs, session=session), None
            if isinstance(data, dict) and data.get("success") is False:   # e.g. wait timed out
                err = f"{act}: timed out after {data.get('waited_ms')} ms" if act == "wait" \
                    else f"{act}: success is false"
        except Exception as ex:   # a failing step is a result, whatever it raised
            first = (str(ex).splitlines() or [""])[0][:300]
            data, err = None, first if isinstance(ex, WbError) else f"{type(ex).__name__}: {first}"
        secs = round(time.monotonic() - t0, 3)
        if err is not None:
            failed = True
            row = {"action": act, "ok": False, "error": err, "secs": secs}
            if data is not None:   # success:false still says why (e.g. captcha_text attempts / last read)
                row["data"] = data
            results.append(row)
        elif s.get("quiet"):
            results.append({"ok": True, "secs": secs})
        else:
            results.append({"action": act, "ok": True, "data": data, "secs": secs})
    return {"ok": not failed, "results": results}


def _dialog_action(ctx, session, a):
    """Status: the session's recent dialogs (this thread / daemon worker: handled ones, newest last)
    and pending ones (handling failed). Dialogs on the session's tabs are handled the moment they open,
    so pending is normally empty."""
    st = _tls
    return {"success": True, "recent": list(st.recent.get(session, [])),
            "pending": [p for p in st.pending if p.get("session") == session]}


def _captcha_text(page, a):
    """Text-image CAPTCHA, offline ddddocr. Reads the image's own pixels (an element screenshot gets
    cropped/rescaled), re-reads a wrong-length answer, fills `input`. With `submit` (+ `ok`, a JS
    expression that is truthy once accepted) it also submits and retries up to `tries` times,
    refilling `fields` ({selector: value}) each time; their values are never echoed back."""
    from . import textcaptcha
    img = a.get("image")
    if not img:
        raise WbError("captcha_text: image (selector of the CAPTCHA <img>) required")
    kw = dict(case=a.get("case"), digits_only=bool(a.get("digits_only")), length=a.get("length"),
              refresh_selector=a.get("refresh"), charset=a.get("charset"), preprocess=a.get("preprocess"),
              min_confidence=a.get("min_confidence"))
    if kw["case"] not in (None, "upper", "lower"):
        raise WbError("captcha_text: case must be upper, lower or omitted")
    if kw["charset"] is not None and not (isinstance(kw["charset"], str) and kw["charset"]):
        raise WbError("captcha_text: charset must be a non-empty string of the allowed characters")
    if kw["preprocess"] not in (None, *textcaptcha.PREPROCESS):
        raise WbError(f"captcha_text: preprocess must be one of {textcaptcha.PREPROCESS} or omitted")
    mc = kw["min_confidence"]
    if mc is not None and not (isinstance(mc, (int, float)) and 0 <= mc <= 1):
        raise WbError("captcha_text: min_confidence must be a number from 0 to 1")
    try:
        if a.get("submit"):
            if not a.get("input"):
                raise WbError("captcha_text: submit needs input")
            r = textcaptcha.solve(page, img, a["input"], tries=int(a.get("tries", 6)),
                                  submit_selector=a["submit"], ok_check=a.get("ok"),
                                  fields=a.get("fields"), **kw)
            return {"success": bool(r["solved"]), **r}
        r = textcaptcha.read_code(page, img, a.get("input"), **kw)
    except RuntimeError as ex:
        raise WbError(f"captcha_text: {ex}")
    if a.get("input"):
        page.fill(a["input"], r["code"], timeout=5000)
    return {"success": r["plausible"], "filled": bool(a.get("input")), **r}


# ---------- handoff: give the tab to the human for a step, then continue ----------

HANDOFF_TIMEOUT = 600000
HANDOFF_POLL_MS = 250
HANDOFF_BUTTON = "Done — hand back to the agent"
HANDOFF_TAG = "deja-handoff"
HANDOFF_UNTIL = ("text", "selector", "js", "url")

# The overlay: a NEW element appended to <html> (outside <body>, so read / wait {text} never see it) holding a
# CLOSED shadow root; built with createElement / textContent / CSSOM (no markup: works under CSP and Trusted
# Types, and the message can't inject anything). State lives in this JS world under o.key (under patchright
# that is an isolated world the page can't see). Called on every poll: re-creates the overlay after a
# navigation, else returns whether the button was clicked. Shows on <frameset> pages too (Edge 158, fixture).
_JS_HANDOFF = r"""(o) => {
  const de = document.documentElement;
  if (!de) return {shown: false};
  const st = window[o.key];
  if (st && st.host.isConnected) return {shown: true, done: st.done};
  const el = (tag, css, text) => { const e = document.createElement(tag); Object.assign(e.style, css);
    if (text !== undefined) e.textContent = text; return e; };
  const host = el(o.tag, {all: 'initial', position: 'fixed', zIndex: '2147483647', right: '16px', bottom: '16px',
    width: '340px', display: 'block'});
  const root = host.attachShadow({mode: 'closed'});
  const box = el('div', {boxSizing: 'border-box', width: '340px', padding: '12px', background: '#fffbe6',
    color: '#111', border: '2px solid #b45309', borderRadius: '8px', boxShadow: '0 4px 16px rgba(0,0,0,.25)',
    font: '14px/1.4 system-ui, sans-serif'});
  box.append(el('div', {fontWeight: '600', marginBottom: '6px'}, o.title),
             el('div', {whiteSpace: 'pre-wrap', marginBottom: '10px', maxHeight: '200px', overflow: 'auto'}, o.message));
  const btn = el('button', {display: 'block', width: '100%', height: '40px', margin: '0', border: '0',
    borderRadius: '6px', background: '#1d4ed8', color: '#fff', font: '600 14px system-ui, sans-serif',
    cursor: 'pointer'}, o.button);
  btn.type = 'button';
  const state = {host, done: false};
  btn.addEventListener('click', ev => { ev.stopPropagation(); state.done = true; btn.disabled = true;
    btn.textContent = o.after; });
  box.append(btn);
  root.append(box);
  de.appendChild(host);
  window[o.key] = state;
  return {shown: true, done: false};
}"""
_JS_HANDOFF_REMOVE = "(k) => { const st = window[k]; if (st) { st.host.remove(); delete window[k]; } return true; }"


def parse_handoff(a):
    """handoff args -> (message, (until key, value) | None, timeout ms). WbError on bad args."""
    msg = a.get("message")
    if not isinstance(msg, str) or not msg.strip():
        raise WbError("handoff: message required (what the human should do in the tab)")
    until = a.get("until")
    cond = None
    if until is not None:
        cond = wait_condition(until, HANDOFF_UNTIL, "handoff: until")
        if cond[0] != "js":
            cond = (cond[0], str(cond[1]))
    timeout = a.get("timeout", HANDOFF_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        raise WbError("handoff: timeout must be a number of milliseconds >= 0")
    return msg.strip()[:2000], cond, int(timeout)


def _handoff(ctx, session, a):
    """Bring the session's current tab to the front, notify the human, show a 'hand back' button in the page
    and wait for: the button, the `until` condition, or the timeout. Dialogs on the session's tabs are still
    handled at once (this action's `dialog` policy, else the default): a dialog left open would block every
    other connection to the browser."""
    msg, cond, timeout = parse_handoff(a)
    page = _current(ctx, session)
    t0 = time.monotonic()

    def done(how, extra=None):
        out = {"success": how != "timeout", "how": how, "waited_ms": int((time.monotonic() - t0) * 1000),
               "url": page.url}
        out.update(extra or {})
        return out
    if cond is not None and condition_now(page, *cond):
        return done("condition")
    import secrets as _secrets
    from . import notify
    key = "__dejaHandoff" + _secrets.token_hex(8)
    o = {"key": key, "tag": HANDOFF_TAG, "title": "deja-browser: your turn", "message": msg,
         "button": HANDOFF_BUTTON, "after": "Handed back"}
    try:
        page.bring_to_front()                       # CDP Page.bringToFront
    except Exception:
        pass
    try:
        title = page.title()
    except Exception:
        title = ""
    host = urllib.parse.urlsplit(page.url).hostname or page.url[:40]
    note = (f"{msg}\nTab \"{title[:60]}\" ({host}); waiting up to {max(timeout // 60000, 1)} min. Press "
            f"\"{HANDOFF_BUTTON}\" in the page when finished.")
    threading.Thread(target=notify.notify, args=("deja-browser: your turn", note), daemon=True,
                     name="deja-handoff-notify").start()
    end = t0 + timeout / 1000
    how, shown = "timeout", False
    try:
        while True:
            if _is_closed(page):
                raise WbError("handoff: the tab was closed during the hand-off")
            try:
                r = page.evaluate(_JS_HANDOFF, o) or {}
                shown = shown or bool(r.get("shown"))
                if r.get("done"):
                    how = "button"
                    break
            except Exception:
                pass                                 # between documents: the next tick re-creates it
            if cond is not None and condition_now(page, *cond):
                how = "condition"
                break
            left = end - time.monotonic()
            if left <= 0:
                break
            page.wait_for_timeout(int(min(HANDOFF_POLL_MS, left * 1000)) or 1)   # keeps driver events flowing
    finally:
        try:
            if not _is_closed(page):
                page.evaluate(_JS_HANDOFF_REMOVE, key)
        except Exception:
            pass
    extra = {"notified": notify.channel()}
    if not shown:
        extra["overlay"] = False
        extra["hint"] = "the hand-back button could not be shown on this page (only until / timeout could end it)"
    return done(how, extra)


SESSION_ACTIONS = {"navigate": _navigate, "network_capture": _network_capture_action,
                   "list_tabs": _list_tabs, "list_browser_tabs": _list_browser_tabs,
                   "find_tab": _find_tab, "attach_tab": _attach_tab, "close_session": _close_session,
                   "batch": _batch, "dialog": _dialog_action, "handoff": _handoff}
PAGE_ACTIONS = {"evaluate": _evaluate, "fill": _fill, "select": _select, "click": _click, "screenshot": _screenshot,
                "save_as_pdf": _save_as_pdf, "snapshot": _snapshot, "cdp": _cdp, "wait": _wait,
                "read": _read, "captcha_text": _captcha_text}


_MAY_OPEN = {"navigate", "click", "fill", "select", "evaluate", "cdp", "network_capture", "captcha_text",
             "wait", "handoff"}   # actions after which new popups are looked for (one Target.getTargets)


def _fill_popup_urls(ctx, page, popups, timeout=2.0):
    """A popup can be adopted while its target still has url "" (seen in-process with idle daemon workers
    attached to the browser: 1 of 3 acceptance runs). Poll Target.getTargets briefly for the url."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and any(not p.get("url") for p in popups):
        try:
            if page is not None:
                page.wait_for_timeout(100)       # keeps the driver's events flowing
            else:
                time.sleep(0.1)
        except Exception:
            time.sleep(0.1)
        targets = _live_targets(ctx) or {}
        for p in popups:
            if not p.get("url"):
                p["url"] = (targets.get(p["tabId"]) or {}).get("url") or ""


def _follow_popup(ctx, session, page, popups, a):
    """follow_popup: wait (popup_timeout, default 3000 ms) for a popup if none came yet; make the last one
    the session's current tab. -> popups found while waiting."""
    found = []
    if not popups:
        end = time.monotonic() + int(a.get("popup_timeout", 3000)) / 1000
        waiter = page
        if waiter is None:
            try:
                waiter = _current(ctx, session)
            except NoTab:
                waiter = None
        while time.monotonic() < end and not found:
            if waiter is not None:
                waiter.wait_for_timeout(100)     # keeps the driver's events flowing (no time.sleep)
            else:
                time.sleep(0.1)
            _adopt_now(ctx, session)
            found = _take_adopted(session)
    last = (found or popups)[-1:]
    if last:
        m = _load()
        e = _entry(m, session)
        if last[0]["tabId"] in e["tabs"]:
            e["current"] = last[0]["tabId"]
            _save(m, session)
    return found


THEN_SNAPSHOT = {"click", "fill", "select", "navigate", "batch", "handoff"}
THEN_SNAPSHOT_DEFAULT = {"interactive_only": True, "refs": True}


NAV_START_MS = 150         # then_snapshot: how long a click / fill / select has to start a navigation


class _NavWatch:
    """Navigation requests a page action starts (a link, a form submit, a postback inside a frame), so
    then_snapshot can wait for those frames to load instead of reading a document that is going away.
    Listens on the page from before the action until settle()."""

    def __init__(self, page):
        self.page, self.asked, self.landed = page, [], []
        self._req = lambda r: self._on_request(r)
        self._nav = lambda f: self.landed.append(f)
        self._fail = lambda r: self._on_failed(r)
        try:
            page.on("request", self._req)
            page.on("framenavigated", self._nav)
            page.on("requestfailed", self._fail)
            self.ok = True
        except Exception:
            self.ok = False

    def _on_request(self, r):
        try:
            if r.is_navigation_request():
                self.asked.append(r.frame)
        except Exception:
            pass

    def _on_failed(self, r):
        try:
            if r.is_navigation_request():
                self.landed.append(r.frame)
        except Exception:
            pass

    def settle(self, timeout_ms):
        """Give the action NAV_START_MS to start a navigation, then wait (up to timeout_ms) until every
        frame that asked for one has navigated and reached domcontentloaded. -> frames waited for."""
        if not self.ok:
            return 0
        try:
            end = time.monotonic() + timeout_ms / 1000
            self.page.wait_for_timeout(NAV_START_MS)
            while time.monotonic() < end and any(not any(f is g for g in self.landed) for f in self.asked):
                self.page.wait_for_timeout(50)
            for f in {id(f): f for f in self.asked}.values():
                try:
                    f.wait_for_load_state("domcontentloaded", timeout=max(int((end - time.monotonic()) * 1000), 1))
                except Exception:
                    pass                 # detached (the frame was replaced) / timed out: snapshot what is there
            return len(self.asked)
        finally:
            self.close()

    def close(self):
        for ev, fn in (("request", self._req), ("framenavigated", self._nav), ("requestfailed", self._fail)):
            try:
                self.page.remove_listener(ev, fn)
            except Exception:
                pass


def _then_snapshot(ctx, session, spec, watch=None):
    """then_snapshot: true (THEN_SNAPSHOT_DEFAULT) or an object of snapshot args, plus an optional
    wait {text | selector | js, timeout} run first. Taken on the session's current tab after the action
    (a followed popup included), once navigations the action started (watch) have loaded. A failure is
    reported, not raised: the action itself did happen."""
    sargs = dict(THEN_SNAPSHOT_DEFAULT) if spec is True else dict(spec) if isinstance(spec, dict) else None
    if sargs is None:
        return {"error": "then_snapshot: true or an object of snapshot args"}
    wait = sargs.pop("wait", None)
    try:
        if watch is not None:
            watch.settle(int(sargs.get("timeout", 10000)))
        page = _current(ctx, session)
        waited = None
        if wait:
            w = _wait(page, wait if isinstance(wait, dict) else {"text": str(wait)})
            waited = None if w.get("success") else w
        try:
            page.wait_for_load_state("domcontentloaded", timeout=int(sargs.get("timeout", 10000)))
        except Exception:
            pass
        try:
            out = _snapshot(page, sargs)
        except Exception as ex:          # the click started a navigation: the old document went away
            if not any(w in str(ex) for w in ("context was destroyed", "navigat", "Target closed", "detached")):
                raise
            page = _current(ctx, session)
            page.wait_for_load_state("domcontentloaded", timeout=int(sargs.get("timeout", 10000)))
            out = _snapshot(page, sargs)
        if waited is not None:
            out["wait"] = waited
        return out
    except Exception as ex:
        return {"error": _first_line(ex) if isinstance(ex, WbError) else f"{type(ex).__name__}: {_first_line(ex)}"}


def wb(action, args=None, session="default"):
    """Run one Kimi-style action. Returns the `data` payload; raises WbError / NoTab on failure.
    Every action takes `dialog` (policy for a dialog it raises) and reports one under `dialog`."""
    args = args or {}
    fn = SESSION_ACTIONS.get(action) or PAGE_ACTIONS.get(action)
    if fn is None:
        raise WbError(f"unsupported action: {action}")
    policy = parse_dialog_policy(args.get("dialog"))
    st = _tls
    st.session = session
    outer = st.scope is None
    ctx = _ctx()
    # page actions resolve the tab first: that CDP round trip also delivers dialog events buffered since
    # the last call, so they count as late (default policy), not as this action's
    page = _current(ctx, session) if action in PAGE_ACTIONS else None
    if page is None and outer and st.dialog_hook is None:
        _live_targets(ctx)
    scope = _Scope(session, policy)
    watch = _NavWatch(page) if page is not None and args.get("then_snapshot") and action in THEN_SNAPSHOT else None
    prev, st.scope = st.scope, scope
    try:
        data = fn(page, args) if action in PAGE_ACTIONS else fn(ctx, session, args)
    except BaseException as ex:
        if watch is not None:
            watch.close()                # the action failed: no snapshot, take the listeners off
        if isinstance(ex, WbError) and scope.dialogs:
            ex.args = (f"{ex} (dialog during this action: {_dialog_note(scope.dialogs)})",)
        raise
    finally:
        st.scope = prev
    if action in _MAY_OPEN:
        _adopt_now(ctx, session)
    popups = _take_adopted(session)
    follow = bool(args.get("follow_popup")) and action in _MAY_OPEN
    if follow:
        popups += _follow_popup(ctx, session, page, popups, args)
    if popups and any(not p.get("url") for p in popups):
        _fill_popup_urls(ctx, page, popups)
    if isinstance(data, dict):
        if popups:
            data["popups"] = popups
        if follow:
            data["followed"] = popups[-1]["tab"] if popups else None
        if args.get("then_snapshot") and action in THEN_SNAPSHOT:
            data["snapshot"] = _then_snapshot(ctx, session, args["then_snapshot"], watch)
        if scope.dialogs:
            rc = _receipt(scope.dialogs)
            data["dialog"] = rc[0]
            if len(rc) > 1:
                data["more_dialogs"] = rc[1:]
        if outer and st.late_in_result and st.sink:
            data["dialogs"], st.sink = st.sink, []
    return data


class _AutoLearn:
    """close_session's auto-learn (recipe.auto_learn_prepare / _finish): called with no args after a
    successful close -> {"learned": ...} or None; never raises (learning must not break a close)."""

    def __init__(self, prep):
        self.prep, self.done = prep, False

    def __call__(self):
        self.done = True
        try:
            from . import recipe
            learned = recipe.auto_learn_finish(self.prep)
        except Exception as ex:
            learned = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:200]}
        return {"learned": learned} if learned is not None else None

    def discard(self):
        import shutil
        if isinstance(self.prep, dict) and self.prep.get("tmp"):
            shutil.rmtree(self.prep["tmp"], ignore_errors=True)


def _auto_learn_hook(session):
    try:
        from . import recipe
        prep = recipe.auto_learn_prepare(session)
    except Exception as ex:
        return {"learned": {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:200]}}
    return None if prep is None else _AutoLearn(prep)


def _main_via_daemon(cmd, args, session, extra=None):
    """CLI action through the daemon -> exit code (output printed), or None to run in-process. `extra` is
    merged into a successful dict reply (close_session's `learned`)."""
    from . import client
    try:
        rep = client.cli_call(cmd, args, session, driver=os.environ.get("EDGE_CDP_DRIVER") or None)
    except client.LostReply as ex:
        print(json.dumps({"ok": False, "error": f"lost the daemon's reply ({ex}); the action may or may not "
                          "have run: check the page before retrying"}, ensure_ascii=False))
        return 1
    if rep is None:
        return None
    dialogs = rep.get("dialogs")
    if not rep.get("ok"):
        if rep.get("kind") == "Exception":      # in-process this would be an uncaught traceback
            print(rep.get("traceback") or rep.get("error"), file=sys.stderr)
            return 1
        out = {"ok": False, "error": rep.get("error")}
        if dialogs:
            out["dialogs"] = dialogs
        print(json.dumps(out, ensure_ascii=False))
        return 1
    out = rep.get("data")
    if dialogs and isinstance(out, dict):
        out = {**out, "dialogs": dialogs}
    if callable(extra):
        extra = extra()
    if extra and isinstance(out, dict):
        out = {**out, **extra}
    print(json.dumps(out, ensure_ascii=False))
    return 1 if cmd == "batch" and isinstance(out, dict) and out.get("ok") is False else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    session = "cli"
    if "--session" in argv:
        i = argv.index("--session")
        session = argv[i + 1]
        del argv[i:i + 2]
    from . import clihelp
    if clihelp.wants_help(argv):
        text, rc = clihelp.cdp_help(clihelp.topic_of(argv))
        print(text)
        return rc if argv else 2
    cmd = argv[0]
    if cmd in SESSION_ACTIONS or cmd in PAGE_ACTIONS:
        if len(argv) > 2:
            raise SystemExit(f"deja-cdp: unexpected arguments {argv[2:]} (usage: <action> '<json>' [--session S])")
        args = json.loads(argv[1]) if len(argv) > 1 else {}
        extra = None
        if cmd == "close_session":     # auto-learn: recording fetched before the close, learned after it
            want = args.pop("learn", True) if isinstance(args, dict) else True
            if want is not False:
                extra = _auto_learn_hook(session)
        r = _main_via_daemon(cmd, args, session, extra)
        if isinstance(extra, _AutoLearn) and not extra.done:
            extra.discard()
        if r is not None:
            return r
        try:
            out = wb(cmd, args, session=session)
        except WbError as ex:    # NoTab included; other exceptions are program bugs and keep their traceback
            print(json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False))
            return 1
        print(json.dumps(out, ensure_ascii=False))
        return 1 if cmd == "batch" and out.get("ok") is False else 0
    if cmd == "launch":
        v, ours = launch()
        out = {"browser": v.get("Browser"), "launched": ours, "profile": str(PROFILE), "port": PORT}
    elif cmd == "status":
        _adopt_saved_port()
        v = version()
        out = {"up": bool(v), "browser": (v or {}).get("Browser"), "profile": str(PROFILE), "port": PORT}
    elif cmd == "stop":
        from . import client
        client.disconnect_all()   # daemon workers let go of the browser first
        out = stop()
    else:
        if len(argv) > 2:
            raise SystemExit(f"deja-cdp: unexpected arguments {argv[2:]} (usage: <action> '<json>' [--session S])")
        try:
            out = wb(cmd, json.loads(argv[1]) if len(argv) > 1 else {}, session=session)
        except WbError as ex:    # NoTab included; other exceptions are program bugs and keep their traceback
            print(json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False))
            return 1
    print(json.dumps(out, ensure_ascii=False))
    return 1 if cmd == "batch" and out.get("ok") is False else 0


if __name__ == "__main__":
    sys.exit(main())
