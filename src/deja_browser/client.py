# -*- coding: utf-8 -*-
"""Thin client for the deja-browser daemon (daemon.py). Stdlib only: no Playwright import, so a CLI call
costs a Python start plus one loopback round trip instead of starting a Playwright driver.

  call(action, args, session, driver) -> reply {ok, data | error+kind, dialogs?}
      Unavailable  the request was NOT delivered (no daemon and none could be started, connection
                   refused, token refused, the daemon refused the job): the caller may run in-process.
      LostReply    the request WAS sent and the connection broke before the reply: never re-run it
                   (a click or a writing recipe would happen twice).

One daemon per (checkout, config file, data dir, configured browser port): state file
<data dir>/daemon/<key>.json = {port, token, pid, version, code_hash, key}, readable only by the user.
code_hash covers the package's source files (mtime + size), its version and the config file, so an
edited checkout or config gets a fresh daemon on the next call. DEJA_BROWSER_DAEMON=0 turns it off.
"""
import hashlib, json, os, socket, subprocess, sys, time
from pathlib import Path

from . import __version__, config

PKG = Path(__file__).resolve().parent
SRC = PKG.parent
SPAWN_WAIT = 5.0           # seconds to wait for a freshly started daemon's state file
CONNECT_TIMEOUT = 2.0      # connecting to the loopback port; the reply itself has no timeout
MAX_LINE = 64 << 20


class Unavailable(Exception):
    """Not delivered: running the action in-process instead is safe. kind "refused" = a live daemon
    declined this job (e.g. a recipe that needs secrets); anything else = no usable daemon."""

    def __init__(self, msg, kind="down"):
        super().__init__(msg)
        self.kind = kind


class LostReply(Exception):
    """Delivered, but no reply: the action may or may not have run."""


def enabled(env=None):
    env = os.environ if env is None else env
    return (env.get("DEJA_BROWSER_DAEMON") or "1").strip().lower() not in ("0", "false", "no", "off")


def config_port():
    return int(config.get("browser", "port", 9222))


def key():
    ident = "|".join(os.path.normcase(str(p)) for p in (PKG, config.config_path(), config.data_dir()))
    return f"{hashlib.sha1(ident.encode('utf-8')).hexdigest()[:12]}-{config_port()}"


def state_dir():
    return config.data_dir() / "daemon"


def state_path(k=None):
    return state_dir() / f"{k or key()}.json"


def code_hash():
    """Changes when any package source file, the version or the config file changes."""
    h = hashlib.sha1(__version__.encode())
    for p in sorted(PKG.glob("*.py")):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
    cp = config.config_path()
    try:
        st = cp.stat()
        h.update(f"cfg:{cp}:{st.st_mtime_ns}:{st.st_size}".encode())
    except OSError:
        h.update(f"cfg:{cp}:-".encode())
    return h.hexdigest()[:16]


# ---------- framing: one JSON object per line ----------

def encode(obj):
    return (json.dumps(obj, ensure_ascii=True, separators=(",", ":"), default=str) + "\n").encode("ascii")


def decode(line):
    if not line.endswith(b"\n"):
        raise ValueError("incomplete line")
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


# ---------- state file ----------

