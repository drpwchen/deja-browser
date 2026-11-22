# -*- coding: utf-8 -*-
"""deja-browser daemon: keeps Playwright drivers and CDP connections warm between CLI calls.

WHY (v0.4, DESIGN.md §7): a CLI call used to start Python, spawn a Playwright node driver (0.3–0.5 s)
and reconnect over CDP for an action that takes ~0.05 s. The daemon holds the connections; the CLI
(client.py, stdlib only) sends one JSON line and reads one back.

  - Listens on 127.0.0.1, random port. Every request carries the token from the state file
    (<data dir>/daemon/<key>.json, user-only); a wrong token is refused.
  - One worker thread per (session, driver), each with its own sync Playwright driver and connection
    (browser.py keeps that state per thread). A long wait in one session does not hold up another;
    requests within a session run in order.
  - Dialogs: each worker listens on its browser context (browser.handle_dialog). A dialog on a tab of
    ITS session, or on a popup one of them opened, is handled at once: by the running action's `dialog`
    policy (the action's reply carries a `dialog` receipt), otherwise the default (alert and
    beforeunload accepted, confirm and prompt dismissed); every one is also listed under `dialogs` in
    the reply that follows. A dialog on any other tab is left alone (the user's, or another session's
    worker handles it).
  - Between requests every connected worker keeps its connection's event loop running (woken at once
    by a new request). Two reasons, both seen live: a dialog that appears after a call returned is
    still handled (a pending dialog blocks every new CDP connection to the browser), and a connection
    whose Python side does not read its driver's events stalls the whole browser once other tabs are
    busy: new tabs' navigations and new connections hang (seen with an idle daemon worker, and with a
    separate in-process caller sleeping in time.sleep while connected; the node driver was blocked
    writing events to its pipe — backpressure is the likely mechanism, not proven).
  - A worker drops its connection after WORKER_IDLE s without requests (disconnect only: the browser
    is never closed); the daemon exits after DAEMON_IDLE s. A browser restart is noticed and the
    worker reconnects.
  - Never resolves secrets: recipes with {secret:…} or a login block run in-process (the client
    routes them; the worker refuses them too), and secretstore.get is disabled in this process.
  - Records each session's first visit for `deja-browser learn` (recording.py: the response rule and
    the schema): every action a worker runs, and the finished document / xhr / fetch responses on the
    session's own tabs. Memory only; `close_session` clears it; daemon action `recording`.
"""
import asyncio, hmac, importlib.util, json, os, queue, secrets, socketserver, sys, threading, time, traceback
from datetime import datetime

from . import __version__, browser as B, client, recording as R

WORKER_IDLE = 600     # s without requests -> the worker drops its connection and ends
DAEMON_IDLE = 1800    # s without requests -> the daemon exits
PUMP_SLICE = 5.0      # s per event-loop slice while idle (a new request wakes it at once)
LOCK_WAIT = 5.0       # s to wait for an exiting daemon's lock


def default_driver():
    return "patchright" if importlib.util.find_spec("patchright") else "playwright"


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _refuse_secret(name, *a, **kw):
    raise RuntimeError("deja-browser daemon: secrets are never resolved in the daemon; "
                       "this run must happen in-process")


class Refused(Exception):
    """The worker will not run this job; the client falls back to in-process (nothing was executed)."""


