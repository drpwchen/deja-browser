#!/usr/bin/env python3
"""Pure-function tests for recipe.py (no browser, no network, no secrets).
run: python -m pytest -q"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import recipe as sr


class SignatureTest(unittest.TestCase):
    def test_extra_keys_ok_missing_key_stale(self):
        want = sr.signature([{"title": "a", "points": 1}])
        self.assertTrue(sr.sig_ok(want, sr.signature([{"title": "b", "points": 2, "new": True}])))
        self.assertFalse(sr.sig_ok(want, sr.signature([{"title": "b"}])))

    def test_null_and_empty_are_lenient(self):
        want = sr.signature([{"url": "x"}])
        self.assertTrue(sr.sig_ok(want, sr.signature([{"url": None}])))
        self.assertTrue(sr.sig_ok(want, sr.signature([])))

    def test_type_change_is_stale(self):
        self.assertFalse(sr.sig_ok(sr.signature({"n": 1}), sr.signature({"n": "1"})))
        self.assertFalse(sr.sig_ok(sr.signature([{"a": 1}]), sr.signature({"a": 1})))


class PickRenderTest(unittest.TestCase):
    def test_pick_path_fields_limit(self):
        data = {"hits": [{"t": i, "x": 0} for i in range(5)]}
        self.assertEqual(sr.pick(data, {"path": "hits", "fields": ["t"], "limit": 2}), [{"t": 0}, {"t": 1}])
        self.assertIs(sr.pick(data, None), data)

    def test_render_params_and_json_braces(self):
        body = '{"query":"{q}","n":{n}}'
        self.assertEqual(sr._render(body, {"q": "a b", "n": "3"}), '{"query":"a b","n":3}')
        self.assertEqual(sr._render({"u": "/s?{q}"}, {}), {"u": "/s?{q}"})   # unknown param left alone

    def test_render_secret_resolved_in_process(self):
        with mock.patch.object(sr, "_secret", return_value="S3CRET") as m:
            self.assertEqual(sr._render({"h": "Bearer {secret:TOK}"}, {}), {"h": "Bearer S3CRET"})
            m.assert_called_once_with("TOK")
        with self.assertRaises(sr.RecipeError):
            sr._render("{secret:TOK}", {}, secrets_ok=False)

    def test_args_required_and_default(self):
        rec = {"params": [{"name": "q", "required": True}, {"name": "n", "default": "20"}]}
        self.assertEqual(sr._args(rec, {"q": "x"}), {"q": "x", "n": "20"})
        with self.assertRaises(sr.RecipeError):
            sr._args(rec, {})


class OnScreenTest(unittest.TestCase):
    def test_parked_hcaptcha_challenge_frame_is_not_a_widget(self):
        self.assertFalse(sr._on_screen({"x": 1, "y": -9999, "width": 300, "height": 150}))
        self.assertTrue(sr._on_screen({"x": 33, "y": 265, "width": 302, "height": 76}))
        self.assertFalse(sr._on_screen({"x": 0, "y": 0, "width": 10, "height": 10}))
        self.assertFalse(sr._on_screen(None))


class GuardTest(unittest.TestCase):
    def test_deny_hosts(self):
        with self.assertRaises(sr.RecipeError):
            sr._check_host("https://mail.google.com/x")
        with self.assertRaises(sr.RecipeError):
            sr._check_host("https://www.esunbank.com.tw/")
        for url in ("https://tixcraft.com/activity", "https://www.ticketmaster.com/",
                    "https://tip.railway.gov.tw/tra-tip-web/tip", "https://irs.thsrc.com.tw/IMINT/"):
            with self.assertRaisesRegex(sr.RecipeError, "ticketing"):
                sr._check_host(url)
        sr._check_host("https://hn.algolia.com/")
        sr._check_host("https://pubmed.ncbi.nlm.nih.gov/")

    def test_ticket_hosts_opt_out(self):
        self.assertIn("railway.gov.tw", sr._ticket_hosts([]))
        some = sr._ticket_hosts(["railway.gov.tw"])
        self.assertNotIn("railway.gov.tw", some)
        self.assertIn("tixcraft.", some)
        self.assertEqual(sr._ticket_hosts(["*"]), ())


class HcaptchaSolverTest(unittest.TestCase):
    def test_off_by_default(self):
        from deja_browser import hcaptcha
        from unittest import mock
        with mock.patch.object(hcaptcha.config, "get", side_effect=lambda s, k, d=None: d):
            self.assertFalse(hcaptcha.enabled())
            self.assertEqual(hcaptcha.solve(object()), {"ran": False, "error": "disabled"})

    def test_opt_in_without_package_does_not_click(self):
        from deja_browser import hcaptcha
        from unittest import mock
        cfg = {("captcha", "hcaptcha_solver"): "gemini"}
        with mock.patch.object(hcaptcha.config, "get", side_effect=lambda s, k, d=None: cfg.get((s, k), d)), \
             mock.patch.object(hcaptcha, "available", return_value=False):
            self.assertTrue(hcaptcha.enabled())
            r = hcaptcha.solve(object())
            self.assertFalse(r["ran"])

    def test_writes_need_flag(self):
        rec = {"entry_url": "https://example.org/", "writes": True, "request": {"url": "https://example.org/a"}}
        with self.assertRaises(sr.RecipeError):
            sr.run(rec, {})

    def test_ladder(self):
        self.assertEqual(sr._ladder({"request": {}, "script": "x", "tier": "page"}), ["page", "http", "script"])
        self.assertEqual(sr._ladder({"script": "x"}), ["script"])
        self.assertEqual(sr._ladder({"script": "x"}, "http"), [])


class RunFallthroughTest(unittest.TestCase):
    def test_stale_shape_falls_through_and_never_returns_bad_data(self):
        rec = {"entry_url": "https://example.org/", "request": {"url": "https://example.org/api"},
               "pick": {"path": "items", "fields": ["id"]}, "expect": {"signature": [{"id": "num"}]}}
        with mock.patch.dict(sr.TIERS, {"http": lambda *a: (200, {"other": 1}),
                                        "page": lambda *a: (200, {"items": [{"id": 7}]})}):
            r = sr.run(rec, {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["tier"], "page")
        self.assertIn("stale", r["tried"][0]["error"])

    def test_all_fail(self):
        rec = {"entry_url": "https://example.org/", "request": {"url": "https://example.org/api"}}
        with mock.patch.dict(sr.TIERS, {"http": lambda *a: (403, "no"),
                                        "page": lambda *a: (0, {"_challenge": {"cleared": False}})}):
            r = sr.run(rec, {})
        self.assertFalse(r["ok"])
        self.assertEqual([t["tier"] for t in r["tried"]], ["http", "page"])


class SaveGateTest(unittest.TestCase):
    def test_save_requires_matching_fresh_verify(self):
        with tempfile.TemporaryDirectory() as d:
            draft = Path(d) / "x.json"
            rec = {"name": "x", "description": "d", "entry_url": "https://example.org/",
                   "request": {"url": "https://example.org/api"}}
            draft.write_text(json.dumps(rec), encoding="utf-8")
            with self.assertRaises(sr.RecipeError):
                sr.cmd_save(str(draft))
            side = draft.with_suffix(".verified.json")
            side.write_text(json.dumps({"at": time.time(), "hash": sr._draft_hash(rec), "passed": ["http"],
                                        "signature": {"a": "num"}, "args": {}}), encoding="utf-8")
            with mock.patch.object(sr, "RECIPES", Path(d) / "store"):
                out = sr.cmd_save(str(draft))
                self.assertEqual(out["saved"], "example.org/x")
                saved = json.loads((Path(d) / "store" / "example.org" / "x.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["expect"]["signature"], {"a": "num"})
            rec["request"]["url"] = "https://example.org/changed"
            draft.write_text(json.dumps(rec), encoding="utf-8")
            with self.assertRaises(sr.RecipeError):
                sr.cmd_save(str(draft))


if __name__ == "__main__":
    unittest.main()


class LateWidgetTest(unittest.TestCase):
    """navigate() returns at DOMContentLoaded; the widget iframe arrives ~1 s later."""
    def _page(self, pending):
        page = mock.Mock()
        page.evaluate.return_value = pending
        return page

    def test_late_widget_is_waited_for(self):
        page = self._page(True)
        with mock.patch.object(sr, "challenge", side_effect=[None, None, "turnstile", None]), \
             mock.patch.object(sr, "_wake_widgets"), \
             mock.patch.object(sr, "_wait_clear", return_value=True):
            g = sr.challenge_gate(page, human_wait=0)
        self.assertEqual(g, {"challenge": "turnstile", "cleared": True, "how": "auto"})

    def test_no_widget_returns_at_once(self):
        page = self._page(False)
        with mock.patch.object(sr, "challenge", return_value=None), \
             mock.patch.object(sr, "_wait_widget") as wait:
            g = sr.challenge_gate(page, human_wait=0)
        wait.assert_not_called()
        self.assertEqual(g["how"], "none")
