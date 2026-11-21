"""Text-in-an-image CAPTCHAs (library catalogues, proxies, NDLTD style), solved offline with ddddocr.

Lessons this module encodes (2026-11-21, a library OPAC login that failed 7 times in a row):
- Never OCR an element screenshot. The page draws the image in a box of a different size than the
  image itself, so the screenshot was cropped (4 characters became 3) and rescaled. Read the image's
  own pixels instead: draw it on a canvas at naturalWidth x naturalHeight. Re-requesting the image's
  URL is NOT a fallback, because many sites issue a new code per request.
- Check the answer before submitting: when the length is known (the input's maxlength, or `length=`)
  a wrong-length read is refreshed and read again instead of burning a login attempt.
- ddddocr answers in lower case; many sites print upper case and some compare case-sensitively.
  With no `case=` given, each new attempt after a rejected one tries the next case (as read, upper,
  lower).
- A rejected submit usually reloads the form EMPTY. Retrying with only a fresh CAPTCHA then posts a
  blank username/password, which fails for a reason that has nothing to do with the CAPTCHA. Pass
  `fields` (selector -> value) so every attempt refills them.
"""
import base64
import re

_JS_GRAB = """(sel) => {
  const i = document.querySelector(sel);
  if (!i) return {err: 'no element'};
  if (i.tagName !== 'IMG' && i.tagName !== 'CANVAS') return {err: 'not an <img>/<canvas>: ' + i.tagName};
  if (i.tagName === 'IMG' && !(i.complete && i.naturalWidth)) return {err: 'image not loaded'};
  try {
    let c = i;
    if (i.tagName === 'IMG') {
      c = document.createElement('canvas');
      c.width = i.naturalWidth; c.height = i.naturalHeight;
      c.getContext('2d').drawImage(i, 0, 0);
    }
    return {b64: c.toDataURL('image/png').split(',')[1], w: c.width, h: c.height,
            shown_w: Math.round(i.getBoundingClientRect().width), shown_h: Math.round(i.getBoundingClientRect().height)};
  } catch (e) { return {err: 'canvas: ' + e.message}; }   // cross-origin image taints the canvas
}"""

_JS_MAXLEN = """(sel) => { const e = document.querySelector(sel); return e && e.maxLength > 0 && e.maxLength < 12 ? e.maxLength : 0; }"""

CASES = (None, "upper", "lower")


def _eval(page, js, arg):
    try:   # patchright runs page.evaluate in an isolated world by default; the DOM is the same
        return page.evaluate(js, arg, isolated_context=False)
    except TypeError:
        return page.evaluate(js, arg)


def grab_image(page, selector):
    """-> (png_bytes, info). The image's own pixels via canvas; element screenshot only when the
    canvas route is impossible (cross-origin image, CSS background), flagged in info['source']."""
    r = _eval(page, _JS_GRAB, selector) or {"err": "no result"}
    if r.get("b64"):
        return base64.b64decode(r["b64"]), {"source": "canvas", "size": [r["w"], r["h"]],
                                            "shown": [r["shown_w"], r["shown_h"]]}
    loc = page.locator(selector).first
    loc.scroll_into_view_if_needed(timeout=3000)
    return loc.screenshot(), {"source": "screenshot", "why": r.get("err")}


def normalize(raw, case=None, digits_only=False):
    """OCR text -> what to type: keep [0-9A-Za-z] (digits only if asked), then apply case."""
    code = re.sub(r"\D" if digits_only else r"[^0-9A-Za-z]", "", raw or "")
    if case == "upper":
        return code.upper()
    if case == "lower":
        return code.lower()
    return code


def case_for_attempt(case, attempt):
    """Fixed case when given; otherwise rotate as-read -> upper -> lower over attempts 1, 2, 3, ..."""
    return case if case else CASES[(attempt - 1) % len(CASES)]


def plausible(code, length):
    return bool(code) and (not length or len(code) == length)


def _ocr():
    try:
        import ddddocr
    except ImportError as ex:
        raise RuntimeError('text CAPTCHA needs ddddocr: pip install "deja-browser[ocr]"') from ex
    return ddddocr.DdddOcr(show_ad=False)


def _refresh(page, image_selector, refresh_selector):
    try:
        if refresh_selector:
            page.locator(refresh_selector).first.click(timeout=2000)
        else:   # most sites swap the image when it is clicked
            page.locator(image_selector).first.click(timeout=2000)
        page.wait_for_timeout(800)
    except Exception:
        pass


def read_code(page, image_selector, input_selector=None, case=None, digits_only=False, length=None,
              refresh_selector=None, rereads=4, ocr=None):
    """OCR the CAPTCHA, re-reading (with a refresh) while the answer has the wrong length.
    -> {code, raw, plausible, length, rereads, source, size, shown}."""
    ocr = ocr or _ocr()
    if not length and input_selector:
        length = _eval(page, _JS_MAXLEN, input_selector) or None
    info = {}
    for n in range(rereads + 1):
        png, info = grab_image(page, image_selector)
        raw = ocr.classification(png)
        code = normalize(raw, case, digits_only)
        if plausible(code, length):
            break
        if n < rereads:
            _refresh(page, image_selector, refresh_selector)
    return {"code": code, "raw": raw, "plausible": plausible(code, length), "length": length,
            "rereads": n, **info}


def solve(page, image_selector, input_selector, case=None, digits_only=False, length=None, tries=6,
          submit_selector=None, ok_check=None, refresh_selector=None, fields=None):
    """Read, fill and (optionally) submit, retrying on a miss.
    ok_check: JS expression, truthy once the site accepted the code (e.g. the login form is gone).
    fields: {selector: value} refilled before every attempt (the form is often reset by a miss).
    -> {solved, attempts, last: read_code() result without the code}."""
    ocr = _ocr()
    last = {}
    for i in range(1, tries + 1):
        for sel, val in (fields or {}).items():
            page.fill(sel, str(val), timeout=5000)
        last = read_code(page, image_selector, input_selector, case_for_attempt(case, i), digits_only,
                         length, refresh_selector, ocr=ocr)
        page.fill(input_selector, last["code"], timeout=5000)
        if not submit_selector:
            return {"solved": None, "attempts": i, "last": last}
        page.locator(submit_selector).first.click(timeout=8000)
        page.wait_for_timeout(2500)
        if ok_check is None or _eval(page, ok_check, None):   # expression or function, as before
            return {"solved": True, "attempts": i, "last": last}
        if page.locator(image_selector).count() and not fields:   # same form, not reloaded: new image
            _refresh(page, image_selector, refresh_selector)
    return {"solved": False, "attempts": tries, "last": last}