class Worker(threading.Thread):
    def __init__(self, d, session, driver):
        super().__init__(name=f"deja-worker:{session}:{driver}", daemon=True)
        self.d, self.session, self.driver = d, session, driver
        self.q = queue.Queue()
        self.dialogs = []
        self.last = time.monotonic()
        self.retiring = False
        self._wake = None        # asyncio.Event of the current idle pump, set from other threads
        self._loop = None
        self._watched = {}       # id(page) -> (page, requestfinished handler): pages we record on
        self._own = {}           # id(page) -> (page, owned?, monotonic time decided)

    # ----- called from server threads -----

    def submit(self, job):
        self.q.put(job)
        w, loop = self._wake, self._loop
        if w is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(w.set)
            except RuntimeError:
                pass

    # ----- the worker thread -----

    def run(self):
        B.use_driver(self.driver)
        st = B._tls
        st.dialog_hook, st.session, st.late_in_result = self._on_dialog, self.session, False
        st.on_connect = self._on_connect
        try:
            while True:
                job = self._next()
                if job is None:
                    if time.monotonic() - self.last > WORKER_IDLE and self.d.retire(self):
                        break
                    continue
                if job["action"] == "worker.exit":
                    self._finish(job, {"ok": True, "data": {"exited": True}})
                    break
                self._do(job)
                self.last = time.monotonic()
        finally:
            B.disconnect()
            while True:                              # nothing may wait forever on a dead worker
                try:
                    job = self.q.get_nowait()
                except queue.Empty:
                    break
                self._finish(job, {"ok": False, "kind": "refused", "error": "worker stopped"})

    def _next(self):
        try:
            return self.q.get_nowait()
        except queue.Empty:
            pass
        if self._pumpable():
            self._pump(PUMP_SLICE)
            try:
                return self.q.get_nowait()
            except queue.Empty:
                return None
        try:
            return self.q.get(timeout=1.0)
        except queue.Empty:
            return None

    def _pumpable(self):
        b = B._tls.browser
        try:
            return b is not None and b.is_connected()
        except Exception:
            return False

    def _pump(self, secs):
        """Run this connection's event loop (dialog events, disconnects) until a request arrives or `secs`
        pass. Sync Playwright only processes events inside a call, so an idle worker must make one."""
        b = B._tls.browser
        sync, loop = getattr(b, "_sync", None), getattr(b, "_loop", None)
        if sync is None or loop is None:          # no private hook: short public waits instead
            try:
                b.contexts[0].wait_for_event("dialog", timeout=50)
            except Exception:
                pass
            return

        async def nap():
            self._loop, self._wake = loop, asyncio.Event()
            if not self.q.empty():
                return
            try:
                await asyncio.wait_for(self._wake.wait(), secs)
            except asyncio.TimeoutError:
                pass
        try:
            sync(nap())
        except Exception:
            pass
        finally:
            self._wake = None

    def _on_dialog(self, dialog):
        """Our session's tab (or a popup it opened): handled now by the running action's policy (else the
        default) and recorded for the reply. Anyone else's tab: left alone. No CDP on the blocked page:
        ownership comes from this thread's targetId cache (_refresh keeps it warm) and the opener."""
        B.handle_dialog(dialog, self.session, leave_foreign=True, sink=self.dialogs)

    # ----- recording (recording.py) -----

    def _rec(self):
        return self.d.recordings.get(self.session)

    def _on_connect(self, browser):
        """A (re)connect: watch every new page of the context (a tab `navigate` opens gets its listener
        before it loads) and the session's existing tabs."""
        self._watched, self._own = {}, {}
        try:
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is None:
                return
            ctx.on("page", lambda page: self._watch(page))
            _, _, pages = B._session_pages(ctx, self.session)
            for _, p in pages:
                self._watch(p)
        except Exception:
            pass

    def _watch(self, page):
        ent = self._watched.get(id(page))
        if ent is not None and ent[0] is page:
            return
        handler = lambda req: self._on_finished(page, req)  # noqa: E731  (a fresh function per page)
        try:
            page.on("requestfinished", handler)
        except Exception:
            return
        self._watched[id(page)] = (page, handler)

    def _unwatch(self, page):
        ent = self._watched.pop(id(page), None)
        if ent is not None and ent[0] is page:
            try:
                page.remove_listener("requestfinished", ent[1])
            except Exception:
                pass

    def _owns(self, page):
        """One of this session's own tabs (not borrowed), or a popup one of them opened. No CDP: a cached
        decision ("yes" sticks; "no" is looked at again after a second) and the session file."""
        now = time.monotonic()
        ent = self._own.get(id(page))
        if ent is not None and ent[0] is page and (ent[1] or now - ent[2] < 1.0):
            return ent[1]
        e = B._load().get(self.session) or {}
        mine = set(e.get("tabs") or []) - set(e.get("borrowed") or [])
        own = B._page_tid(page) in mine
        if not own:
            op = getattr(getattr(page, "_impl_obj", None), "_opener", None)
            own = op is not None and any(getattr(p, "_impl_obj", None) is op and t in mine
                                         for p, t in list(B._tls.tid.values()))
        self._own[id(page)] = (page, own, now)
        return own

    def _on_finished(self, page, req):
        """requestfinished on a watched page: keep it if the response rule says so. The body is fetched now,
        while the browser still has it (the request has finished, so this never waits on a stream)."""
        try:
            rt = req.resource_type
            if rt not in R.RECORD_TYPES:
                return
            main = req.frame.parent_frame is None
            if (rt == "document" and not main and req.method == "GET") or not self._owns(page):
                return
            resp = req.response()
            if resp is None:
                return
            mime = (resp.headers.get("content-type") or "")[:80]
            page_url = req.url if rt == "document" and main else page.url
            if not R.keep_response(page_url, req.url, rt, mime, main, req.method):
                return
            item = {"at": round(time.time(), 3), "method": req.method, "url": req.url[:2000], "status": resp.status,
                    "type": rt, "mime": mime, "page_url": page_url[:2000], "req_headers": R.safe_headers(req.headers)}
            if rt == "document" and not main:
                item["subframe"] = True
            pd = req.post_data
            if pd:
                item["post_data"] = pd[:R.MAX_POST]
                if len(pd) > R.MAX_POST:
                    item["post_data_truncated"] = True
            body = None
            if not 300 <= resp.status < 400:
                try:
                    body = resp.body()
                except Exception as ex:
                    item["body_error"] = (str(ex).splitlines() or [""])[0][:120]
            self._rec().add_response(item, body)
        except Exception:
            pass

    _JS_SECRET = "e => e.type === 'password' || /password|one-time-code|cc-/.test(e.autocomplete || '')"

    def _fill_targets(self, action, args):
        """{selector: secret?} for the fills of a job whose target is on the session's page now (asked before
        the job runs, so a fill whose form submits and navigates away is still checked). No browser yet,
        or no fills: {}."""
        fills = []
        if action == "fill":
            fills = [args]
        elif action == "batch" and isinstance(args, dict) and isinstance(args.get("steps"), list):
            fills = [s.get("args") for s in args["steps"] if isinstance(s, dict) and s.get("action") == "fill"]
        sels = [f.get("selector") for f in fills if isinstance(f, dict) and isinstance(f.get("selector"), str)]
        if not sels:
            return {}
        b = B._tls.browser
        try:
            if b is None or not b.is_connected():
                return {}
            page = B._current(b.contexts[0], self.session)
        except Exception:
            return {}
        return {sel: v for sel in sels for v in [self._secret_on(page, sel)] if v is not None}

    def _secret_on(self, page, sel):
        """True / False when the selector's element is on `page`, None when it is not (or can't be asked)."""
        if R.secret_selector(sel):
            return True
        try:
            if B.ref_id(sel):                    # a ref: look at the element it stands for
                sel = B._sel(page, sel)
                if R.secret_selector(sel):
                    return True
            loc = page.locator(sel).first
            if not loc.count():
                return None
            return bool(loc.evaluate(self._JS_SECRET, timeout=500))
        except Exception:
            return None

    def _note_secrets(self, action, args, before):
        """Before a job runs: the values it will type into secret fields (a fill whose target is a password /
        one-time-code / card input, captcha_text `fields`) go to the recording's redaction set, so the form
        post they end up in is recorded with "<redacted>" (recording.redact_post)."""
        steps = [{"action": action, "args": args}]
        if action == "batch" and isinstance(args, dict) and isinstance(args.get("steps"), list):
            steps = [s for s in args["steps"] if isinstance(s, dict)]
        vals = []
        for s in steps:
            sa = s.get("args") if isinstance(s.get("args"), dict) else {}
            if s.get("action") == "captcha_text" and isinstance(sa.get("fields"), dict):
                vals += [v for v in sa["fields"].values() if isinstance(v, str)]
            elif s.get("action") == "fill" and isinstance(sa.get("value"), str):
                sel = sa.get("selector")
                if R.secret_selector(sel) or (before or {}).get(sel):
                    vals.append(sa["value"])
        if vals:
            self._rec().add_secrets(vals)

    def _secret_target(self, args, before=None):
        """Is a fill's target a password / one-time-code / card input? Asked before the job (`before`), else
        now; when neither can tell (the page navigated away, no browser) the value counts as secret."""
        sel = (args or {}).get("selector")
        if not sel:
            return False
        if R.secret_selector(sel):
            return True
        if before and sel in before:
            return before[sel]
        b = B._tls.browser
        try:
            if b is None or not b.is_connected():
                return True
            v = self._secret_on(B._current(b.contexts[0], self.session), sel)
        except Exception:
            return True
        return True if v is None else v

    @staticmethod
    def _with_resolved(sargs, resolved):
        """A command's args plus the selector / frame prefix its refs resolved to (learn path 3)."""
        if not resolved or not isinstance(sargs, dict):
            return sargs
        out = dict(sargs)
        for k in ("selector", "frame"):
            rid = B.ref_id(sargs.get(k))
            if rid and rid in resolved:
                out[f"{k}_resolved"] = resolved[rid]
        return out

    def _record(self, action, args, rep, url_after, before=None, resolved=None):
        if action in R.SKIP_ACTIONS:
            return
        if action == "close_session":
            if rep.get("ok"):
                self.d.recordings.clear(self.session)
            return
        rec = self._rec()
        steps = R.expand(action, args, rep)
        prev = rec.last_url()
        for i, (a, sargs, ok, err, data) in enumerate(steps):
            if a in R.SKIP_ACTIONS or a == "close_session":
                continue
            last = i == len(steps) - 1
            after = url_after if last else (data.get("url") if isinstance(data, dict) and a == "navigate" else None)
            secret = a == "fill" and self._secret_target(sargs, before)
            if secret and isinstance((sargs or {}).get("value"), str):
                rec.add_secrets([sargs["value"]])     # its post may already be recorded: snapshot redacts again
            sargs = self._with_resolved(sargs, resolved)
            rec.add_command(R.command(rec.next_n(), a, sargs, ok, err, data, prev, after, secret,
                                      batch=i if action == "batch" else None))
            prev = after or prev

    def _do(self, job):
        a = job["action"]
        before = None
        B._tls.resolved = {}
        if a in ("fill", "batch"):
            try:
                before = self._fill_targets(a, job["args"])
            except Exception:
                before = None
        if a in ("fill", "batch", "captcha_text"):
            try:
                self._note_secrets(a, job["args"], before)
            except Exception:
                pass
        try:
            if a == "worker.disconnect":
                B.disconnect()
                data = {"disconnected": True}
            elif a == "recipe.run":
                data = self._recipe(job["args"])
            else:
                data = self._wb(a, job["args"])
            rep = {"ok": True, "data": data}
        except Refused as ex:
            rep = {"ok": False, "kind": "refused", "error": str(ex)}
        except B.WbError as ex:
            rep = {"ok": False, "kind": "NoTab" if isinstance(ex, B.NoTab) else "WbError", "error": str(ex)}
        except Exception as ex:
            kind = "RecipeError" if type(ex).__name__ == "RecipeError" else "Exception"
            rep = {"ok": False, "kind": kind, "error": (str(ex).splitlines() or [""])[0][:500] or type(ex).__name__}
            if kind == "Exception":
                rep["traceback"] = traceback.format_exc()
                print(f"{_now()} {self.name} {a}: {rep['traceback']}", file=sys.stderr, flush=True)
        url = None
        if a != "worker.disconnect":
            url = self._refresh()
        if a in B.SESSION_ACTIONS or a in B.PAGE_ACTIONS:
            try:
                self._record(a, job["args"], rep, url, before, dict(B._tls.resolved))
            except Exception:
                print(f"{_now()} {self.name} recording {a}: {traceback.format_exc()}", file=sys.stderr, flush=True)
        self._finish(job, rep)

    def _finish(self, job, rep):
        if self.dialogs:
            rep["dialogs"], self.dialogs = self.dialogs, []
        job["reply"] = rep
        job["done"].set()

    def _wb(self, action, args):
        st = B._tls
        if st.browser is not None and not st.browser.is_connected():
            B._drop_connection()
        try:
            return B.wb(action, args, session=self.session)
        except B.WbError:
            raise
        except Exception:
            b = B._tls.browser
            if b is None or b.is_connected():
                raise
        # the browser went away under us (restart / crash): the action never reached a live page
        B._drop_connection()
        try:
            return B.wb(action, args, session=self.session)
        except B.WbError:
            raise
        except Exception:
            B.disconnect()          # maybe the driver itself died: start fresh next time
            raise

    def _recipe(self, a):
        from . import recipe as R
        rec = R.load(a["rid"])
        if R.needs_in_process(rec):
            raise Refused("recipe uses secrets / a login / the cloud hCaptcha solver: run it in-process")
        return R.cmd_run(a["rid"], a.get("given") or {}, a.get("tier"), bool(a.get("allow_write")),
                         bool(a.get("data_only")))

    def _refresh(self):
        """After a job: resolve the session's tabs' targetIds now, so the dialog handler never needs CDP;
        record on all of them, stop recording on pages that turned out not to be ours.
        -> the session's current tab URL, or None."""
        try:
            b = B._tls.browser
            if b is None or not b.is_connected():
                return None
            _, e, pages = B._session_pages(b.contexts[0], self.session)
            ours = {id(p) for _, p in pages}
            for _, p in pages:
                self._watch(p)
            for k, (p, _) in list(self._watched.items()):
                if k not in ours and (B._is_closed(p) or not self._owns(p)):
                    self._unwatch(p)
            return next((p.url for t, p in pages if t == e.get("current")), None)
        except Exception:
            return None


