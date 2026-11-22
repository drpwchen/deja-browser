#!/usr/bin/env python3
"""Text-CAPTCHA decoding: charset-limited CTC decoding, exact-length decoding, confidence gate,
background flattening, and accuracy on real sample images. run: python -m pytest -q

The decoder tests need nothing but the standard library. The preprocessing test needs Pillow and
numpy, the accuracy test needs ddddocr; both are skipped when those are not installed."""
import csv
import os
import unittest
from unittest import mock

from deja_browser import textcaptcha as tc

DIGITS = "0123456789"
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "textcaptcha")


def frame(probs, alphabet=DIGITS):
    """{symbol: p} -> one frame over [blank] + alphabet; '' is the blank."""
    row = [probs.get("", 0.0)] + [probs.get(c, 0.0) for c in alphabet]
    rest = (1.0 - sum(row)) / len(row)
    return [p + rest for p in row]


class TestCtcDecode(unittest.TestCase):
    def test_greedy_collapses_repeats_and_drops_blanks(self):
        f = [frame({"7": .9}), frame({"": .9}), frame({"7": .9}), frame({"9": .9}), frame({"9": .9}),
             frame({"": .9}), frame({"9": .9})]
        self.assertEqual(tc.ctc_decode(f, DIGITS)[0], "7799")

    def test_exact_length_splits_a_merged_repeat(self):
        # greedy path 0 0 0 5 4 collapses to "054"; the code has 4 characters, so the weak middle
        # frame must be the blank that separates two zeros
        f = [frame({"0": .9}), frame({"0": .55, "": .44}), frame({"0": .9}), frame({"5": .9}), frame({"4": .9})]
        self.assertEqual(tc.ctc_decode(f, DIGITS)[0], "054")
        self.assertEqual(tc.ctc_decode(f, DIGITS, length=4)[0], "0054")

    def test_exact_length_drops_a_weak_extra_character(self):
        f = [frame({"1": .9}), frame({"8": .4, "": .35}), frame({"2": .9}), frame({"3": .9}), frame({"4": .9})]
        self.assertEqual(tc.ctc_decode(f, DIGITS)[0], "18234")
        self.assertEqual(tc.ctc_decode(f, DIGITS, length=4)[0], "1234")

    def test_too_few_frames_for_length_falls_back_to_greedy(self):
        f = [frame({"1": .9}), frame({"2": .9})]
        self.assertEqual(tc.ctc_decode(f, DIGITS, length=5)[0], "12")

    def test_confidence_is_the_weakest_frame(self):
        f = [frame({"1": .9}), frame({"2": .6}), frame({"3": .95})]
        code, conf = tc.ctc_decode(f, DIGITS)
        self.assertEqual(code, "123")
        self.assertAlmostEqual(conf, frame({"2": .6})[3], places=6)


class ProbOcr:
    """ddddocr stand-in with the probability API of ddddocr 1.6 (frames shaped T x 1 x C)."""
    CHARSET = ["", "o", "a", "q"] + list(DIGITS)

    def __init__(self, reads):
        self.reads = list(reads)          # each read: list of {symbol: p} frames

    def classification(self, png, probability=False):
        frames = self.reads.pop(0)
        rows = []
        for fr in frames:
            row = [fr.get(c, 0.0) for c in self.CHARSET]
            rest = (1.0 - sum(row)) / len(row)
            rows.append([[p + rest for p in row]])
        if not probability:
            raise AssertionError("charset decoding must use probability=True")
        return {"text": "", "probabilities": rows, "charset": list(self.CHARSET), "confidence": 0.0}


class TextOcr:
    """An OCR without the probability API (older ddddocr, or anything else): text only."""
    def __init__(self, reads):
        self.reads = list(reads)

    def classification(self, png):
        return self.reads.pop(0)


class Page:
    def __init__(self, maxlength=0):
        self.maxlength, self.clicks = maxlength, []

    def evaluate(self, js, arg=None, isolated_context=None):
        if js is tc._JS_GRAB:
            return {"b64": "iVBORw0K", "w": 150, "h": 40, "shown_w": 120, "shown_h": 44}
        if js is tc._JS_MAXLEN:
            return self.maxlength
        return None

    def locator(self, sel):
        page = self

        class L:
            first = None

            def click(self, timeout=None):
                page.clicks.append(sel)
        loc = L()
        loc.first = loc
        return loc

    def wait_for_timeout(self, ms):
        pass


