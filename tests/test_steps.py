#!/usr/bin/env python3
"""v0.4 batch E: `steps` recipes (recipe.py), learn path 3 (learn.plan_steps), `handoff` args, and the recording
additions they need. No browser: browser.wb and friends are mocked.
run: python -m pytest -q"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import browser as B
from deja_browser import client, daemon, learn, recipe as sr, recording as R

FH = "#work >> internal:control=enter-frame >> "
HIS = "http://his.test/his/main.html"


def _steps():
    return [{"action": "navigate", "args": {"url": HIS + "?u={user|url}"}},
            {"action": "select", "args": {"selector": FH + "#dept", "value": "{dept}"},
             "expect": {"selector": FH + "#doc", "state": "attached", "timeout": 5000}},
            {"action": "fill", "args": {"selector": FH + "#pid", "value": "{pid}"}},
            {"action": "click", "args": {"selector": FH + "#go"}, "dialog": "accept",
             "expect": {"url": "/his/result.aspx"}},
            {"action": "evaluate", "args": {"code": "(args) => `${args.pid}:` + document.title"}}]


def _recipe(**kw):
    d = {"name": "q", "description": "d", "entry_url": HIS, "steps": _steps(),
         "params": [{"name": "user", "default": "a b"}, {"name": "dept", "required": True},
                    {"name": "pid", "required": True}]}
    d.update(kw)
    return d


# ---------- check_steps ----------

class CheckStepsTest(unittest.TestCase):
    def bad(self, steps, needle):
        with self.assertRaises(sr.RecipeError) as cm:
            sr.check_steps(steps)
        self.assertIn(needle, str(cm.exception))

    def test_valid(self):
        self.assertEqual(sr.check_steps(_steps()), _steps())

    def test_refs_are_refused_everywhere(self):
        for k, v in (("selector", "@e5"), ("selector", "e5"), ("frame", "@e2")):
            s = _steps()
            s[2]["args"][k] = v
            self.bad(s, "is a ref")
        s = _steps()
        s[1]["expect"] = {"selector": "@e9"}
        self.bad(s, "is a ref")

    def test_secrets_are_refused(self):
        s = _steps()
        s[2]["args"]["value"] = "{secret:HIS_PASSWORD}"
        self.bad(s, "{secret:")

    def test_shape(self):
        self.bad([], "non-empty")
        self.bad([{"action": "handoff", "args": {}}], "action must be one of")
        self.bad([{"action": "snapshot"}, {"action": "read"}], "snapshot only as the last step")
        self.bad(_steps()[:-1], "the last step must be one of")
        s = _steps()
        s[0]["args"]["url"] = "https://{host}/x"
        self.bad(s, "placeholder may not be in the host")
        s = _steps()
        s[0]["args"]["url"] = "{url}"
        self.bad(s, "needs an http(s) url")
        s = _steps()
        s[1]["expect"] = {"selector": "#a", "text": "b"}
        self.bad(s, "exactly one of")
        s = _steps()
        s[3]["dialog"] = 3
        self.bad(s, "dialog")
        s = _steps()
        s[1]["oops"] = 1
        self.bad(s, "unknown key")

    def test_check_draft_and_save_use_it(self):
        d = _recipe()
        d["steps"][2]["args"]["selector"] = "@e5"
        with self.assertRaises(sr.RecipeError):
            sr._check_draft(d)
        d = _recipe()
        d["steps"][0]["args"]["url"] = "https://mail.google.com/x"
        with self.assertRaises(sr.RecipeError):
            sr._check_draft(d)
        self.assertNotEqual(sr._draft_hash(_recipe()), sr._draft_hash(_recipe(steps=_steps()[2:])))


class RenderAndLadderTest(unittest.TestCase):
    def test_params_fill_strings_not_code(self):
        s = _steps()
        out = [sr._render_step(x["args"], {"user": "a b", "dept": "PMR", "pid": "A1"}) for x in s]
        self.assertEqual(out[0]["url"], HIS + "?u=a%20b")
        self.assertEqual(out[1]["value"], "PMR")
        self.assertEqual(out[4]["code"], s[4]["args"]["code"])        # ${args.pid} untouched
        e = sr._render_step({"js": "x => '{pid}'", "text": "{pid}"}, {"pid": "A1"})
        self.assertEqual(e, {"js": "x => '{pid}'", "text": "A1"})

    def test_ladder(self):
        self.assertEqual(sr._ladder(_recipe()), ["steps"])
        self.assertEqual(sr._ladder(_recipe(), "steps"), ["steps"])
        self.assertEqual(sr._ladder(_recipe(), "http"), [])
        self.assertEqual(sr._ladder({"script": "1", "steps": _steps()}), ["script", "steps"])
        self.assertEqual(sr._ladder({"request": {"url": "x"}}), ["http", "page"])


# ---------- tier_steps / run (browser mocked) ----------

class _Watch:
    def __init__(self, page):
        self.settled = None

    def settle(self, ms):
        self.settled = ms

    def close(self):
        pass


class TierStepsTest(unittest.TestCase):
    def go(self, recipe, given, results=None, **kw):
        calls = []
        results = results or {}

        def wb(action, args=None, session="default"):
            calls.append((action, dict(args or {}), session))
            r = results.get((action, len([c for c in calls if c[0] == action]) - 1))
            if isinstance(r, Exception):
                raise r
            if r is not None:
                return r
            if action == "evaluate":
                return {"type": "string", "value": f"{args['arg']['pid']}:title"}
            if action == "wait":
                return {"success": True, "waited_ms": 3}
            return {"success": True}
        with mock.patch.object(B, "wb", side_effect=wb), mock.patch.object(B, "_ctx"), \
                mock.patch.object(B, "_current"), mock.patch.object(B, "_NavWatch", _Watch), \
                mock.patch.object(sr, "challenge_gate", return_value={"cleared": True}), \
                mock.patch.object(B, "current_driver", return_value="patchright"):
            return sr.run(recipe, given, **kw), calls

    def test_runs_in_order_with_params_dialog_and_expects(self):
        res, calls = self.go(_recipe(), {"dept": "IM", "pid": "B4"})
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["tier"], res["data"]), ("steps", "B4:title"))
        acts = [(a, x.get("selector") or x.get("url") or x.get("code", "")[:8]) for a, x, _ in calls]
        self.assertEqual([a for a, _ in acts], ["navigate", "select", "wait", "fill", "click", "wait", "evaluate"])
        self.assertEqual(calls[0][1]["url"], HIS + "?u=a%20b")               # default param, url-encoded
        self.assertEqual(calls[1][1]["value"], "IM")
        self.assertEqual(calls[2][1], {"selector": FH + "#doc", "state": "attached", "timeout": 5000})
        self.assertEqual(calls[4][1]["dialog"], "accept")
        self.assertEqual(calls[5][1], {"url": "/his/result.aspx", "timeout": sr.STEP_EXPECT_MS})
        self.assertEqual(calls[6][1]["arg"], {"user": "a b", "dept": "IM", "pid": "B4"})
        self.assertTrue(all(s == "recipe-his.test" for _, _, s in calls))

    def test_expect_not_met_is_stale_with_step(self):
        res, calls = self.go(_recipe(), {"dept": "IM", "pid": "B4"},
                             {("click", 0): {"success": True, "dialog": {"type": "confirm", "message": "sure?",
                                                                         "handled": "dismissed"}},
                              ("wait", 1): {"success": False, "waited_ms": 15000}})
        self.assertFalse(res["ok"])
        t = res["tried"][0]
        self.assertEqual((t["tier"], t["step"], t["action"], t["stale"]), ("steps", 3, "click", True))
        self.assertTrue(t["error"].startswith("step 3 (click): expect url"), t["error"])
        self.assertIn("confirm 'sure?' dismissed", t["error"])               # the usual cause, said
        self.assertNotIn("evaluate", [a for a, _, _ in calls])

    def test_failing_action_and_wait_step(self):
        res, _ = self.go(_recipe(), {"dept": "IM", "pid": "B4"},
                         {("fill", 0): B.WbError("fill: no element matches '#pid'")})
        self.assertEqual(res["tried"][0]["step"], 2)
        self.assertIn("step 2 (fill): fill: no element", res["tried"][0]["error"])
        steps = _steps()
        steps.insert(3, {"action": "wait", "args": {"text": "ready", "timeout": 10}})
        res, _ = self.go(_recipe(steps=steps), {"dept": "IM", "pid": "B4"},
                         {("wait", 1): {"success": False, "waited_ms": 10}})
        self.assertEqual((res["tried"][0]["step"], res["tried"][0]["action"]), (3, "wait"))

    def test_read_output_and_writes(self):
        steps = _steps()[:4] + [{"action": "read", "args": {"selector": "#r"}}]
        res, _ = self.go(_recipe(steps=steps), {"dept": "IM", "pid": "B4"},
                         {("read", 0): {"text": "RESULT B4", "url": HIS}})
        self.assertEqual(res["data"], "RESULT B4")
        with self.assertRaises(sr.RecipeError):
            self.go(_recipe(writes=True), {"dept": "IM", "pid": "B4"})
        res, _ = self.go(_recipe(writes=True), {"dept": "IM", "pid": "B4"}, allow_write=True)
        self.assertTrue(res["ok"])

    def test_signature_check_applies(self):
        rec = _recipe(expect={"signature": {"rows": ["str"]}})
        res, _ = self.go(rec, {"dept": "IM", "pid": "B4"})
        self.assertFalse(res["ok"])
        self.assertIn("shape changed", res["tried"][0]["error"])


# ---------- learn path 3 ----------

def _cmd(n, action, args, ok=True, before=None, after=None, result=None, at=None):
    c = {"n": n, "at": at or 1000.0 + n, "action": action, "args": args, "ok": ok, "url_before": before,
         "url_after": after}
    if result is not None:
        c["result"] = result
    return c


RESULT = "查詢結果：病歷號 A123　復健科 李醫師　下次回診 2026-12-20"
SAVE = "http://his.test/his/save.aspx"


def _flow(extract=None, save=False):
    """The /his/ visit as recording.py would hold it: refs used and resolved, noise in between."""
    role = FH + 'internal:role=combobox[name="科別"s] >> nth=0'
    pid = FH + 'internal:role=textbox[name="病歷號"s] >> nth=0'
    btn = FH + ('#btnSave' if save else 'internal:role=button[name="查詢"s] >> nth=0')
    c = [_cmd(1, "navigate", {"url": HIS}, after=HIS, result={"status": 200}),
         _cmd(2, "snapshot", {"refs": True}, before=HIS, after=HIS),
         _cmd(3, "select", {"selector": "@e2", "label": "復健科", "selector_resolved": role}, before=HIS, after=HIS,
              result={"selected": {"value": "PMR", "label": "復健科"}}),
         _cmd(4, "click", {"selector": "#nope"}, ok=False, before=HIS, after=HIS),
         _cmd(5, "select", {"selector": FH + "#doc", "label": "李醫師"}, before=HIS, after=HIS,
              result={"selected": {"value": "d2", "label": "李醫師"}}),
         _cmd(6, "fill", {"selector": "@e8", "value": "A123", "selector_resolved": pid}, before=HIS, after=HIS),
         _cmd(7, "click", {"selector": "@e9" if not save else btn, "dialog": "accept",
                           **({} if save else {"selector_resolved": btn})}, before=HIS, after=HIS),
         _cmd(8, "wait", {"text": "x"}, before=HIS, after=SAVE if save else HIS, result={"success": True})]
    c.append(extract or _cmd(9, "read", {"frame": "#work"} if not save else {"selector": "#saved"},
                             before=SAVE if save else HIS, after=SAVE if save else HIS,
                             result=RESULT if not save else "已登錄回診：病歷號 A123（第 1 筆）"))
    resp = [{"i": 0, "at": 1000.5, "method": "GET", "url": HIS, "type": "document", "mime": "text/html"},
            {"i": 1, "at": 1007.5, "method": "POST", "type": "document",
             "url": SAVE if save else "http://his.test/his/query.aspx", "mime": "text/html", "subframe": not save}]
    return {"version": 1, "session": "s", "url": HIS, "commands": c, "responses": resp}


class PlanStepsTest(unittest.TestCase):
    def test_flow_becomes_steps(self):
        p = learn.plan_steps(_flow(), ["下次回診 2026-12-20"], {"dept": "復健科", "doc": "李醫師", "pid": "A123"}, "q")
        d = p["draft"]
        self.assertEqual(p["kind"], "steps")
        self.assertEqual([s["action"] for s in d["steps"]], ["navigate", "select", "select", "fill", "click", "read"])
        s = d["steps"]
        self.assertEqual(s[1]["args"], {"selector": FH + 'internal:role=combobox[name="科別"s] >> nth=0',
                                        "value": "{dept}"})                     # ref resolved, label -> value
        self.assertEqual(s[1]["expect"], {"selector": FH + "#doc", "state": "attached", "timeout": learn.STEP_WAIT_MS})
        self.assertEqual(s[3]["args"]["value"], "{pid}")
        self.assertEqual(s[4]["dialog"], "accept")
        self.assertNotIn("dialog", s[4]["args"])
        self.assertEqual(s[5]["args"], {"frame": "#work"})
        self.assertNotIn("@e", json.dumps(d))
        self.assertFalse(d["writes"])
        self.assertEqual(d["entry_url"], HIS)
        self.assertEqual({x["name"]: x["required"] for x in d["params"]}, {"dept": True, "doc": True, "pid": True})
        sr.check_steps(d["steps"])

    def test_auto_param_names(self):
        p = learn.plan_steps(_flow(), ["下次回診 2026-12-20"], {}, "q")
        self.assertEqual(p["args"], {"combobox": "復健科", "doc": "李醫師", "textbox": "A123"})

    def test_url_expect_and_write(self):
        rec = _flow(save=True)
        with self.assertRaises(learn.LearnError) as cm:
            learn.plan_steps(rec, ["已登錄回診：病歷號 A123"], {}, "s")
        self.assertIn("may change something", cm.exception.reason)
        self.assertFalse(cm.exception.fallback)
        p = learn.plan_steps(rec, ["已登錄回診：病歷號 A123"], {}, "s", write=True)
        d = p["draft"]
        self.assertTrue(d["writes"])
        click = d["steps"][4]
        self.assertEqual(click["expect"], {"url": "/his/save.aspx", "timeout": learn.STEP_WAIT_MS})

    def test_query_post_in_a_frame_is_a_read(self):
        p = learn.plan_steps(_flow(), ["下次回診 2026-12-20"], {}, "q")
        self.assertFalse(p["draft"]["writes"])
        rec = _flow()
        rec["responses"][1]["url"] = "http://his.test/his/update.aspx"
        with self.assertRaises(learn.LearnError):
            learn.plan_steps(rec, ["下次回診 2026-12-20"], {}, "q")

    def test_evaluate_extraction_gets_params_as_args(self):
        ev = _cmd(9, "evaluate", {"code": "document.querySelector('#work').contentDocument.body.innerText"
                                          ".includes('A123') && 'ok A123 下次回診'"},
                  before=HIS, after=HIS, result="ok A123 下次回診")
        p = learn.plan_steps(_flow(ev), ["ok A123"], {"pid": "A123"}, "q")
        code = p["draft"]["steps"][-1]["args"]["code"]
        self.assertTrue(code.startswith("async (args) =>"), code)
        self.assertIn(".includes(args.pid)", code)
        self.assertNotIn("'A123'", code)

    def test_refusals(self):
        def refused(rec, needle, explicit=None):
            with self.assertRaises(learn.LearnError) as cm:
                learn.plan_steps(rec, ["下次回診 2026-12-20"], explicit or {}, "q")
            self.assertIn(needle, cm.exception.reason)
        rec = _flow()
        rec["commands"][5]["args"].update(value="<redacted>", redacted=True)
        refused(rec, "password")
        rec = _flow()
        rec["commands"].insert(4, _cmd(4.5, "handoff", {"message": "2FA"}, before=HIS, after=HIS))
        refused(rec, "handed the tab to a human")
        rec = _flow()
        del rec["commands"][2]["args"]["selector_resolved"]
        refused(rec, "used the ref @e2")
        rec = _flow()
        rec["commands"][0]["args"]["url"] = HIS + "?session_token=abc"
        refused(rec, "credential-looking query field")
        refused(_flow(), "--param nope", {"nope": "zzz"})
        rec = _flow()
        rec["commands"] = rec["commands"][1:]
        refused(rec, "does not start with navigate")
        self.assertIsNone(learn.plan_steps(_flow(), ["NOT THERE"], {}, "q"))

    def test_step_param_name(self):
        self.assertEqual(learn.step_param_name(FH + "#pid"), "pid")
        self.assertEqual(learn.step_param_name('internal:role=textbox[name="Patient ID"s] >> nth=0'), "patient_id")
        self.assertEqual(learn.step_param_name(FH + 'internal:role=combobox[name="科別"s] >> nth=0'), "combobox")
        self.assertEqual(learn.step_param_name("select[name=dept]"), "dept")


class CmdLearnPath3Test(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.store = Path(self._t.name) / "store"

    def tearDown(self):
        self._t.cleanup()

    def learn(self, argv, rec):
        seen = {}

        def verify(d, given, expects=(), session=None):
            seen.update(kind=d.get("steps") and "steps", session=session, given=given)
            return [{"tier": "steps", "ok": True, "out_chars": 40}], ["steps"], {"steps": "str"}
        closed = []
        with mock.patch.dict("os.environ", {"DEJA_BROWSER_DAEMON": "1"}), \
                mock.patch.object(client, "call_running", return_value={"ok": True, "data": rec}), \
                mock.patch.object(sr, "_verify_tiers", side_effect=verify), \
                mock.patch.object(B, "wb", side_effect=lambda a, args=None, session=None: closed.append((a, session))), \
                mock.patch.object(sr, "RECIPES", self.store):
            out, rc = sr.cmd_learn(argv)
        return out, rc, seen, closed

    def test_path3_after_path2_needs_steps(self):
        # an evaluate in the top page that holds the answer, but the typed values are not in URL or code:
        # path 2 can't (fallback) -> path 3 saves a steps recipe, verified in its own session, then closed
        ev = _cmd(9, "evaluate", {"code": "document.querySelector('#work').contentDocument.body.innerText"},
                  before=HIS, after=HIS, result=RESULT)
        out, rc, seen, closed = self.learn(["--name", "q", "--expect", "下次回診 2026-12-20"], _flow(ev))
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["tier"], out["kind"]), ("steps", "steps"))
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), sr.LEARN_OUT)
        self.assertEqual(seen["session"], "learn-his.test")
        self.assertIn(("close_session", "learn-his.test"), closed)
        saved = json.loads((self.store / "his.test" / "q.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tier"], "steps")
        self.assertEqual(saved["verified_args"], {"combobox": "復健科", "doc": "李醫師", "textbox": "A123"})

    def test_blocking_refusal_wins(self):
        rec = _flow(save=True)
        out, rc, _, _ = self.learn(["--name", "s", "--expect", "已登錄回診：病歷號 A123"], rec)
        self.assertEqual(rc, 1)
        self.assertIn("may change something", out["reason"])
        self.assertFalse(self.store.exists())


# ---------- handoff args / wait conditions ----------

class HandoffArgsTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(B.parse_handoff({"message": " do 2FA "}), ("do 2FA", None, B.HANDOFF_TIMEOUT))
        self.assertEqual(B.parse_handoff({"message": "m", "until": {"url_contains": "/done"}, "timeout": 5000}),
                         ("m", ("url", "/done"), 5000))
        self.assertEqual(B.parse_handoff({"message": "m", "until": {"text": 12}})[1], ("text", "12"))
        self.assertEqual(B.parse_handoff({"message": "m", "until": {"js": "() => window.ok"}})[1],
                         ("js", "() => window.ok"))
        for bad in ({}, {"message": "  "}, {"message": "m", "until": {}}, {"message": "m", "until": "x"},
                    {"message": "m", "until": {"text": "a", "js": "b"}}, {"message": "m", "timeout": -1},
                    {"message": "m", "timeout": True}, {"message": "m", "timeout": "5"}):
            with self.assertRaises(B.WbError, msg=bad):
                B.parse_handoff(bad)

    def test_handoff_is_a_session_action_that_may_open_and_snapshot(self):
        self.assertIn("handoff", B.SESSION_ACTIONS)
        self.assertIn("handoff", B._MAY_OPEN)
        self.assertIn("handoff", B.THEN_SNAPSHOT)

    def test_wait_condition(self):
        self.assertEqual(B.wait_condition({"url_contains": "/x"}), ("url", "/x"))
        self.assertEqual(B.wait_condition({"url": "/y", "timeout": 3}), ("url", "/y"))
        with self.assertRaises(B.WbError):
            B.wait_condition({"text": "a", "url": "b"})

    def test_overlay_js_builds_no_markup(self):
        js = B._JS_HANDOFF
        self.assertIn("attachShadow({mode: 'closed'})", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("textContent", js)
        self.assertIn("document.documentElement", js)

    def test_wait_url_polls_without_page_js(self):
        class P:
            url, frames, n = "http://a/start", [], 0

            def wait_for_timeout(self, ms):
                P.n += 1
                if P.n == 3:
                    P.url = "http://a/done?x=1"
        r = B._wait(P(), {"url": "/done", "timeout": 5000})
        self.assertTrue(r["success"])
        P.url, P.n = "http://a/start", -100
        self.assertFalse(B._wait(P(), {"url_contains": "/never", "timeout": 30})["success"])


# ---------- recording additions ----------

class RecordingAdditionsTest(unittest.TestCase):
    def test_subframe_posts_are_kept(self):
        self.assertTrue(R.keep_response(HIS, "http://his.test/his/query.aspx", "document", "text/html", False, "POST"))
        self.assertFalse(R.keep_response(HIS, "http://his.test/his/form.aspx", "document", "text/html", False, "GET"))

    def test_wait_result_kept(self):
        c = R.command(1, "wait", {"text": "x"}, True, data={"success": False, "waited_ms": 9})
        self.assertEqual(c["result"], {"success": False})

    def test_resolved_refs_added(self):
        out = daemon.Worker._with_resolved({"selector": "@e3", "frame": "e2", "value": "v"},
                                           {"e3": "internal:role=textbox >> nth=0", "e2": "#work >> "})
        self.assertEqual(out["selector_resolved"], "internal:role=textbox >> nth=0")
        self.assertEqual(out["frame_resolved"], "#work >> ")
        self.assertEqual(daemon.Worker._with_resolved({"selector": "#a"}, {"e3": "x"}), {"selector": "#a"})
        rec = R.command(1, "fill", out, True)
        self.assertEqual(rec["args"]["selector_resolved"], "internal:role=textbox >> nth=0")

    def test_resolve_ref_logs_what_it_resolved_to(self):
        B._tls.resolved = {}
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        with mock.patch.object(B, "_ref_record", return_value=("e4", {"r": "button", "n": "Go", "i": 0, "c": 1})):
            sel = B.resolve_ref(page, "@e4")
        self.assertEqual(B._tls.resolved, {"e4": sel})
        self.assertEqual(sel, 'internal:role=button[name="Go"s] >> nth=0')


if __name__ == "__main__":
    unittest.main()
