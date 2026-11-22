# -*- coding: utf-8 -*-
"""Keep the Playwright driver's output pipe drained while the Python side is not inside a call.

WHY (v0.4, bench/legacy/stall_probe.py): sync Playwright reads the node driver's stdout only while the
caller is inside a Playwright call. On Windows node makes stdout a BLOCKING pipe (lib/net.js: "Make stdout
and stderr blocking on Windows"), and asyncio's pipe holds about 8 kB, so a caller that stays connected and
idles (time.sleep, urlopen, waiting on a person) freezes its node driver after a page or two of browser
events. A frozen driver no longer answers Chromium, and Playwright attaches to every target with
waitForDebuggerOnStart, so every new tab and out-of-process frame in the browser stays paused until it
answers: other clients' navigations time out. (POSIX: node's pipe writes are asynchronous, no stall.)

The fix: the driver is started by Python's subprocess, and a thread reads its stdout as it comes and hands
the bytes to the event loop (call_soon_threadsafe). The loop parses them on the caller's next call, exactly
as before; a thread never touches a Playwright object. A second thread writes to the driver's stdin, so
send() never blocks (as with asyncio's writer). Memory: what the driver says while the caller idles is
buffered until its next call (a new tab with a page load is ~4 kB).

install(cm) swaps the PipeTransport the sync context manager builds for one subclass per driver package
(playwright / patchright; the class lives in the context manager's module up to playwright 1.58 and in
_impl/out_of_process.py from 1.65). Anything unexpected (another layout of the private module) leaves the stock
transport in place. ENABLED (default: Windows only) is read at connect time.
"""
import asyncio
import importlib
import os
import queue
import subprocess
import sys
import threading

ENABLED = sys.platform == "win32"
READ_CHUNK = 1 << 16
_LOCK = threading.Lock()
_DRAINED = {}          # context-manager module name -> our subclass


def _builder(cm):
    """The module whose global PipeTransport the driver connection is built from: the context manager's
    own module (playwright <= 1.58, patchright), or <package>._impl.out_of_process (playwright 1.65:
    `from playwright._impl.transport import PipeTransport` there). None when neither has one."""
    name = type(cm).__module__
    cands = [name, name.split(".")[0] + "._impl.out_of_process"]
    for n in cands:
        mod = sys.modules.get(n)
        if mod is None and n != name:
            try:
                mod = importlib.import_module(n)
            except Exception:
                mod = None
        if mod is not None and isinstance(getattr(mod, "PipeTransport", None), type):
            return mod
    return None


def install(cm):
    """cm: a sync_playwright() context manager (not started yet). -> True when the module that builds its
    transport now builds a drained one."""
    mod = _builder(cm)
    if mod is None:
        return False
    with _LOCK:
        cur = getattr(mod, "PipeTransport", None)
        mine = _DRAINED.get(mod.__name__)
        if mine is not None and cur is mine:
            return True
        if cur is None or not isinstance(cur, type):
            return False
        base_mod = sys.modules.get(cur.__module__)
        if base_mod is None or not all(hasattr(base_mod, n) for n in
                                       ("compute_driver_executable", "get_driver_env", "_get_stderr_fileno")):
            return False
        cls = type("DrainedPipeTransport", (cur,), {"connect": _connect, "_base_mod": base_mod, "_stock": cur})
        _DRAINED[mod.__name__] = cls
        mod.PipeTransport = cls
        return True


async def _connect(self):
    if not ENABLED:
        return await self._stock.connect(self)
    bm = self._base_mod
    try:
        exe = bm.compute_driver_executable()
    except Exception:
        exe = None
    if not (isinstance(exe, tuple) and len(exe) == 2):      # an older/newer layout: stock transport
        return await self._stock.connect(self)
    self._stopped_future = asyncio.Future()
    try:
        env = bm.get_driver_env()
        if getattr(sys, "frozen", False) or getattr(bm, "__compiled__", None):
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        kw = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kw["startupinfo"] = si
        popen = subprocess.Popen([str(exe[0]), str(exe[1]), "run-driver"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=bm._get_stderr_fileno(), env=env, bufsize=0, **kw)
    except Exception as exc:
        self.on_error_future.set_exception(exc)
        raise
    self._proc = DrainedProc(popen, asyncio.get_running_loop())
    self._output = self._proc.stdin


class _Writer:
    """stdin of the driver: write() queues, a thread writes in order; close() after what was queued."""

    _CLOSE = object()

    def __init__(self, f):
        self._f, self._q = f, queue.SimpleQueue()
        threading.Thread(target=self._run, name="deja-driver-stdin", daemon=True).start()

    def write(self, data):
        self._q.put(bytes(data))

    def close(self):
        self._q.put(self._CLOSE)

    def _run(self):
        broken = False
        while True:
            data = self._q.get()
            if data is self._CLOSE:
                break
            if broken:
                continue
            try:
                mv = memoryview(data)
                while mv:
                    n = self._f.write(mv)
                    mv = mv[n or 0:]
            except (OSError, ValueError):
                broken = True                 # the driver is gone: its stdout hits EOF and run() reports it
        try:
            self._f.close()
        except OSError:
            pass


class DrainedProc:
    """What PipeTransport.run() / send() use of the asyncio Process: stdout (a StreamReader), stdin
    (write / close), communicate(), pid."""

    def __init__(self, popen, loop):
        self._popen, self._loop, self.pid = popen, loop, popen.pid
        self.stdout = asyncio.StreamReader(limit=32768)
        self.stdin = _Writer(popen.stdin)
        self._exited = loop.create_future()
        self._buf, self._lock, self._posted = bytearray(), threading.Lock(), False
        self.pending = 0                      # bytes read from the driver, not yet handed to the loop
        threading.Thread(target=self._drain, name="deja-driver-stdout", daemon=True).start()

    def _drain(self):
        fd = self._popen.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, READ_CHUNK)
                if not chunk:
                    break
                with self._lock:
                    self._buf += chunk
                    self.pending = len(self._buf)
                    if self._posted:
                        continue
                    self._posted = True
                self._post(self._flush)
        except OSError:
            pass
        finally:
            try:
                code = self._popen.wait()
            except Exception:
                code = None
            self._post(self._eof, code)

    def _post(self, fn, *a):
        try:
            self._loop.call_soon_threadsafe(fn, *a)
        except RuntimeError:                  # loop closed: nobody listens any more
            pass

    def _flush(self):
        with self._lock:
            data = bytes(self._buf)
            self._buf.clear()
            self.pending, self._posted = 0, False
        if data:
            self.stdout.feed_data(data)

    def _eof(self, code):
        self._flush()
        self.stdout.feed_eof()
        if not self._exited.done():
            self._exited.set_result(code)

    async def communicate(self):
        await self._exited
        # PipeTransport.run() returns right after this, and in sync mode that stops the event loop. A call
        # still waiting (asyncio.wait on on_error_future, set a moment ago by run()) needs a few more loop
        # turns to see the error; without them it waits forever (`_sync` spins on a stopped loop: a driver
        # that crashed hung its caller, bench/legacy/README.md). asyncio's own Process gets those turns
        # while it waits for the exit notification.
        await asyncio.sleep(0.05)
        return None, None
