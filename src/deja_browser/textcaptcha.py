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

Tuning for hard images (2026-11-22, 85 hand-labelled samples of digits in random colours, rotated,
overlapping and crossed by random-colour lines; whole-code accuracy, 4-digit / 6-digit kind):
- plain ddddocr text: 37% / 55% (14/38, 26/47). Most misses were a dropped or merged
  character, or a letter that looks like a digit (o, q, g).
- `charset` limits the model's per-frame scores to the allowed characters BEFORE choosing, instead of
  deleting other characters from the text afterwards (which is what `digits_only` does).
- With `charset` and a known length, decoding picks the best reading with exactly that many
  characters (repeated digits like 7799 survive; a weak extra character is dropped).
- `preprocess="binarize"`: the most common colour (the flat background) becomes white and every other
  pixel black, so coloured glyphs reach the grey-scale model at full contrast.
- Together: 76% / 96% (29/38, 45/47). Erasing thin lines (morphological opening), padding, the beta
  model, keeping only the glyph colours, and averaging several of these did not beat it beyond noise.
- `min_confidence` refreshes a read whose weakest frame is less sure than that, like a wrong length.
"""
import base64
import io
import math
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


PREPROCESS = ("binarize",)


def preprocess_image(png, mode):
    """'binarize': most common colour (the background) -> white, every other pixel -> black."""
    if mode not in PREPROCESS:
        raise ValueError(f"preprocess must be one of {PREPROCESS}")
    import numpy as np            # ddddocr's own dependencies
    from PIL import Image
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(int)
    colours, counts = np.unique(a.reshape(-1, 3), axis=0, return_counts=True)
    fg = np.abs(a - colours[counts.argmax()]).sum(axis=2) > 40
    out = io.BytesIO()
    Image.fromarray(np.where(fg, 0, 255).astype(np.uint8)).save(out, "PNG")
    return out.getvalue()


def ctc_decode(frames, alphabet, length=None):
    """CTC frames -> (text, confidence). frames: per-frame probabilities over [blank] + alphabet.
    Without `length`: best symbol per frame, repeats collapsed, blanks dropped. With `length`: the
    best single path whose collapsed text has exactly `length` characters (greedy if impossible).
    confidence: the weakest frame's best probability."""
    rows = []
    for f in frames:
        s = sum(f) or 1.0
        rows.append([p / s for p in f])
    if not rows:
        return "", 0.0
    conf = min(max(r) for r in rows)
    best = None
    if length and len(rows) >= length:
        best = _ctc_fixed_length(rows, length)
    if best is None:
        best = [max(range(len(r)), key=r.__getitem__) for r in rows]
    out, prev = [], None
    for s in best:
        if s != prev and s != 0:
            out.append(alphabet[s - 1])
        prev = s
    return "".join(out), conf


def _ctc_fixed_length(rows, n):
    """Viterbi over (characters emitted k, last symbol s) -> best path of symbols, or None."""
    S, NEG = len(rows[0]), float("-inf")
    lg = [[math.log(p) if p > 0 else -1e9 for p in r] for r in rows]
    V = [[NEG] * S for _ in range(n + 1)]
    V[0][0] = lg[0][0]
    for s in range(1, S):
        V[1][s] = lg[0][s]
    back = []
    for t in range(1, len(rows)):
        W = [[NEG] * S for _ in range(n + 1)]
        B = [[None] * S for _ in range(n + 1)]
        for k in range(n + 1):
            row = V[k]
            top = max(range(S), key=row.__getitem__)
            W[k][0], B[k][0] = row[top] + lg[t][0], (k, top)          # blank keeps k
            if k:
                prev = V[k - 1]   # a new character: from any other symbol one step back in k
                order = sorted(range(S), key=prev.__getitem__, reverse=True)[:2]
            for s in range(1, S):
                cand = (row[s], (k, s))                                 # same character continues
                if k:
                    s0 = order[0] if order[0] != s else order[1]
                    if prev[s0] > cand[0]:
                        cand = (prev[s0], (k - 1, s0))
                if cand[0] > NEG:
                    W[k][s], B[k][s] = cand[0] + lg[t][s], cand[1]
        V = W
        back.append(B)
    s = max(range(S), key=V[n].__getitem__)
    if V[n][s] == NEG:
        return None
    k, path = n, [s]
    for B in reversed(back):
        k, s = B[k][s]
        path.append(s)
    return path[::-1]


