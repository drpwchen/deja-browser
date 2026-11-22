# -*- coding: utf-8 -*-
"""Draft a recipe from a `network_capture` result (`deja-browser draft`).

GOAL: when the data a page shows comes from a JSON API, turn the first visit's capture into a
`request` recipe draft that `deja-browser verify` can run as is, so the agent only reviews and renames
instead of writing JSON by hand. Where there is no such API it says so, and writes nothing.

INPUT
  capture   the JSON that `deja-cdp network_capture '{...}'` printed (saved to a file); its requests
            point at body files. Nothing is fetched and no browser is used: this is a pure function
            of the capture plus the files it names.
  expect    one or more strings the page shows and the answer needs (`--expect`, repeatable, >= 3
            chars). The response that holds ALL of them is the one drafted. This is how the right
            response is picked: by what the answer is, not by how the response looks.
  params    `--param name=value` (repeatable): the value that was used when the capture was taken
            (the search term, the id). Every URL path segment, query value or body string equal to it
            becomes a template placeholder. A URL placeholder is `{name|url}` (percent-encoded when
            filled in), a body placeholder is `{name}`.

OUTPUT (a draft file for `verify` + a JSON report)
  A draft: {name, description, when_to_use, entry_url, params, request{method,url,headers?,body?},
  pick, writes, notes}. `pick` is one of
    {"path": "hits", "fields": ["title"], "limit": 20}        every expect sits in the rows of ONE list
    {"select": {"version": "crate.max_stable_version", ...}}   the expects are single values
  (`select` maps a name to a dotted path; a number in the path indexes a list.)
  The report lists, per expect, where it was found, plus other candidate responses, warnings and the
  next command. Nothing from a response body goes into the draft except field names and paths.

NOT DRAFTED ("applicable": false; no file, exit code 2) when
  - no response with a 2xx status and a JSON body contains every expect (the data is in the HTML,
    or an expect string is wrong). The report says which non-JSON responses do contain it, and that
    a `script` recipe (DOM read) is the way; `read {query}` finds the text on the page.
  - the only responses that qualify are to a refused host, or are POSTs whose captured body was cut off.

SAFETY
  - Header values are never copied, except Content-Type / Accept, and only when they are not
    credential-looking. Header NAMES that were redacted in the capture are listed in `notes`.
  - A query parameter or JSON body key whose name looks like a credential (token, key, secret,
    password, auth, sig, session…) is DROPPED, never copied; its name is listed in `warnings`. The
    entry URL loses its query string. Put real credentials in the secret store and reference them
    from a header as {secret:NAME}.
  - A non-GET request gets "writes": true (a replay needs --allow-write); set it to false by hand if
    that POST only reads.
"""
import json
import re
import urllib.parse
from pathlib import Path

MIN_EXPECT = 3
MAX_BODY = 4_000_000       # bytes of one response body that will be parsed
MAX_MATCHES = 40           # per expect and response
MAX_DEPTH = 12
DEFAULT_LIMIT = 20
KEEP_HEADERS = ("content-type", "accept")

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CRED_SUB = ("token", "secret", "passw", "apikey", "api_key", "api-key", "csrf", "xsrf", "signature",
             "credential", "bearer", "session")
_CRED_WORD = re.compile(r"(?:^|[^a-z])(key|auth|sig|sid|session|jwt|pwd)(?:[^a-z]|$)")


class DraftError(ValueError):
    pass


def credential_name(name):
    n = (name or "").lower()
    return any(s in n for s in _CRED_SUB) or bool(_CRED_WORD.search(n))


# ---------- finding the answer in a body ----------

def find_paths(obj, needle, _path=(), _out=None):
    """Every place `needle` shows up in a parsed JSON value -> [(path tuple, exact)]. A string leaf
    matches when it contains the needle (exact: equals it); a number or bool leaf matches only when
    its text equals the needle. Keys are never searched."""
    out = [] if _out is None else _out
    if len(out) >= MAX_MATCHES or len(_path) > MAX_DEPTH:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_paths(v, needle, _path + (str(k),), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_paths(v, needle, _path + (str(i),), out)
    elif isinstance(obj, str):
        if needle in obj:
            out.append((_path, obj == needle))
    elif obj is not None and str(obj) == needle:
        out.append((_path, True))
    return out


def _is_index(seg):
    return seg.isdigit()


def _order(path):
    """Sort key for a path: list indexes compare as numbers (hits.2 before hits.17)."""
    return tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in path)


