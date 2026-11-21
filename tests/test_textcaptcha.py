#!/usr/bin/env python3
"""Text-image CAPTCHA helper (no browser, no ddddocr: page and OCR are fakes). run: python -m pytest -q"""
import base64
import unittest
from unittest import mock

from deja_browser import browser as ec
from deja_browser import textcaptcha as tc

PNG = b"\x89PNGfake"


class FakeOcr:
    def __init__(self, reads):
        self.reads = list(reads)

    def classification(self, png):
        assert png == PNG
        return self.reads.pop(0)


class FakeLoc:
    def __init__(self, page, sel):
        self.page, self.sel, self.first = page, sel, self

    def click(self, timeout=None):
        self.page.clicks.append(self.sel)
        if self.sel == self.page.submit:
            self.page.on_submit()

    def count(self):
        return 1 if self.page.form else 0

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def screenshot(self):
        self.page.screenshots += 1
        return PNG


class FakePage:
    """A login form that is reset (all fields emptied) after every rejected submit."""
    def __init__(self, answer, canvas=True, maxlength=0):
        self.answer, self.canvas, self.maxlength = answer, canvas, maxlength
        self.values, self.clicks, self.posted = {}, [], []
        self.form, self.screenshots, self.submit = True, 0, "#go"

    def evaluate(self, js, arg=None, isolated_context=None):
        if js is tc._JS_GRAB:
            if not self.canvas:
                return {"err": "canvas: tainted"}
            return {"b64": base64.b64encode(PNG).decode(), "w": 70, "h": 25, "shown_w": 56, "shown_h": 20}
        if js is tc._JS_MAXLEN:
            return self.maxlength
        return not self.form          # ok_check

    def locator(self, sel):
        return FakeLoc(self, sel)

    def fill(self, sel, val, timeout=None):
        self.values[sel] = val

    def wait_for_timeout(self, ms):
        pass

    def on_submit(self):
        self.posted.append(dict(self.values))
        if self.values.get("#user") == "u" and self.values.get("#cap") == self.answer:
            self.form = False
        else:
            self.values = {}


class TestPure(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(tc.normalize(" B2-rt ", None), "B2rt")
        self.assertEqual(tc.normalize("B2rt", "upper"), "B2RT")
        self.assertEqual(tc.normalize("B2rt", "lower"), "b2rt")
        self.assertEqual(tc.normalize("1a2b3", digits_only=True), "123")

    def test_case_rotation_only_when_case_not_given(self):
        self.assertEqual([tc.case_for_attempt(None, i) for i in range(1, 5)], [None, "upper", "lower", None])
        self.assertEqual([tc.case_for_attempt("upper", i) for i in range(1, 4)], ["upper"] * 3)

    def test_plausible(self):
        self.assertTrue(tc.plausible("AB12", 4))
        self.assertFalse(tc.plausible("AB1", 4))
        self.assertTrue(tc.plausible("AB1", None))
        self.assertFalse(tc.plausible("", None))


class TestSolve(unittest.TestCase):
    def run_solve(self, page, reads, **kw):
        with mock.patch.object(tc, "_ocr", return_value=FakeOcr(reads)):
            return tc.solve(page, "img.cap", "#cap", submit_selector="#go",
                            ok_check="!form", **kw)

    def test_reads_canvas_not_screenshot(self):
        page = FakePage("AB12")
        r = self.run_solve(page, ["AB12"], fields={"#user": "u"})
        self.assertTrue(r["solved"])
        self.assertEqual(r["last"]["source"], "canvas")
        self.assertEqual(page.screenshots, 0)

    def test_screenshot_only_as_fallback(self):
        page = FakePage("AB12", canvas=False)
        r = self.run_solve(page, ["AB12"], fields={"#user": "u"})
        self.assertEqual(r["last"]["source"], "screenshot")
        self.assertEqual(page.screenshots, 1)

    def test_fields_refilled_after_a_reset_and_case_rotates(self):
        page = FakePage("FQ18")                       # site wants upper case, OCR reads mixed case
        r = self.run_solve(page, ["Fq18", "Fq18"], fields={"#user": "u"})
        self.assertEqual((r["solved"], r["attempts"]), (True, 2))
        self.assertEqual([p.get("#user") for p in page.posted], ["u", "u"])   # never posted blank
        self.assertEqual([p["#cap"] for p in page.posted], ["Fq18", "FQ18"])

    def test_wrong_length_is_reread_before_submitting(self):
        page = FakePage("WQ2M", maxlength=4)
        r = self.run_solve(page, ["wq2", "WQ2M"], fields={"#user": "u"}, case="upper")
        self.assertEqual((r["solved"], r["attempts"]), (True, 1))
        self.assertEqual(len(page.posted), 1)          # the 3-character read was never sent
        self.assertEqual(r["last"]["rereads"], 1)
        self.assertIn("img.cap", page.clicks)          # refreshed the image to re-read

    def test_gives_up_after_tries(self):
        page = FakePage("ZZZZ")
        r = self.run_solve(page, ["AAAA"] * 3, fields={"#user": "u"}, tries=3)
        self.assertEqual((r["solved"], r["attempts"]), (False, 3))


class TestAction(unittest.TestCase):
    def test_action_fills_and_hides_field_values(self):
        page = FakePage("AB12")
        with mock.patch.object(tc, "_ocr", return_value=FakeOcr(["AB12"])):
            r = ec._captcha_text(page, {"image": "img.cap", "input": "#cap", "submit": "#go", "ok": "!form",
                                        "fields": {"#user": "u"}})
        self.assertTrue(r["success"])
        self.assertNotIn("#user", repr(r))

    def test_read_only_mode(self):
        page = FakePage("AB12")
        with mock.patch.object(tc, "_ocr", return_value=FakeOcr(["ab12"])):
            r = ec._captcha_text(page, {"image": "img.cap", "input": "#cap", "case": "upper"})
        self.assertEqual((r["code"], r["filled"]), ("AB12", True))
        self.assertEqual(page.values["#cap"], "AB12")

    def test_bad_args(self):
        with self.assertRaises(ec.WbError):
            ec._captcha_text(FakePage("x"), {})
        with self.assertRaises(ec.WbError):
            ec._captcha_text(FakePage("x"), {"image": "i", "case": "title"})
        with self.assertRaises(ec.WbError):
            ec._captcha_text(FakePage("x"), {"image": "i", "submit": "#go"})


if __name__ == "__main__":
    unittest.main()