def _probability_frames(ocr, png, alphabet):
    """ddddocr probability output restricted to `alphabet` -> (frames, alphabet actually used), or
    None when this OCR has no probability API. Key names and nesting differ between releases."""
    try:
        r = ocr.classification(png, probability=True)
    except TypeError:
        return None
    if not isinstance(r, dict):
        return None
    charset = r.get("charset") or r.get("charsets")
    probs = r.get("probabilities") or r.get("probability")
    if not charset or not probs:
        return None
    index = {c: i for i, c in enumerate(charset)}
    cols, used, seen = [0], [], set()
    for c in alphabet:   # the model may only know lower case: map to it, report the char as asked
        i = index.get(c, index.get(c.lower()))
        if i is not None and i not in seen:
            seen.add(i)
            cols.append(i)
            used.append(c)
    frames = []
    for f in probs:
        while f and isinstance(f[0], (list, tuple)):    # (T, 1, C) -> (T, C)
            f = f[0]
        frames.append([f[i] for i in cols])
    return frames, "".join(used)


def read_png(png, ocr, case=None, digits_only=False, length=None, charset=None, preprocess=None):
    """One OCR pass over image bytes -> {code, raw, confidence, decoder}. With `charset`, decodes the
    model's per-frame scores restricted to those characters (exact length when `length` is known);
    falls back to text + filtering when the OCR has no probability API."""
    if preprocess:
        png = preprocess_image(png, preprocess)
    if charset:
        got = _probability_frames(ocr, png, charset)
        if got:
            code, conf = ctc_decode(got[0], got[1], length)
            return {"code": normalize(code, case), "raw": code, "confidence": round(conf, 3),
                    "decoder": "ctc"}
    raw = ocr.classification(png)
    code = normalize(raw, case, digits_only)
    if charset:
        code = "".join(c for c in code if c in charset or c.lower() in charset or c.upper() in charset)
    return {"code": code, "raw": raw, "confidence": None, "decoder": "text"}


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


def _sure(read, length, min_confidence):
    if not plausible(read["code"], length):
        return False
    return min_confidence is None or read["confidence"] is None or read["confidence"] >= min_confidence


def read_code(page, image_selector, input_selector=None, case=None, digits_only=False, length=None,
              refresh_selector=None, rereads=4, ocr=None, charset=None, preprocess=None,
              min_confidence=None):
    """OCR the CAPTCHA, re-reading (with a refresh) while the answer has the wrong length or, with
    `min_confidence`, is less sure than that.
    -> {code, raw, plausible, length, rereads, confidence, decoder, source, size, shown}."""
    ocr = ocr or _ocr()
    if not length and input_selector:
        length = _eval(page, _JS_MAXLEN, input_selector) or None
    info = {}
    for n in range(rereads + 1):
        png, info = grab_image(page, image_selector)
        read = read_png(png, ocr, case, digits_only, length, charset, preprocess)
        if _sure(read, length, min_confidence):
            break
        if n < rereads:
            _refresh(page, image_selector, refresh_selector)
    return {**read, "plausible": _sure(read, length, min_confidence), "length": length, "rereads": n, **info}


def solve(page, image_selector, input_selector, case=None, digits_only=False, length=None, tries=6,
          submit_selector=None, ok_check=None, refresh_selector=None, fields=None, charset=None,
          preprocess=None, min_confidence=None):
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
                         length, refresh_selector, ocr=ocr, charset=charset, preprocess=preprocess,
                         min_confidence=min_confidence)
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
