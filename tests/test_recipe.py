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


def _items(n):
    return [{"title": f"Story {i} " + "x" * 80, "url": f"https://example.org/{i}", "points": i} for i in range(n)]


class RunLimitTest(unittest.TestCase):
    def test_pick_limit_only_lowers(self):
        data = {"hits": [{"t": i} for i in range(10)]}
        spec = {"path": "hits", "limit": 5}
        self.assertEqual(len(sr.pick(data, spec, 2)), 2)
        self.assertEqual(len(sr.pick(data, spec, 99)), 5)       # cannot raise the saved limit
        self.assertEqual(len(sr.pick(data, spec)), 5)
        self.assertEqual(len(sr.pick({"hits": [{"t": i} for i in range(80)]}, {"path": "hits"}, 99)), 50)
        self.assertEqual(len(sr.pick([1, 2, 3, 4], None, 2)), 2)   # a recipe with no pick spec
        self.assertEqual(len(sr.pick(list(range(80)), None, 70)), 70)   # ... is not capped at the default 50

    def test_take_limit(self):
        self.assertEqual(sr._take_limit({"q": "x", "_limit": "3"}), ({"q": "x"}, 3))
        self.assertEqual(sr._take_limit({"q": "x"}), ({"q": "x"}, None))
        self.assertEqual(sr._take_limit({"_limit": 0})[1], 0)
        for bad in ("abc", -1, 2.5, True, "2.5", [1], "-1"):
            with self.assertRaises(sr.RecipeError):
                sr._take_limit({"_limit": bad})

    def _stored(self, d):
        store = Path(d) / "store"
        (store / "example.org").mkdir(parents=True)
        rec = {"name": "t1", "entry_url": "https://example.org/", "request": {"url": "https://example.org/api"},
               "tier": "http", "pick": {"path": "hits", "fields": ["title", "url", "points"], "limit": 20}}
        (store / "example.org" / "t1.json").write_text(json.dumps(rec), encoding="utf-8")
        return store

    def test_run_limit_shrinks_output_to_a_sixth(self):
        payload = {"hits": _items(60)}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(sr, "RECIPES", self._stored(d)), \
             mock.patch.dict(sr.TIERS, {"http": lambda *a: (200, payload)}):
            full = sr.cmd_run("example.org/t1", {})
            small = sr.cmd_run("example.org/t1", {"_limit": 3})
        self.assertEqual((len(full["data"]), len(small["data"])), (20, 3))
        n_full, n_small = (len(json.dumps(x, ensure_ascii=False)) for x in (full, small))
        self.assertLess(n_small * 5, n_full)                       # ~1/6 of the replay output

    def test_limit_is_not_a_template_param_or_signature_input(self):
        rec = {"entry_url": "https://example.org/", "request": {"url": "https://example.org/api"},
               "pick": {"path": "hits", "limit": 20}, "expect": {"signature": [{"title": "str"}]}}
        seen = []
        with mock.patch.dict(sr.TIERS, {"http": lambda r, a, s: (seen.append(a) or 200, {"hits": _items(5)})}):
            self.assertTrue(sr.run(rec, sr._take_limit({"_limit": 1})[0])["ok"])
        self.assertEqual(seen, [{}])


class EnvelopeTest(unittest.TestCase):
    def _run(self, tiers, **kw):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "store"
            (store / "example.org").mkdir(parents=True)
            rec = {"name": "t", "entry_url": "https://example.org/", "request": {"url": "https://example.org/a"},
                   "tier": "http"}
            (store / "example.org" / "t.json").write_text(json.dumps(rec), encoding="utf-8")
            with mock.patch.object(sr, "RECIPES", store), mock.patch.dict(sr.TIERS, tiers):
                return sr.cmd_run("example.org/t", {}, **kw)

    def test_empty_tried_is_dropped_but_a_fallthrough_stays(self):
        ok = self._run({"http": lambda *a: (200, {"a": 1})})
        self.assertEqual(ok, {"ok": True, "tier": "http", "data": {"a": 1}})
        fell = self._run({"http": lambda *a: (403, "no"), "page": lambda *a: (200, {"a": 1})})
        self.assertEqual(fell["tried"], [{"tier": "http", "status": 403}])

    def test_data_only_prints_just_the_data_and_failures_stay_full(self):
        self.assertEqual(self._run({"http": lambda *a: (200, {"a": 1})}, data_only=True), {"a": 1})
        bad = self._run({"http": lambda *a: (500, "x"), "page": lambda *a: (500, "x")}, data_only=True)
        self.assertFalse(bad["ok"])
        self.assertEqual(len(bad["tried"]), 2)

    def test_cli_flags(self):
        out = []
        with mock.patch.object(sr, "cmd_run", return_value={"a": 1}) as m, \
             mock.patch("builtins.print", side_effect=out.append):
            rc = sr.main(["run", "example.org/t", '{"_limit": 2}', "--data-only"])
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_args.args, ("example.org/t", {"_limit": 2}, None, False, True))
        self.assertEqual(json.loads(out[0]), {"a": 1})


