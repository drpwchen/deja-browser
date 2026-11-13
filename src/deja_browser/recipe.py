# -*- coding: utf-8 -*-
"""Site recipes: learn a site's data path once, replay it cheaply forever after.

WHY (2026-11-11): Kimi WebBridge's `web-skill-creator` + `run_saved_skill`, agent-browser's `flow`
and libretto's tier ladder all do the same thing — the first visit explores (network capture,
verify a replay), the result is saved as a parameterised recipe, and later runs map a request
onto `args` and run the recipe without an LLM reading the page again. This is our version, on
top of the dedicated agent browser (`browser.py`).

Recipe file: <recipes dir>/<host>/<name>.json   (id = "<host>/<name>"; dir = [recipes] dir in
  config.toml, default <data dir>/recipes — see config.py)
  {
    "version": 1, "name", "description", "when_to_use", "entry_url",
    "params": [{"name", "description", "required", "default"}],
    "request": {"method", "url", "headers", "body"},      # API recipe; strings may hold {param}
                                                          # and {secret:NAME} (secretstore.py, resolved
                                                          # in-process, never printed/returned)
    "script": "<async fn body of (args), returns small JSON>",   # DOM/form recipe (instead of request)
    "pick": {"path": "hits", "fields": ["title", "url"], "limit": 20},   # shrink the result
    "writes": false,                                      # true = side effects -> needs --allow-write
    "driver": "playwright",                               # optional; default is patchright
    "login": {"check": "<JS expr -> true when logged in>", "url", "user_secret", "pass_secret",
              "user_selector", "pass_selector", "submit_selector"},     # optional
    "tier": "http" | "page" | "script",                   # cheapest tier that verified
    "expect": {"signature": {...}},                       # JSON shape of a good response
    "verified_at", "last_run": {"at", "ok", "tier", "note"}, "notes": [...]
  }

Tier ladder (cheapest first; a run falls through on failure and records what worked):
  http   — urllib outside the browser, cookies pulled live from the agent browser (nothing stored)
  page   — fetch() inside a tab on entry_url (site cookies, cf_clearance, CSRF all native)
  script — the recipe's own script in the page (DOM read / form fill)
Every response is checked against expect.signature; a mismatch is reported as stale, never
returned as data. Challenge pages go through challenge_gate(), which solves them itself:
auto-clear (lazy Turnstile scrolled into view) -> trusted checkbox click -> reCAPTCHA audio via local
whisper; text-image CAPTCHAs -> solve_image_captcha() (ddddocr). The human is the last resort.

CLI:
  deja-browser list
  deja-browser show <id>
  deja-browser run <id> '<json args>' [--tier http|page|script] [--allow-write]
  deja-browser verify <draft.json> '<json args>'     try every tier, print shape + preview
  deja-browser save <draft.json>                     needs a passing verify of THIS draft
  deja-browser gate <url>                            open url, clear any challenge, report
"""
import gzip, hashlib, importlib.util, json, os, re, sys, time, urllib.parse, urllib.request, zlib
from datetime import datetime
from pathlib import Path

# must precede the import: browser.py reads it once. patchright only if installed (optional dep).
if importlib.util.find_spec("patchright"):
    os.environ.setdefault("EDGE_CDP_DRIVER", "patchright")
from . import browser  # noqa: E402
from . import config, guard, hcaptcha, notify, secretstore  # noqa: E402
# patchright by default (2026-11-11 live test): with plain playwright the Cloudflare full-page
# interstitial re-rendered its checkbox after the click (= rejected, Runtime.enable CDP signal);
# patchright passed it on the first click. A recipe may still say "driver": "playwright".

RECIPES = config.path("recipes", "dir", config.data_dir() / "recipes")
MAX_OUT = 20000
# deny / ticket host lists live in guard.py (shared with the browser layer)
DENY_HOSTS, TICKET_HOSTS, TICKET_HOSTS_DEFAULT = guard.DENY_HOSTS, guard.TICKET_HOSTS, guard.TICKET_HOSTS_DEFAULT
_ticket_hosts = guard.ticket_hosts


class RecipeError(RuntimeError):
    pass


# ---------- helpers ----------

def _now():
    return datetime.now().isoformat(timespec="seconds")


def _host(url):
    return urllib.parse.urlsplit(url).hostname or ""


