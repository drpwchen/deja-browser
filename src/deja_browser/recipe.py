# -*- coding: utf-8 -*-
"""Site recipes: learn a site's data path once, replay it cheaply forever after.

WHY (2026-11-11): Kimi WebBridge's `web-skill-creator` + `run_saved_skill`, agent-browser's `flow`
and libretto's tier ladder all do the same thing — the first visit explores (network capture,
verify a replay), the result is saved as a parameterised recipe, and later runs map a request
onto `args` and run the recipe without an LLM reading the page again. This is our version, on
top of the dedicated agent browser (`browser.py`).

Recipe file: <recipes dir>/<host>/<name>.json (id "<host>/<name>"; [recipes] dir in config.toml, default
<data dir>/recipes — see config.py). Tiers: http -> page -> script, or steps for a steps recipe; every response is
checked against expect.signature, a mismatch is stale, never data. Challenge pages go through challenge_gate():
auto-clear (lazy Turnstile scrolled into view) -> trusted checkbox click -> reCAPTCHA audio via local whisper;
text-image CAPTCHAs -> solve_image_captcha() (ddddocr). The human is the last resort.

The recipe JSON format, the tier ladder, steps recipes and every CLI command are documented for users in
clihelp.py (`deja-browser help [<command | topic>]`: format, tiers, steps). Keep it in step with this file.
"""
import gzip, hashlib, importlib.util, json, os, re, sys, time, urllib.parse, urllib.request, zlib
from datetime import datetime
from pathlib import Path

# must precede the import: browser.py reads it once. patchright only if installed (optional dep).
if importlib.util.find_spec("patchright"):
    os.environ.setdefault("EDGE_CDP_DRIVER", "patchright")
from . import browser  # noqa: E402
from . import config, draft, guard, hcaptcha, notify, secretstore  # noqa: E402
# patchright by default (2026-11-11 live test): with plain playwright the Cloudflare full-page
# interstitial re-rendered its checkbox after the click (= rejected, Runtime.enable CDP signal);
# patchright passed it on the first click. A recipe may still say "driver": "playwright".

RECIPES = config.path("recipes", "dir", config.data_dir() / "recipes")
MAX_OUT = 20000
PREVIEW_OUT = 2000       # verify prints at most this much of a tier's result
PAGE_RATIO = 0.5         # verify warns when a recipe's output is more than this share of the page's text
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


_PH = re.compile(r"\{(secret:)?([A-Za-z_][A-Za-z0-9_]*)(\|url)?\}")


def _render(x, args, secrets_ok=True):
    """Fill {param} / {param|url} / {secret:NAME} in every string of a JSON-ish structure."""
    if isinstance(x, str):
        def sub(m):
            if m.group(1):
                if not secrets_ok:
                    raise RecipeError("secrets are not allowed in this field")
                return _secret(m.group(2))
            if m.group(2) not in args:
                return m.group(0)
            v = str(args[m.group(2)])
            return urllib.parse.quote(v, safe="") if m.group(3) else v   # {name|url}: percent-encoded
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


def _dig(data, path):
    """Value at a dotted path; a number indexes a list. Missing anywhere -> None."""
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def pick(data, spec, limit=None):
    """Shrink `data` by a recipe's pick spec. `limit` (a run-time `_limit`) can only lower the saved
    pick.limit, never raise it."""
    if limit is not None:
        spec = dict(spec or {})
        # a recipe with a pick spec but no limit has the default 50; with no spec at all, no cap
        spec["limit"] = min(limit, int(spec.get("limit", 50))) if spec else limit
    if not spec:
        return data
    for part in (spec.get("path") or "").split("."):
        if part:
            data = data.get(part) if isinstance(data, dict) else None
    sel = spec.get("select")
    if sel and isinstance(data, dict):     # {"name": "dotted.path"} -> {"name": value}
        return {k: _dig(data, p) for k, p in sel.items()}
    if isinstance(data, list):
        data = data[: int(spec.get("limit", 50))]
        if sel:
            data = [{k: _dig(d, p) for k, p in sel.items()} if isinstance(d, dict) else d for d in data]
        elif spec.get("fields"):
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
                        ok_check=None, case=None, length=None, refresh_selector=None, fields=None,
                        charset=None, preprocess=None, min_confidence=None):
    """Classic text-in-an-image CAPTCHA (library proxy, NDLTD style): offline ddddocr, retry on a miss.
    Reads the image's own pixels (not an element screenshot), re-reads wrong-length answers before
    submitting, rotates letter case across attempts unless `case` is given. See textcaptcha.py.
    -> attempts used, or 0 on failure."""
    from . import textcaptcha
    r = textcaptcha.solve(page, image_selector, input_selector, case=case, digits_only=digits_only,
                          length=length, tries=tries, submit_selector=submit_selector, ok_check=ok_check,
                          refresh_selector=refresh_selector, fields=fields, charset=charset,
                          preprocess=preprocess, min_confidence=min_confidence)
    return 0 if r["solved"] is False else r["attempts"]


def ntfy(title, message):
    notify.notify(title, message)


