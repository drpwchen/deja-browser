# -*- coding: utf-8 -*-
"""Passive recording of a session's first visit (daemon only): the input of `deja-browser learn`.

WHY (v0.4, DESIGN.md §7 "first visit only answers; learning is a by-product"): while an agent does
its first visit normally (navigate / read / evaluate / fill / click …) through the daemon, the
daemon keeps what happened on that session's tabs. Afterwards `deja-browser learn` turns it into a
verified, saved recipe without the model reading the page again. In-process mode (no daemon,
DEJA_BROWSER_DAEMON=0, `import edge_cdp`) records nothing.

LIFETIME: per session, in the daemon's memory only. Cleared by `close_session` (and lost when the
daemon exits or restarts, e.g. after the package code changed). Never written to disk, except that
`recording {body_dir}` writes the bodies into a directory the caller made (learn deletes it).

WHAT IS RECORDED (RESPONSE RULE). A response on a tab the session owns (its own tabs and the popups
they opened; borrowed tabs — find_tab active / attach_tab of someone else's tab — are not recorded),
once the request has finished, when
  - its resource type is document, xhr or fetch (a child frame's document only when it is not a GET:
    a form posted inside an <iframe>, kept with "subframe": true so learn sees what a flow posted), and
  - neither its URL nor the tab's URL is a refused host (guard.py: mail, sign-in, banks, brokers,
    ticketing; with [recipes] deny_hosts_enabled = false the mail / bank / broker list is off here too,
    since learn needs the recording to make a recipe there; secret values are redacted either way), and
  - neither URL is online banking / web ATM / online trading (NO_RECORD: a token in host + path such as
    "ebank", "netbank", "webatm", "mybank"; [recipes] no_record_extra adds more). This one holds even
    with deny_hosts_enabled = false: a bank's campaign or sign-up pages are recorded, its online banking
    is not. Commands whose URL before or after is such a page are not recorded either, and
  - it is the main document, OR it is same-site as the tab's page (same registrable domain,
    approximated as the last two labels, three under a two-letter country code with a generic
    second level like co.uk / com.tw), OR it is an xhr / fetch that returned JSON (a page's own
    API on another domain: hn.algolia.com -> *.algolia.net).
  Ring buffer: the last MAX_RESPONSES responses and at most MAX_SESSION_BYTES of bodies per session.
  A body larger than MAX_BODY is not kept
  (`body_error: "too big"`).

SCHEMA (version 1) — the reply of the daemon action `recording {session}`:
  {"version": 1, "session", "started": epoch s, "url": the session's last page URL,
   "responses": [response...], "commands": [command...], "dropped": {"responses": n, "commands": n}}
  response (the shape of a network_capture item, so draft.build reads it as is):
    {"i": seq, "at": epoch s, "method", "url" (<= 2000 chars), "status", "type" (document|xhr|fetch),
     "mime" (<= 80), "page_url": URL of the tab when it finished, "req_headers" (credential values
     replaced by "<redacted len=N>", exactly like network_capture), "post_data"? (<= 4000 chars; the value
     of a credential-looking field (urlencoded / JSON) and any value typed into a password / one-time-code
     field or given as captcha_text `fields` in this session replaced by "<redacted>", see redact_post),
     "post_data_truncated"?, "body_bytes", "body_path"? (only with body_dir), "body_error"?,
     "subframe"?: true for a child frame's document}
  command (one per action, batch steps unpacked; learn path 3 turns these into `steps` recipes):
    {"n": seq, "at": epoch s, "action", "args": {...}, "ok": bool, "error"?: str (<= 300),
     "url_before": the session's page URL before (the previous command's url_after; null at first),
     "url_after": the current tab's URL after (null when unknown, e.g. a batch step in the middle),
     "batch"?: step index inside a batch,
     "result"?: the action's value when its JSON is <= MAX_PREVIEW chars (evaluate: the returned
       value; read: the text; select: {"selected": ...}; wait: {"success": ...}), else
     "result_preview": its JSON cut to MAX_PREVIEW chars + "result_chars": full length}
  args are kept as given, except:
    - fill `value`: replaced by "<redacted>" (+ "redacted": true) when the target is a password /
      one-time-code input or the selector looks like a credential field; otherwise kept (a typed
      value is a param candidate for learn) and cut to MAX_VALUE chars
    - captcha_text `fields`: values replaced by "<redacted>"
    - evaluate `code`: cut to MAX_CODE chars (+ "code_truncated": true)
    - any other string longer than MAX_VALUE chars is cut
    - a `selector` / `frame` that was a ref (@eN) gets `selector_resolved` / `frame_resolved`: the selector
      / frame prefix it resolved to when the command ran (refs are per snapshot; learn needs a stable one)
"""
import json
import re
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path