def _check_host(url):
    try:
        guard.check_host(url)
    except guard.HostRefused as ex:
        raise RecipeError(str(ex)) from None


def _secret(name):
    try:
        return secretstore.get(name)   # value stays in this process
    except secretstore.SecretMissing as ex:
        raise RecipeError(str(ex)) from None


_PH = re.compile(r"\{(secret:)?([A-Za-z_][A-Za-z0-9_]*)\}")


def _render(x, args, secrets_ok=True):
    """Fill {param} / {secret:NAME} in every string of a JSON-ish structure."""
    if isinstance(x, str):
        def sub(m):
            if m.group(1):
                if not secrets_ok:
                    raise RecipeError("secrets are not allowed in this field")
                return _secret(m.group(2))
            return str(args[m.group(2)]) if m.group(2) in args else m.group(0)
        return _PH.sub(sub, x)
    if isinstance(x, list):
        return [_render(v, args, secrets_ok) for v in x]
    if isinstance(x, dict):
        return {k: _render(v, args, secrets_ok) for k, v in x.items()}
    return x


def _args(recipe, given):
    out = {}
    for p in recipe.get("params", []):
        v = given.get(p["name"], p.get("default"))
        if v is None and p.get("required"):
            raise RecipeError(f"missing required param {p['name']!r}: {p.get('description', '')}")
        if v is not None:
            out[p["name"]] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return out