HUMAN_WAIT = None    # set (0) by auto-learn: a verify at close_session never waits for a person


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
    if HUMAN_WAIT is not None:
        human_wait = HUMAN_WAIT
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

def _entry(recipe, args=None):
    """entry_url with its {param} placeholders filled in (script recipes from `learn` may have them)."""
    return _render(recipe["entry_url"], args or {}, secrets_ok=False)


def _page_for(recipe, session, args=None):
    """A tab on the recipe's entry origin (reused when already there). With "at_entry" or a templated
    entry_url the tab must show that exact URL (navigated there unless it already does)."""
    ctx = browser._ctx()
    want = _entry(recipe, args)
    exact = bool(recipe.get("at_entry")) or want != recipe["entry_url"]
    try:
        page = browser._current(ctx, session)
        if exact:
            if page.url.split("#")[0] != want.split("#")[0]:
                raise browser.NoTab("other page")
        elif urllib.parse.urlsplit(page.url)[:2] != urllib.parse.urlsplit(want)[:2]:
            raise browser.NoTab("other origin")
    except browser.NoTab:
        browser.wb("navigate", {"url": want}, session=session)
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
    page = _page_for(recipe, session, args)
    gate = challenge_gate(page)
    if not gate["cleared"]:
        return 0, {"_challenge": gate}
    login_gate(page, recipe.get("login"))
    code = f"async (args) => {{\n{recipe['script']}\n}}"
    return 200, page.evaluate(code, args)   # script gets plain params; no secrets in scripts


# ---------- steps: a recorded flow of browser actions, replayed in the session ----------

STEP_ACTIONS = ("navigate", "click", "fill", "select", "wait", "evaluate", "read", "snapshot")
STEP_OUTPUT = ("evaluate", "read", "snapshot")      # the last step: its result is the recipe's output
STEP_KEYS = {"action", "args", "expect", "dialog", "note"}
STEP_SETTLE = ("click", "select", "evaluate")       # may start a navigation (postback): settle before going on
STEP_SETTLE_MS = 15000
STEP_EXPECT_MS = 15000
_CODE_ARGS = ("code", "js")                         # JavaScript: never string-substituted (params come as `args`)


class StepFailed(RecipeError):
    def __init__(self, step, action, msg):
        super().__init__(f"step {step} ({action}): {msg}")
        self.step, self.action = step, action


def check_steps(steps):
    """Validate a steps list (verify, save and every run). RecipeError on: an unknown action / key, snapshot
    before the last step, a last step that returns nothing, a ref (@eN: refs are per snapshot), a
    {secret:…} placeholder (not supported in steps), a placeholder in a navigate URL's host, a bad expect or
    dialog."""
    if not isinstance(steps, list) or not steps:
        raise RecipeError("steps must be a non-empty list of {action, args, expect?, dialog?}")
    if "{secret:" in json.dumps(steps, ensure_ascii=False):
        raise RecipeError("steps may not use {secret:…} (a steps recipe never resolves secrets)")
    last = len(steps) - 1
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or s.get("action") not in STEP_ACTIONS:
            raise RecipeError(f"step {i}: action must be one of {', '.join(STEP_ACTIONS)}")
        act = s["action"]
        extra = set(s) - STEP_KEYS
        if extra:
            raise RecipeError(f"step {i}: unknown key(s) {', '.join(sorted(extra))}")
        a = s.get("args") or {}
        if not isinstance(a, dict):
            raise RecipeError(f"step {i}: args must be an object")
        if act == "snapshot" and i != last:
            raise RecipeError(f"step {i}: snapshot only as the last step")
        if i == last and act not in STEP_OUTPUT:
            raise RecipeError(f"the last step must be one of {', '.join(STEP_OUTPUT)} (its result is the output)")
        exp = s.get("expect")
        for where, obj in (("args", a), ("expect", exp if isinstance(exp, dict) else {})):
            for k in ("selector", "frame"):
                if browser.ref_id(obj.get(k)):
                    raise RecipeError(f"step {i}: {where}.{k} {obj[k]!r} is a ref; refs belong to one snapshot, "
                                      "use a CSS / role selector")
        if act == "navigate":
            u = a.get("url")
            if not isinstance(u, str) or not u.startswith(("http://", "https://")):
                raise RecipeError(f"step {i}: navigate needs an http(s) url")
            if "{" in urllib.parse.urlsplit(u).netloc:
                raise RecipeError(f"step {i}: a placeholder may not be in the host of a navigate url")
        if exp is not None:
            try:
                browser.wait_condition(exp, what=f"step {i}: expect")
            except browser.WbError as ex:
                raise RecipeError(str(ex)) from None
        if s.get("dialog") is not None:
            try:
                browser.parse_dialog_policy(s["dialog"])
            except browser.WbError as ex:
                raise RecipeError(f"step {i}: {ex}") from None
    return steps


def _render_step(obj, args):
    """{param} / {param|url} in a step's string args; JavaScript (`code`, `js`) is left as is."""
    if isinstance(obj, dict):
        return {k: (v if k in _CODE_ARGS else _render_step(v, args)) for k, v in obj.items()}
    return _render(obj, args, secrets_ok=False)


