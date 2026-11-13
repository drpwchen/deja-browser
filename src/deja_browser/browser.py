# -*- coding: utf-8 -*-
"""Kimi-WebBridge-compatible `wb(action, args)` on a dedicated agent browser over CDP.

  Dedicated profile ([browser] profile, default <data dir>/profile), CDP on 127.0.0.1:9222 only.
  Never touches the user's everyday browser profile. Any Chromium browser works: [browser]
  executable / env DEJA_BROWSER_BROWSER, else the first of Edge, Chrome, Chromium, Brave found on
  this machine. Started on demand and left running afterwards — close_session only closes this
  session's tabs.

  from deja_browser.browser import wb
  wb("navigate", {"url": "https://example.com"}, session="my-task")   -> {success, url, tabId, title, status}
  wb("evaluate", {"code": "document.title"}, session="my-task")       -> {type, value}
  wb("close_session", {}, session="my-task")

Actions (return shapes follow the Kimi `data` payload):
  navigate {url, newTab}             {success, url, tabId, title, status}
  evaluate {code}                    {type, value}            (runs in the page's main world)
  fill {selector, value}             {success, tag, mode}     (readonly/hidden inputs: JS fallback)
  click {selector}                   {success, tag, text}     (trusted CDP input events)
  screenshot {path, format, quality} {format, path, sizeBytes, mimeType}
  save_as_pdf {path, paper_format, landscape, scale, print_background}
                                     {path, sizeBytes, mimeType, pageTitle}
  list_tabs                          {success, tabs:[{tabId, url, title, active}]}   this session
  list_browser_tabs                  {success, tabs:[...]}                            whole browser
  find_tab {url, active}             {success, url, tabId, borrowed}
  attach_tab {tabId}                 {success, tabId, url, title, borrowed}
  snapshot {max_chars, selector, interactive_only, query}
                                     {url, title, tree_text, chars, full_chars, truncated}
  network_capture {trigger, url_filter, types, wait_ms, body_dir}
                                     {url, count, body_dir, requests:[{i, method, url, status, type, mime,
                                      req_headers (credentials redacted), post_data, body_path, body_bytes}]}
  cdp {method, params}               raw CDP result
  close_session                      {success, closed}        (borrowed tabs are left open)

session -> tab mapping is keyed by CDP targetId in [browser] sessions_file, so a later
process finds the same tab again. One CDP connection per process (lazy singleton).
Driver: playwright (default) or patchright (EDGE_CDP_DRIVER=patchright or use_driver()).

CLI:
  deja-cdp launch                     start the agent browser (e.g. to log in to a site by hand once)
  deja-cdp status                     is CDP up? which version?
  deja-cdp stop                       kill ONLY the agent-profile browser (refuses any other 9222 owner)
  deja-cdp <action> '<json args>' [--session S]
"""
import atexit, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

from . import config, guard


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
PORT = int(config.get("browser", "port", 9222))
ENDPOINT = f"http://127.0.0.1:{PORT}"
SHOT_DIR = Path(tempfile.gettempdir()) / "deja-cdp"


def launch_args():
    return [f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check"]


class WbError(RuntimeError):
    pass


class NoTab(WbError):
    """The session has no live tab (never opened, or closed by the user / an Edge restart)."""


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
        raise WbError(f"port {PORT} is held by a browser on another profile ({other}), not the agent "
                      f"profile {PROFILE}. Close that browser, or use another [browser] port.")
    _remember(bid)


def launch(timeout=20, url="about:blank"):
    """Start headed Edge on the agent profile unless CDP is already up. -> (version_json, launched_by_us)."""
    v = version()
    if v:
        check_profile(v)
        return v, False
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
            return v, True
        time.sleep(0.5)
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


# ---------- one CDP connection per process ----------

_DRIVER = os.environ.get("EDGE_CDP_DRIVER", "playwright")
_pw = _browser = None
_tid = {}   # id(page) -> targetId cache


def use_driver(name):
    """'playwright' or 'patchright' (same API; patchright hides Runtime.enable). Call before the first wb()."""
    global _DRIVER
    if _browser is not None and name != _DRIVER:
        raise WbError(f"already connected with {_DRIVER}")
    _DRIVER = name


def _sync_playwright():
    if _DRIVER == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright
    return sync_playwright


def _connect():
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    launch()
    # the node driver prints a DEP0169 url.parse() warning on every connect; it is noise
    os.environ.setdefault("NODE_NO_WARNINGS", "1")
    if _pw is None:
        _pw = _sync_playwright()().start()
    _browser = _pw.chromium.connect_over_cdp(ENDPOINT)
    return _browser


@atexit.register
def disconnect():
    """Drop the CDP connection. On connect_over_cdp, browser.close() only disconnects — Edge stays up."""
    global _pw, _browser
    try:
        if _browser is not None:
            _browser.close()
        if _pw is not None:
            _pw.stop()
    except Exception:
        pass
    _pw = _browser = None


def _ctx():
    b = _connect()
    return b.contexts[0] if b.contexts else b.new_context()


def target_id(page):
    k = id(page)
    if k not in _tid:
        s = page.context.new_cdp_session(page)
        try:
            _tid[k] = s.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        finally:
            s.detach()
    return _tid[k]


# ---------- session map (persisted across processes) ----------

def _load():
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(m):
    tmp = SESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SESSIONS_FILE)