def signature(x, depth=0):
    """JSON shape of what a recipe RETURNS (after pick): dict -> {key: shape}, list -> [shape of first
    item], scalar -> type name. Kept shallow so incidental nesting changes don't read as 'stale'."""
    if depth > 3:
        return "…"
    if isinstance(x, dict):
        return {k: signature(v, depth + 1) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return [signature(x[0], depth + 1)] if x else []
    return {bool: "bool", int: "num", float: "num", str: "str", type(None): "null"}.get(type(x), "other")


def sig_ok(want, got, top=True):
    """Every key the saved shape has must still be there (extra keys are fine). A null / [] FIELD is
    lenient (optional data); a null or missing WHOLE result is not — that is the stale case."""
    if want in ("null", "…", []):
        return True
    if got == "null":
        return not top
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and sig_ok(v, got[k], False) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(got, list) and (not got or sig_ok(want[0], got[0], False))
    if want == "num" and got == "num" or want == got:
        return True
    return False


def pick(data, spec):
    if not spec:
        return data
    for part in (spec.get("path") or "").split("."):
        if part:
            data = data.get(part) if isinstance(data, dict) else None
    if isinstance(data, list):
        data = data[: int(spec.get("limit", 50))]
        if spec.get("fields"):
            data = [{f: d.get(f) for f in spec["fields"]} if isinstance(d, dict) else d for d in data]
    return data


def _parse_body(raw, enc=""):
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


# ---------- challenge / login gates ----------

_JS_CHALLENGE = r"""() => {
  const t = (document.title || '').toLowerCase();
  // interstitial pages only — NOT bare 'recaptcha' (Google's own demo page is titled "ReCAPTCHA 示範";
  // a widget embedded in a normal page is caught by the iframe scan below)
  const titles = ['just a moment', 'attention required', 'checking your browser', 'verify you are human',
                  '請稍候', '正在檢查', '安全性檢查', '人機驗證'];
  if (titles.some(s => t.includes(s))) return 'title:' + document.title.slice(0, 60);
  // an embedded widget that already holds its token is solved, even though its iframe stays visible
  const tok = n => { const e = document.querySelector(`[name="${n}"]`); return !!(e && e.value); };
  for (const f of document.querySelectorAll('iframe')) {
    const s = f.src || '', r = f.getBoundingClientRect();
    if (r.width < 30 || r.height < 30) continue;          // invisible v3 / badge iframes don't block
    if (s.includes('recaptcha/api2/bframe') || s.includes('recaptcha/enterprise/bframe')) return 'recaptcha-images';
    if ((s.includes('recaptcha/api2/anchor') || s.includes('recaptcha/enterprise/anchor'))
        && !tok('g-recaptcha-response')) return 'recaptcha';
    if (s.includes('hcaptcha.com') && !tok('h-captcha-response')) return 'hcaptcha';
    if (s.includes('challenges.cloudflare.com') && !tok('cf-turnstile-response')) return 'turnstile';
  }
  return null;
}"""


_FRAME_KINDS = (("challenges.cloudflare.com", "turnstile", "cf-turnstile-response"),
                ("hcaptcha.com", "hcaptcha", "h-captcha-response"))


def _widget_frames(page):
    """Visible Turnstile / hCaptcha frames. Cloudflare puts its iframe inside a CLOSED shadow root,
    so document.querySelectorAll('iframe') never sees it — Playwright's frame list does."""
    out = []
    for fr in page.frames:
        u = fr.url or ""
        for needle, kind, tok in _FRAME_KINDS:
            if needle in u:
                try:
                    box = fr.frame_element().bounding_box()
                except Exception:
                    box = None
                if _on_screen(box):
                    out.append((kind, tok, fr, box))
    return out


def _on_screen(box):
    """hCaptcha parks its (hidden) challenge frame at y=-9999; clicking 'its checkbox' hits nothing.
    A widget counts only if it is big enough and at least partly inside the page."""
    return bool(box and box["width"] >= 30 and box["height"] >= 30
                and box["x"] + box["width"] > 0 and box["y"] + box["height"] > 0)


def _token(page, name):
    try:
        return bool(page.evaluate("n => { const e = document.querySelector(`[name=\"${n}\"]`); "
                                  "return !!(e && e.value) }", name))
    except Exception:
        return False


def challenge(page):
    try:
        k = page.evaluate(_JS_CHALLENGE)
    except Exception:
        k = None
    if k:
        return k
    for kind, tok, _, _ in _widget_frames(page):
        if not _token(page, tok):
            return kind
    return None


def _wait_clear(page, seconds):
    end = time.time() + seconds
    while time.time() < end:
        page.wait_for_timeout(1000)
        if not challenge(page):
            return True
    return False


_JS_WIDGET_PENDING = r"""() => {
  const solved = ['g-recaptcha-response', 'h-captcha-response', 'cf-turnstile-response']
    .some(n => { const e = document.querySelector(`[name="${n}"]`); return !!(e && e.value); });
  return !solved && !!document.querySelector(
    '.cf-turnstile, .g-recaptcha:not([data-size="invisible"]), .h-captcha:not([data-size="invisible"]), '
    + '[data-sitekey]:not([data-size="invisible"])');
}"""


def _widget_pending(page):
    """A widget container is in the DOM but its iframe may not be yet: navigate() returns at
    DOMContentLoaded, ~1 s before Turnstile / hCaptcha inject their frame (seen on the public demos,
    2026-11-12). Without this the gate reported 'no challenge' on pages that had one."""
    try:
        return bool(page.evaluate(_JS_WIDGET_PENDING))
    except Exception:
        return False


def _wait_widget(page, seconds):
    """Scroll widgets into view and poll until one shows up -> its kind, or None after `seconds`."""
    _wake_widgets(page)
    end = time.time() + seconds
    while time.time() < end:
        page.wait_for_timeout(500)
        kind = challenge(page)
        if kind:
            return kind
    return None


def _wake_widgets(page):
    """Turnstile renders lazily (IntersectionObserver): it does not start until it is on screen
    (beclass lesson, memory project_autoreg_bots). Scroll every widget into view."""
    try:
        page.evaluate("""() => document.querySelectorAll(
          '.cf-turnstile, iframe[src*="challenges.cloudflare.com"], .g-recaptcha, .h-captcha')
          .forEach(e => e.scrollIntoView({block: 'center'}))""")
    except Exception:
        pass


def _frame(page, *needles):
    for fr in page.frames:
        u = fr.url or ""
        if all(n in u for n in needles):
            return fr
    return None


def _click_checkbox(page):
    # Turnstile / hCaptcha: the checkbox sits ~30 px from the frame's left edge, vertically centred;
    # its DOM is in a closed shadow root, so click the coordinates (trusted CDP mouse event).
    for kind, tok, fr, box in _widget_frames(page):
        if not _token(page, tok):
            page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
            return True
    for fr in page.frames:
        u = fr.url or ""
        sel = ("#recaptcha-anchor" if "recaptcha" in u and "anchor" in u else
               "#checkbox" if "hcaptcha.com" in u else
               "input[type=checkbox], label" if "challenges.cloudflare.com" in u else None)
        if sel:
            try:
                fr.locator(sel).first.click(timeout=4000)   # trusted CDP input
                return True
            except Exception:
                pass
    return False


_WHISPER = None


def transcribe(audio_bytes):
    """reCAPTCHA audio challenge -> text, local faster-whisper on CPU (clips are ~5 s; no GPU lease)."""
    global _WHISPER
    import tempfile
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel("base.en", device="cpu", compute_type="int8")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio_bytes)
        path = f.name
    try:
        segs, _ = _WHISPER.transcribe(path, language="en", beam_size=5)
        text = " ".join(s.text for s in segs)
    finally:
        Path(path).unlink(missing_ok=True)
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def solve_recaptcha_audio(page, tries=3):
    """reCAPTCHA v2 image challenge -> switch to the audio challenge -> transcribe locally -> submit.
    -> "solved" | "blocked" (Google refused audio: 'try again later') | "failed"."""
    bf = _frame(page, "recaptcha", "bframe")
    if bf is None:
        return "failed"
    try:
        bf.locator("#recaptcha-audio-button").click(timeout=5000)
    except Exception:
        pass
    for _ in range(tries):
        page.wait_for_timeout(1500)
        bf = _frame(page, "recaptcha", "bframe")
        if bf is None:
            return "solved" if not challenge(page) else "failed"
        try:
            if bf.locator(".rc-doscaptcha-header").count():   # "Try again later / automated queries"
                return "blocked"
            src = bf.locator("#audio-source").get_attribute("src", timeout=6000)
            answer = transcribe(page.request.get(src).body())
            if not answer:
                bf.locator("#recaptcha-reload-button").click(timeout=4000)
                continue
            bf.locator("#audio-response").fill(answer, timeout=4000)
            bf.locator("#recaptcha-verify-button").click(timeout=4000)
        except Exception:
            continue
        if _wait_clear(page, 5):
            return "solved"
    return "failed"


