# -*- coding: utf-8 -*-
"""`deja-browser learn`: turn a session's recording (recording.py) into a recipe draft. Pure functions of
the recording plus the body files it names; no browser. recipe.cmd_learn then verifies and saves.

PATH 1 — JSON API. The recorded responses go through draft.build, the same rules as `deja-browser
draft` (the response that holds every --expect; exact-value templating only; credential-looking
query / body fields dropped unless --keep-param). The draft's entry_url is the page the tab showed
when that request was made.
PATH 2 — the agent's own extraction. When no JSON response holds the answer, the last recorded
`evaluate` whose result contains every --expect becomes a `script` recipe that runs that code on the
page it ran on (entry_url = that page, "at_entry": true). The script retries the code for up to
SCRIPT_WAIT_MS until it returns something non-empty (a client-rendered page may not be ready right
after the load).
PATH 2b — a form that posts back. When the page the evaluate read was the answer to a form POST
(server-side forms, ASP.NET __VIEWSTATE chains), the script replays the recorded chain of document
requests with fetch() from the GET page it started at: typed values become params, each hidden input is
read again from the page before it on every run (fresh server state, never the recorded value), other
fields are sent as recorded; then the agent's code runs on the last page (DOMParser; `document` is that
page). Refused when a post goes to another origin, is not urlencoded or was cut off, posts a
credential-looking field (password, token …: its value would land in the recipe), does not look like a
read (without --write), or a non-hidden field holds an id-like value from an earlier page.

PATH 3 - the browser actions (a `steps` recipe, recipe.py STEPS). When paths 1 / 2 / 2b produce no recipe
(none applies, or path 2 / 2b can't replay it: the evaluate ran in a frame, the answer depends on values typed
into the page, the form chain is not a plain same-origin urlencoded one ...), the recorded commands from the
last navigate before the extraction up to it become steps. The extraction is the last `read` or `evaluate`
whose result holds every --expect (a read keeps its selector / query / frame). Kept: navigate, click, fill,
select, evaluate, with a ref replaced by the selector it resolved to when it ran (the daemon records it) and a
`dialog` arg moved to the step; `select {label}` becomes `select {value}` (which matches a label too). Dropped:
failed commands and look-only ones (snapshot, read, wait, screenshot, list_tabs, dialog ...). A click / select /
fill / evaluate after which the tab's URL path changed (seen in the later commands' URLs: url_after is read
right after a click, often before the navigation commits) gets expect {url: that path}; else a click / select
gets expect {selector: the next step's target, state: attached}. Params: --param, else the values typed (fill,
select; named after the field: its id / name, a role selector's ASCII name, else its role) and the navigate
URLs' values; templated where a whole value: a fill / select value, a wait text, a read query, a URL segment /
query value, a string literal in evaluate code (-> args.NAME). Refused: a fill into a password / one-time-code
field, a flow that used attach_tab / find_tab / handoff / captcha_text / cdp / network_capture, a ref whose
selector was not recorded, a credential-looking query field in a navigate URL, and a non-GET request in the
flow that does not look like a read (main document, a form posted inside a frame, xhr / fetch) unless --write
(then writes: true). Derived values are no problem here: the browser carries them from page to page itself.

PARAMS. --param name=value as given (each must land in the request / code / URL, else learn fails).
Without --param, candidates come from the recording: values the agent typed (fill, select), then the
query values and the last path segment of the URLs it navigated to and of the page the API was called
from. A candidate is kept only where its value is a WHOLE value: a URL path segment, a query value, a
JSON string / number, a form field, a whole JS string literal in the code. A value that only occurs
inside a longer one is never replaced (agent-browser templated "an" inside a sec-ch-ua header).

REFUSED (learn fails with a reason and a hint, nothing saved):
  - DERIVED VALUES: an id-like value of the request (a digit and >= 4 chars, or >= 16 chars) that is not a
    param but occurs as a whole value in an EARLIER recorded response: a replay with new params would
    silently reuse the old one (agent-browser's second pitfall). Pass it as --param (it becomes a required
    param) or learn from a page whose URL holds it. Path 1 first tries the next JSON responses that hold the
    answer (up to MAX_API_TRIES); only when none passes is the first refusal reported.
  - a non-GET request that does not look like a read (search / query / filter / graphql query …),
    unless --write (then the recipe is saved with writes: true and every run needs --allow-write).
  - PATH 2 when the answer depends on values typed into the page that are neither in its URL nor in the
    code (and no form post carried them), or the evaluate ran in a frame: path 3 is tried instead.
  - an evaluate whose code was cut off in the recording.
"""
import json
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from . import draft as D

MIN_VALUE = 2
MAX_API_TRIES = 8          # path 1: candidate responses tried after a refused one (derived value)
SCRIPT_WAIT_MS = 10000
_READ_WORDS = re.compile(r"search|quer(y|ies)|filter|find|list|lookup|suggest|autocomplete|graphql|browse|facet",
                         re.I)
_CONFIG_NAME = re.compile(r"(app(lication)?|client|site|tenant|project|account|org)[-_]?id$|agent|version|locale"
                          r"|^lang", re.I)