def _entry(m, session):
    # {"tabs": [targetId...], "borrowed": [targetId...], "current": targetId}
    return m.setdefault(session, {"tabs": [], "borrowed": [], "current": None})


def _pages_by_tid(ctx):
    out = {}
    for p in ctx.pages:
        try:
            out[target_id(p)] = p
        except Exception:
            pass
    return out


def _session_pages(ctx, session, m=None):
    """-> (map, entry, [(tid, page)...]) with dead tabs pruned from the entry."""
    m = m if m is not None else _load()
    e = _entry(m, session)
    live = _pages_by_tid(ctx)
    e["tabs"] = [t for t in e["tabs"] if t in live]
    e["borrowed"] = [t for t in e["borrowed"] if t in e["tabs"]]
    if e["current"] not in e["tabs"]:
        e["current"] = e["tabs"][-1] if e["tabs"] else None
    return m, e, [(t, live[t]) for t in e["tabs"]]


def _add(m, e, tid, borrowed=False):
    if tid not in e["tabs"]:
        e["tabs"].append(tid)
    if borrowed and tid not in e["borrowed"]:
        e["borrowed"].append(tid)
    e["current"] = tid
    _save(m)


def _current(ctx, session):
    m, e, pages = _session_pages(ctx, session)
    _save(m)
    for t, p in pages:
        if t == e["current"]:
            return p
    raise NoTab(f"session {session!r} has no tab — navigate first")


def _tab_info(t, p, cur=None):
    try:
        title = p.title()
    except Exception:
        title = ""
    return {"tabId": t, "url": p.url, "title": title, "active": t == cur}


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
    m, e, pages = _session_pages(ctx, session)
    page = None
    if not a.get("newTab"):
        page = next((p for t, p in pages if t == e["current"]), None)
    if page is None:
        page = ctx.new_page()
    resp = page.goto(url, wait_until=a.get("wait_until", "domcontentloaded"), timeout=a.get("timeout", 45000))
    tid = target_id(page)
    _add(m, e, tid)
    return {"success": True, "url": page.url, "tabId": tid, "title": page.title(),
            "status": resp.status if resp else None}


def _evaluate(page, a):
    code = a.get("code")
    if code is None:
        raise WbError("evaluate: code required")
    if _DRIVER == "patchright":   # patchright defaults to an isolated world; Kimi ran in the page's own
        v = page.evaluate(code, isolated_context=False)
    else:
        v = page.evaluate(code)
    t = "undefined" if v is None else {bool: "boolean", int: "number", float: "number", str: "string"}.get(type(v), "object")
    return {"type": t, "value": v}


_JS_FILL = """([sel, val]) => {const e=document.querySelector(sel); if(!e) return null;
if (e.isContentEditable) {e.focus(); e.textContent=val; e.dispatchEvent(new InputEvent('input',{bubbles:true}));
  return [e.tagName.toLowerCase(), 'contenteditable'];}
const proto = e.tagName==='TEXTAREA' ? HTMLTextAreaElement.prototype : e.tagName==='SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(proto,'value').set.call(e, val);
e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));
return [e.tagName.toLowerCase(), 'value'];}"""


def _fill(page, a):
    sel, val = a.get("selector"), a.get("value", "")
    if not sel:
        raise WbError("fill: selector required")
    try:
        page.fill(sel, str(val), timeout=a.get("timeout", 5000))
        tag, ce = page.eval_on_selector(sel, "e => [e.tagName.toLowerCase(), !!e.isContentEditable]")
        return {"success": True, "tag": tag, "mode": "contenteditable" if ce else "value"}
    except Exception as ex:
        # readonly date pickers / hidden inputs: Playwright refuses, Kimi just set .value — do the same
        kw = {"isolated_context": False} if _DRIVER == "patchright" else {}
        r = page.evaluate(_JS_FILL, [sel, str(val)], **kw)
        if not r:
            raise WbError(f"fill: no element matches {sel!r} ({str(ex).splitlines()[0][:160]})")
        return {"success": True, "tag": r[0], "mode": r[1], "fallback": "js"}


def _click(page, a):
    sel = a.get("selector")
    if not sel:
        raise WbError("click: selector required")
    loc = page.locator(sel).first
    loc.click(timeout=a.get("timeout", 10000))
    try:
        tag, text = loc.evaluate("e => [e.tagName.toLowerCase(), (e.innerText||e.value||'').trim().slice(0,100)]",
                                 timeout=1000)
    except Exception:   # the click navigated away — the element is gone, the click still happened
        tag, text = "", ""
    return {"success": True, "tag": tag, "text": text}


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
        page.locator(a["selector"]).first.screenshot(**kw)
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
        _ROLE = re.compile(r"^\s*-\s+([a-z]+)")
    m = _ROLE.match(line)
    return m.group(1) if m else ""