def solve_image_captcha(page, image_selector, input_selector, digits_only=False, tries=6, submit_selector=None,
                        ok_check=None):
    """Classic text-in-an-image CAPTCHA (library proxy, NDLTD style): offline ddddocr, retry on a miss.
    Same engine as paper-fetch library_session.solve_captcha / ndltd. -> attempts used, or 0 on failure."""
    import ddddocr
    ocr = ddddocr.DdddOcr(show_ad=False)
    for i in range(1, tries + 1):
        png = page.locator(image_selector).first.screenshot()
        code = ocr.classification(png)
        code = re.sub(r"\D", "", code) if digits_only else re.sub(r"[^0-9A-Za-z]", "", code)
        page.fill(input_selector, code, timeout=5000)
        if not submit_selector:
            return i
        page.locator(submit_selector).first.click(timeout=8000)
        page.wait_for_timeout(2500)
        if ok_check is None or page.evaluate(ok_check):
            return i
        try:   # most sites swap the image on a miss; click it to force a fresh one if not
            page.locator(image_selector).first.click(timeout=2000)
        except Exception:
            pass
    return 0


def ntfy(title, message):
    notify.notify(title, message)


def challenge_gate(page, human_wait=300):
    """-> {"challenge": kind|None, "cleared": bool, "how": ...}. Claude solves these itself; the
    human is the LAST resort.
      1. auto  — scroll widgets into view (lazy Turnstile) and wait: Cloudflare / Turnstile /
                 invisible v3 pass on this real, headed, long-lived profile
      2. click — one trusted CDP click on the checkbox (reCAPTCHA v2 / hCaptcha / Turnstile);
                 for hCaptcha with [captcha] hcaptcha_solver = "gemini" (opt-in, sends the
                 challenge image to Google) the solver clicks and answers instead
      3. audio — reCAPTCHA v2 image grid -> audio challenge -> local faster-whisper -> submit
      4. human — notify + wait, only when Google refuses audio ('automated queries') or all else fails
    reCAPTCHA v3 *score* failures are not a widget: give the recipe "driver": "patchright"
    (hides the Runtime.enable CDP signal)."""
    kind = challenge(page)
    if not kind and _widget_pending(page):
        kind = _wait_widget(page, 8)
    if not kind:
        return {"challenge": None, "cleared": True, "how": "none"}
    _wake_widgets(page)
    if _wait_clear(page, 10):
        return {"challenge": kind, "cleared": True, "how": "auto"}
    solver = hcaptcha.solve(page) if kind == "hcaptcha" and hcaptcha.enabled() else None
    if solver and solver["ran"]:
        if _wait_clear(page, 5):
            return {"challenge": kind, "cleared": True, "how": "gemini"}
    elif _click_checkbox(page) and _wait_clear(page, 8):
        return {"challenge": kind, "cleared": True, "how": "click"}
    extra = {"solver": solver.get("signal") or solver.get("error")} if solver else {}
    audio = None
    if challenge(page) == "recaptcha-images":
        audio = solve_recaptcha_audio(page)
        if audio == "solved":
            return {"challenge": kind, "cleared": True, "how": "audio"}
    if human_wait <= 0:
        return {"challenge": challenge(page), "cleared": False, "how": "timeout", "audio": audio, **extra}
    try:
        page.bring_to_front()
    except Exception:
        pass
    ntfy("Agent browser: challenge not solved automatically",
         f"{_host(page.url)}: {kind} (audio: {audio or 'n/a'}). Please finish it in the agent browser tab "
         f"\"{page.title()[:40]}\"; waiting {human_wait // 60} min.")
    ok = _wait_clear(page, human_wait)
    return {"challenge": kind, "cleared": ok, "how": "human" if ok else "timeout", "audio": audio, **extra}