_FILE_EXT = re.compile(r"\.(html?|aspx?|php|jsp|cgi|xml)$", re.I)
_JS_STRING = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")


class LearnError(ValueError):
    """fallback=True: path 2 / 2b can't do it but a steps recipe (path 3) might; False: a refusal that holds
    for every path (a credential, a write without --write)."""

    def __init__(self, reason, hint="", fallback=False):
        super().__init__(reason)
        self.reason, self.hint, self.fallback = reason, hint, fallback


# ---------- params ----------

def param_name(raw, fallback="value"):
    """A selector / query key / path word -> a param name (letters, digits, _)."""
    s = str(raw or "")
    m = (re.search(r"""name\s*=\s*['"]?([\w-]+)""", s) or re.search(r"#([\w-]+)", s)
         or re.search(r"""id\s*=\s*['"]?([\w-]+)""", s) or re.search(r"([A-Za-z][\w-]*)\W*$", s))
    n = re.sub(r"[^A-Za-z0-9_]", "_", m.group(1) if m else s).strip("_")
    if not n:
        return fallback
    if n[0].isdigit():
        n = "p_" + n
    return n


def _singular(word):
    w = param_name(word, "path")
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _url_candidates(url, source="url"):
    out = []
    p = urllib.parse.urlsplit(url or "")
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False):
        if not D.credential_name(k):
            out.append((param_name(k, "q"), v, source))
    segs = [urllib.parse.unquote(x) for x in p.path.split("/") if x]
    if segs and not _FILE_EXT.search(segs[-1]):
        out.append((_singular(segs[-2]) if len(segs) > 1 else "path", segs[-1], source))
    return out


def candidate_list(rec, extra_urls=()):
    """-> [(name, value, source)] in priority order: "typed" (fill / select values), "url" (the URLs the
    agent navigated to), "page" (URLs the site moved to: navigate's final URL, the page an API was called
    from). A value is listed once (its first source); names are made unique."""
    raw = []
    for c in rec.get("commands") or []:
        a, args = c.get("action"), c.get("args") or {}
        if a == "fill" and not args.get("redacted") and isinstance(args.get("value"), str):
            raw.append((param_name(args.get("selector")), args["value"], "typed"))
        elif a == "select" and c.get("ok"):
            sel = ((c.get("result") or {}).get("selected"))
            vals = sel if isinstance(sel, list) else [sel]
            for v in vals:
                if isinstance(v, dict) and isinstance(v.get("value"), str):
                    raw.append((param_name(args.get("selector")), v["value"], "typed"))
    navs = [c for c in rec.get("commands") or [] if c.get("action") == "navigate"]
    for c in navs:
        raw += _url_candidates((c.get("args") or {}).get("url"), "url")
    for c in navs:
        raw += _url_candidates(c.get("url_after"), "page")
    for u in extra_urls:
        raw += _url_candidates(u, "page")
    out, names, seen = [], set(), set()
    for name, value, src in raw:
        if not isinstance(value, str) or len(value) < MIN_VALUE or value in seen or "<redacted" in value:
            continue
        seen.add(value)
        n, k = name, 2
        while n in names:
            n, k = f"{name}{k}", k + 1
        names.add(n)
        out.append((n, value, src))
    return out


def candidates(rec, extra_urls=()):
    """-> {name: value} of candidate_list."""
    return {n: v for n, v, _ in candidate_list(rec, extra_urls)}


def _finish_params(plist, values, required):
    """Draft params -> descriptions with the recorded value; a candidate the site put in the URL by itself
    (not typed, not in a URL the agent asked for) gets that value as its default instead of being required."""
    for p in plist:
        v = values.get(p["name"], "")
        p["description"] = f"recorded with {p['name']}={v[:60]!r}".replace("'", '"')
        if p["name"] in required:
            p["required"] = True
            p.pop("default", None)
        else:
            p["required"] = False
            p["default"] = v
    return plist


def parse_params(pairs):
    out = {}
    for kv in pairs or []:
        k, eq, v = kv.partition("=")
        if not eq or not k:
            raise LearnError(f"--param wants name=value, got {kv!r}")
        if not D._NAME.match(k):
            raise LearnError(f"param name {k!r} must be letters, digits and _")
        out[k] = v
    return out


def _placed(obj, name):
    s = json.dumps(obj, ensure_ascii=False)
    return "{" + name + "}" in s or "{" + name + "|url}" in s


# ---------- derived values (pitfall 2) ----------

def _id_like(v):
    return isinstance(v, str) and (len(v) >= 16 or (len(v) >= 4 and any(ch.isdigit() for ch in v)))


def request_atoms(request):
    """Whole values of a templated request that are not placeholders -> [(where, value)]."""
    out = []
    p = urllib.parse.urlsplit(request.get("url", ""))
    for seg in p.path.split("/"):
        if seg and "{" not in seg:
            out.append(("url path", urllib.parse.unquote(seg)))
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False):
        if "{" not in v:
            out.append((f"query {k}", v))
    body = request.get("body")

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}.{i}")
        elif isinstance(x, str):
            if "{" in x and "}" in x and D._FORMLIKE.fullmatch(x):
                for k, v in urllib.parse.parse_qsl(x):
                    if "{" not in v:
                        out.append((f"body {path}.{k}", v))
            elif "{" not in x:
                out.append((f"body {path}", x))
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            out.append((f"body {path}", str(x)))
    if isinstance(body, (dict, list)):
        walk(body, "")
    elif isinstance(body, str):
        try:
            walk(json.loads(body), "")
        except ValueError:
            for k, v in urllib.parse.parse_qsl(body):
                if "{" not in v:
                    out.append((f"body {k}", v))
    return out