def _with_ancestors(lines, keep):
    """keep[i] -> also keep every ancestor (lower indentation above it), so a hit has its path."""
    out, stack = set(), []   # stack = [(indent, line index)] of the open ancestors
    for i, ln in enumerate(lines):
        ind = len(ln) - len(ln.lstrip())
        while stack and stack[-1][0] >= ind:
            stack.pop()
        if keep[i]:
            out.update(j for _, j in stack)
            out.add(i)
        stack.append((ind, i))
    return [lines[i] for i in sorted(out)]


def _snapshot(page, a):
    """Aria tree, optionally scoped. selector = subtree root; interactive_only = only controls/links
    (with their ancestor path); query = only lines containing the text (case-insensitive) + ancestors."""
    sel = a.get("selector") or "body"
    text = page.locator(sel).first.aria_snapshot(timeout=a.get("timeout", 10000))
    lines = text.splitlines()
    full = len(text)
    if a.get("query"):
        q = str(a["query"]).lower()
        lines = _with_ancestors(lines, [q in ln.lower() for ln in lines])
    if a.get("interactive_only"):
        lines = [ln for ln in lines if _role(ln) in _INTERACTIVE]
    text = "\n".join(lines)
    n = int(a.get("max_chars") or 20000)
    return {"url": page.url, "title": page.title(), "tree_text": text[:n],
            "chars": len(text), "full_chars": full, "truncated": len(text) > n}


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
        elif trig.get("evaluate"):
            page.evaluate(trig["evaluate"])
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


def _cdp(page, a):
    s = page.context.new_cdp_session(page)
    try:
        return s.send(a["method"], a.get("params") or {})
    finally:
        s.detach()


def _list_tabs(ctx, session, a):
    m, e, pages = _session_pages(ctx, session)
    _save(m)
    return {"success": True, "tabs": [_tab_info(t, p, e["current"]) for t, p in pages]}


def _list_browser_tabs(ctx, session, a):
    return {"success": True, "tabs": [_tab_info(t, p) for t, p in _pages_by_tid(ctx).items()]}


def _find_tab(ctx, session, a):
    url = a.get("url") or ""
    m, e, pages = _session_pages(ctx, session)
    hit = next(((t, p) for t, p in pages if p.url.startswith(url)), None)
    borrowed = False
    if hit is None and a.get("active"):   # borrow a tab this session did not open
        hit = next(((t, p) for t, p in _pages_by_tid(ctx).items() if p.url.startswith(url)), None)
        borrowed = hit is not None
    if hit is None:
        _save(m)
        raise NoTab(f"find_tab: no tab matching {url!r}")
    _add(m, e, hit[0], borrowed=borrowed or hit[0] in e["borrowed"])
    return {"success": True, "url": hit[1].url, "tabId": hit[0], "borrowed": hit[0] in e["borrowed"]}


def _attach_tab(ctx, session, a):
    tid = a.get("tabId")
    m, e, _ = _session_pages(ctx, session)
    page = _pages_by_tid(ctx).get(tid)
    if page is None:
        _save(m)
        raise NoTab(f"attach_tab: no tab {tid!r}")
    mine = tid in e["tabs"] and tid not in e["borrowed"]
    _add(m, e, tid, borrowed=not mine)
    return {"success": True, "tabId": tid, "url": page.url, "title": page.title(), "borrowed": not mine}


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
    _save(m)
    return {"success": True, "closed": closed}


SESSION_ACTIONS = {"navigate": _navigate, "list_tabs": _list_tabs, "list_browser_tabs": _list_browser_tabs,
                   "find_tab": _find_tab, "attach_tab": _attach_tab, "close_session": _close_session}
PAGE_ACTIONS = {"evaluate": _evaluate, "fill": _fill, "click": _click, "screenshot": _screenshot,
                "save_as_pdf": _save_as_pdf, "snapshot": _snapshot, "cdp": _cdp,
                "network_capture": _network_capture}


def wb(action, args=None, session="default"):
    """Run one Kimi-style action. Returns the `data` payload; raises WbError / NoTab on failure."""
    args = args or {}
    if action in SESSION_ACTIONS:
        return SESSION_ACTIONS[action](_ctx(), session, args)
    if action in PAGE_ACTIONS:
        return PAGE_ACTIONS[action](_current(_ctx(), session), args)
    raise WbError(f"unsupported action: {action}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    session = "cli"
    if "--session" in argv:
        i = argv.index("--session")
        session = argv[i + 1]
        del argv[i:i + 2]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    cmd = argv[0]
    if cmd == "launch":
        v, ours = launch()
        out = {"browser": v.get("Browser"), "launched": ours, "profile": str(PROFILE)}
    elif cmd == "status":
        v = version()
        out = {"up": bool(v), "browser": (v or {}).get("Browser"), "profile": str(PROFILE)}
    elif cmd == "stop":
        out = stop()
    else:
        if len(argv) > 2:
            raise SystemExit(f"deja-cdp: unexpected arguments {argv[2:]} (usage: <action> '<json>' [--session S])")
        out = wb(cmd, json.loads(argv[1]) if len(argv) > 1 else {}, session=session)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