def login_gate(page, login):
    """Log in with stored secrets when the recipe's check says we're logged out. Values never leave
    this function; nothing about them is returned."""
    if not login:
        return {"login": "n/a"}
    check = login.get("check")
    if check and page.evaluate(check):
        return {"login": "already"}
    if not (login.get("user_secret") and login.get("pass_secret")):
        ntfy("Agent browser needs a login", f"{_host(page.url)} is logged out; log in once in the agent browser.")
        ok = False
        for _ in range(60):
            page.wait_for_timeout(5000)
            if check and page.evaluate(check):
                ok = True
                break
        return {"login": "human" if ok else "timeout"}
    if login.get("url"):
        page.goto(login["url"], wait_until="domcontentloaded", timeout=45000)
    challenge_gate(page)
    _check_host(page.url)   # wherever the login page redirected to, secrets never go to a denied host
    page.fill(login["user_selector"], _secret(login["user_secret"]), timeout=10000)
    page.fill(login["pass_selector"], _secret(login["pass_secret"]), timeout=10000)
    page.locator(login["submit_selector"]).first.click(timeout=10000)
    page.wait_for_timeout(4000)
    g = challenge_gate(page)
    ok = bool(page.evaluate(check)) if check else None
    return {"login": "secrets", "ok": ok, "challenge": g["challenge"]}


# ---------- tiers ----------

def _page_for(recipe, session):
    """A tab on the recipe's entry origin (reused when already there)."""
    ctx = browser._ctx()
    try:
        page = browser._current(ctx, session)
        if urllib.parse.urlsplit(page.url)[:2] != urllib.parse.urlsplit(recipe["entry_url"])[:2]:
            raise browser.NoTab("other origin")
    except browser.NoTab:
        browser.wb("navigate", {"url": recipe["entry_url"]}, session=session)
        page = browser._current(ctx, session)
    return page


def tier_http(recipe, args, session):
    req = _render(recipe["request"], args)
    url = req["url"]
    ctx = browser._ctx()
    cookies = ctx.cookies(url)   # live from the agent browser; not stored anywhere
    ua = (browser.version() or {}).get("User-Agent", "Mozilla/5.0")
    headers = {"User-Agent": ua, "Accept": "application/json, */*", "Accept-Encoding": "gzip"}
    headers.update(req.get("headers") or {})
    if cookies:
        headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    body = req.get("body")
    data = (body if isinstance(body, str) else json.dumps(body)).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=req.get("method", "GET"), headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, _parse_body(resp.read(), resp.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read() or b"", e.headers.get("Content-Encoding", ""))