class Daemon:
    def __init__(self, k, token, chash):
        self.key, self.token, self.code_hash = k, token, chash
        self.workers = {}
        self.recordings = R.Store()
        self.lock = threading.Lock()
        self.last = time.monotonic()
        self.inflight = 0
        self.stopping = threading.Event()
        self.server = None

    def _worker_submit(self, session, driver, job):
        with self.lock:
            w = self.workers.get((session, driver))
            if w is None or w.retiring or not w.is_alive():
                w = Worker(self, session, driver)
                self.workers[(session, driver)] = w
                w.start()
            w.submit(job)
        return w

    def retire(self, w):
        with self.lock:
            if not w.q.empty() or self.workers.get((w.session, w.driver)) is not w:
                return False
            w.retiring = True
            del self.workers[(w.session, w.driver)]
            return True

    def _broadcast(self, action, wait=15.0):
        with self.lock:
            ws = list(self.workers.values())
        jobs = []
        for w in ws:
            job = {"action": action, "args": {}, "done": threading.Event(), "reply": None}
            w.submit(job)
            jobs.append((w, job))
        for w, job in jobs:
            job["done"].wait(wait)
        return [f"{w.session}/{w.driver}" for w, _ in jobs]

    def handle(self, req):
        if not hmac.compare_digest(str(req.get("token") or ""), self.token):
            return {"ok": False, "kind": "auth", "error": "bad token"}
        self.last = time.monotonic()
        a = req.get("action")
        if a == "daemon.ping":
            with self.lock:
                ws = [f"{s}/{d}" for s, d in self.workers]
            return {"ok": True, "data": {"pid": os.getpid(), "version": __version__,
                                         "code_hash": self.code_hash, "workers": ws}}
        if a == "daemon.shutdown":
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {"ok": True, "data": {"stopping": True}}
        if a == "daemon.disconnect":
            return {"ok": True, "data": {"disconnected": self._broadcast("worker.disconnect")}}
        if a == "recording":
            return self._recording(str(req.get("session") or "default"), req.get("args") or {})
        if self.stopping.is_set():
            return {"ok": False, "kind": "refused", "error": "daemon is stopping"}
        if not isinstance(a, str) or not (a == "recipe.run" or a in B.SESSION_ACTIONS or a in B.PAGE_ACTIONS):
            return {"ok": False, "kind": "refused", "error": f"unsupported action {a!r}"}
        driver = req.get("driver") or default_driver()
        if driver not in ("playwright", "patchright"):
            return {"ok": False, "kind": "refused", "error": f"unknown driver {driver!r}"}
        args = req.get("args") or {}
        if not isinstance(args, dict):
            return {"ok": False, "kind": "refused", "error": "args must be an object"}
        session = str(req.get("session") or "default")
        job = {"action": a, "args": args, "done": threading.Event(), "reply": None}
        with self.lock:
            self.inflight += 1
        try:
            self._worker_submit(session, driver, job)
            job["done"].wait()
        finally:
            with self.lock:
                self.inflight -= 1
            self.last = time.monotonic()
        return job["reply"]

    def _recording(self, session, args):
        """`recording {clear?, body_dir?}`: the session's recording (recording.py schema), or clear it.
        body_dir must be an existing absolute directory the caller made; the bodies are written there."""
        if not isinstance(args, dict):
            return {"ok": False, "kind": "WbError", "error": "recording: args must be an object"}
        if args.get("clear"):
            return {"ok": True, "data": {"cleared": self.recordings.clear(session)}}
        rec = self.recordings.get(session, create=False)
        if rec is None:
            return {"ok": True, "data": {"version": R.VERSION, "session": session, "url": None,
                                         "responses": [], "commands": []}}
        bd = args.get("body_dir")
        if bd is not None and not (isinstance(bd, str) and os.path.isabs(bd) and os.path.isdir(bd)):
            return {"ok": False, "kind": "WbError", "error": "recording: body_dir must be an existing absolute directory"}
        try:
            return {"ok": True, "data": rec.snapshot(bd)}
        except OSError as ex:
            return {"ok": False, "kind": "WbError", "error": f"recording: cannot write bodies: {ex}"}

    def idle_watch(self):
        while not self.stopping.wait(15):
            with self.lock:
                busy = self.inflight
            if not busy and time.monotonic() - self.last > DAEMON_IDLE:
                self.shutdown()
                return

    def shutdown(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        with self.lock:
            ws = list(self.workers.values())
        for w in ws:
            w.submit({"action": "worker.exit", "args": {}, "done": threading.Event(), "reply": None})
        for w in ws:
            w.join(10)
        client.remove_state(client.state_path(self.key), os.getpid())
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()     # a late client gets "refused", not a connection nobody reads


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(client.MAX_LINE)
            if not line:
                return
            try:
                req = client.decode(line)
            except ValueError:
                rep = {"ok": False, "kind": "refused", "error": "bad request"}
            else:
                rep = self.server.daemon_obj.handle(req)
            self.wfile.write(client.encode(rep))
        except OSError:
            pass          # the client went away; the job (if any) still ran to completion


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def _lock(path):
    """Exclusive lock for this daemon key, held for the process lifetime. -> file object, or None."""
    f = open(path, "a+")
    end = time.monotonic() + LOCK_WAIT
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() > end:
                f.close()
                return None
            time.sleep(0.1)


def main():
    k, chash = client.key(), client.code_hash()
    sd = client.state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    lp = sd / f"{k}.log"
    big = lp.exists() and lp.stat().st_size > 1_000_000
    log = open(lp, "w" if big else "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    lk = _lock(sd / f"{k}.lock")
    if lk is None:
        print(f"{_now()} pid {os.getpid()}: another daemon holds the lock; exiting", flush=True)
        return 0
    from . import secretstore
    secretstore.get = _refuse_secret          # backstop: this process never reads a secret
    d = Daemon(k, secrets.token_hex(16), chash)
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.daemon_obj, d.server = d, srv
    port = srv.server_address[1]
    client.write_state(client.state_path(k), {"port": port, "token": d.token, "pid": os.getpid(),
                                              "version": __version__, "code_hash": chash, "key": k,
                                              "started": _now()})
    print(f"{_now()} pid {os.getpid()} listening on 127.0.0.1:{port} (code {chash}, {B.ENDPOINT})", flush=True)
    threading.Thread(target=d.idle_watch, daemon=True, name="idle-watch").start()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        d.stopping.set()
        client.remove_state(client.state_path(k), os.getpid())
        print(f"{_now()} pid {os.getpid()} exiting", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