def _expect_text(exp):
    k, v = browser.wait_condition(exp)
    return f"{k} {json.dumps(v, ensure_ascii=False)[:80]}"


def tier_steps(recipe, args, session):
    """Run the recipe's steps in order in the session's current tab (the first step normally navigates). A
    step that fails or whose `expect` is not met within its timeout -> StepFailed (stale, with the index).
    -> (200, the last step's value: evaluate value / read text / snapshot tree)."""
    steps = check_steps(recipe["steps"])
    ctx = browser._ctx()
    if steps[0]["action"] != "navigate":
        _page_for(recipe, session, args)
    out, last_dialog, gated = None, None, False
    for i, s in enumerate(steps):
        act = s["action"]
        sargs = _render_step(dict(s.get("args") or {}), args)
        if s.get("dialog") is not None:
            sargs["dialog"] = s["dialog"]
        if act == "navigate":
            _check_host(sargs["url"])
        if act == "evaluate":
            sargs["arg"] = dict(args)           # a function in `code` gets the params as its argument
        watch = None
        if act in STEP_SETTLE and i < len(steps) - 1:
            try:
                watch = browser._NavWatch(browser._current(ctx, session))
            except browser.WbError:
                watch = None
        try:
            data = browser.wb(act, sargs, session=session)
        except Exception as ex:
            if watch is not None:
                watch.close()
            note = f" (last dialog: {last_dialog})" if last_dialog else ""
            raise StepFailed(i, act, f"{(str(ex).splitlines() or [''])[0][:200]}{note}") from None
        if isinstance(data, dict) and data.get("dialog"):
            d = data["dialog"]
            last_dialog = f"{d.get('type')} {str(d.get('message'))[:60]!r} {d.get('handled')}"
        if act == "wait" and isinstance(data, dict) and data.get("success") is False:
            raise StepFailed(i, act, f"timed out after {data.get('waited_ms')} ms (stale recipe?)")
        if watch is not None:
            watch.settle(STEP_SETTLE_MS)
        if act == "navigate":
            page = browser._current(ctx, session)
            gate = challenge_gate(page)
            if not gate["cleared"]:
                return 0, {"_challenge": gate}
            if not gated and recipe.get("login"):
                login_gate(page, recipe["login"])
            gated = True
        exp = s.get("expect")
        if exp:
            e = _render_step(dict(exp), args)
            e.setdefault("timeout", STEP_EXPECT_MS)
            try:
                w = browser.wb("wait", e, session=session)
            except Exception as ex:
                raise StepFailed(i, act, f"expect {_expect_text(exp)}: {(str(ex).splitlines() or [''])[0][:160]}") \
                    from None
            if not w.get("success"):
                note = f"; last dialog: {last_dialog}" if last_dialog else ""
                raise StepFailed(i, act, f"expect {_expect_text(exp)} not met within {e['timeout']} ms "
                                         f"(stale recipe?{note})")
        out = data
    last = steps[-1]["action"]
    key = {"evaluate": "value", "read": "text", "snapshot": "tree_text"}[last]
    return 200, (out or {}).get(key)


TIERS = {"http": tier_http, "page": tier_page, "script": tier_script, "steps": tier_steps}


def _ladder(recipe, start=None):
    have = ([t for t in ("http", "page") if recipe.get("request") is not None]
            + (["script"] if recipe.get("script") else []) + (["steps"] if recipe.get("steps") else []))
    if start:
        return [start] if start in have else []
    pref = recipe.get("tier")
    return ([pref] if pref in have else []) + [t for t in have if t != pref]


def run(recipe, given, tier=None, allow_write=False, session=None, check_sig=True):
    _check_host(recipe["entry_url"])
    if "{" in urllib.parse.urlsplit(recipe["entry_url"]).netloc:
        raise RecipeError("entry_url: a placeholder may not be in the host")
    if recipe.get("driver") and recipe["driver"] != browser.current_driver():
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
        except StepFailed as ex:                 # the site no longer matches the recorded flow
            tried.append({"tier": t, "step": ex.step, "action": ex.action, "stale": True,
                          "error": str(ex).splitlines()[0][:300]})
            continue
        except Exception as ex:
            tried.append({"tier": t, "error": (str(ex).splitlines() or [""])[0][:200]})
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


def _preview(data, n=PREVIEW_OUT):
    s = json.dumps(data, ensure_ascii=False)
    return data if len(s) <= n else {"truncated": True, "chars": len(s), "head": s[:n]}


def _take_limit(given):
    """Split the reserved `_limit` run parameter off the caller's args -> (args, limit|None)."""
    given = dict(given or {})
    raw = given.pop("_limit", None)
    if raw is None:
        return given, None
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise RecipeError("_limit must be a non-negative integer")
    limit = raw
    return given, limit


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
    if d.get("steps") is not None:
        keep["steps"] = d["steps"]
    if d.get("at_entry"):
        keep["at_entry"] = True
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def needs_in_process(recipe):
    """A recipe that may resolve a secret runs in the calling process, never in the daemon: {secret:…}
    anywhere, a login block, or the opt-in cloud hCaptcha solver (reads its API key)."""
    return (bool(recipe.get("login")) or "{secret:" in json.dumps(recipe, ensure_ascii=False)
            or hcaptcha.enabled())