_JS_FETCH = """async (r) => {
  const o = {method: r.method || 'GET', headers: r.headers || {}, credentials: 'include'};
  if (r.body !== undefined && r.body !== null) o.body = typeof r.body === 'string' ? r.body : JSON.stringify(r.body);
  let res;
  try { res = await fetch(r.url, o); }
  catch (e) {   // cross-origin APIs that answer `*` reject credentialed requests (CORS) — retry without
    if (new URL(r.url, location.href).origin === location.origin) throw e;
    o.credentials = 'omit'; res = await fetch(r.url, o);
  }
  return {status: res.status, text: await res.text()};
}"""


def tier_page(recipe, args, session):
    page = _page_for(recipe, session)
    gate = challenge_gate(page)
    if not gate["cleared"]:
        return 0, {"_challenge": gate}
    login_gate(page, recipe.get("login"))
    r = page.evaluate(_JS_FETCH, _render(recipe["request"], args))
    try:
        return r["status"], json.loads(r["text"])
    except ValueError:
        return r["status"], r["text"]


def tier_script(recipe, args, session):
    page = _page_for(recipe, session)
    gate = challenge_gate(page)
    if not gate["cleared"]:
        return 0, {"_challenge": gate}
    login_gate(page, recipe.get("login"))
    code = f"async (args) => {{\n{recipe['script']}\n}}"
    return 200, page.evaluate(code, args)   # script gets plain params; no secrets in scripts


TIERS = {"http": tier_http, "page": tier_page, "script": tier_script}


def _ladder(recipe, start=None):
    have = ([t for t in ("http", "page") if recipe.get("request") is not None]
            + (["script"] if recipe.get("script") else []))
    if start:
        return [start] if start in have else []
    pref = recipe.get("tier")
    return ([pref] if pref in have else []) + [t for t in have if t != pref]


def run(recipe, given, tier=None, allow_write=False, session=None, check_sig=True):
    _check_host(recipe["entry_url"])
    if recipe.get("driver") and recipe["driver"] != browser._DRIVER:
        browser.disconnect()
        browser.use_driver(recipe["driver"])   # "patchright" for reCAPTCHA-v3-scored pages
    if recipe.get("writes") and not allow_write:
        raise RecipeError("this recipe has side effects — pass --allow-write")
    args = _args(recipe, given)
    if recipe.get("request"):   # the API host can differ from entry_url; it gets cookies and secrets
        _check_host(_render(recipe["request"].get("url", ""), args, secrets_ok=False))
    session = session or f"recipe-{_host(recipe['entry_url'])}"
    want = (recipe.get("expect") or {}).get("signature")
    tried = []
    for t in _ladder(recipe, tier):
        try:
            status, data = TIERS[t](recipe, args, session)
        except Exception as ex:
            tried.append({"tier": t, "error": str(ex).splitlines()[0][:200]})
            continue
        if isinstance(data, dict) and "_challenge" in data:
            tried.append({"tier": t, "challenge": data["_challenge"]})
            continue
        if not (200 <= status < 300):
            tried.append({"tier": t, "status": status})
            continue
        if check_sig and want is not None and not sig_ok(want, signature(pick(data, recipe.get("pick")))):
            tried.append({"tier": t, "status": status, "error": "response shape changed (stale recipe?)"})
            continue
        return {"ok": True, "tier": t, "data": data, "tried": tried}
    return {"ok": False, "tried": tried}


def _out(data):
    s = json.dumps(data, ensure_ascii=False)
    return data if len(s) <= MAX_OUT else {"truncated": True, "chars": len(s), "head": s[:MAX_OUT]}


# ---------- store ----------

def _path(rid):
    host, _, name = rid.partition("/")
    if not name or "/" in name or ".." in rid:
        raise RecipeError("id must be <host>/<name>")
    return RECIPES / host / f"{name}.json"


def load(rid):
    p = _path(rid)
    if not p.exists():
        raise RecipeError(f"no recipe {rid}")
    return json.loads(p.read_text(encoding="utf-8"))