def _field_key(path):
    return tuple(s for s in path if not _is_index(s))


def _best_paths(found):
    """found: {expect: [(path, exact)]} -> {expect: chosen path}. Exact beats substring, shallow beats
    deep, a field name several expects share beats one only a single expect has, then name order."""
    votes = {}
    for hits in found.values():
        for fk in {_field_key(p) for p, _ in hits}:
            votes[fk] = votes.get(fk, 0) + 1
    return {e: min(hits, key=lambda h: (not h[1], len(h[0]), -votes[_field_key(h[0])], _order(h[0])))[0]
            for e, hits in found.items()}


def build_pick(paths):
    """Chosen paths (one per expect) -> a recipe `pick` spec."""
    plist = [tuple(p) for p in paths]
    firsts = [next((i for i, s in enumerate(p) if _is_index(s)), None) for p in plist]
    if plist and all(i is not None and i < len(p) - 1 for p, i in zip(plist, firsts)):
        prefixes = {p[:i] for p, i in zip(plist, firsts)}
        if len(prefixes) == 1:
            prefix = prefixes.pop()
            fields = []
            for p, i in zip(plist, firsts):
                if p[i + 1] not in fields:
                    fields.append(p[i + 1])
            limit = max(DEFAULT_LIMIT, max(int(p[i]) for p, i in zip(plist, firsts)) + 1)
            return {"path": ".".join(prefix), "fields": fields, "limit": limit}
    select = {}
    for p in plist:
        words = [s for s in p if not _is_index(s)] or ["value"]
        name = words[-1]
        if name in select and len(words) > 1:
            name = f"{words[-2]}_{words[-1]}"
        while name in select:
            name += "_"
        select[name] = ".".join(p)
    return {"select": select}


# ---------- templating the request ----------

def _split_kv(raw):
    k, _, v = raw.partition("=")
    return k, v, "=" in raw


def _is_dropped(name, keep):
    return credential_name(name) and name not in keep


def _template_query(qs, params, hits, dropped, keep=()):
    out = []
    for piece in qs.split("&"):
        if not piece:
            continue
        k, v, has_eq = _split_kv(piece)
        key = urllib.parse.unquote_plus(k)
        if _is_dropped(key, keep):
            dropped.append(key)
            continue
        val = urllib.parse.unquote_plus(v)
        name = next((n for n, pv in params.items() if pv == val and val != ""), None)
        if name:
            hits[name] = hits.get(name, 0) + 1
            out.append(f"{k}={{{name}|url}}")
        else:
            out.append(piece if has_eq else k)
    return "&".join(out)


def template_url(url, params, keep=()):
    """-> (url template, {param: hits}, [dropped credential-looking query names]). Names in `keep`
    are copied verbatim even if they look like credentials."""
    parts = urllib.parse.urlsplit(url)
    hits, dropped = {}, []
    segs = []
    for seg in parts.path.split("/"):
        val = urllib.parse.unquote(seg)
        name = next((n for n, pv in params.items() if pv == val and val != ""), None)
        if name:
            hits[name] = hits.get(name, 0) + 1
            segs.append(f"{{{name}|url}}")
        else:
            segs.append(seg)
    query = _template_query(parts.query, params, hits, dropped, keep)
    out = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/".join(segs), "", ""))
    if query:
        out += "?" + query
    return out, hits, dropped


_FORMLIKE = re.compile(r"[^=&\s]+=[^&]*(&[^=&\s]+=[^&]*)*")