from . import guard

VERSION = 1
MAX_RESPONSES = 100
MAX_COMMANDS = 200
MAX_BODY = 256 * 1024            # a body larger than this is not kept (crates.io's crate API answer: 3.7 kB)
MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_SESSIONS = 20                # recordings kept at once (least recently used dropped)
MAX_POST = 4000
MAX_PREVIEW = 2000
MAX_CODE = 4000
MAX_VALUE = 500
RECORD_TYPES = ("document", "xhr", "fetch")
SKIP_ACTIONS = ("dialog", "list_tabs", "list_browser_tabs")   # status queries: nothing to learn from

_SLD2 = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "mil", "nom", "sch", "ltd", "plc",
         "gob", "idv", "gen", "biz", "info"}
_SECRET_SEL = re.compile(r"passw|pwd|otp|one-time|secret|token|card|cvv|cvc|pin\b|ssn", re.I)


# ---------- the response rule ----------

def site(host):
    """Registrable domain, approximated without the public suffix list (see the module docstring)."""
    h = (host or "").lower().rstrip(".")
    if not h or re.fullmatch(r"[\d.]+|\[.*\]|[0-9a-f:]+", h):
        return h
    labels = h.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SLD2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(a_url, b_url):
    a, b = urllib.parse.urlsplit(a_url or ""), urllib.parse.urlsplit(b_url or "")
    return bool(a.hostname) and site(a.hostname) == site(b.hostname)


def _refused(url):
    try:
        guard.check_host(url)
        return False
    except guard.HostRefused:
        return True


# online banking, web ATM, online trading: never recorded, whatever deny_hosts_enabled says (the user may
# let the agent onto a bank's campaign pages; the account pages themselves stay out of the recording).
# Substrings of host + path, lower case.
NO_RECORD_DEFAULT = ("ebank", "e-bank", "netbank", "net-bank", "ibank", "mybank", "webatm", "web-atm",
                     "onlinebanking", "online-banking", "internetbanking", "ipost.post.gov.tw", "mma.sinopac.",
                     "etrade", "webtrade", "web-trade", "ectrade", "e-trade", "eorder", "e-order")


def _no_record_tokens():
    from . import config
    extra = config.get("recipes", "no_record_extra", []) or []
    return NO_RECORD_DEFAULT + tuple(str(x).lower() for x in extra if str(x).strip())


def no_record(url):
    """True for an online banking / web ATM / online trading URL (see NO_RECORD_DEFAULT)."""
    if not url or not str(url).startswith(("http://", "https://")):
        return False
    s = urllib.parse.urlsplit(url)
    where = ((s.hostname or "") + s.path).lower()
    return any(tok in where for tok in _no_record_tokens())


def keep_response(page_url, url, rtype, mime, main_frame=True, method="GET"):
    """The response rule. -> True when this response is recorded."""
    if rtype not in RECORD_TYPES or not (url or "").startswith(("http://", "https://")):
        return False
    if rtype == "document" and not main_frame and (method or "GET").upper() == "GET":
        return False
    if _refused(url) or (page_url and page_url.startswith("http") and _refused(page_url)):
        return False
    if no_record(url) or no_record(page_url):
        return False
    if rtype == "document":
        return True
    return same_site(page_url, url) or "json" in (mime or "").lower()


def safe_headers(h):
    """Header names + values, credential-looking values replaced by their length: network_capture's own
    function, so the two can never differ."""
    from . import browser
    return browser._safe_headers(h)


def body_cap(mime):
    return MAX_BODY


# ---------- commands ----------

def secret_selector(sel):
    return bool(_SECRET_SEL.search(sel or ""))


REDACTED = "<redacted>"