def _in_body(item, value):
    p = item.get("body_path")
    if not p:
        return False
    try:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if "json" in (item.get("mime") or ""):
        try:
            return any(exact for _, exact in D.find_paths(json.loads(text), value))
        except ValueError:
            pass
    return any(q + value + q in text for q in ('"', "'"))


def derived_values(request, picked, responses, known=(), keep=()):
    """-> [(where, value, url of the earlier response)]: id-like whole values of the request found in a
    response recorded before it, that are not URL / typed values (`known`), not in a field named by
    --keep-param (`keep`: declared a public key) and not in a field whose name says it is the site's
    configuration (an app / client id, an agent or version string) when it was found in a page's HTML
    (site config; a value from an earlier JSON response is flagged whatever its field is called)."""
    known = set(known)
    earlier = [r for r in responses if r.get("i", 0) < picked.get("i", 0) and r is not picked]
    out = []
    for where, v in request_atoms(request):
        field = re.split(r"[ .]", where)[-1]
        if not _id_like(v) or v in known or field in keep:
            continue
        config = bool(_CONFIG_NAME.search(field))
        src = next((r for r in reversed(earlier) if _in_body(r, v)
                    and not (config and "json" not in (r.get("mime") or ""))), None)
        if src is not None:
            out.append((where, v, D.clean_url(src.get("url", ""))))
    return out


def looks_read(request):
    if (request.get("method") or "GET").upper() == "GET":
        return True
    body = request.get("body")
    q = body.get("query") if isinstance(body, dict) else None
    if isinstance(q, str) and not q.lstrip().startswith("mutation"):
        return True
    return bool(_READ_WORDS.search(urllib.parse.urlsplit(request.get("url", "")).path))


# ---------- path 1: JSON API ----------

def _entry_of(url):
    p = urllib.parse.urlsplit(url or "")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))


def plan_api(rec, expects, explicit, keep=(), name=None, host_ok=None, write=False, description=None):
    """-> plan {kind: "api", draft, args, report} | None when no JSON response holds every expect
    (then the draft report says why). Raises LearnError for the refusals above."""
    capture = {"url": rec.get("url"), "requests": rec.get("responses") or []}
    pages = list(dict.fromkeys(r.get("page_url") for r in capture["requests"] if r.get("page_url")))
    if explicit:
        params, required = dict(explicit), set(explicit)
    else:
        cl = candidate_list(rec, pages[-3:])
        params, required = {n: v for n, v, _ in cl}, {n for n, _, s in cl if s != "page"}
    all_params = dict(params)
    skip, refused = set(), None
    # the best-ranked response can be refused (a derived value) while another one that also holds the answer is
    # fine (crates.io: /api/v1/crates/<c>/<version>/... ranks first, /api/v1/crates/<c> holds the version too):
    # try the next candidates before giving up; the first refusal is the one reported
    for _ in range(MAX_API_TRIES):
        params = dict(all_params)
        try:
            rep = D.build(capture, expects, params, name=name, host_ok=host_ok, keep=keep, description=description,
                          skip=skip)
        except D.DraftError as ex:
            raise LearnError(str(ex)) from None
        if not rep["applicable"]:
            if refused is not None:
                raise refused
            return None, rep
        d = rep["draft"]
        placed = {n for n in params if _placed(d["request"], n)}
        if explicit and placed != set(params):
            if refused is not None:
                raise refused
            miss = sorted(set(params) - placed)
            raise LearnError(f"--param {', '.join(miss)}: value not found as a whole value in the request that holds "
                             f"the answer ({D.clean_url(_item(rep, capture)['url'])[:120]})",
                             "check the value, or leave --param out to let learn pick the typed / URL values")
        if not explicit and placed != set(params):
            params = {n: v for n, v in params.items() if n in placed}
            rep = D.build(capture, expects, params, name=name, host_ok=host_ok, keep=keep, description=description,
                          skip=skip)
            d = rep["draft"]
        it = _item(rep, capture)
        if "<redacted>" in json.dumps(d["request"].get("body"), ensure_ascii=False):
            # the recorded body held a secret (recording.redact_post): replaying "<redacted>" can't work
            raise LearnError("the request that holds the answer carries a value typed into a password / secret "
                             "field; it is not recorded, so it can't be replayed",
                             "recipes that need secrets are out of scope for learn: write one by hand with "
                             "{secret:NAME} (see recipe.py: login)")
        known = set(params.values()) | set(candidates(rec, pages).values())
        der = derived_values(d["request"], it, capture["requests"], known, set(keep or ()))
        if not der:
            break
        where, v, src = der[0]
        refused = refused or LearnError(
            f"the request's {where} = {v[:40]!r} came from an earlier response ({src[:90]}); a replay "
            "with new params would reuse it",
            f"pass it as --param NAME={v[:40]} (a required param), or learn from a page whose URL holds it")
        skip.add(it.get("i"))
    else:
        raise refused
    if it.get("page_url"):
        d["entry_url"] = _entry_of(it["page_url"])
    if d.get("writes"):
        if write:
            d["writes"] = True
        elif looks_read(d["request"]):
            d["writes"] = False
            d["notes"].append(f"{d['request']['method']} treated as a read (learn: the URL / body looks like a query)")
        else:
            raise LearnError(f"the answer came from a {d['request']['method']} to "
                             f"{urllib.parse.urlsplit(d['request']['url']).path[:80]} that may change something; "
                             "it was not replayed",
                             "if replaying it is safe, learn again with --write (runs then need --allow-write)")
    _finish_params(d.get("params", []), params, required)
    d["when_to_use"] = "TODO: when an agent should pick this recipe"
    return {"kind": "api", "draft": d, "args": params, "report": rep, "request_item": it}, rep