def _run_via_daemon(rid, given, tier, allow_write, data_only):
    """`run` through the daemon (warm driver + connection). -> (out, rc), or None to run in-process."""
    from . import client
    if not client.enabled():
        return None
    rec = load(rid)
    if needs_in_process(rec):
        return None
    _check_host(rec["entry_url"])
    try:
        rep = client.cli_call("recipe.run", {"rid": rid, "given": given, "tier": tier, "allow_write": allow_write,
                                             "data_only": data_only},
                              session=f"recipe-{_host(rec['entry_url'])}", driver=rec.get("driver"),
                              prog="deja-browser")
    except client.LostReply as ex:
        return {"ok": False, "error": f"lost the daemon's reply ({ex}); the recipe may or may not have run"}, 1
    if rep is None:
        return None
    dialogs = rep.get("dialogs")
    if not rep.get("ok"):
        if rep.get("kind") == "Exception":
            print(rep.get("traceback") or rep.get("error"), file=sys.stderr)
            return None, 1
        out = {"ok": False, "error": rep.get("error")}
        if dialogs:
            out["dialogs"] = dialogs
        return out, 1
    out = rep.get("data")
    if dialogs:
        if isinstance(out, dict) and not data_only:
            out = {**out, "dialogs": dialogs}
        else:
            print(json.dumps({"dialogs": dialogs}, ensure_ascii=False), file=sys.stderr)
    return out, 0 if not (isinstance(out, dict) and out.get("ok") is False) else 1


def cmd_run(rid, given, tier=None, allow_write=False, data_only=False):
    given, limit = _take_limit(given)
    rec = load(rid)
    res = run(rec, given, tier, allow_write)
    rec["last_run"] = {"at": _now(), "ok": res["ok"], "tier": res.get("tier"),
                       "note": None if res["ok"] else json.dumps(res["tried"], ensure_ascii=False)[:300]}
    if res["ok"] and res["tier"] != rec.get("tier") and tier is None:
        rec.setdefault("notes", []).append(f"{_now()} tier {rec.get('tier')} failed, {res['tier']} worked")
        rec["tier"] = res["tier"]
    _write(_path(rid), rec)
    if res["ok"]:
        res["data"] = _out(pick(res["data"], rec.get("pick"), limit))
        if not res["tried"]:
            del res["tried"]       # nothing fell through: don't spend output on an empty list
        if data_only:
            return res["data"]
    return res


def _page_chars(recipe, session, args=None):
    """Length of the entry page's visible text — what reading the page instead would cost. None when
    it can't be measured (no browser, or the page is a challenge, whose text says nothing)."""
    try:
        page = _page_for(recipe, session, args)
        if challenge(page):
            return None
        return int(page.evaluate("() => (document.body ? document.body.innerText : '').length"))
    except Exception:
        return None