def _redact_json(v, secrets):
    if isinstance(v, dict):
        from .draft import credential_name
        return {k: (REDACTED if credential_name(k) and x not in ("", None) else _redact_json(x, secrets))
                for k, x in v.items()}
    if isinstance(v, list):
        return [_redact_json(x, secrets) for x in v]
    if isinstance(v, str) and v in secrets:
        return REDACTED
    return v


def redact_post(pd, secrets=()):
    """A request body as recorded: in a urlencoded or JSON body the value of a credential-looking field
    (draft.credential_name: password, token, key, session ...) becomes "<redacted>"; then every known
    secret value of the session (typed into a password / one-time-code field, captcha_text `fields`; >= 3
    chars), raw or URL-encoded, is replaced wherever it still occurs."""
    if not isinstance(pd, str) or not pd:
        return pd
    secrets = {s for s in secrets if isinstance(s, str) and len(s) >= 3}
    out = pd
    s = pd.strip()
    if s[:1] in "{[":                    # re-serialised only when something was redacted
        try:
            obj = json.loads(s)
            red = _redact_json(obj, secrets)
            if red != obj:
                out = json.dumps(red, ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            pass
    elif "=" in s:
        from .draft import credential_name
        pairs = urllib.parse.parse_qsl(s, keep_blank_values=True)
        red = [(k, REDACTED if (credential_name(k) and v) or v in secrets else v) for k, v in pairs]
        if red != pairs:
            out = urllib.parse.urlencode(red, safe="<>")
    for sec in sorted(secrets, key=len, reverse=True):
        for form in {sec, urllib.parse.quote(sec, safe=""), urllib.parse.quote_plus(sec)}:
            out = out.replace(form, REDACTED)
    return out


def _cut(v, n=MAX_VALUE):
    if isinstance(v, str) and len(v) > n:
        return v[:n]
    if isinstance(v, dict):
        return {k: _cut(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_cut(x, n) for x in v[:50]]
    return v


def clean_args(action, args, secret_target=False):
    """A command's args as recorded (see the module docstring)."""
    a = dict(args or {}) if isinstance(args, dict) else {}
    code = a.pop("code", None) if action == "evaluate" else None
    fields = a.pop("fields", None) if action == "captcha_text" else None
    redact = action == "fill" and "value" in a and (secret_target or secret_selector(a.get("selector")))
    if redact:
        a["value"], a["redacted"] = "<redacted>", True
    a = _cut(a)
    if isinstance(code, str):
        a["code"] = code[:MAX_CODE]
        if len(code) > MAX_CODE:
            a["code_truncated"] = True
    if isinstance(fields, dict):
        a["fields"] = {k: "<redacted>" for k in fields}
    return a


def result_of(action, data):
    """-> the part of an action's reply worth keeping (None: nothing)."""
    if not isinstance(data, dict):
        return None
    if action == "evaluate":
        return data.get("value")
    if action == "read":
        return data.get("text") if "text" in data else data
    if action == "select":
        return {"selected": data.get("selected")}
    if action == "navigate":
        return {"status": data.get("status"), "title": data.get("title")}
    if action == "wait":
        return {"success": data.get("success")}
    return None


def command(n, action, args, ok, error=None, data=None, url_before=None, url_after=None, secret_target=False,
            batch=None):
    rec = {"n": n, "at": round(time.time(), 3), "action": action,
           "args": clean_args(action, args, secret_target), "ok": bool(ok),
           "url_before": url_before, "url_after": url_after}
    if error:
        rec["error"] = str(error)[:300]
    if batch is not None:
        rec["batch"] = batch
    res = result_of(action, data) if ok else None
    if res is not None:
        s = json.dumps(res, ensure_ascii=False, default=str)
        if len(s) <= MAX_PREVIEW:
            rec["result"] = res
        else:
            rec["result_preview"], rec["result_chars"] = s[:MAX_PREVIEW], len(s)
    return rec


def expand(action, args, rep):
    """One daemon job -> [(action, args, ok, error, data)]: a batch becomes its steps."""
    ok = bool(rep.get("ok"))
    data = rep.get("data") if ok else None
    if action != "batch" or not isinstance(args, dict) or not isinstance(args.get("steps"), list):
        return [(action, args, ok, None if ok else rep.get("error"), data)]
    rows = (data or {}).get("results") if isinstance(data, dict) else None
    out = []
    for i, s in enumerate(args["steps"]):
        if not isinstance(s, dict):
            continue
        r = rows[i] if isinstance(rows, list) and i < len(rows) else {}
        if r.get("skipped"):
            continue
        sargs = s.get("args") if isinstance(s.get("args"), dict) else {}
        step_ok = bool(r.get("ok")) if r else ok
        out.append((s.get("action"), sargs, step_ok, r.get("error") or (None if step_ok else rep.get("error")),
                    r.get("data")))
    return out


# ---------- the per-session store ----------

class Recording:
    def __init__(self, session):
        self.session = session
        self.lock = threading.Lock()
        self.started = time.time()
        self.used = time.monotonic()
        self.responses = deque()          # (item, body bytes | None)
        self.commands = deque(maxlen=MAX_COMMANDS)
        self.bytes = 0
        self.seq = 0
        self.nseq = 0
        self.url = None
        self.dropped = {"responses": 0, "commands": 0}
        self.redact_values = set()              # values typed into secret fields: never leave this object

    def add_secrets(self, values):
        """Values to redact from request bodies (call BEFORE the job that types them runs: its form post can
        be recorded before the command is)."""
        with self.lock:
            self.redact_values.update(v for v in values if isinstance(v, str) and len(v) >= 3)

    def add_response(self, item, body):
        with self.lock:
            self.used = time.monotonic()
            item = dict(item, i=self.seq)
            if item.get("post_data"):
                item["post_data"] = redact_post(item["post_data"], self.redact_values)
            self.seq += 1
            if body is not None and len(body) > body_cap(item.get("mime")):
                item["body_bytes"], item["body_error"], body = len(body), "too big", None
            elif body is not None:
                item["body_bytes"] = len(body)
            self.responses.append((item, body))
            self.bytes += len(body or b"")
            while self.responses and (len(self.responses) > MAX_RESPONSES or self.bytes > MAX_SESSION_BYTES):
                _, b = self.responses.popleft()
                self.bytes -= len(b or b"")
                self.dropped["responses"] += 1

    def next_n(self):
        with self.lock:
            self.nseq += 1
            return self.nseq

    def add_command(self, rec):
        if no_record(rec.get("url_before")) or no_record(rec.get("url_after")):
            with self.lock:                       # online banking: not kept, but the page is still tracked
                self.dropped["no_record"] = self.dropped.get("no_record", 0) + 1
                if rec.get("url_after"):
                    self.url = rec["url_after"]
            return
        with self.lock:
            self.used = time.monotonic()
            if len(self.commands) == self.commands.maxlen:
                self.dropped["commands"] += 1
            self.commands.append(rec)
            if rec.get("url_after"):
                self.url = rec["url_after"]

    def last_url(self):
        with self.lock:
            return self.url

    def snapshot(self, body_dir=None):
        """-> the schema above. With body_dir (an existing directory) each kept body is written there as
        <i>.json / <i>.txt and the item gets body_path."""
        with self.lock:
            resp = [(dict(it), b) for it, b in self.responses]
            for it, _ in resp:                    # again: a secret learned after its post was recorded
                if it.get("post_data"):
                    it["post_data"] = redact_post(it["post_data"], self.redact_values)
            cmds = [dict(c) for c in self.commands]
            out = {"version": VERSION, "session": self.session, "started": round(self.started, 3), "url": self.url,
                   "dropped": dict(self.dropped)}
        items = []
        bd = Path(body_dir) if body_dir else None
        for it, b in resp:
            if bd is not None and b is not None:
                p = bd / f"{it['i']:04d}.{'json' if 'json' in (it.get('mime') or '') else 'txt'}"
                p.write_bytes(b)
                it["body_path"] = str(p)
            items.append(it)
        out["responses"], out["commands"] = items, cmds
        return out

    def summary(self):
        with self.lock:
            return {"session": self.session, "responses": len(self.responses), "commands": len(self.commands),
                    "body_bytes": self.bytes, "url": self.url}


class Store:
    """All sessions' recordings in one daemon (thread-safe)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by = {}

    def get(self, session, create=True):
        with self.lock:
            r = self.by.get(session)
            if r is None and create:
                if len(self.by) >= MAX_SESSIONS:
                    old = min(self.by.values(), key=lambda x: x.used)
                    del self.by[old.session]
                r = self.by[session] = Recording(session)
            return r

    def clear(self, session):
        with self.lock:
            return self.by.pop(session, None) is not None