def _item(rep, capture):
    i = rep.get("picked", {}).get("i")
    return next((r for r in capture["requests"] if r.get("i") == i), {})


# ---------- path 2: the agent's evaluate ----------

def _result_has(c, expects):
    if "result" in c:
        return all(D.find_paths(c["result"], e) for e in expects)
    prev = c.get("result_preview") or ""
    return all(e in prev or json.dumps(e, ensure_ascii=False)[1:-1] in prev for e in expects)


def _unescape(lit):
    try:
        return json.loads('"' + lit[1:-1].replace('"', '\\"').replace("\\'", "'") + '"') if lit[0] == "'" \
            else json.loads(lit)
    except ValueError:
        return None


def template_code(code, params):
    """Whole JS string literals equal to a param value -> args.NAME. -> (code, {name: hits})."""
    hits = {}

    def sub(m):
        val = _unescape(m.group(0))
        name = next((n for n, v in params.items() if v == val and v != ""), None)
        if name is None:
            return m.group(0)
        hits[name] = hits.get(name, 0) + 1
        return f"args.{name}"
    return _JS_STRING.sub(sub, code), hits


def wrap_script(code):
    """The agent's evaluate code (an expression or a function) as a recipe script body that waits until it
    returns something non-empty (SCRIPT_WAIT_MS)."""
    body = code.strip().rstrip(";").strip()
    tries = max(1, SCRIPT_WAIT_MS // 250)
    return (f"const __run = async () => {{ const v = (\n{body}\n); return typeof v === 'function' ? await v() : await v; }};\n"
            "const __empty = v => v == null || v === '' || (Array.isArray(v) && !v.length)"
            " || (typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length);\n"
            f"let __v, __e;\nfor (let i = 0; i < {tries}; i++) {{\n"
            "  try { __v = await __run(); __e = null; if (!__empty(__v)) return __v; } catch (e) { __e = e; }\n"
            "  await new Promise(r => setTimeout(r, 250));\n}\n"
            "if (__e) throw __e;\nreturn __v;")


def plan_script(rec, expects, explicit, name=None, description=None, write=False):
    """-> plan {kind: "script", draft, args} | None when no evaluate result holds every expect.
    Raises LearnError for the refusals above."""
    cmds = rec.get("commands") or []
    evs = [c for c in cmds if c.get("action") == "evaluate" and c.get("ok") and (c.get("args") or {}).get("code")]
    hits = [c for c in evs if _result_has(c, expects)]
    if not hits:
        return None
    c = hits[-1]
    args = c["args"]
    if args.get("frame"):
        raise LearnError("the evaluate that holds the answer ran in a frame; script recipes run in the top page",
                         "evaluate the same code without frame (if the frame is same-origin), then learn again", fallback=True)
    if args.get("code_truncated"):
        raise LearnError("the evaluate code was too long to record", "shorten it, run it again, then learn", fallback=True)
    page_url = c.get("url_before") or c.get("url_after") or rec.get("url") or ""
    if not page_url.startswith("http"):
        raise LearnError("the page the evaluate ran on is unknown", "navigate to the page, evaluate again, learn", fallback=True)
    base = page_url.split("#")[0]
    docs = [r for r in rec.get("responses") or [] if r.get("type") == "document" and not r.get("subframe")
            and r.get("url", "").split("#")[0] == base]
    if docs and (docs[-1].get("method") or "GET").upper() != "GET":
        return plan_form(rec, c, docs[-1], explicit, name, description, write)
    if explicit:
        params, required = dict(explicit), set(explicit)
    else:
        cl = candidate_list(rec, [base])
        params, required = {n: v for n, v, _ in cl}, {n for n, _, s in cl if s != "page"}
    code, chits = template_code(args["code"], params)
    url_t, uhits, _ = D.template_url(base, params)
    placed = set(chits) | set(uhits)
    if explicit and placed != set(params):
        miss = sorted(set(params) - placed)
        raise LearnError(f"--param {', '.join(miss)}: value not found as a whole value in the page URL or as a "
                         "string literal in the evaluate code", "check the value, or leave --param out", fallback=True)
    last_nav = max((x["n"] for x in cmds if x.get("action") == "navigate" and x["n"] < c["n"]), default=0)
    typed = {v for x in cmds if x["n"] > last_nav and x["n"] < c["n"] and x.get("action") in ("fill", "select")
             for v in candidates({"commands": [x]}).values()}
    lost = sorted(v for v in typed if v not in {params[n] for n in placed})
    if lost:
        raise LearnError(f"the answer depends on values typed into the page ({', '.join(x[:30] for x in lost[:3])}) "
                         "that are neither in its URL nor in the code", "learn path 3 (a steps "
                         "recipe) replays such a flow in the browser", fallback=True)
    params = {n: params[n] for n in params if n in placed}
    host = urllib.parse.urlsplit(base).hostname or "site"
    path = urllib.parse.urlsplit(base).path or "/"
    d = {"name": name, "description": description or f"{host}{path[:60]}: the page's data, read by the code the "
         "agent ran on its first visit (learn)",
         "when_to_use": "TODO: when an agent should pick this recipe",
         "entry_url": url_t, "at_entry": True,
         "params": _finish_params([{"name": n} for n in params], params, required),
         "script": wrap_script(code), "writes": False,
         "notes": ["learned from an evaluate (deja-browser learn); review the description"]}
    if not params:
        d.pop("params")
    return {"kind": "script", "draft": d, "args": params}


# ---------- path 2b: a form that posts back (server-side forms, ASP.NET __VIEWSTATE chains) ----------

class _Hidden(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and (a.get("type") or "").lower() == "hidden" and a.get("name") is not None:
            self.fields.setdefault(a["name"], a.get("value") or "")


def hidden_fields(html):
    """{name: value} of the hidden inputs of an HTML page (the first one of each name)."""
    p = _Hidden()
    try:
        p.feed(html or "")
    except Exception:
        pass
    return p.fields


def _text(item):
    try:
        return Path(item["body_path"]).read_text(encoding="utf-8", errors="replace") if item.get("body_path") else None
    except OSError:
        return None


def _js(v):
    return json.dumps(v, ensure_ascii=False)


def plan_form(rec, c, last_doc, explicit, name=None, description=None, write=False):
    """The evaluate ran on a page that a form POST produced. Replay the chain of document requests that led
    there, inside the page with fetch(): the GET page the chain starts from, then every POST, each one's
    hidden fields read again from the page before it (fresh server state, never the recorded value), the
    typed values as params, everything else as recorded; then the agent's code on the last page, parsed
    with DOMParser (its `document` is that page). -> plan {kind: "form", ...}; LearnError when the chain can't
    be replayed."""
    docs = [r for r in rec.get("responses") or [] if r.get("type") == "document" and not r.get("subframe")
            and r.get("i", 0) <= last_doc.get("i", 0)]
    chain = []
    for r in reversed(docs):
        chain.insert(0, r)
        if (r.get("method") or "GET").upper() == "GET":
            break
    hint = "learn path 3 (a steps recipe) replays the flow in the browser instead"
    start = chain[0]
    if (start.get("method") or "GET").upper() != "GET":
        raise LearnError("the form chain's first page was not recorded (the visit must start with navigate)", hint, fallback=True)
    origin = urllib.parse.urlsplit(start.get("url", ""))[:2]
    for r in chain[1:]:
        ct = next((v for k, v in (r.get("req_headers") or {}).items() if k.lower() == "content-type"), "")
        if urllib.parse.urlsplit(r.get("url", ""))[:2] != origin:
            raise LearnError(f"the form posts to another origin ({D.clean_url(r.get('url', ''))[:80]})", hint, fallback=True)
        if "x-www-form-urlencoded" not in ct or r.get("post_data_truncated") or r.get("post_data") is None:
            raise LearnError("the form post is not a plain urlencoded form (or its body was cut off)", hint, fallback=True)
        if not write and not looks_read({"method": "POST", "url": r.get("url", "")}):
            raise LearnError(f"the form posts to {urllib.parse.urlsplit(r.get('url', '')).path[:80]}, which may change "
                             "something; it was not replayed",
                             "if replaying it is safe, learn again with --write (runs then need --allow-write)")
    if explicit:
        params, required = dict(explicit), set(explicit)
    else:
        cl = candidate_list(rec)
        params, required = {n: v for n, v, s in cl if s == "typed"}, {n for n, _, s in cl if s == "typed"}
    by_value = {v: n for n, v in params.items()}
    lines = ["const __parse = h => new DOMParser().parseFromString(h, 'text/html');",
             "const __get = async u => __parse(await (await fetch(u, {credentials: 'include'})).text());",
             "const __post = async (u, f) => __parse(await (await fetch(u, {method: 'POST', credentials: 'include', "
             "headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: new URLSearchParams(f)})).text());",
             "const __hidden = (d, n) => { const e = d.querySelector(`input[name=\"${CSS.escape(n)}\"]`); "
             "if (!e) throw new Error('form field ' + n + ' is gone: the site changed'); return e.value; };",
             f"let d0 = await __get({_js(start['url'])});"]
    placed, prev_hidden = set(), hidden_fields(_text(start))
    for j, r in enumerate(chain[1:], 1):
        pairs = []
        for k, v in urllib.parse.parse_qsl(r["post_data"], keep_blank_values=True):
            if v in by_value and v != "":
                pairs.append(f"[{_js(k)}, args.{by_value[v]}]")
                placed.add(by_value[v])
            elif k in prev_hidden and prev_hidden[k] == v:
                pairs.append(f"[{_js(k)}, __hidden(d{j - 1}, {_js(k)})]")
            elif (D.credential_name(k) and v != "") or v == "<redacted>":
                raise LearnError(f"the form posts a credential-looking field ({k}); its value would be saved in the "
                                 "recipe", "recipes that need secrets are out of scope for learn: write one by hand "
                                 "with {secret:NAME} (see recipe.py: login)")
            else:
                src = next((x for x in reversed(chain[:j]) if _id_like(v) and _in_body(x, v)), None)
                if src is not None:
                    raise LearnError(f"the form field {k} = {v[:40]!r} came from an earlier page "
                                     f"({D.clean_url(src.get('url', ''))[:80]}) but is not a hidden input of the page "
                                     "before; a replay would reuse it", f"pass it as --param NAME={v[:40]}, or {hint}", fallback=True)
                pairs.append(f"[{_js(k)}, {_js(v)}]")
        lines.append(f"let d{j} = await __post({_js(r['url'])}, [{', '.join(pairs)}]);")
        prev_hidden = hidden_fields(_text(r))
    if explicit and placed != set(params):
        raise LearnError(f"--param {', '.join(sorted(set(params) - placed))}: value not found in the form posts", "", fallback=True)
    lost = sorted(v for n, v in params.items() if n not in placed)
    if lost:
        raise LearnError(f"the answer depends on values typed into the page ({', '.join(x[:30] for x in lost[:3])}) "
                         "that the form posts don't carry", hint, fallback=True)
    params = {n: params[n] for n in params if n in placed}
    body = c["args"]["code"].strip().rstrip(";").strip()
    lines.append(f"return await ((document) => {{ const v = (\n{body}\n); "
                 f"return typeof v === 'function' ? v() : v; }})(d{len(chain) - 1});")
    p = urllib.parse.urlsplit(start["url"])
    d = {"name": name, "description": description or f"{p.hostname}{p.path[:60]}: a form ({len(chain) - 1} post"
         f"{'s' if len(chain) > 2 else ''}) replayed with fetch, read by the code the agent ran on its first visit (learn)",
         "when_to_use": "TODO: when an agent should pick this recipe",
         "entry_url": start["url"].split("#")[0], "at_entry": True,
         "params": _finish_params([{"name": n} for n in params], params, required),
         "script": "\n".join(lines), "writes": bool(write),
         "notes": ["learned from a form post-back chain + an evaluate (deja-browser learn); hidden fields are read "
                   "again from each page on every run"]}
    if not params:
        d.pop("params")
    return {"kind": "form", "draft": d, "args": params}


# ---------- path 3: a steps recipe (the recorded browser actions, replayed in the browser) ----------

STEP_KEEP = ("navigate", "click", "fill", "select", "evaluate")
STEP_REFUSE = {"attach_tab": "switched to a tab by id", "find_tab": "borrowed another tab",
               "handoff": "handed the tab to a human (what they did can't be replayed)",
               "captcha_text": "solved a text CAPTCHA", "cdp": "sent a raw CDP command",
               "network_capture": "ran network_capture (its trigger acts on the page)"}
_DROP_ARGS = ("then_snapshot", "quiet", "redacted", "code_truncated", "selector_resolved", "frame_resolved",
              "newTab", "dialog")
_TEMPLATED = ("value", "label", "text", "query")
STEP_WAIT_MS = 15000
_REF = re.compile(r"^@?(e[0-9]+)$")
_ROLE_SEL = re.compile(r'internal:role=([a-z]+)(?:\[name="((?:[^"\\]|\\.)*)"[si]?\])?')


def step_param_name(sel, fallback="value"):
    """Param name for a typed value from its (stable) selector: the last part after the frame hops; a role
    selector gives its accessible name (when it has ASCII letters) or its role."""
    segs = [x.strip() for x in str(sel or "").split(">>")]
    segs = [x for x in segs if x and not x.startswith(("nth=", "internal:control="))]
    last = segs[-1] if segs else str(sel or "")
    m = _ROLE_SEL.match(last)
    if m:
        words = re.sub(r"[^A-Za-z0-9]+", "_", m.group(2) or "").strip("_").lower()
        n = words if words and not words[0].isdigit() else ""
        return n or m.group(1)
    return param_name(last, fallback)


def _stable(c, key):
    """A command's selector / frame arg with a ref replaced by what it resolved to when it ran."""
    args = c.get("args") or {}
    v = args.get(key)
    if isinstance(v, str) and _REF.match(v.strip()):
        r = args.get(f"{key}_resolved")
        if not r:
            raise LearnError(f"command {c.get('n')} ({c.get('action')}) used the ref {v} and its selector was not "
                             "recorded", "do the visit again (the daemon records what a ref resolved to), or use "
                             "CSS selectors")
        return r
    return v


def _path_of(url):
    return urllib.parse.urlsplit(url or "").path or "/"


def _step_args(c):
    args = c.get("args") or {}
    out = {k: v for k, v in args.items() if k not in _DROP_ARGS}
    for k in ("selector", "frame"):
        if args.get(k):
            out[k] = _stable(c, k)
    if c.get("action") == "select" and "label" in out and "value" not in out:
        out["value"] = out.pop("label")          # select {value} also matches a label: a new param may be either
    return out


def _steps_candidates(flow):
    raw = []
    for c in flow:
        a, args = c.get("action"), c.get("args") or {}
        if a == "fill" and isinstance(args.get("value"), str):
            raw.append((step_param_name(_stable(c, "selector")), args["value"], "typed"))
        elif a == "select":
            v = next((args[k] for k in ("value", "label") if isinstance(args.get(k), str)), None)
            if v is not None:
                raw.append((step_param_name(_stable(c, "selector")), v, "typed"))
    for c in flow:
        if c.get("action") == "navigate":
            raw += _url_candidates((c.get("args") or {}).get("url"), "url")
    out, names, seen = [], set(), set()
    for name, value, src in raw:
        if len(value) < MIN_VALUE or value in seen or "<redacted" in value:
            continue
        seen.add(value)
        n, k = name, 2
        while n in names:
            n, k = f"{name}{k}", k + 1
        names.add(n)
        out.append((n, value, src))
    return out


def _template_step(step, params, hits):
    """Whole values equal to a param -> {name} (navigate url: {name|url} segments / query values; evaluate
    code: string literals -> args.NAME, the code wrapped as a function of args). Counts placements in hits."""
    a = step["args"]
    by_value = {v: n for n, v in params.items() if v != ""}

    def whole(v):
        n = by_value.get(v) if isinstance(v, str) else None
        if n is None:
            return v
        hits[n] = hits.get(n, 0) + 1
        return "{" + n + "}"
    if step["action"] == "navigate":
        q = urllib.parse.urlsplit(a["url"]).query
        bad = [k for k, _ in urllib.parse.parse_qsl(q, keep_blank_values=True) if D.credential_name(k)]
        if bad:
            raise LearnError(f"the flow navigates to a URL with a credential-looking query field ({bad[0]})",
                             "recipes that need secrets are out of scope for learn")
        url_t, uh, _ = D.template_url(a["url"], params)
        for n, k in uh.items():
            hits[n] = hits.get(n, 0) + k
        a["url"] = url_t
    for k in _TEMPLATED:
        if k in a:
            a[k] = whole(a[k])
    if step["action"] == "evaluate" and isinstance(a.get("code"), str):
        code, ch = template_code(a["code"], params)
        if ch:
            body = code.strip().rstrip(";").strip()
            a["code"] = (f"async (args) => {{ const __v = (\n{body}\n); "
                         "return typeof __v === 'function' ? await __v(args) : await __v; }")
            for n, k in ch.items():
                hits[n] = hits.get(n, 0) + k
    exp = step.get("expect")
    if exp and "text" in exp:
        exp["text"] = whole(exp["text"])
    return step


def _flow_posts(rec, t_lo, t_hi):
    """Non-GET requests recorded while the flow ran (main documents, form posts inside frames, xhr / fetch)
    -> [(response item, looks like a read?)]."""
    out = []
    for r in rec.get("responses") or []:
        if (r.get("method") or "GET").upper() == "GET" or not (t_lo < float(r.get("at") or 0) <= t_hi):
            continue
        body = r.get("post_data")
        try:
            body = json.loads(body) if isinstance(body, str) else body
        except ValueError:
            pass
        out.append((r, looks_read({"method": r.get("method"), "url": r.get("url", ""), "body": body})))
    return out


def _moved_to(flow, cmds, i):
    """The URL the page moved to because of flow[i] (another path than before it), or None. A command's
    url_after is read right after it, often before the navigation commits: later commands up to the next
    step say where the tab really went."""
    c, nxt = flow[i], flow[i + 1]
    seen = [c.get("url_after")] + [x.get(k) for x in cmds if c["n"] < x["n"] < nxt["n"]
                                   for k in ("url_before", "url_after")] + [nxt.get("url_before")]
    if nxt["action"] in ("read", "wait", "fill") or i + 1 == len(flow) - 1:
        seen.append(nxt.get("url_after"))
    before = _path_of(c.get("url_before"))
    return next((u for u in seen if u and u.startswith("http") and _path_of(u) != before), None)


def plan_steps(rec, expects, explicit, name=None, description=None, write=False):
    """PATH 3. -> plan {kind: "steps", draft, args} | None when no read / evaluate result holds every expect.
    The flow = the commands from the last navigate before that extraction up to it: navigate / click / fill /
    select / evaluate kept (refs replaced by the selector they resolved to), failed commands and look-only
    ones (snapshot, read, wait, screenshot, list_tabs ...) dropped; each step that changed the page gets an
    `expect` (the URL path it moved to, else the next step's target). Raises LearnError for the refusals."""
    cmds = rec.get("commands") or []
    ext = next((c for c in reversed(cmds) if c.get("action") in ("read", "evaluate") and c.get("ok")
                and _result_has(c, expects)), None)
    if ext is None:
        return None
    if ext["action"] == "evaluate" and (ext.get("args") or {}).get("code_truncated"):
        raise LearnError("the evaluate code was too long to record", "shorten it, run it again, then learn")
    start = next((c for c in reversed(cmds) if c.get("action") == "navigate" and c.get("ok") and c["n"] < ext["n"]),
                 None)
    if start is None:
        raise LearnError("the flow that led to the answer does not start with navigate",
                         "do the visit again from a navigate (same --session), then learn")
    flow = []
    for c in cmds:
        if not start["n"] <= c["n"] < ext["n"]:
            continue
        a = c.get("action")
        if a in STEP_REFUSE:
            raise LearnError(f"the flow {STEP_REFUSE[a]} (command {c['n']}); a steps recipe can't replay that",
                             "do the visit again without it, then learn")
        if not c.get("ok") or a not in STEP_KEEP:
            continue                              # failed, or only looked at the page
        if a == "fill" and (c.get("args") or {}).get("redacted"):
            raise LearnError("the flow typed into a password / one-time-code field; its value can't be saved",
                             "recipes that need secrets are out of scope for learn: write one by hand with a "
                             "login block (see recipe.py)")
        flow.append(c)
    flow.append(ext)
    steps = []
    for c in flow:
        st = {"action": c["action"], "args": _step_args(c)}
        dlg = (c.get("args") or {}).get("dialog")
        if dlg is not None:
            st["dialog"] = dlg
        steps.append(st)
    for i, c in enumerate(flow[:-1]):             # expect: the page a step moved to, else the next step's target
        if c["action"] not in ("click", "select", "evaluate", "fill") or steps[i + 1]["action"] == "navigate":
            continue
        moved = _moved_to(flow, cmds, i)
        nargs = steps[i + 1]["args"]
        if moved:
            steps[i]["expect"] = {"url": _path_of(moved), "timeout": STEP_WAIT_MS}
        elif c["action"] in ("click", "select") and nargs.get("selector"):
            e = {"selector": nargs["selector"], "state": "attached", "timeout": STEP_WAIT_MS}
            if nargs.get("frame"):
                e["frame"] = nargs["frame"]
            steps[i]["expect"] = e
    if explicit:
        params, required = dict(explicit), set(explicit)
    else:
        cl = _steps_candidates(flow)
        params, required = {n: v for n, v, _ in cl}, {n for n, _, s in cl if s != "page"}
    hits = {}
    for st in steps:
        _template_step(st, params, hits)
    placed = set(hits)
    if explicit and placed != set(params):
        miss = sorted(set(params) - placed)
        raise LearnError(f"--param {', '.join(miss)}: value not found as a whole value in the flow (a typed value, "
                         "a URL segment / query value, a string literal in evaluate code)",
                         "check the value, or leave --param out to let learn pick the typed / URL values")
    params = {n: v for n, v in params.items() if n in placed}
    for st in steps:                              # an expect's URL path may hold a param too
        e = st.get("expect")
        if e and "url" in e:
            e["url"] = D.template_url("http://h" + e["url"], params)[0][len("http://h"):]
    posts = _flow_posts(rec, float(start.get("at") or 0), float(ext.get("at") or 0) + 1.0)
    changes = [r for r, read in posts if not read]
    if changes and not write:
        w = changes[0]
        raise LearnError(f"the flow sent a {w.get('method')} to {_path_of(w.get('url'))[:80]} that may change "
                         "something; it was not replayed",
                         "if replaying it is safe, learn again with --write (runs then need --allow-write)")
    p0 = urllib.parse.urlsplit(steps[0]["args"]["url"])
    d = {"name": name, "description": description or f"{p0.hostname}{p0.path[:60]}: a flow of {len(steps)} browser "
         "steps replayed in the session; output = the last step (learn)",
         "when_to_use": "TODO: when an agent should pick this recipe",
         "entry_url": steps[0]["args"]["url"],
         "params": _finish_params([{"name": n} for n in params], params, required),
         "steps": steps, "writes": bool(write),
         "notes": ["learned from the recorded browser actions (deja-browser learn, path 3); review the steps"]}
    if not params:
        d.pop("params")
    return {"kind": "steps", "draft": d, "args": params}


def found_in_documents(rec, expects):
    """URLs of recorded non-JSON responses (the page HTML) that contain every expect."""
    out = []
    for r in rec.get("responses") or []:
        if "json" in (r.get("mime") or "") or not r.get("body_path"):
            continue
        try:
            text = Path(r["body_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if all(e in text for e in expects):
            out.append(D.clean_url(r.get("url", "")))
    return out


def compact(obj, limit=400):
    """JSON of obj within `limit` chars: long strings are cut (reason / hint first) until it fits."""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= limit:
        return obj
    obj = dict(obj)
    for k in ("hint", "reason"):
        if isinstance(obj.get(k), str):
            over = len(json.dumps(obj, ensure_ascii=False)) - limit
            if over > 0:
                obj[k] = obj[k][:max(40, len(obj[k]) - over - 1)] + "…"
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) > limit and isinstance(obj.get("params"), dict):
        obj["params"] = sorted(obj["params"])
    return obj