def _write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _draft_hash(d):
    keep = {k: d.get(k) for k in ("entry_url", "request", "script", "params", "pick", "login", "writes")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def cmd_run(rid, given, tier=None, allow_write=False):
    rec = load(rid)
    res = run(rec, given, tier, allow_write)
    rec["last_run"] = {"at": _now(), "ok": res["ok"], "tier": res.get("tier"),
                       "note": None if res["ok"] else json.dumps(res["tried"], ensure_ascii=False)[:300]}
    if res["ok"] and res["tier"] != rec.get("tier") and tier is None:
        rec.setdefault("notes", []).append(f"{_now()} tier {rec.get('tier')} failed, {res['tier']} worked")
        rec["tier"] = res["tier"]
    _write(_path(rid), rec)
    if res["ok"]:
        res["data"] = _out(pick(res["data"], rec.get("pick")))
    return res


def cmd_verify(draft_path, given):
    d = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    for k in ("name", "description", "entry_url"):
        if not d.get(k):
            raise RecipeError(f"draft needs {k!r}")
    if not (d.get("request") or d.get("script")):
        raise RecipeError("draft needs request or script")
    _check_host(d["entry_url"])
    results, sigs = [], {}
    for t in _ladder(d):
        r = run(d, given, tier=t, allow_write=True, check_sig=False)
        if r["ok"]:
            sigs[t] = signature(pick(r["data"], d.get("pick")))
            results.append({"tier": t, "ok": True, "preview": _out(pick(r["data"], d.get("pick")))})
        else:
            results.append({"tier": t, "ok": False, "tried": r["tried"]})
    passed = [r["tier"] for r in results if r["ok"]]
    side = Path(draft_path).with_suffix(".verified.json")
    _write(side, {"at": time.time(), "hash": _draft_hash(d), "passed": passed,
                  "signature": sigs[passed[0]] if passed else None, "args": given})
    return {"passed": passed, "cheapest": passed[0] if passed else None, "results": results,
            "next": f"{Path(sys.argv[0]).name} save {draft_path}" if passed else "fix the draft and verify again"}


def cmd_save(draft_path):
    d = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    side = Path(draft_path).with_suffix(".verified.json")
    if not side.exists():
        raise RecipeError("verify this draft first")
    v = json.loads(side.read_text(encoding="utf-8"))
    if v["hash"] != _draft_hash(d):
        raise RecipeError("draft changed after verify — verify again")
    if not v["passed"] or time.time() - v["at"] > 3600:
        raise RecipeError("no passing verify in the last hour")
    d.update({"version": 1, "tier": v["passed"][0], "expect": {"signature": v["signature"]},
              "verified_at": _now(), "verified_args": v["args"]})
    rid = f"{_host(d['entry_url'])}/{d['name']}"
    _write(_path(rid), d)
    return {"saved": rid, "tier": d["tier"], "path": str(_path(rid))}


def cmd_list():
    out = []
    for p in sorted(RECIPES.glob("*/*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        lr = d.get("last_run") or {}
        out.append({"id": f"{p.parent.name}/{p.stem}", "description": d.get("description"),
                    "when_to_use": d.get("when_to_use"), "tier": d.get("tier"),
                    "params": [x["name"] + ("*" if x.get("required") else "") for x in d.get("params", [])],
                    "verified_at": d.get("verified_at"), "last_ok": lr.get("ok"), "writes": d.get("writes", False)})
    return out


def cmd_gate(url, session="recipe-gate"):
    _check_host(url)
    browser.wb("navigate", {"url": url}, session=session)
    page = browser._current(browser._ctx(), session)
    g = challenge_gate(page)
    return {**g, "url": page.url, "title": page.title(), "session": session}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = {f for f in ("--allow-write",) if f in argv}
    argv = [a for a in argv if a not in flags]
    tier = None
    if "--tier" in argv:
        i = argv.index("--tier")
        tier = argv[i + 1]
        del argv[i:i + 2]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    js = lambda i: json.loads(rest[i]) if len(rest) > i else {}  # noqa: E731
    try:
        if cmd == "list":
            out = cmd_list()
        elif cmd == "show":
            out = load(rest[0])
        elif cmd == "run":
            out = cmd_run(rest[0], js(1), tier, "--allow-write" in flags)
        elif cmd == "verify":
            out = cmd_verify(rest[0], js(1))
        elif cmd == "save":
            out = cmd_save(rest[0])
        elif cmd == "gate":
            out = cmd_gate(rest[0])
        else:
            print(__doc__)
            return 2
    except (RecipeError, browser.WbError) as ex:
        print(json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0 if not (isinstance(out, dict) and out.get("ok") is False) else 1


if __name__ == "__main__":
    sys.exit(main())