def _template_json(obj, params, hits, dropped, keep):
    if isinstance(obj, str):
        name = next((n for n, pv in params.items() if pv == obj and obj != ""), None)
        if name:
            hits[name] = hits.get(name, 0) + 1
            return f"{{{name}}}"
        if _FORMLIKE.fullmatch(obj):    # e.g. {"params": "query=rust&page=0"}: a query string inside JSON
            return _template_query(obj, params, hits, dropped, keep)
        return obj
    if isinstance(obj, list):
        return [_template_json(v, params, hits, dropped, keep) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_dropped(k, keep):
                dropped.append(k)
                continue
            out[k] = _template_json(v, params, hits, dropped, keep)
        return out
    return obj


def template_body(post_data, content_type, params, keep=()):
    """-> (body, {param: hits}, [dropped names]). A JSON body stays JSON; a form body stays a string;
    anything else is kept verbatim (and the caller warns about a param it could not place)."""
    hits, dropped = {}, []
    try:
        parsed = json.loads(post_data)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        return _template_json(parsed, params, hits, dropped, keep), hits, dropped
    if "x-www-form-urlencoded" in (content_type or "") or _FORMLIKE.fullmatch(post_data):
        return _template_query(post_data, params, hits, dropped, keep), hits, dropped
    return post_data, hits, dropped


def clean_url(url):
    """The URL with credential-looking query parameters removed — what may be printed."""
    return template_url(url, {})[0]


# ---------- the whole thing ----------

def _load_body(item):
    p = item.get("body_path")
    if not p or item.get("body_error") or not item.get("body_bytes") or item["body_bytes"] > MAX_BODY:
        return None, None
    try:
        raw = Path(p).read_bytes()
    except OSError:
        return None, None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text), text
    except ValueError:
        return None, text


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "api"