class TestReadCode(unittest.TestCase):
    def test_charset_masks_letters_that_look_like_digits(self):
        # the model's best guess for the 2nd frame is the letter "o"; with a digit charset it is "0"
        ocr = ProbOcr([[{"4": .9}, {"o": .6, "0": .3}, {"": .9}, {"9": .9}, {"q": .5, "2": .4}]])
        r = tc.read_code(Page(), "img", charset=DIGITS, ocr=ocr)
        self.assertEqual((r["code"], r["decoder"]), ("4092", "ctc"))

    def test_charset_and_length_use_exact_length_decoding(self):
        ocr = ProbOcr([[{"0": .9}, {"0": .55, "": .44}, {"0": .9}, {"5": .9}, {"4": .9}]])
        r = tc.read_code(Page(), "img", charset=DIGITS, length=4, ocr=ocr)
        self.assertEqual(r["code"], "0054")
        self.assertTrue(r["plausible"])

    def test_without_probability_api_falls_back_to_text_filtered_by_charset(self):
        r = tc.read_code(Page(), "img", charset=DIGITS, length=4, ocr=TextOcr(["4a09-2"]))
        self.assertEqual((r["code"], r["decoder"]), ("4092", "text"))

    def test_low_confidence_read_is_refreshed_and_read_again(self):
        weak = [{"1": .9}, {"2": .3, "7": .25}, {"3": .9}, {"4": .9}]
        sure = [{"5": .95}, {"6": .95}, {"7": .95}, {"8": .95}]
        page = Page()
        r = tc.read_code(page, "img", charset=DIGITS, length=4, min_confidence=0.8, ocr=ProbOcr([weak, sure]))
        self.assertEqual((r["code"], r["rereads"]), ("5678", 1))
        self.assertEqual(page.clicks, ["img"])
        self.assertGreaterEqual(r["confidence"], 0.8)

    def test_defaults_unchanged_without_charset(self):
        r = tc.read_code(Page(), "img", ocr=TextOcr(["Ab12"]))
        self.assertEqual((r["code"], r["decoder"]), ("Ab12", "text"))
        self.assertIsNone(r["confidence"])

    def test_preprocess_is_applied_before_ocr(self):
        seen = []
        with mock.patch.object(tc, "preprocess_image", side_effect=lambda png, mode: seen.append(mode) or png):
            tc.read_code(Page(), "img", preprocess="binarize", ocr=TextOcr(["12"]))
        self.assertEqual(seen, ["binarize"])

    def test_unknown_preprocess_is_an_error(self):
        with self.assertRaises(ValueError):
            tc.preprocess_image(b"", "sharpen")


class TestActionArgs(unittest.TestCase):
    def test_new_args_reach_read_code(self):
        from deja_browser import browser as ec
        with mock.patch.object(tc, "read_code", return_value={"code": "1234", "plausible": True}) as rc:
            ec._captcha_text(Page(), {"image": "img", "charset": DIGITS, "length": 4,
                                      "preprocess": "binarize", "min_confidence": 0.8})
        kw = rc.call_args.kwargs
        self.assertEqual((kw["charset"], kw["preprocess"], kw["min_confidence"]), (DIGITS, "binarize", 0.8))

    def test_bad_new_args(self):
        from deja_browser import browser as ec
        for bad in ({"preprocess": "sharpen"}, {"charset": ""}, {"min_confidence": 2}):
            with self.assertRaises(ec.WbError):
                ec._captcha_text(Page(), {"image": "img", **bad})


class TestBinarize(unittest.TestCase):
    def test_background_white_everything_else_black(self):
        try:
            import io
            import numpy  # noqa: F401
            from PIL import Image
        except ImportError:
            self.skipTest("needs Pillow and numpy")
        im = Image.new("RGB", (20, 10), (32, 178, 170))
        im.paste((255, 69, 0), (2, 2, 6, 8))          # a glyph
        im.paste((40, 180, 172), (10, 2, 12, 4))      # background noise within tolerance
        buf = io.BytesIO(); im.save(buf, "PNG")
        out = Image.open(io.BytesIO(tc.preprocess_image(buf.getvalue(), "binarize"))).convert("L")
        self.assertEqual(out.getpixel((3, 3)), 0)
        self.assertEqual(out.getpixel((11, 3)), 255)
        self.assertEqual(out.getpixel((15, 8)), 255)


def _fixture_rows():
    with open(os.path.join(FIX, "labels.csv"), newline="") as f:
        return list(csv.DictReader(f))


class TestSampleAccuracy(unittest.TestCase):
    """Real CAPTCHAs (digits in random colours, rotated and overlapping, crossed by random-colour
    lines; 4-digit and 6-digit kinds), hand-labelled. `set` = tuning (settings were chosen on these)
    or holdout (labelled before the settings were run on them). Thresholds sit a little below the
    measured values; ddddocr itself is deterministic, the margin is for other ddddocr releases."""

    @classmethod
    def setUpClass(cls):
        try:
            import ddddocr  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("needs ddddocr (pip install deja-browser[ocr])")
        cls.ocr = tc._ocr()

    def accuracy(self, prefix, **kw):
        rows = [r for r in _fixture_rows() if r["file"].startswith(prefix)]
        ok = 0
        for r in rows:
            with open(os.path.join(FIX, r["file"]), "rb") as f:
                png = f.read()
            ok += tc.read_png(png, self.ocr, length=len(r["label"]), **kw)["code"] == r["label"]
        return ok / len(rows)

    def test_tuned_settings_beat_plain_reading(self):
        tuned = dict(charset=DIGITS, preprocess="binarize")
        plain4, tuned4 = self.accuracy("d4_"), self.accuracy("d4_", **tuned)
        plain6, tuned6 = self.accuracy("d6_"), self.accuracy("d6_", **tuned)
        self.assertGreater(tuned4, plain4 + 0.2)
        self.assertGreater(tuned6, plain6 + 0.2)
        self.assertGreaterEqual(tuned4, 0.70)   # measured 29/38 = 0.76
        self.assertGreaterEqual(tuned6, 0.90)   # measured 45/47 = 0.96


if __name__ == "__main__":
    unittest.main()