def cmd_draft(argv):
    """`draft <capture.json> --expect S … [--param n=v …]` -> report dict (draft file written when applicable)."""
    import argparse
    ap = argparse.ArgumentParser(prog="deja-browser draft", add_help=False)
    ap.add_argument("capture")
    ap.add_argument("--expect", action="append", default=[])
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--name")
    ap.add_argument("--entry-url")
    ap.add_argument("--description")
    ap.add_argument("--out")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-param", action="append", default=[])
    ap.add_argument("--verbose", action="store_true")
    try:
        ns = ap.parse_args(argv)
    except SystemExit:
        raise RecipeError("usage: draft <capture.json> --expect <text> [--expect …] [--param name=value …] "
                          "[--name N] [--entry-url U] [--out FILE] [--force] [--keep-param NAME] [--verbose]") from None
    params = {}
    for kv in ns.param:
        k, eq, v = kv.partition("=")
        if not eq or not k:
            raise RecipeError(f"--param wants name=value, got {kv!r}")
        params[k] = v
    cap_path = Path(ns.capture)
    try:
        capture = json.loads(cap_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise RecipeError(f"cannot read capture {ns.capture}: {ex}") from None
    if ns.entry_url:
        _check_host(ns.entry_url)

    def host_ok(url):
        try:
            guard.check_host(url)
            return True
        except guard.HostRefused:
            return False
    try:
        rep = draft.build(capture, ns.expect, params, name=ns.name, entry_url=ns.entry_url,
                          description=ns.description, host_ok=host_ok, keep=ns.keep_param)
    except draft.DraftError as ex:
        raise RecipeError(str(ex)) from None
    if not rep["applicable"]:
        return rep if ns.verbose else draft_summary(rep)
    d = rep["draft"]
    _check_host(d["entry_url"])
    out = Path(ns.out) if ns.out else cap_path.with_suffix(".draft.json")
    if out.exists() and not ns.force:
        raise RecipeError(f"{out} exists; pass --force to overwrite it, or --out <file>")
    _write(out, d)
    sample = json.dumps(params, ensure_ascii=False)
    rep["draft_path"] = str(out)
    rep["next"] = f"{Path(sys.argv[0]).name} verify {out} '{sample}'"
    return rep if ns.verbose else draft_summary(rep)


def draft_summary(rep):
    """The default `draft` output: what to check before verify, not the whole report (--verbose)."""
    if not rep.get("applicable"):
        out = {"applicable": False, "reason": rep.get("reason")}
        if rep.get("found_in_non_json"):
            out["found_in_non_json"] = rep["found_in_non_json"][:2]
        if rep.get("skipped"):
            out["skipped"] = [f"{s['why']}: {s['url'][:80]}" for s in rep["skipped"][:2]]
        out["suggest"] = rep.get("suggest")
        return out
    d = rep["draft"]
    req = d["request"]
    p = urllib.parse.urlsplit(req.get("url", ""))
    out = {"applicable": True, "draft_path": rep.get("draft_path"),
           "request": f"{req.get('method', 'GET')} {p.netloc}{p.path[:100]}",
           "params": [x["name"] for x in d.get("params", [])], "pick": d.get("pick"), "matched": rep.get("matched")}
    w = rep.get("warnings") or []
    if w:
        out["warnings"] = [x[:200] for x in w[:3]]
        if len(w) > 3:
            out["more_warnings"] = len(w) - 3
    if rep.get("alternatives"):
        out["alternatives"] = len(rep["alternatives"])
    out["next"] = rep.get("next")
    return out


def _verify_tiers(d, given, expects=(), session=None):
    """Run a draft on every tier it has. -> (results, passed tiers, {tier: signature}). With `expects`, a tier
    passes only if its (picked) output holds every one of them as a value (draft.find_paths)."""
    results, sigs = [], {}
    for t in _ladder(d):
        r = run(d, given, tier=t, allow_write=True, session=session, check_sig=False)
        if r["ok"]:
            picked = pick(r["data"], d.get("pick"))
            missing = [e for e in expects if not draft.find_paths(picked, e)]
            sigs[t] = signature(picked)
            same = next((u for u, g in sigs.items() if u != t and g == sigs[t]), None)
            row = {"tier": t, "ok": not missing, "out_chars": len(json.dumps(picked, ensure_ascii=False)),
                   "preview": f"same shape as {same}" if same else _preview(picked)}
            if missing:
                row["missing"] = missing
            results.append(row)
        else:
            results.append({"tier": t, "ok": False, "tried": r["tried"]})
    return results, [r["tier"] for r in results if r["ok"]], sigs


def _write_verified(draft_path, d, passed, sigs, given):
    _write(Path(draft_path).with_suffix(".verified.json"),
           {"at": time.time(), "hash": _draft_hash(d), "passed": passed,
            "signature": sigs[passed[0]] if passed else None, "args": given})


def _check_draft(d):
    for k in ("name", "description", "entry_url"):
        if not d.get(k):
            raise RecipeError(f"draft needs {k!r}")
    if not (d.get("request") or d.get("script") or d.get("steps")):
        raise RecipeError("draft needs request, script or steps")
    _check_host(d["entry_url"])
    if d.get("steps") is not None:
        check_steps(d["steps"])
        for s in d["steps"]:
            if s["action"] == "navigate":
                _check_host(s["args"]["url"])


def cmd_verify(draft_path, given):
    d = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    _check_draft(d)
    results, passed, sigs = _verify_tiers(d, given)
    _write_verified(draft_path, d, passed, sigs, given)
    out = {"passed": passed, "cheapest": passed[0] if passed else None, "results": results,
           "next": f"{Path(sys.argv[0]).name} save {draft_path}" if passed else "fix the draft and verify again"}
    if passed and not d.get("steps"):           # a steps recipe's entry page is not where its answer is
        pc = _page_chars(d, f"recipe-{_host(d['entry_url'])}", _args(d, given))
        if pc is not None:
            out["page_chars"] = pc
            big = next(r for r in results if r["ok"])["out_chars"]
            if big > pc * PAGE_RATIO:
                out["warning"] = (f"recipe output ({big} chars) is not much smaller than the page's text "
                                  f"({pc} chars) — return only the fields the answer needs, or just read the page")
    return out


def cmd_save(draft_path):
    d = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    side = Path(draft_path).with_suffix(".verified.json")
    if not side.exists():
        raise RecipeError("verify this draft first")
    v = json.loads(side.read_text(encoding="utf-8"))
    if d.get("steps") is not None:
        check_steps(d["steps"])
    if v["hash"] != _draft_hash(d):
        raise RecipeError("draft changed after verify — verify again")
    if not v["passed"] or time.time() - v["at"] > 3600:
        raise RecipeError("no passing verify in the last hour")
    d.update({"version": 1, "tier": v["passed"][0], "expect": {"signature": v["signature"]},
              "verified_at": _now(), "verified_args": v["args"]})
    rid = f"{_host(d['entry_url'])}/{d['name']}"
    _write(_path(rid), d)
    return {"saved": rid, "tier": d["tier"], "path": str(_path(rid))}


# ---------- learn: recording -> verified, saved recipe ----------

LEARN_OUT = 400      # chars of the default learn output (success or failure)


def _learn_fail(reason, hint="", verbose=None):
    out = {"ok": False, "reason": reason, "hint": hint}
    if verbose is not None:
        return {**out, "details": verbose}
    from . import learn
    return learn.compact(out, LEARN_OUT)


def _host_ok(url):
    try:
        guard.check_host(url)
        return True
    except guard.HostRefused:
        return False


def _verify_failure(kind, results, plan):
    """One-line reason + hint for a plan whose verify failed."""
    bits = []
    for r in results:
        if r.get("missing"):
            bits.append(f"{r['tier']}: output lacks {r['missing'][0][:40]!r}")
        elif not r["ok"]:
            t = (r.get("tried") or [{}])[-1]
            why = t.get("error") or (f"status {t['status']}" if t.get("status") else None) or \
                ("challenge not cleared" if t.get("challenge") else "failed")
            bits.append(f"{r['tier']}: {str(why)[:80]}")
    reason = f"verify of the {kind} recipe failed ({'; '.join(bits) or 'no tier to run'}); nothing saved"
    rep = plan.get("report") or {}
    if rep.get("dropped"):
        hint = (f"the request lost credential-looking field(s) {', '.join(rep['dropped'][:3])}; if it is a public "
                f"key from the page source, learn again with --keep-param {rep['dropped'][0]}")
    elif rep.get("redacted_headers"):
        hint = (f"the page sent {', '.join(rep['redacted_headers'][:3])} (not copied); recipes that need secrets are "
                "out of scope for learn: write one by hand with {secret:NAME}")
    elif kind == "script":
        hint = "the code may depend on page state from the visit (clicks, scrolling, a login); check it with verify"
    elif kind == "steps":
        hint = ("the replayed flow did not reach the same answer (see the step); learn again with --verbose to see "
                "the steps, or with --param to name the values that change")
    else:
        hint = "check the --expect strings, or learn again with --verbose for the details"
    return reason, hint


def cmd_learn(argv, recording=None):
    """`learn --name N --expect S … [--session S] [--param n=v …] [--keep-param K …] [--write] [--force]
    [--verbose]` -> (output dict, exit code). See learn.py for the rules."""
    import argparse
    import shutil
    import tempfile
    from . import client, learn
    ap = argparse.ArgumentParser(prog="deja-browser learn", add_help=False)
    ap.add_argument("--session", default="cli")
    ap.add_argument("--name")
    ap.add_argument("--expect", action="append", default=[])
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--keep-param", action="append", default=[])
    ap.add_argument("--description")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    try:
        ns = ap.parse_args(argv)
    except SystemExit:
        return _learn_fail("usage: learn --name N --expect TEXT [--expect …] [--session S] [--param name=value …] "
                           "[--keep-param NAME] [--write] [--force] [--verbose]"), 2
    V = {} if ns.verbose else None
    if not ns.name or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", ns.name):
        return _learn_fail("--name is required (letters, digits, _ . -)", "", V), 2
    if not ns.expect or any(len(e) < draft.MIN_EXPECT for e in ns.expect):
        return _learn_fail(f"give --expect: text the answer needs, >= {draft.MIN_EXPECT} chars each", "", V), 2
    try:
        explicit = learn.parse_params(ns.param)
    except learn.LearnError as ex:
        return _learn_fail(ex.reason, ex.hint, V), 2
    if not client.enabled():
        return _learn_fail("nothing is recorded in-process (DEJA_BROWSER_DAEMON=0): only the daemon records visits",
                           "capture the request with deja-cdp network_capture, then deja-browser draft", V), 1
    tmp = Path(tempfile.mkdtemp(prefix="deja-learn-"))
    try:
        if recording is not None:          # auto-learn: fetched (bodies included) before close_session
            rec = recording
        else:
            try:
                rep = client.call_running("recording", {"body_dir": str(tmp)}, session=ns.session)
            except (client.Unavailable, client.LostReply) as ex:
                return _learn_fail(f"no recording to learn from ({ex})",
                                   "do the visit with deja-cdp (the daemon records it), then learn before "
                                   "close_session", V), 1
            if not rep.get("ok"):
                return _learn_fail(f"recording: {rep.get('error')}", "", V), 1
            rec = rep["data"]
        if V is not None:
            V["recording"] = {"responses": len(rec.get("responses") or []), "commands": len(rec.get("commands") or []),
                              "url": rec.get("url")}
        if not rec.get("responses") and not rec.get("commands"):
            return _learn_fail(f"session {ns.session!r} has no recording",
                               "learn with the --session of the visit, before close_session (it clears the "
                               "recording)", V), 1
        plans, errors = [], []
        try:
            p1, rep1 = learn.plan_api(rec, ns.expect, explicit, ns.keep_param, ns.name, _host_ok, ns.write,
                                      ns.description)
            if V is not None:
                V["draft"] = rep1
            if p1:
                plans.append(p1)
        except learn.LearnError as ex:
            errors.append(ex)
        try:
            p2 = learn.plan_script(rec, ns.expect, explicit, ns.name, ns.description, ns.write)
            if p2:
                plans.append(p2)
        except learn.LearnError as ex:
            errors.append(ex)
        if not plans:                     # path 3: replay the recorded browser actions
            blocking = [e for e in errors if not getattr(e, "fallback", False)]
            if blocking:
                return _learn_fail(blocking[0].reason, blocking[0].hint, V), 1
            try:
                p3 = learn.plan_steps(rec, ns.expect, explicit, ns.name, ns.description, ns.write)
            except learn.LearnError as ex:
                return _learn_fail(ex.reason, ex.hint, V), 1
            if p3:
                plans.append(p3)
        if not plans:
            if errors:
                return _learn_fail(errors[0].reason, errors[0].hint, V), 1
            docs = learn.found_in_documents(rec, ns.expect)
            if docs:
                return _learn_fail(f"the answer is in the page HTML ({docs[-1][:100]}), not in a JSON response, and "
                                   "no evaluate returned it",
                                   "run one evaluate that returns just the answer's fields, then learn again", V), 1
            big = any(r.get("type") == "document" and r.get("body_error") == "too big"
                      for r in rec.get("responses") or [])
            return _learn_fail("no recorded JSON response, read or evaluate result contains every --expect"
                               + (" (the page HTML was too big to keep)" if big else ""),
                               "read the answer (deja-cdp read / evaluate, same --session) so the recording holds "
                               "it, then learn again; or check the --expect text", V), 1
        rid = f"{_host(plans[0]['draft']['entry_url'])}/{ns.name}"
        if _path(rid).exists() and not ns.force:
            return _learn_fail(f"recipe {rid} exists", "pass --force to replace it, or choose another --name", V), 1
        first_fail = None
        for plan in plans:
            d = plan["draft"]
            # a steps flow is verified in a fresh tab of its own session (closed afterwards), never in the tab
            # of the visit or of an earlier run
            vs = f"learn-{_host(d['entry_url'])}" if plan["kind"] == "steps" else None
            try:
                _check_draft(d)
                results, passed, sigs = _verify_tiers(d, plan["args"], ns.expect, **({"session": vs} if vs else {}))
            except (RecipeError, browser.WbError) as ex:
                results, passed, sigs = [{"tier": "-", "ok": False, "tried": [{"error": str(ex)[:200]}]}], [], {}
            finally:
                if vs:
                    try:
                        browser.wb("close_session", {}, session=vs)
                    except Exception:
                        pass
            if V is not None:
                V.setdefault("verify", []).append({"kind": plan["kind"], "draft": d, "results": results})
            if not passed:
                first_fail = first_fail or _verify_failure(plan["kind"], results, plan)
                continue
            dp = tmp / "learn.draft.json"
            _write(dp, d)
            _write_verified(dp, d, passed, sigs, plan["args"])
            saved = cmd_save(str(dp))
            row = next(r for r in results if r["tier"] == passed[0])
            out = {"ok": True, "id": saved["saved"], "tier": saved["tier"], "kind": plan["kind"],
                   "params": {k: str(v)[:40] for k, v in plan["args"].items()}, "sample_chars": row["out_chars"]}
            if V is not None:
                return {**out, "details": V}, 0
            return learn.compact(out, LEARN_OUT), 0
        return _learn_fail(*first_fail, V), 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def auto_enabled():
    """[recipes] auto_learn (default true); DEJA_BROWSER_AUTO_LEARN=0 turns it off for one process."""
    env = os.environ.get("DEJA_BROWSER_AUTO_LEARN")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(config.get("recipes", "auto_learn", True))


def auto_learn_prepare(session):
    """BEFORE close_session (which clears the recording): fetch the recording, bodies into a temp dir, and
    decide. -> None (nothing to say: off, no daemon, nothing recorded, a recipe / learn session),
    {"result": {...}} (skipped), or {"session", "plan", "rec", "tmp"} for auto_learn_finish."""
    import shutil
    import tempfile
    from . import client, learn
    if not auto_enabled() or (session or "").startswith(learn.AUTO_SKIP_SESSIONS) or not client.enabled():
        return None
    def fetch(args):
        try:
            rep = client.call_running("recording", args, session=session)
        except (client.Unavailable, client.LostReply):
            return {}
        return (rep.get("data") or {}) if rep.get("ok") else {}
    rec = fetch({})                        # metadata only: nothing is written unless it will learn
    if not rec.get("commands"):
        return None
    plan = learn.auto_plan(rec, session)
    if "skip" not in plan:
        hd = RECIPES / plan["host"]
        have = sorted(x.stem for x in hd.glob("*.json")) if hd.is_dir() else []
        if have:
            plan = {"skip": f"{plan['host']} already has recipes ({', '.join(have[:5])}); learn by hand "
                            "to add another"}
    if "skip" in plan:
        return {"result": {"ok": False, "skipped": plan["skip"][:200]}}
    tmp = Path(tempfile.mkdtemp(prefix="deja-autolearn-"))
    try:
        rec = fetch({"body_dir": str(tmp)})
        if not rec.get("commands"):
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        return {"session": session, "plan": plan, "rec": rec, "tmp": tmp}
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def auto_learn_finish(prep):
    """AFTER close_session: learn from the prepared recording -> the reply's `learned` (or None). The visit's
    tabs are already closed, so a slow or killed learn never leaves them open; a challenge met while verifying
    is never handed to a person (HUMAN_WAIT 0); the verify's own recipe-<host> session is closed afterwards
    (the host had no recipe, so no earlier run owned it)."""
    import shutil
    global HUMAN_WAIT
    if prep is None or "result" in prep:
        return None if prep is None else prep["result"]
    plan, session = prep["plan"], prep["session"]
    argv = ["--session", session, "--name", plan["name"]]
    for e in plan["expects"]:
        argv += ["--expect", e]
    old, HUMAN_WAIT = HUMAN_WAIT, 0
    try:
        out, _ = cmd_learn(argv, recording=prep["rec"])
    finally:
        HUMAN_WAIT = old
        shutil.rmtree(prep["tmp"], ignore_errors=True)
        try:
            if browser.version():          # never start a browser just to close a session
                browser.wb("close_session", {}, session=f"recipe-{plan['host']}")
        except Exception:
            pass
    out = dict(out)
    if out.get("ok"):
        return {"ok": True, "id": out.get("id"), "tier": out.get("tier"), "kind": out.get("kind"),
                "params": sorted(out.get("params") or {})}
    return {"ok": False, "reason": str(out.get("reason") or out.get("error") or "")[:200]}


def auto_learn(session):
    """prepare + finish in one call (no close in between): tests and library callers."""
    return auto_learn_finish(auto_learn_prepare(session))


def cmd_recording(argv):
    """`recording [--session S] [--clear] [--verbose]`: what the daemon recorded for a session (no bodies)."""
    from . import client
    session = "cli"
    if "--session" in argv:
        i = argv.index("--session")
        session = argv[i + 1]
    try:
        if "--clear" in argv:
            rep = client.call_running("recording", {"clear": True}, session=session)
        else:
            rep = client.call_running("recording", {}, session=session)
    except (client.Unavailable, client.LostReply) as ex:
        return {"ok": False, "error": f"no daemon recording: {ex}"}
    if not rep.get("ok"):
        return {"ok": False, "error": rep.get("error")}
    data = rep["data"]
    if "--verbose" in argv or "cleared" in data:
        return data
    resp = data.get("responses") or []
    return {"session": session, "url": data.get("url"), "responses": len(resp), "commands": len(data.get("commands") or []),
            "json": [f"{r.get('method')} {draft.clean_url(r.get('url', ''))[:100]}" for r in resp
                     if "json" in (r.get("mime") or "")][-5:],
            "last": [f"{c['n']} {c['action']} {'ok' if c.get('ok') else 'failed'}" for c in data.get("commands") or []][-5:]}


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
    flags = {f for f in ("--allow-write", "--data-only") if f in argv}
    argv = [a for a in argv if a not in flags]
    tier = None
    if "--tier" in argv:
        i = argv.index("--tier")
        tier = argv[i + 1]
        del argv[i:i + 2]
    from . import clihelp
    if clihelp.wants_help(argv):
        text, rc = clihelp.recipe_help(clihelp.topic_of(argv))
        print(text)
        return rc if argv else 2
    cmd, rest = argv[0], argv[1:]
    js = lambda i: json.loads(rest[i]) if len(rest) > i else {}  # noqa: E731
    try:
        if cmd == "list":
            out = cmd_list()
        elif cmd == "show":
            out = load(rest[0])
        elif cmd == "run":
            r = _run_via_daemon(rest[0], js(1), tier, "--allow-write" in flags, "--data-only" in flags)
            if r is not None:
                if r[0] is not None:
                    print(json.dumps(r[0], ensure_ascii=False))
                return r[1]
            out = cmd_run(rest[0], js(1), tier, "--allow-write" in flags, "--data-only" in flags)
        elif cmd == "draft":
            out = cmd_draft(rest)
            print(json.dumps(out, ensure_ascii=False))
            return 0 if out.get("applicable") else 2
        elif cmd == "learn":
            out, rc = cmd_learn(rest)
            print(json.dumps(out, ensure_ascii=False))
            return rc
        elif cmd == "recording":
            out = cmd_recording(rest)
        elif cmd == "verify":
            out = cmd_verify(rest[0], js(1))
        elif cmd == "save":
            out = cmd_save(rest[0])
        elif cmd == "gate":
            out = cmd_gate(rest[0])
        else:
            print(f"deja-browser: unknown command {cmd!r}", clihelp.recipe_help()[0], sep="\n\n")
            return 2
    except (RecipeError, browser.WbError) as ex:
        print(json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0 if not (isinstance(out, dict) and out.get("ok") is False) else 1


if __name__ == "__main__":
    sys.exit(main())