def build(capture, expects, params=None, name=None, entry_url=None, description=None, host_ok=None, keep=(),
          skip=()):
    """capture: the parsed network_capture result. -> report dict; report["draft"] is the recipe
    draft when report["applicable"] is true. skip: request indices ("i") not to pick (learn: a candidate
    it refused)."""
    params = dict(params or {})
    keep = set(keep or ())
    expects = list(expects or [])
    if not expects:
        raise DraftError("give at least one --expect: a string the page shows and the answer needs")
    for e in expects:
        if len(e) < MIN_EXPECT:
            raise DraftError(f"--expect {e!r} is shorter than {MIN_EXPECT} characters; it would match anywhere")
    for n in params:
        if not _NAME.match(n):
            raise DraftError(f"param name {n!r} must be letters, digits and _ (not starting with a digit)")
    reqs = capture.get("requests") if isinstance(capture, dict) else None
    if not isinstance(reqs, list):
        raise DraftError("not a network_capture result (no 'requests' list)")

    cands, non_json, n_json, skipped = [], [], 0, []
    for item in reqs:
        status = item.get("status")
        if not isinstance(status, int) or not 200 <= status < 300:
            continue
        if skip and item.get("i") in skip:
            continue
        data, text = _load_body(item)
        if data is None:
            if text is not None and all(e in text for e in expects):
                non_json.append(clean_url(item.get("url", "")))
            continue
        n_json += 1
        found = {e: find_paths(data, e) for e in expects}
        if not all(found.values()):
            continue
        url = item.get("url", "")
        if host_ok is not None and not host_ok(url):
            skipped.append({"url": clean_url(url), "why": "refused host"})
            continue
        if item.get("post_data_truncated"):
            skipped.append({"url": clean_url(url), "why": "captured request body was cut off"})
            continue
        cands.append({"item": item, "found": found})

    base = {"requests": len(reqs), "json_bodies": n_json}
    if not cands:
        out = {"applicable": False, "checked": base,
               "reason": "no JSON response with a 2xx status contains every expect string"}
        if non_json:
            out["found_in_non_json"] = non_json[:5]
            out["reason"] += "; it is in a non-JSON response, so the data comes as HTML or text"
        if skipped:
            out["skipped"] = skipped
        out["suggest"] = ("a script recipe: read the page (deja-cdp read '{\"query\": \"" + expects[0].replace('"', "'")
                          + "\"}') and write a DOM script; or check the expect strings and the trigger")
        return out

    scored = []
    for c in cands:
        it = c["item"]
        url_t, uh, ud = template_url(it.get("url", ""), params, keep)
        body_t, bh, bd = (None, {}, [])
        pd = it.get("post_data")
        if pd:
            ct = (it.get("req_headers") or {})
            ct = next((v for k, v in ct.items() if k.lower() == "content-type"), "")
            body_t, bh, bd = template_body(pd, ct, params, keep)
        hits = {n: uh.get(n, 0) + bh.get(n, 0) for n in params}
        exact = sum(1 for e in expects if any(x for _, x in c["found"][e]))
        c.update(url_t=url_t, body_t=body_t, hits=hits, dropped=sorted(set(ud + bd)))
        key = (-sum(1 for v in hits.values() if v), -exact, it.get("method", "GET") != "GET",
               it.get("body_bytes", 0), it.get("i", 0))
        scored.append((key, c))
    scored.sort(key=lambda t: t[0])
    best = scored[0][1]
    it = best["item"]
    chosen = _best_paths(best["found"])
    pick = build_pick(chosen.values())

    method = it.get("method", "GET")
    request = {"method": method, "url": best["url_t"]}
    headers = {}
    for k, v in (it.get("req_headers") or {}).items():
        if k.lower() in KEEP_HEADERS and isinstance(v, str) and len(v) <= 100 and "<redacted" not in v:
            headers[k] = v
    if headers:
        request["headers"] = headers
    if best["body_t"] is not None:
        request["body"] = best["body_t"]

    warnings = []
    if best["dropped"]:
        warnings.append("dropped credential-looking field(s) " + ", ".join(best["dropped"])
                        + " from the request; if the replay needs one and it is a public key (a search-only "
                        "client key in the page source), repeat the draft with --keep-param NAME")
    kept = sorted(n for n in keep if n in it.get("url", "") or n in (it.get("post_data") or ""))
    if kept:
        warnings.append("kept credential-looking field(s) " + ", ".join(kept)
                        + " verbatim: their values are now in the draft and the saved recipe file in plain text")
    for n, v in params.items():
        if best["hits"].get(n):
            if best["hits"][n] > 1:
                warnings.append(f"param {n} replaced {best['hits'][n]} places; check they are all the same input")
        elif v in it.get("url", "") or v in (it.get("post_data") or ""):
            warnings.append(f"param {n}: its value appears only inside a longer value of the request, "
                            "so it was NOT replaced; edit the url/body by hand")
        else:
            warnings.append(f"param {n}: its value is not in the request, so the draft will always run with "
                            "the captured one")
    for e, hits in best["found"].items():
        others = sorted({".".join(p) for p, ex in hits if ex and _field_key(p) != _field_key(chosen[e])}, key=lambda x: x)
        if others:
            warnings.append(f"expect {e!r} is also an exact value at {', '.join(others[:3])}; "
                            f"{'.'.join(chosen[e])} was chosen: check it is the field you mean")
    if method != "GET":
        warnings.append(f"method is {method}: the draft has writes=true; set it to false if this only reads")
    redacted = sorted(k for k, v in (it.get("req_headers") or {}).items() if isinstance(v, str) and v.startswith("<redacted"))
    notes = ["drafted from a network_capture; review params, pick and the description before saving"]
    if redacted:
        notes.append("the browser sent credential header(s) " + ", ".join(redacted)
                     + " (values not copied); add them as {secret:NAME} only if replay needs them")

    host = urllib.parse.urlsplit(it.get("url", "")).hostname or "api"
    if entry_url is None:
        p = urllib.parse.urlsplit(capture.get("url") or it.get("url", ""))
        entry_url = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))
    segs = [x for x in urllib.parse.urlsplit(best["url_t"]).path.split("/") if x and "{" not in x]
    draft = {"name": name or _slug(segs[-1] if segs else host),
             "description": description or f"{host}: TODO describe what this returns",
             "when_to_use": "TODO: when an agent should pick this recipe",
             "entry_url": entry_url,
             "params": [{"name": n, "description": f"TODO (captured with {n}={v!r})".replace("'", '"'),
                         "required": True} for n, v in params.items()],
             "request": request, "pick": pick, "writes": method != "GET", "notes": notes}
    if not params:
        draft.pop("params")

    alts = []
    for _, c in scored[1:4]:
        ch = _best_paths(c["found"])
        alts.append({"i": c["item"].get("i"), "method": c["item"].get("method", "GET"), "url": clean_url(c["item"].get("url", "")),
                     "bytes": c["item"].get("body_bytes"), "paths": {e: ".".join(p) for e, p in ch.items()}})
    return {"applicable": True, "checked": base, "picked": {"i": it.get("i"), "bytes": it.get("body_bytes")},
            "dropped": best["dropped"], "redacted_headers": redacted,
            "matched": {e: ".".join(p) for e, p in chosen.items()},
            "other_matches": {e: [".".join(p) for p, _ in sorted(best["found"][e], key=lambda h: (not h[1], len(h[0]), _order(h[0])))
                                  if p != chosen[e]][:3] for e in expects},
            "warnings": warnings, "alternatives": alts, "draft": draft}