class VerifyOutputTest(unittest.TestCase):
    def _verify(self, tiers, page_chars=None, pick=None):
        draft = {"name": "t1", "description": "d", "entry_url": "https://example.org/", "script": "return 1",
                 "request": {"url": "https://example.org/api"}}
        if pick:
            draft["pick"] = pick
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t1.json"
            p.write_text(json.dumps(draft), encoding="utf-8")
            # tiers the test does not give must fail without a browser (the real page/script tiers launch one)
            stub = {t: (lambda *a: (500, "not under test")) for t in sr.TIERS}
            with mock.patch.dict(sr.TIERS, {**stub, **tiers}),                     mock.patch.object(sr, "_page_chars", return_value=page_chars):
                return sr.cmd_verify(str(p), {})

    def test_same_shape_preview_printed_once_and_capped(self):
        payload = {"hits": _items(60)}
        pick = {"path": "hits", "fields": ["title", "url", "points"], "limit": 20}
        same = lambda *a: (200, payload)  # noqa: E731
        out = self._verify({"http": same, "page": same, "script": same}, pick=pick)
        self.assertEqual(out["passed"], ["http", "page", "script"])
        first, second, third = out["results"]
        self.assertTrue(first["preview"]["truncated"])
        self.assertLessEqual(len(first["preview"]["head"]), sr.PREVIEW_OUT)
        self.assertEqual(second["preview"], "same shape as http")
        self.assertEqual(third["preview"], "same shape as http")
        self.assertLess(len(json.dumps(out, ensure_ascii=False)), 3000)      # was ~3 full previews
        self.assertGreater(first["out_chars"], 2500)                         # real size still reported

    def test_a_different_shape_still_gets_its_own_preview(self):
        out = self._verify({"http": lambda *a: (200, {"a": 1}), "page": lambda *a: (200, {"b": "x"}),
                            "script": lambda *a: (500, "no")})
        self.assertEqual(out["results"][0]["preview"], {"a": 1})
        self.assertEqual(out["results"][1]["preview"], {"b": "x"})
        self.assertFalse(out["results"][2]["ok"])

    def test_warns_when_output_is_not_much_smaller_than_the_page(self):
        t5 = {"title": "Bot", "h1": "Bot", "text": "menu " * 60}               # ~330 chars of nav text
        out = self._verify({"http": lambda *a: (200, t5)}, page_chars=500)
        self.assertEqual(out["page_chars"], 500)
        self.assertIn("not much smaller", out["warning"])
        slim = self._verify({"http": lambda *a: (200, {"title": "Bot"})}, page_chars=500)
        self.assertNotIn("warning", slim)
        self.assertEqual(slim["page_chars"], 500)

    def test_output_larger_than_the_page_warns(self):
        out = self._verify({"http": lambda *a: (200, {"a": "x" * 900})}, page_chars=100)
        self.assertIn("warning", out)

    def test_no_page_measure_no_warning(self):
        out = self._verify({"http": lambda *a: (200, {"a": "x" * 5000})}, page_chars=None)
        self.assertNotIn("warning", out)
        self.assertNotIn("page_chars", out)

    def test_page_chars_skips_challenge_pages_and_errors(self):
        page = mock.Mock()
        page.evaluate.return_value = 1234
        rec = {"entry_url": "https://example.org/"}
        with mock.patch.object(sr, "_page_for", return_value=page), mock.patch.object(sr, "challenge", return_value=None):
            self.assertEqual(sr._page_chars(rec, "s"), 1234)
        with mock.patch.object(sr, "_page_for", return_value=page), mock.patch.object(sr, "challenge", return_value="turnstile"):
            self.assertIsNone(sr._page_chars(rec, "s"))
        with mock.patch.object(sr, "_page_for", side_effect=RuntimeError("no browser")):
            self.assertIsNone(sr._page_chars(rec, "s"))
