#!/usr/bin/env python3
"""close_session's auto-learn (learn.auto_plan / auto_expects / auto_name, recipe.auto_learn, the CLI hook).
No browser, no network: recordings built by hand, the daemon client and the tier runs mocked.
run: python -m pytest -q"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from deja_browser import browser as B
from deja_browser import client, learn, recipe as sr

from test_learn import TITLES, _Rec, _hits


class AutoNameTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(learn.auto_name("booking-1029"), "booking")
        self.assertEqual(learn.auto_name("oe q1"), "oe-q1")
        self.assertEqual(learn.auto_name("crates-v-2026-11-22"), "crates-v")
        self.assertEqual(learn.auto_name("Hn_Search"), "hn_search")
        self.assertEqual(learn.auto_name("123"), "auto")
        self.assertEqual(learn.auto_name("台鐵"), "auto")
        self.assertEqual(learn.auto_name(""), "auto")


class AutoExpectsTest(unittest.TestCase):
    def test_distinctive_values_first_typed_values_excluded(self):
        c = {"result": {"name": "serde", "version": "1.0.228", "downloads": 712345678,
                        "description": "A generic serialization/deserialization framework"}}
        self.assertEqual(learn.auto_expects(c, ["serde"]), ["1.0.228", "712345678"])

    def test_json_string_result_and_long_values_cut(self):
        c = {"result": json.dumps([{"title": TITLES[0]}, {"title": TITLES[1]}])}
        out = learn.auto_expects(c)
        self.assertEqual(out, [TITLES[1][:40].rstrip(), TITLES[0][:40].rstrip()])
        self.assertTrue(all(len(e) <= learn.AUTO_MAX_EXPECT_LEN for e in out))

    def test_read_text_lines_and_preview(self):
        c = {"result": "Results\nAlbert Einstein\n“The world as we have created it”\n10 quotes"}
        self.assertEqual(learn.auto_expects(c, ["einstein"])[0], "10 quotes")
        c = {"result_preview": "\"Version 2.4.1 released\"", "result_chars": 9000}
        self.assertEqual(learn.auto_expects(c), ['"Version 2.4.1 released"'])

    def test_nothing_distinctive(self):
        self.assertEqual(learn.auto_expects({"result": {"a": True, "b": 3.5, "c": "ok", "d": None}}), [])
        self.assertEqual(learn.auto_expects({"result": "rust"}, ["https://app.test/?q=rust"]), [])

    def test_no_value_inside_another(self):
        self.assertEqual(learn.auto_expects({"result": ["2026-11-22", "2026-11-22 10:00"]}), ["2026-11-22"])


class AutoPlanTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.bodies = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def visit(self):
        r = _Rec(self.bodies)
        r.cmd("navigate", {"url": "https://app.test/?q=rust"}, url_after="https://app.test/?q=rust")
        r.resp("https://api.app.test/search?query=rust", _hits())
        r.cmd("evaluate", {"code": "() => [...document.querySelectorAll('li')].map(x => x.textContent)"},
              result=TITLES, url_before="https://app.test/?q=rust", url_after="https://app.test/?q=rust")
        return r

    def test_plan(self):
        plan = learn.auto_plan(self.visit().data(), "hn-search-3")
        self.assertEqual(plan["name"], "hn-search")
        self.assertEqual(plan["host"], "app.test")
        self.assertEqual(len(plan["expects"]), 2)

    def test_skips(self):
        self.assertIn("recipe", learn.auto_plan(self.visit().data(), "recipe-app.test")["skip"])
        r = self.visit()
        r.resp("https://app.test/cart/add", {"ok": 1}, method="POST", post="id=3")
        self.assertIn("may change something", learn.auto_plan(r.data(), "s")["skip"])
        r = self.visit()
        r.resp("https://app.test/api/search", {"hits": []}, method="POST", post='{"q":"x"}')
        self.assertNotIn("skip", learn.auto_plan(r.data(), "s"))          # a POST that reads is fine
        r = self.visit()
        r.cmd("fill", {"selector": "#pw", "value": "<redacted>", "redacted": True})
        self.assertIn("password", learn.auto_plan(r.data(), "s")["skip"])
        r = self.visit()
        r.cmd("handoff", {"message": "2FA"})
        self.assertIn("handoff", learn.auto_plan(r.data(), "s")["skip"])
        r = _Rec(self.bodies)
        r.cmd("navigate", {"url": "https://app.test/"}, url_after="https://app.test/")
        r.cmd("screenshot", {"path": "x.png"})
        self.assertIn("no read / evaluate", learn.auto_plan(r.data(), "s")["skip"])


class AutoLearnTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.store = Path(self._t.name) / "store"
        self.bodies = Path(self._t.name) / "bodies"
        self.bodies.mkdir()

    def tearDown(self):
        self._t.cleanup()

    def rec(self):
        r = _Rec(self.bodies)
        r.cmd("navigate", {"url": "https://app.test/?q=rust"}, url_after="https://app.test/?q=rust")
        r.resp("https://api.app.test/search?query=rust", _hits())
        r.cmd("evaluate", {"code": "() => 1"}, result=[TITLES[0]], url_before="https://app.test/?q=rust",
              url_after="https://app.test/?q=rust")
        return r.data()

    def run_auto(self, rec, session="hn-1", env=None, cfg=None, tiers_ok=True):
        def verify(d, given, expects=()):
            if not tiers_ok:
                return [{"tier": "http", "ok": False, "tried": [{"tier": "http", "status": 403}]}], [], {}
            return [{"tier": "http", "ok": True, "out_chars": 99}], ["http"], {"http": [{"title": "str"}]}
        environ = {"DEJA_BROWSER_DAEMON": "1", **(env or {})}
        with mock.patch.dict("os.environ", environ), \
                mock.patch.object(client, "call_running", return_value={"ok": True, "data": rec}) as cr, \
                mock.patch.object(sr, "_verify_tiers", side_effect=verify), \
                mock.patch.object(sr, "RECIPES", self.store), \
                mock.patch.object(sr.config, "get", side_effect=lambda s, k, d=None: (cfg or {}).get(k, d)):
            if "DEJA_BROWSER_AUTO_LEARN" not in environ:
                import os
                os.environ.pop("DEJA_BROWSER_AUTO_LEARN", None)
            return sr.auto_learn(session), cr

    def test_learns_and_saves(self):
        out, _ = self.run_auto(self.rec())
        self.assertEqual(out, {"ok": True, "id": "app.test/hn", "tier": "http", "kind": "api", "params": ["q"]})
        self.assertTrue((self.store / "app.test" / "hn.json").exists())

    def test_host_with_recipes_is_skipped(self):
        (self.store / "app.test").mkdir(parents=True)
        (self.store / "app.test" / "search.json").write_text("{}", encoding="utf-8")
        out, _ = self.run_auto(self.rec())
        self.assertFalse(out["ok"])
        self.assertIn("already has recipes (search)", out["skipped"])

    def test_failed_verify_saves_nothing(self):
        out, _ = self.run_auto(self.rec(), tiers_ok=False)
        self.assertFalse(out["ok"])
        self.assertIn("403", out["reason"])
        self.assertFalse(self.store.exists())

    def test_off_switches(self):
        out, cr = self.run_auto(self.rec(), cfg={"auto_learn": False})
        self.assertIsNone(out)
        cr.assert_not_called()
        out, cr = self.run_auto(self.rec(), env={"DEJA_BROWSER_AUTO_LEARN": "0"})
        self.assertIsNone(out)
        out, cr = self.run_auto(self.rec(), env={"DEJA_BROWSER_DAEMON": "0"})
        self.assertIsNone(out)
        out, cr = self.run_auto(self.rec(), session="learn-app.test")
        self.assertIsNone(out)
        out, cr = self.run_auto({"commands": [], "responses": []})
        self.assertIsNone(out)


class AutoReadTest(unittest.TestCase):
    def test_writes_and_reads(self):
        r = lambda url, body=None, m="POST": learn.auto_read({"method": m, "url": url, "body": body})
        self.assertTrue(r("https://x.test/a", m="GET"))
        self.assertTrue(r("https://quotes.toscrape.com/filter.aspx"))
        self.assertTrue(r("https://x-dsn.algolia.net/1/indexes/*/queries"))
        self.assertTrue(r("https://x.test/graphql", {"query": "{ items { id } }"}))
        self.assertTrue(r("https://x.test/api/searchItems"))
        self.assertFalse(r("https://x.test/wishlist/add"))
        self.assertFalse(r("https://x.test/checklist/complete"))
        self.assertFalse(r("https://x.test/api/notes", {"query": "free text"}))
        self.assertFalse(r("https://x.test/graphql", {"query": "mutation { like(id: 1) }"}))
        self.assertFalse(r("https://x.test/search/save"))
        self.assertFalse(r("https://x.test/api/addToCart"))
        self.assertFalse(r("https://x.test/post"))
        self.assertFalse(r("https://x.com/i/api/graphql/abc123/FavoriteTweet", {"variables": {"id": "1"}}))
        self.assertFalse(r("https://x.test/graphql", "{\"query\": \"query { a"))      # cut off, unparsed

    def test_dropped_entries_skip(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Rec(d)
            r.cmd("navigate", {"url": "https://app.test/"}, url_after="https://app.test/")
            r.cmd("evaluate", {"code": "() => 1"}, result="Item 2026-11-22", url_after="https://app.test/")
            data = r.data()
            self.assertNotIn("skip", learn.auto_plan(data, "s"))
            data["dropped"] = {"responses": 3, "commands": 0}
            self.assertIn("too long", learn.auto_plan(data, "s")["skip"])

    def test_plan_uses_it(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Rec(d)
            r.cmd("navigate", {"url": "https://app.test/"}, url_after="https://app.test/")
            r.resp("https://app.test/wishlist/add", {"ok": 1}, method="POST", post="id=3")
            r.cmd("evaluate", {"code": "() => 1"}, result="Saved 2026-11-22", url_after="https://app.test/")
            self.assertIn("may change something", learn.auto_plan(r.data(), "s")["skip"])


class AutoFinishTest(unittest.TestCase):
    def test_human_wait_zero_during_learn_and_restored(self):
        seen = {}

        def fake_learn(argv, recording=None):
            seen["wait"] = sr.HUMAN_WAIT
            seen["rec"] = recording
            return {"ok": True, "id": "app.test/x", "tier": "page", "kind": "script", "params": {}}, 0
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sr, "cmd_learn", side_effect=fake_learn), \
                mock.patch.object(B, "version", return_value=None):
            out = sr.auto_learn_finish({"session": "s", "plan": {"name": "x", "expects": ["abc"], "host": "app.test"},
                                        "rec": {"commands": [1]}, "tmp": Path(d) / "t"})
        self.assertEqual(seen, {"wait": 0, "rec": {"commands": [1]}})
        self.assertIsNone(sr.HUMAN_WAIT)
        self.assertTrue(out["ok"])

    def test_skipped_visit_writes_no_bodies(self):
        calls = []

        def cr(action, args, session=None):
            calls.append(args)
            return {"ok": True, "data": {"commands": [{"n": 1, "action": "navigate", "args": {}, "ok": True}]}}
        with mock.patch.dict("os.environ", {"DEJA_BROWSER_DAEMON": "1", "DEJA_BROWSER_AUTO_LEARN": "1"}), \
                mock.patch.object(client, "call_running", side_effect=cr):
            out = sr.auto_learn_prepare("s")
        self.assertIn("no read / evaluate", out["result"]["skipped"])
        self.assertEqual(calls, [{}])


class CliHookTest(unittest.TestCase):
    def call(self, args, prepare, finish=None):
        order = []

        def via(cmd, a, session, extra=None):
            order.append(("close", cmd, a))
            if callable(extra):
                extra = extra()
            print(json.dumps({"success": True, "closed": 1, **(extra or {})}))
            return 0

        def prep(s):
            order.append(("prepare", s))
            return prepare(s)

        def fin(p):
            order.append(("finish",))
            return finish(p)
        buf = io.StringIO()
        with mock.patch.object(B, "_main_via_daemon", side_effect=via),                 mock.patch.object(sr, "auto_learn_prepare", side_effect=prep),                 mock.patch.object(sr, "auto_learn_finish", side_effect=fin), redirect_stdout(buf):
            rc = B.main(["close_session", json.dumps(args), "--session", "task-1"])
        return rc, json.loads(buf.getvalue()), order

    def test_recording_before_close_learning_after(self):
        rc, out, order = self.call({}, lambda s: {"plan": 1}, lambda p: {"ok": True, "id": "app.test/task"})
        self.assertEqual((rc, out["learned"]["id"]), (0, "app.test/task"))
        self.assertEqual([o[0] for o in order], ["prepare", "close", "finish"])

    def test_learn_false_skips_and_is_not_sent(self):
        rc, out, order = self.call({"learn": False}, lambda s: {"plan": 1}, lambda p: {"ok": True})
        self.assertEqual(order, [("close", "close_session", {})])
        self.assertNotIn("learned", out)

    def test_nothing_to_say(self):
        rc, out, order = self.call({}, lambda s: None)
        self.assertNotIn("learned", out)
        self.assertEqual([o[0] for o in order], ["prepare", "close"])

    def test_errors_never_stop_the_close(self):
        def boom(*a):
            raise RuntimeError("bad")
        rc, out, order = self.call({}, boom)
        self.assertEqual(rc, 0)
        self.assertIn("RuntimeError", out["learned"]["error"])
        rc, out, order = self.call({}, lambda s: {"plan": 1}, boom)
        self.assertEqual(rc, 0)
        self.assertIn("RuntimeError", out["learned"]["error"])
        self.assertEqual([o[0] for o in order], ["prepare", "close", "finish"])


if __name__ == "__main__":
    unittest.main()