def read_state(path=None):
    try:
        d = json.loads(Path(path or state_path()).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("port") and d.get("token") else None
    except (OSError, ValueError):
        return None


def write_state(path, data):
    """Atomic write, mode 0600 (POSIX; on Windows the per-user data dir is already private)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.02 * (i + 1))


def remove_state(path, pid):
    """Delete the state file only if it still belongs to `pid`."""
    st = read_state(path)
    if st and st.get("pid") == pid:
        try:
            Path(path).unlink()
        except OSError:
            pass


# ---------- request ----------

_PATH_ARGS = {"screenshot": ("path",), "save_as_pdf": ("path",), "network_capture": ("body_dir",)}


def absolutize(action, args, cwd=None):
    """The daemon's working directory is not the caller's: make relative output paths absolute here
    (batch steps included)."""
    if not isinstance(args, dict):
        return args
    cwd = cwd or os.getcwd()
    out = dict(args)
    for k in _PATH_ARGS.get(action, ()):
        v = out.get(k)
        if isinstance(v, str) and v and not os.path.isabs(os.path.expanduser(v)):
            out[k] = os.path.join(cwd, v)
    if action == "batch" and isinstance(out.get("steps"), list):
        out["steps"] = [dict(s, args=absolutize(s.get("action"), s.get("args"), cwd))
                        if isinstance(s, dict) and isinstance(s.get("args"), dict) else s
                        for s in out["steps"]]
    return out


def _send(st, msg):
    try:
        s = socket.create_connection(("127.0.0.1", int(st["port"])), timeout=CONNECT_TIMEOUT)
    except OSError as ex:
        raise Unavailable(f"cannot connect to port {st.get('port')}: {ex.__class__.__name__}") from None
    with s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            s.sendall(encode({**msg, "token": st["token"]}))
        except OSError as ex:
            raise Unavailable(f"send failed: {ex.__class__.__name__}") from None
        s.settimeout(None)    # navigate allows 45 s, a challenge gate minutes: no reply timeout
        try:
            line = s.makefile("rb").readline(MAX_LINE)
        except OSError as ex:
            raise LostReply(f"connection broke: {ex.__class__.__name__}") from None
    if not line:
        raise LostReply("the daemon closed the connection without a reply")
    try:
        rep = decode(line)
    except ValueError:
        raise LostReply("unreadable reply") from None
    if rep.get("kind") in ("refused", "auth"):   # not executed: a job it won't run / not our daemon
        raise Unavailable(f"{rep['kind']}: {rep.get('error')}", rep["kind"])
    return rep


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259                              # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def shutdown(st, wait=5.0):
    """Ask a daemon to exit and wait until its process is gone. -> True if it is gone."""
    try:
        _send(st, {"action": "daemon.shutdown"})
    except (Unavailable, LostReply):
        pass
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not _pid_alive(st.get("pid")):
            return True
        time.sleep(0.05)
    return not _pid_alive(st.get("pid"))


def _python_for_daemon():
    if os.name == "nt":
        w = Path(sys.executable).with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return sys.executable


def spawn(k=None, want=None):
    """Start a detached daemon (no console window, std handles to DEVNULL so a caller that captures our
    output is not held open) and wait for its state file. -> state."""
    k, want = k or key(), want or code_hash()
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    code = (f"import sys; sys.path.insert(0, {str(SRC)!r}); "
            "from deja_browser import daemon; sys.exit(daemon.main())")
    cmd = [_python_for_daemon(), "-c", code]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=str(sd), env=dict(os.environ))
    try:
        if os.name == "nt":
            base = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
            try:
                subprocess.Popen(cmd, creationflags=base | 0x01000000, **kw)   # | CREATE_BREAKAWAY_FROM_JOB
            except OSError:
                subprocess.Popen(cmd, creationflags=base, **kw)                # the job forbids breakaway
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
    except OSError as ex:
        raise Unavailable(f"could not start the daemon: {ex}") from None
    end = time.monotonic() + SPAWN_WAIT
    path = state_path(k)
    while time.monotonic() < end:
        st = read_state(path)
        if st and st.get("code_hash") == want and _pid_alive(st.get("pid")):
            return st
        time.sleep(0.03)
    raise Unavailable(f"the daemon did not start within {SPAWN_WAIT:g} s (log: {sd / (k + '.log')})")


def call(action, args=None, session="cli", driver=None):
    """Send one request, starting (or restarting) the daemon when needed. See the module docstring."""
    if not enabled():
        raise Unavailable("DEJA_BROWSER_DAEMON=0")
    k, want = key(), code_hash()
    msg = {"action": action, "args": absolutize(action, args or {}), "session": session, "driver": driver}
    st = read_state(state_path(k))
    if st and st.get("code_hash") != want:
        shutdown(st)              # older code or config: replace it
        st = None
    if st:
        try:
            return _send(st, msg)
        except Unavailable as ex:
            if ex.kind == "refused":
                raise
    return _send(spawn(k, want), msg)


def live_state():
    """State of a running daemon for this checkout/config, or None (never starts one)."""
    st = read_state(state_path())
    return st if st and _pid_alive(st.get("pid")) else None


def call_running(action, args=None, session="cli"):
    """Send one request to the daemon that is running now; never starts or replaces one (a replaced
    daemon has lost its sessions' recordings). Raises Unavailable when none runs."""
    if not enabled():
        raise Unavailable("DEJA_BROWSER_DAEMON=0")
    st = live_state()
    if not st:
        raise Unavailable("no daemon is running")
    return _send(st, {"action": action, "args": args or {}, "session": session})


def disconnect_all():
    """Make a running daemon's workers drop their browser connections (before `stop`). Never starts one."""
    st = live_state() if enabled() else None
    if not st:
        return None
    try:
        return _send(st, {"action": "daemon.disconnect"}).get("data")
    except (Unavailable, LostReply):
        return None


def cli_call(action, args, session, driver=None, prog="deja-cdp"):
    """For the CLIs: the daemon's reply, or None to run in-process (with a note on stderr unless the
    daemon is switched off). LostReply propagates: the caller reports it, it does not re-run."""
    if not enabled():
        return None
    try:
        return call(action, args, session, driver)
    except Unavailable as ex:
        print(f"{prog}: daemon unavailable ({ex}); running in-process", file=sys.stderr)
        return None
