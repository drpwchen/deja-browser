#!/usr/bin/env python3
"""Tests for the CDP browser layer (Edge / Playwright fully mocked — nothing launches,
nothing connects). run: python -m pytest -q"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import browser as ec


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.first = self

    def click(self, timeout=None):
        if self.sel not in self.page.dom:
            raise TimeoutError(f"waiting for {self.sel}")
        self.page.clicked.append(self.sel)

    def evaluate(self, js, timeout=None):
        return ["button", self.page.dom[self.sel].get("text", "")]

    def screenshot(self, path=None, type=None, quality=None):
        Path(path).write_bytes(b"\xff\xd8shot")

    def aria_snapshot(self, timeout=None):
        return self.page.aria.get(self.sel, "- heading \"Hi\"\n" * 50)

    def inner_text(self, timeout=None):
        if self.sel not in self.page.texts:
            raise TimeoutError(f"waiting for {self.sel}")
        return self.page.texts[self.sel]


class FakePage:
    def __init__(self, ctx, url="about:blank", tid=None):
        self.context, self.url, self.closed = ctx, url, False
        self.tid = tid or f"T{len(ctx.all_pages) + 1}"
        self.dom = {}           # selector -> {"readonly": bool, "text": str}
        self.values, self.clicked, self.evals = {}, [], []
        self.pdf_raises = False
        self.waits, self.wait_fails = [], False
        self.aria = {}          # selector -> aria_snapshot text (default: 50 heading lines)
        self.texts = {}         # selector -> innerText

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        return mock.Mock(status=200)

    def title(self):
        return f"title:{self.url}"

    def evaluate(self, code, arg=None, isolated_context=None):
        self.evals.append((code, arg, isolated_context))
        if arg is not None:     # the JS fill fallback
            sel, val = arg
            if sel not in self.dom:
                return None
            self.values[sel] = val
            return ["input", "value"]
        return {"document.title": "T", "1+1": 2, "undefined": None}.get(code, code)

    def fill(self, sel, val, timeout=None):
        if sel not in self.dom:
            raise TimeoutError(f"waiting for {sel}")
        if self.dom[sel].get("readonly"):
            raise RuntimeError("Element is not editable")
        self.values[sel] = val

    def eval_on_selector(self, sel, js):
        return ["input", False]

    def locator(self, sel):
        return FakeLocator(self, sel)

    def screenshot(self, path=None, type=None, quality=None):
        Path(path).write_bytes(b"shot")

    def pdf(self, path=None, **kw):
        if self.pdf_raises:
            raise RuntimeError("PDF generation is only supported for headless")
        Path(path).write_bytes(b"%PDF-1.4 fake")

    def on(self, event, handler):
        pass

    def remove_listener(self, event, handler):
        pass

    def wait_for_timeout(self, ms):
        pass

    def wait_for_function(self, expression, arg=None, timeout=None, polling=None):
        self.waits.append(("function", expression, arg, timeout))
        if self.wait_fails:
            raise TimeoutError(f"Timeout {timeout}ms exceeded.")

    def wait_for_selector(self, selector, state=None, timeout=None):
        self.waits.append(("selector", selector, state, timeout))
        if self.wait_fails or selector not in self.dom:
            raise TimeoutError(f"Timeout {timeout}ms exceeded.")

    def reload(self, wait_until=None, timeout=None):
        pass

    def close(self):
        self.closed = True
        self.context.all_pages.remove(self)


class FakeCdp:
    def send(self, method, params=None):
        if method == "Page.printToPDF":
            import base64
            return {"data": base64.b64encode(b"%PDF-1.7 cdp").decode()}
        return {"method": method, "params": params}

    def detach(self):
        pass


class FakeCtx:
    def __init__(self):
        self.all_pages = []

    @property
    def pages(self):
        return list(self.all_pages)

    def new_page(self):
        p = FakePage(self)
        self.all_pages.append(p)
        return p

    def new_cdp_session(self, page):
        return FakeCdp()


class _FakeBrowserCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = FakeCtx()
        self.patches = [
            mock.patch.object(ec, "SESSIONS_FILE", Path(self.tmp.name) / "sessions.json"),
            mock.patch.object(ec, "SHOT_DIR", Path(self.tmp.name) / "shots"),
            mock.patch.object(ec, "_ctx", lambda: self.ctx),
            mock.patch.object(ec, "target_id", lambda p: p.tid),
            mock.patch.object(ec, "_DRIVER", "playwright"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def nav(self, url="https://a.example/x", session="s1", **kw):
        return ec.wb("navigate", {"url": url, **kw}, session=session)


class EdgeCdpTest(_FakeBrowserCase):
    def test_navigate_opens_one_tab_then_reuses_it(self):
        r1 = self.nav()
        self.assertEqual(r1["success"], True)
        self.assertEqual(r1["status"], 200)
        r2 = self.nav("https://a.example/y")
        self.assertEqual(r1["tabId"], r2["tabId"])
        self.assertEqual(len(self.ctx.pages), 1)
        r3 = self.nav("https://a.example/z", newTab=True)
        self.assertNotEqual(r3["tabId"], r1["tabId"])
        self.assertEqual(len(self.ctx.pages), 2)

    def test_session_map_persists_targetids(self):
        r = self.nav()
        saved = json.loads(ec.SESSIONS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["s1"]["tabs"], [r["tabId"]])
        self.assertEqual(saved["s1"]["current"], r["tabId"])

    def test_page_action_without_tab_raises_notab(self):
        with self.assertRaises(ec.NoTab):
            ec.wb("evaluate", {"code": "1+1"}, session="nobody")

    def test_dead_tab_is_pruned_and_reads_as_notab(self):
        self.nav()
        self.ctx.pages[0].close()         # user closed it / Edge restarted
        with self.assertRaises(ec.NoTab):
            ec.wb("evaluate", {"code": "1+1"}, session="s1")
        self.assertEqual(json.loads(ec.SESSIONS_FILE.read_text(encoding="utf-8"))["s1"]["tabs"], [])

    def test_evaluate_shape(self):
        self.nav()
        self.assertEqual(ec.wb("evaluate", {"code": "1+1"}, session="s1"), {"type": "number", "value": 2})
        self.assertEqual(ec.wb("evaluate", {"code": "undefined"}, session="s1")["type"], "undefined")
        self.assertEqual(ec.wb("evaluate", {"code": "document.title"}, session="s1"), {"type": "string", "value": "T"})

    def test_patchright_evaluates_in_main_world(self):
        self.nav()
        with mock.patch.object(ec, "_DRIVER", "patchright"):
            ec.wb("evaluate", {"code": "1+1"}, session="s1")
        self.assertIs(self.ctx.pages[0].evals[-1][2], False)

    def test_fill_native_and_readonly_fallback(self):
        self.nav()
        page = self.ctx.pages[0]
        page.dom = {"#a": {}, "#date": {"readonly": True}}
        r = ec.wb("fill", {"selector": "#a", "value": "中文"}, session="s1")
        self.assertEqual((r["success"], r["mode"]), (True, "value"))
        self.assertNotIn("fallback", r)
        r = ec.wb("fill", {"selector": "#date", "value": "2026/11/02"}, session="s1")
        self.assertEqual(r["fallback"], "js")
        self.assertEqual(page.values, {"#a": "中文", "#date": "2026/11/02"})
        with self.assertRaises(ec.WbError):
            ec.wb("fill", {"selector": "#missing", "value": "x"}, session="s1")

    def test_click(self):
        self.nav()
        page = self.ctx.pages[0]
        page.dom = {"text=查詢": {"text": "查詢"}}
        r = ec.wb("click", {"selector": "text=查詢"}, session="s1")
        self.assertEqual(r, {"success": True, "tag": "button", "text": "查詢"})
        self.assertEqual(page.clicked, ["text=查詢"])

    def test_screenshot_returns_path_only(self):
        self.nav()
        out = str(Path(self.tmp.name) / "sub" / "a.jpg")
        r = ec.wb("screenshot", {"format": "jpeg", "quality": 70, "path": out}, session="s1")
        self.assertEqual((r["path"], r["format"], r["mimeType"]), (out, "jpeg", "image/jpeg"))
        self.assertGreater(r["sizeBytes"], 0)
        r = ec.wb("screenshot", {}, session="s1")          # no path -> temp file
        self.assertTrue(Path(r["path"]).exists())

    def test_save_as_pdf_and_cdp_fallback(self):
        self.nav()
        out = str(Path(self.tmp.name) / "p.pdf")
        r = ec.wb("save_as_pdf", {"path": out}, session="s1")
        self.assertEqual(r["mimeType"], "application/pdf")
        self.assertTrue(Path(out).read_bytes().startswith(b"%PDF"))
        self.ctx.pages[0].pdf_raises = True
        r = ec.wb("save_as_pdf", {"path": out}, session="s1")
        self.assertEqual(Path(out).read_bytes(), b"%PDF-1.7 cdp")

    def test_list_and_find_tabs(self):
        a = self.nav("https://a.example/1")
        b = self.nav("https://b.example/2", newTab=True)
        tabs = ec.wb("list_tabs", {}, session="s1")["tabs"]
        self.assertEqual([t["tabId"] for t in tabs], [a["tabId"], b["tabId"]])
        self.assertEqual([t["active"] for t in tabs], [False, True])
        r = ec.wb("find_tab", {"url": "https://a.example"}, session="s1")
        self.assertEqual((r["tabId"], r["borrowed"]), (a["tabId"], False))
        self.assertEqual(ec.wb("evaluate", {"code": "x"}, session="s1")["value"], "x")
        with self.assertRaises(ec.NoTab):
            ec.wb("find_tab", {"url": "https://nope"}, session="s1")

    def test_sessions_are_isolated_and_close_session_only_closes_own_tabs(self):
        mine = self.nav("https://a.example/1", session="s1")
        other = self.nav("https://o.example/1", session="s2")
        # borrow a tab the user (or another session) had open
        user_tab = FakePage(self.ctx, "https://user.example/inbox", tid="U1")
        self.ctx.all_pages.append(user_tab)
        r = ec.wb("find_tab", {"url": "https://user.example", "active": True}, session="s1")
        self.assertTrue(r["borrowed"])
        self.assertEqual(len(ec.wb("list_tabs", {}, session="s2")["tabs"]), 1)
        r = ec.wb("close_session", {}, session="s1")
        self.assertEqual(r, {"success": True, "closed": 1})
        alive = {p.tid for p in self.ctx.pages}
        self.assertNotIn(mine["tabId"], alive)
        self.assertIn(other["tabId"], alive)
        self.assertIn("U1", alive)                              # borrowed tab survives
        self.assertNotIn("s1", json.loads(ec.SESSIONS_FILE.read_text(encoding="utf-8")))

    def test_attach_tab_and_snapshot_and_cdp(self):
        user_tab = FakePage(self.ctx, "https://user.example/", tid="U9")
        self.ctx.all_pages.append(user_tab)
        r = ec.wb("attach_tab", {"tabId": "U9"}, session="s3")
        self.assertTrue(r["borrowed"])
        snap = ec.wb("snapshot", {"max_chars": 30}, session="s3")
        self.assertLessEqual(len(snap["tree_text"]), 30)        # cut on a line boundary since the hint work
        self.assertTrue(snap["truncated"])
        self.assertEqual(ec.wb("cdp", {"method": "Page.getLayoutMetrics"}, session="s3")["method"],
                         "Page.getLayoutMetrics")

    def test_unsupported_action(self):
        with self.assertRaises(ec.WbError):
            ec.wb("network", {"cmd": "start"}, session="s1")

    def test_network_capture_navigate_opens_tab_in_fresh_session(self):
        body = str(Path(self.tmp.name) / "net")
        r = ec.wb("network_capture", {"trigger": {"navigate": "https://a.example/app"},
                                      "wait_ms": 0, "body_dir": body}, session="fresh")
        self.assertEqual(set(r), {"url", "count", "body_dir", "requests"})   # output shape unchanged
        self.assertEqual(r["url"], "https://a.example/app")
        self.assertEqual(len(self.ctx.pages), 1)
        self.assertEqual(len(ec.wb("list_tabs", {}, session="fresh")["tabs"]), 1)
        self.assertEqual(ec.wb("close_session", {}, session="fresh"), {"success": True, "closed": 1})
        self.assertEqual(self.ctx.pages, [])

    def test_network_capture_navigate_reuses_existing_tab(self):
        self.nav(session="s1")
        ec.wb("network_capture", {"trigger": {"navigate": "https://a.example/y"}, "wait_ms": 0,
                                  "body_dir": str(Path(self.tmp.name) / "net")}, session="s1")
        self.assertEqual(len(self.ctx.pages), 1)

    def test_network_capture_without_navigate_still_needs_a_tab(self):
        for args in ({}, {"trigger": {"reload": True}}, {"trigger": {"click": "#x"}}):
            with self.assertRaises(ec.NoTab):
                ec.wb("network_capture", {**args, "wait_ms": 0}, session="fresh")
        self.assertEqual(self.ctx.pages, [])

    def test_network_capture_denied_host_opens_no_tab(self):
        with self.assertRaises(ec.WbError):
            ec.wb("network_capture", {"trigger": {"navigate": "https://mail.google.com/"}, "wait_ms": 0},
                  session="fresh")
        self.assertEqual(self.ctx.pages, [])

    def test_network_capture_bad_trigger_type_opens_no_tab(self):
        with self.assertRaises(ec.WbError):
            ec.wb("network_capture", {"trigger": "https://a.example/", "wait_ms": 0}, session="fresh")
        self.assertEqual(self.ctx.pages, [])


class CliErrorTest(_FakeBrowserCase):
    def run_main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ec.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_notab_prints_json_error_and_rc1(self):
        rc, out, err = self.run_main(["evaluate", "{}", "--session", "none"])
        self.assertEqual(rc, 1)
        j = json.loads(out)
        self.assertIs(j["ok"], False)
        self.assertIn("no tab", j["error"])
        self.assertNotIn("Traceback", err)

    def test_wberror_prints_json_error_and_rc1(self):
        rc, out, err = self.run_main(["bogus", "{}"])
        self.assertEqual(rc, 1)
        self.assertIn("unsupported action", json.loads(out)["error"])
        self.assertNotIn("Traceback", err)

    def test_programming_errors_are_not_swallowed(self):
        with mock.patch.object(ec, "wb", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError):
                self.run_main(["evaluate", "{}"])


class WaitTest(_FakeBrowserCase):
    def setUp(self):
        super().setUp()
        ec.wb("navigate", {"url": "https://a.example/"}, session="w")
        self.page = self.ctx.pages[0]

    def test_wait_text_polls_inner_text(self):
        r = ec.wb("wait", {"text": "Results"}, session="w")
        self.assertIs(r["success"], True)
        self.assertIsInstance(r["waited_ms"], int)
        kind, expr, arg, timeout = self.page.waits[0]
        self.assertEqual(kind, "function")
        self.assertIn("innerText", expr)
        self.assertEqual(arg, "Results")
        self.assertEqual(timeout, 15000)

    def test_wait_selector_default_state_visible(self):
        self.page.dom["#out"] = {}
        r = ec.wb("wait", {"selector": "#out", "timeout": 3000}, session="w")
        self.assertIs(r["success"], True)
        self.assertEqual(self.page.waits[0], ("selector", "#out", "visible", 3000))

    def test_wait_selector_state_passthrough(self):
        self.page.dom["#out"] = {}
        ec.wb("wait", {"selector": "#out", "state": "attached"}, session="w")
        self.assertEqual(self.page.waits[0][2], "attached")

    def test_wait_js_uses_wait_for_function(self):
        r = ec.wb("wait", {"js": "() => window.ready === true"}, session="w")
        self.assertIs(r["success"], True)
        self.assertEqual(self.page.waits[0], ("function", "() => window.ready === true", None, 15000))

    def test_wait_timeout_returns_failure_not_exception(self):
        self.page.wait_fails = True
        for args in ({"text": "x", "timeout": 10}, {"selector": "#nope", "timeout": 10},
                     {"js": "() => false", "timeout": 10}):
            r = ec.wb("wait", args, session="w")
            self.assertIs(r["success"], False)
            self.assertIn("waited_ms", r)

    def test_wait_needs_exactly_one_condition(self):
        for args in ({}, {"text": "a", "selector": "b"}):
            with self.assertRaises(ec.WbError):
                ec.wb("wait", args, session="w")

    def test_wait_non_timeout_error_is_wberror(self):
        with mock.patch.object(self.page, "wait_for_function", side_effect=ValueError("SyntaxError: bad js")):
            with self.assertRaises(ec.WbError) as cm:
                ec.wb("wait", {"js": "((("}, session="w")
        self.assertIn("SyntaxError", str(cm.exception))

    def test_wait_without_tab_raises_notab(self):
        with self.assertRaises(ec.NoTab):
            ec.wb("wait", {"text": "x"}, session="nobody")


class BatchTest(_FakeBrowserCase):
    def steps(self):
        return [{"action": "navigate", "args": {"url": "https://a.example/"}},
                {"action": "wait", "args": {"text": "hello"}},
                {"action": "evaluate", "args": {"code": "document.title"}}]

    def test_runs_in_order_on_one_page(self):
        r = ec.wb("batch", {"steps": self.steps()}, session="b")
        self.assertIs(r["ok"], True)
        self.assertEqual([x["action"] for x in r["results"]], ["navigate", "wait", "evaluate"])
        self.assertTrue(all(x["ok"] for x in r["results"]))
        self.assertEqual(r["results"][0]["data"]["url"], "https://a.example/")
        self.assertEqual(r["results"][2]["data"], {"type": "string", "value": "T"})
        self.assertTrue(all(isinstance(x["secs"], float) for x in r["results"]))
        self.assertEqual(len(self.ctx.pages), 1)                      # one tab for all three steps
        self.assertEqual(self.ctx.pages[0].waits[0][2], "hello")      # wait + evaluate hit that same page
        self.assertEqual(len(self.ctx.pages[0].evals), 1)

    def test_args_optional(self):
        r = ec.wb("batch", {"steps": [{"action": "navigate", "args": {"url": "https://a.example/"}},
                                      {"action": "list_tabs"}]}, session="b")
        self.assertIs(r["ok"], True)
        self.assertEqual(len(r["results"][1]["data"]["tabs"]), 1)

    def test_failure_stops_and_skips_the_rest(self):
        steps = self.steps()
        steps.insert(1, {"action": "click", "args": {"selector": "#missing"}})
        r = ec.wb("batch", {"steps": steps}, session="b")
        self.assertIs(r["ok"], False)
        self.assertEqual([x["ok"] for x in r["results"]], [True, False, False, False])
        self.assertIn("missing", r["results"][1]["error"])
        self.assertNotIn("data", r["results"][1])
        self.assertTrue(r["results"][2].get("skipped") and r["results"][3].get("skipped"))
        self.assertEqual(r["results"][2]["action"], "wait")
        self.assertEqual(self.ctx.pages[0].evals, [])                 # the skipped evaluate never ran

    def test_stop_on_error_false_keeps_going(self):
        steps = self.steps()
        steps.insert(1, {"action": "click", "args": {"selector": "#missing"}})
        r = ec.wb("batch", {"steps": steps, "stop_on_error": False}, session="b")
        self.assertIs(r["ok"], False)
        self.assertEqual([x["ok"] for x in r["results"]], [True, False, True, True])
        self.assertFalse(any(x.get("skipped") for x in r["results"]))

    def test_nested_batch_rejected_before_anything_runs(self):
        steps = [{"action": "navigate", "args": {"url": "https://a.example/"}},
                 {"action": "batch", "args": {"steps": []}}]
        with self.assertRaises(ec.WbError) as cm:
            ec.wb("batch", {"steps": steps}, session="b")
        self.assertIn("nested", str(cm.exception))
        self.assertEqual(self.ctx.pages, [])

    def test_bad_step_shapes_rejected(self):
        for bad in ({}, {"steps": []}, {"steps": "x"}, {"steps": [{"args": {}}]}, {"steps": [["navigate"]]},
                    {"steps": [{"action": "navigate", "args": "u"}]}):
            with self.assertRaises(ec.WbError, msg=bad):
                ec.wb("batch", bad, session="b")

    def test_unknown_action_is_a_step_error(self):
        r = ec.wb("batch", {"steps": [{"action": "bogus"}]}, session="b")
        self.assertIs(r["ok"], False)
        self.assertIn("unsupported action", r["results"][0]["error"])

    def test_guard_still_blocks_navigate_inside_batch(self):
        steps = [{"action": "navigate", "args": {"url": "https://mail.google.com/"}},
                 {"action": "evaluate", "args": {"code": "1+1"}}]
        r = ec.wb("batch", {"steps": steps}, session="b")
        self.assertIs(r["ok"], False)
        self.assertFalse(r["results"][0]["ok"])
        self.assertTrue(r["results"][1]["skipped"])
        self.assertEqual(self.ctx.pages, [])                          # no tab was opened

    def test_quiet_drops_the_payload(self):
        steps = self.steps()
        steps[0]["quiet"] = True
        steps[2]["quiet"] = True
        r = ec.wb("batch", {"steps": steps}, session="b")
        self.assertEqual(set(r["results"][0]), {"ok", "secs"})
        self.assertEqual(set(r["results"][2]), {"ok", "secs"})
        self.assertIn("data", r["results"][1])                         # not quiet -> full result
        self.assertIn("action", r["results"][1])

    def test_quiet_does_not_hide_errors(self):
        r = ec.wb("batch", {"steps": [{"action": "click", "args": {"selector": "#x"}, "quiet": True}]},
                  session="b")
        self.assertIn("error", r["results"][0])

    def test_wait_timeout_in_batch_is_a_failed_step(self):
        steps = self.steps()
        steps[1]["args"]["timeout"] = 10
        orig = ec.PAGE_ACTIONS["wait"]

        def timing_out(page, a):
            page.wait_fails = True
            try:
                return orig(page, a)
            finally:
                page.wait_fails = False
        with mock.patch.dict(ec.PAGE_ACTIONS, {"wait": timing_out}):
            r = ec.wb("batch", {"steps": steps}, session="b")
        self.assertIs(r["ok"], False)
        self.assertEqual([x["ok"] for x in r["results"]], [True, False, False])
        self.assertIn("timed out", r["results"][1]["error"])
        self.assertTrue(r["results"][2]["skipped"])
        self.assertEqual(self.ctx.pages[0].evals, [])                 # the step after the failed wait never ran

    def test_quiet_wait_timeout_is_still_visible(self):
        steps = [{"action": "navigate", "args": {"url": "https://a.example/"}, "quiet": True},
                 {"action": "wait", "args": {"text": "x", "timeout": 10}, "quiet": True}]
        with mock.patch.dict(ec.PAGE_ACTIONS, {"wait": lambda page, a: {"success": False, "waited_ms": 10}}):
            r = ec.wb("batch", {"steps": steps}, session="b")
        self.assertIs(r["ok"], False)
        self.assertIn("error", r["results"][1])
        self.assertIn("timed out", r["results"][1]["error"])
        self.assertEqual(r["results"][1]["data"], {"success": False, "waited_ms": 10})   # why, not just "failed"

    def test_wait_timeout_in_batch_continues_when_stop_on_error_false(self):
        steps = self.steps()
        orig = ec.PAGE_ACTIONS["wait"]
        with mock.patch.dict(ec.PAGE_ACTIONS, {"wait": lambda page, a: {"success": False, "waited_ms": 7}}):
            r = ec.wb("batch", {"steps": steps, "stop_on_error": False}, session="b")
        self.assertIs(r["ok"], False)
        self.assertEqual([x["ok"] for x in r["results"]], [True, False, True])
        self.assertIn("7 ms", r["results"][1]["error"])

    def test_unexpected_exception_becomes_step_error(self):
        steps = self.steps()
        with mock.patch.dict(ec.PAGE_ACTIONS, {"evaluate": mock.Mock(side_effect=KeyError("boom"))}):
            r = ec.wb("batch", {"steps": steps}, session="b")
        self.assertIs(r["ok"], False)
        self.assertIn("boom", r["results"][2]["error"])

    def test_cli_batch_json_and_exit_code(self):
        rc, out, err = CliErrorTest.run_main(self, ["batch", json.dumps({"steps": self.steps()}), "--session", "b"])
        self.assertEqual(rc, 0)
        self.assertIs(json.loads(out)["ok"], True)
        rc, out, err = CliErrorTest.run_main(
            self, ["batch", json.dumps({"steps": [{"action": "click", "args": {"selector": "#z"}}]}),
                   "--session", "b"])
        self.assertEqual(rc, 1)
        self.assertIs(json.loads(out)["ok"], False)


class LaunchTest(unittest.TestCase):
    def test_launch_noop_when_cdp_up(self):
        with mock.patch.object(ec, "version", return_value={"Browser": "Edg/156"}), \
             mock.patch.object(ec, "check_profile") as chk, \
             mock.patch.object(ec.subprocess, "Popen") as popen:
            self.assertEqual(ec.launch(), ({"Browser": "Edg/156"}, False))
            popen.assert_not_called()
            chk.assert_called_once_with({"Browser": "Edg/156"})

    def test_launch_starts_dedicated_profile_on_loopback(self):
        seq = iter([None, None, {"Browser": "Edg/156"}])
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ec, "version", side_effect=lambda: next(seq)), \
             mock.patch.object(ec, "PROFILE", Path(td) / "agent-profile"), \
             mock.patch.object(ec, "EDGE", "/fake/browser"), \
             mock.patch.object(ec.guard, "harden_profile"), \
             mock.patch.object(ec.guard, "password_managers", return_value=[]), \
             mock.patch.object(ec.os.path, "exists", return_value=True), \
             mock.patch.object(ec.time, "sleep"), \
             mock.patch.object(ec, "listener_pid", return_value=None), \
             mock.patch.object(ec.subprocess, "Popen") as popen:
            v, ours = ec.launch()
            self.assertTrue(ours)
            argv = popen.call_args[0][0]
            self.assertIn("--remote-debugging-address=127.0.0.1", argv)
            self.assertIn(f"--remote-debugging-port={ec.PORT}", argv)
            self.assertNotEqual(ec.PORT, 9222)              # tests run on a random port (conftest)
            self.assertIn("--no-first-run", argv)
            self.assertTrue(any(a.startswith("--user-data-dir=") and a.endswith("agent-profile") for a in argv))

    def test_stop_refuses_foreign_listener(self):
        with mock.patch.object(ec, "listener_pid", return_value=1234), \
             mock.patch.object(ec.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="msedge.exe --user-data-dir=C:\\Users\\X\\AppData\\Edge Default")
            r = ec.stop()
            self.assertIsNone(r["killed"])
            self.assertEqual(run.call_count, 1)                 # no taskkill


class SnapshotReadTest(_FakeBrowserCase):
    """snapshot truncation (line boundary, offset, hint) and the filterable `read` action."""

    ROWS = [f"- row {i:03d}: item" for i in range(100)]            # 100 lines, each 15 chars
    BODY = "\n".join(ROWS)

    def page(self, session="s1"):
        self.nav(session=session)
        return self.ctx.pages[-1]

    def snap(self, **a):
        return ec.wb("snapshot", a, session="s1")

    def test_snapshot_truncates_on_a_line_boundary_with_a_hint(self):
        self.page().aria["body"] = self.BODY
        r = self.snap(max_chars=100)
        self.assertTrue({"url", "title", "tree_text", "chars", "full_chars", "truncated"} <= set(r))
        self.assertTrue(r["truncated"])
        self.assertEqual((r["chars"], r["full_chars"]), (len(self.BODY), len(self.BODY)))
        self.assertLessEqual(len(r["tree_text"]), 100)
        self.assertTrue(all(ln in self.ROWS for ln in r["tree_text"].splitlines()))   # no half lines
        self.assertEqual(r["next_offset"], len(r["tree_text"]) + 1)                   # past the newline
        for needle in (f"{r['next_offset']:,} of {r['chars']:,}", "query=", "selector=",
                       f"offset={r['next_offset']}", "read {query}"):
            self.assertIn(needle, r["hint"])

    def test_snapshot_offset_pages_chain_back_to_the_full_text(self):
        self.page().aria["body"] = self.BODY
        parts, off = [], 0
        for _ in range(100):
            r = self.snap(max_chars=100, offset=off)
            parts.append(r["tree_text"])
            if not r["truncated"]:
                break
            off = r["next_offset"]
        self.assertGreater(len(parts), 1)
        self.assertEqual("\n".join(parts), self.BODY)
        self.assertNotIn("hint", r)                      # the last page is complete: no hint
        self.assertNotIn("next_offset", r)

    def test_snapshot_not_truncated_has_no_hint(self):
        self.page().aria["body"] = "\n".join(self.ROWS[:3])
        r = self.snap(max_chars=100)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["tree_text"], "\n".join(self.ROWS[:3]))
        self.assertNotIn("hint", r)
        self.assertNotIn("next_offset", r)

    def test_snapshot_default_cap_is_unchanged_and_offset_applies_after_query(self):
        self.page().aria["body"] = "\n".join(f"- line {i} {'x' * 20}" for i in range(2000))   # ~56k chars
        r = self.snap()
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["tree_text"]), 20000)
        self.assertGreater(len(r["tree_text"]), 19900)
        q = self.snap(query="line 1999", offset=0)
        self.assertEqual(q["tree_text"], "- line 1999 " + "x" * 20)
        self.assertFalse(q["truncated"])

    def test_snapshot_single_line_longer_than_the_cap_still_advances(self):
        self.page().aria["body"] = "y" * 250
        r1 = self.snap(max_chars=100)
        self.assertEqual((len(r1["tree_text"]), r1["next_offset"]), (100, 100))
        r3 = self.snap(max_chars=100, offset=200)
        self.assertEqual(r3["tree_text"], "y" * 50)
        self.assertFalse(r3["truncated"])

    def test_snapshot_offset_past_the_end_is_empty(self):
        self.page().aria["body"] = "\n".join(self.ROWS[:3])
        r = self.snap(offset=9999)
        self.assertEqual((r["tree_text"], r["truncated"]), ("", False))

    # ---- read ----
    TEXT = ("Intro paragraph\n\nAlpha 1\nBeta 2\n\nIron\n55.845\nFe\n\nGamma 3\nDelta 4\nEpsilon 5\n"
            "Zeta 6\nEta 7\nTheta 8\nIota 9\nKappa 10\nLambda IRON again\nMu 12")

    def test_read_without_query_returns_the_text_and_old_style_fields(self):
        self.page().texts["body"] = "Hello\nWorld"
        r = ec.wb("read", {}, session="s1")
        self.assertEqual(r["text"], "Hello\nWorld")
        self.assertEqual((r["chars"], r["full_chars"], r["truncated"]), (11, 11, False))
        self.assertEqual(r["url"], "https://a.example/x")
        self.assertEqual(r["title"], "title:https://a.example/x")
        self.assertNotIn("hint", r)

    def test_read_query_keeps_hit_lines_with_context_and_gap_markers(self):
        self.page().texts["body"] = self.TEXT
        r = ec.wb("read", {"query": "iron", "context_lines": 1}, session="s1")      # case-insensitive
        self.assertEqual(r["text"].splitlines(), ["Beta 2", "Iron", "55.845", "…", "Kappa 10",
                                                  "Lambda IRON again", "Mu 12"])
        self.assertIn("55.845", r["text"])
        self.assertLess(r["chars"], r["full_chars"])
        self.assertFalse(r["truncated"])

    def test_read_query_default_context_is_two_lines_and_merges_close_hits(self):
        self.page().texts["body"] = "a\nb\nc\nHIT\nd\ne\nf\nHIT\ng"
        r = ec.wb("read", {"query": "hit"}, session="s1")
        self.assertEqual(r["text"].splitlines(), ["b", "c", "HIT", "d", "e", "f", "HIT", "g"])   # no "…"

    def test_read_query_without_a_match_returns_empty_text_and_a_hint(self):
        self.page().texts["body"] = self.TEXT
        r = ec.wb("read", {"query": "unobtainium"}, session="s1")
        self.assertEqual((r["text"], r["chars"], r["truncated"]), ("", 0, False))
        self.assertEqual(r["full_chars"], len(self.TEXT))
        self.assertIn("unobtainium", r["hint"])

    def test_read_selector_scopes_the_text(self):
        p = self.page()
        p.texts["body"] = "whole page Iron"
        p.texts["#table"] = "table only Iron"
        r = ec.wb("read", {"selector": "#table", "query": "iron"}, session="s1")
        self.assertEqual(r["text"], "table only Iron")
        with self.assertRaises(ec.WbError):
            ec.wb("read", {"selector": "#missing"}, session="s1")

    def test_read_truncates_on_lines_and_offset_pages_through(self):
        self.page().texts["body"] = "\n".join(f"line {i:02d} text" for i in range(50))     # 12 chars/line
        r = ec.wb("read", {"max_chars": 100}, session="s1")
        self.assertTrue(r["truncated"])
        self.assertTrue(all(ln.startswith("line ") and ln.endswith(" text") for ln in r["text"].splitlines()))
        self.assertIn(f"offset={r['next_offset']}", r["hint"])
        self.assertIn("query=", r["hint"])
        parts, off = [], 0
        for _ in range(100):
            p = ec.wb("read", {"max_chars": 100, "offset": off}, session="s1")
            parts.append(p["text"])
            if not p["truncated"]:
                break
            off = p["next_offset"]
        self.assertEqual("\n".join(parts), "\n".join(f"line {i:02d} text" for i in range(50)))

    def test_read_works_inside_a_batch(self):
        self.page().texts["body"] = "x\nIron 55.845\ny"
        r = ec.wb("batch", {"steps": [{"action": "read", "args": {"query": "iron"}}]}, session="s1")
        self.assertTrue(r["ok"])
        self.assertIn("55.845", r["results"][0]["data"]["text"])

    def test_read_without_a_tab_raises_notab(self):
        with self.assertRaises(ec.NoTab):
            ec.wb("read", {}, session="nobody")


# snapshot/network helpers (tests moved from site_recipe, 2026-11-11)
class EdgeHelpersTest(unittest.TestCase):
    def test_with_ancestors_keeps_full_path(self):
        lines = ["- main:", "  - article:", "    - heading", "    - text: 5 stars today", "  - article:",
                 "    - text: 9 stars today", "- footer"]
        keep = ["stars" in ln for ln in lines]
        self.assertEqual(ec._with_ancestors(lines, keep),
                         ["- main:", "  - article:", "    - text: 5 stars today", "  - article:",
                          "    - text: 9 stars today"])

    def test_role(self):
        self.assertEqual(ec._role('  - button "Go"'), "button")
        self.assertEqual(ec._role("  - /url: x"), "")

    def test_safe_headers_redacts_credentials(self):
        h = ec._safe_headers({"Authorization": "Bearer abc", "Cookie": "s=1", "X-CSRF-Token": "t",
                              "content-type": "application/json"})
        self.assertEqual(h["Authorization"], "<redacted len=10>")
        self.assertTrue(h["Cookie"].startswith("<redacted"))
        self.assertTrue(h["X-CSRF-Token"].startswith("<redacted"))
        self.assertEqual(h["content-type"], "application/json")


class ProfileCheckTest(unittest.TestCase):
    """launch() must not hand out a browser on PORT that runs another profile; the check is free
    once the browser's id is recorded in the profile dir."""
    V = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/aaaa-1111"}

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.prof = Path(self.td.name) / "profile"
        self.prof.mkdir()
        self.p = mock.patch.object(ec, "PROFILE", self.prof)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.td.cleanup()

    def test_recorded_id_is_a_string_compare_only(self):
        (self.prof / ec.ID_FILE).write_text("aaaa-1111", encoding="utf-8")
        with mock.patch.object(ec, "listener_pid", side_effect=AssertionError("no lookup")):
            ec.check_profile(self.V)

    def test_unseen_browser_on_our_profile_is_recorded(self):
        cl = f'"msedge.exe" --user-data-dir={self.prof} --remote-debugging-port=9222'
        with mock.patch.object(ec, "listener_pid", return_value=42),                 mock.patch.object(ec, "_cmdline", return_value=cl) as cmd:
            ec.check_profile(self.V)
            self.assertEqual((self.prof / ec.ID_FILE).read_text(encoding="utf-8"), "aaaa-1111")
            ec.check_profile(self.V)          # second time: fast path
            self.assertEqual(cmd.call_count, 1)

    def test_browser_on_another_profile_is_refused(self):
        (self.prof / ec.ID_FILE).write_text("old-id", encoding="utf-8")
        cl = f'msedge.exe --user-data-dir={Path(self.td.name) / "other"} --remote-debugging-port=9222'
        with mock.patch.object(ec, "listener_pid", return_value=42),                 mock.patch.object(ec, "_cmdline", return_value=cl):
            with self.assertRaises(ec.WbError) as cm:
                ec.check_profile(self.V)
            self.assertIn("another profile", str(cm.exception))
        self.assertEqual((self.prof / ec.ID_FILE).read_text(encoding="utf-8"), "old-id")

    def test_unknown_listener_is_allowed_but_not_recorded(self):
        with mock.patch.object(ec, "listener_pid", return_value=42),                 mock.patch.object(ec, "_cmdline", return_value=""):
            ec.check_profile(self.V)
        self.assertFalse((self.prof / ec.ID_FILE).exists())

    def test_check_can_be_turned_off(self):
        with mock.patch.object(ec.config, "CONFIG", {"browser": {"check_profile": False}}),                 mock.patch.object(ec, "listener_pid", side_effect=AssertionError("no lookup")):
            ec.check_profile(self.V)

    def test_foreign_browser_carries_pid_and_profile(self):
        other = str(Path(self.td.name) / "other")
        with mock.patch.object(ec, "listener_pid", return_value=42), \
                mock.patch.object(ec, "_cmdline", return_value=f"msedge.exe --user-data-dir={other} --x"):
            with self.assertRaises(ec.ForeignBrowser) as cm:
                ec.check_profile(self.V)
        self.assertEqual((cm.exception.pid, cm.exception.profile), (42, other))

    def test_user_data_dir_parsing(self):
        f = ec._user_data_dir
        self.assertEqual(f(r"edge --user-data-dir=C:\x\p --remote-debugging-port=9222"), r"C:\x\p")
        self.assertEqual(f(r'"edge.exe" "--user-data-dir=C:\a b\p" --remote-debugging-port=1'), r"C:\a b\p")
        self.assertEqual(f(r'edge --user-data-dir="C:\a b\p" --x'), r"C:\a b\p")
        self.assertEqual(f("chrome --user-data-dir=/Users/u/Library/Application Support/d/profile --remote-debugging-port=9222"),
                         "/Users/u/Library/Application Support/d/profile")
        self.assertIsNone(f("edge --remote-debugging-port=9222"))


class PortTakenTest(unittest.TestCase):
    """The configured port is held by another browser: our own orphaned test browser is closed and the
    port reused; anything else is left running and the agent browser moves to a spare port, recorded
    in the profile for later runs. Everything is mocked: nothing is killed, nothing launches."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.prof = Path(self.td.name) / "agent-profile"
        self.prof.mkdir()
        self.cfg_port = ec.CONFIG_PORT
        self.spare = self.cfg_port + 1
        self.saved_port = ec.PORT
        self.up = {}                      # port -> /json/version of the browser listening there
        self.owner = {}                   # port -> (pid, command line)
        self.leftover = str(Path(tempfile.gettempdir()) / "deja-browser-test-abc123" / "data" / "profile")
        self.patches = [
            mock.patch.object(ec, "PROFILE", self.prof),
            mock.patch.object(ec, "EDGE", "/fake/browser"),
            mock.patch.object(ec.guard, "harden_profile"),
            mock.patch.object(ec.guard, "password_managers", return_value=[]),
            mock.patch.object(ec.os.path, "exists", return_value=True),
            mock.patch.object(ec.time, "sleep"),
            mock.patch.object(ec, "version", side_effect=lambda: self.up.get(ec.PORT)),
            mock.patch.object(ec, "listener_pid", side_effect=lambda: (self.owner.get(ec.PORT) or (None,))[0]),
            mock.patch.object(ec, "_cmdline", side_effect=self._cmdline),
            mock.patch.object(ec, "_port_free", side_effect=lambda p: p not in self.owner),
        ]
        for p in self.patches:
            p.start()
        self.popen = mock.patch.object(ec.subprocess, "Popen", side_effect=self._start).start()
        self.kill = mock.patch.object(ec, "_kill_tree", side_effect=self._kill).start()

    def tearDown(self):
        mock.patch.stopall()
        for p in self.patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        ec._use_port(self.saved_port)
        self.td.cleanup()

    # fake process table
    def _cmdline(self, pid):
        return next((cl for p, cl in self.owner.values() if p == pid), "")

    def _browser(self, port, pid, profile, bid):
        self.owner[port] = (pid, f'msedge.exe --user-data-dir="{profile}" --remote-debugging-port={port}')
        self.up[port] = {"Browser": "Edg/156", "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/{bid}"}

    def _start(self, argv, **kw):
        port = int(next(a for a in argv if a.startswith("--remote-debugging-port=")).split("=")[1])
        self._browser(port, 900, self.prof, "agent-id")

    def _kill(self, pid):
        for port, (p, _) in list(self.owner.items()):
            if p == pid:
                del self.owner[port]
                self.up.pop(port, None)
        return True

    def launched_port(self):
        argv = self.popen.call_args[0][0]
        return int(next(a for a in argv if a.startswith("--remote-debugging-port=")).split("=")[1])

    # cases
    def test_orphaned_test_browser_is_closed_and_the_port_reused(self):
        self._browser(self.cfg_port, 4652, self.leftover, "test-id")
        with mock.patch.object(ec, "_parent_state", return_value=("dead", "")):
            v, ours = ec.launch()
        self.kill.assert_called_once_with(4652)
        self.assertTrue(ours)
        self.assertEqual((self.launched_port(), ec.PORT), (self.cfg_port, self.cfg_port))
        self.assertFalse((self.prof / ec.PORT_FILE).exists())

    def test_test_browser_with_a_live_test_runner_is_not_killed(self):
        self._browser(self.cfg_port, 4652, self.leftover, "test-id")
        with mock.patch.object(ec, "_parent_state", return_value=("alive", "python.exe -m pytest -q")):
            ec.launch()
        self.kill.assert_not_called()
        self.assertEqual(self.launched_port(), self.spare)

    def test_unknown_parent_is_not_killed(self):
        self._browser(self.cfg_port, 4652, self.leftover, "test-id")
        with mock.patch.object(ec, "_parent_state", return_value=(None, "")):
            ec.launch()
        self.kill.assert_not_called()
        self.assertEqual(self.launched_port(), self.spare)

    def test_unknown_browser_is_left_alone_and_the_agent_moves_to_a_spare_port(self):
        self._browser(self.cfg_port, 777, r"D:\Profiles\Microsoft\Edge\User Data", "user-id")
        with mock.patch.object(ec, "_parent_state", side_effect=AssertionError("not a test profile: no lookup")):
            v, ours = ec.launch()
        self.kill.assert_not_called()
        self.assertTrue(ours)
        self.assertEqual((self.launched_port(), ec.PORT), (self.spare, self.spare))
        self.assertEqual(ec.ENDPOINT, f"http://127.0.0.1:{self.spare}")
        self.assertEqual((self.prof / ec.PORT_FILE).read_text(encoding="utf-8"), str(self.spare))
        self.assertIn(self.cfg_port, self.owner)                 # the other browser still runs

    def test_spare_port_skips_ports_already_in_use(self):
        self._browser(self.cfg_port, 777, "/home/u/.config/chrome", "user-id")
        self.owner[self.spare] = (555, "nginx")
        ec.launch()
        self.assertEqual(self.launched_port(), self.spare + 1)

    def test_non_cdp_process_on_the_port_is_left_alone(self):
        self.owner[self.cfg_port] = (555, "some-server --listen")   # holds the port, no /json/version
        ec.launch()
        self.kill.assert_not_called()
        self.assertEqual(self.launched_port(), self.spare)

    def test_later_run_reuses_the_recorded_spare_port(self):
        self._browser(self.cfg_port, 777, "/home/u/.config/chrome", "user-id")
        ec.launch()
        self.popen.reset_mock()
        ec._use_port(self.cfg_port)                              # a fresh process starts on the config port
        v, ours = ec.launch()
        self.assertFalse(ours)
        self.popen.assert_not_called()
        self.assertEqual(ec.PORT, self.spare)

    def test_stale_spare_port_record_is_dropped(self):
        (self.prof / ec.PORT_FILE).write_text(str(self.spare), encoding="utf-8")   # nothing runs there
        ec.launch()
        self.assertEqual(self.launched_port(), self.cfg_port)
        self.assertFalse((self.prof / ec.PORT_FILE).exists())

    def test_kill_that_does_not_free_the_port_falls_back_to_a_spare_port(self):
        self._browser(self.cfg_port, 4652, self.leftover, "test-id")
        self.kill.side_effect = lambda pid: True                 # "killed" but the port stays taken
        with mock.patch.object(ec, "_parent_state", return_value=("dead", "")), \
                mock.patch.object(ec, "_wait_port_free", return_value=False):
            ec.launch()
        self.assertEqual(self.launched_port(), self.spare)

    def test_fast_path_has_no_process_lookup(self):
        self._browser(self.cfg_port, 900, self.prof, "agent-id")
        (self.prof / ec.ID_FILE).write_text("agent-id", encoding="utf-8")
        with mock.patch.object(ec, "listener_pid", side_effect=AssertionError("no lookup")), \
                mock.patch.object(ec, "_cmdline", side_effect=AssertionError("no lookup")):
            self.assertEqual(ec.launch()[1], False)
        self.popen.assert_not_called()

    def test_stop_finds_the_agent_on_its_spare_port(self):
        self._browser(self.cfg_port, 777, "/home/u/.config/chrome", "user-id")
        ec.launch()
        ec._use_port(self.cfg_port)
        with mock.patch.object(ec.subprocess, "run") as run, mock.patch.object(ec.os, "kill") as kill:
            run.return_value = mock.Mock(returncode=0)
            r = ec.stop()
        self.assertEqual(r["killed"], 900)                       # the agent browser, not pid 777


class TestLeftoverTest(unittest.TestCase):
    TMP = Path(tempfile.gettempdir())

    def test_only_temp_dirs_named_by_our_test_suite(self):
        f = ec.is_test_leftover
        self.assertTrue(f(str(self.TMP / "deja-browser-test-3na8yxbl" / "data" / "profile")))
        self.assertFalse(f(str(self.TMP / "some-other-tool" / "profile")))
        self.assertFalse(f(str(Path.home() / "deja-browser-test-x" / "profile")))   # not under temp
        self.assertFalse(f("/opt/deja-browser-test-x/profile"))
        self.assertFalse(f(None))

    def test_the_agent_profile_is_never_a_leftover(self):
        prof = self.TMP / "deja-browser-test-y" / "data" / "profile"
        with mock.patch.object(ec, "PROFILE", prof):
            self.assertFalse(ec.is_test_leftover(str(prof)))

    def test_orphan_rules(self):
        lo = str(self.TMP / "deja-browser-test-z" / "profile")
        cases = [(("dead", ""), True), ((None, ""), False), (("alive", "C:\\Python\\python.exe -m pytest"), False),
                 (("alive", "/sbin/init"), True)]
        for state, want in cases:
            with mock.patch.object(ec, "_parent_state", return_value=state):
                self.assertEqual(ec.is_orphaned_test_browser(1, lo), want, state)
        with mock.patch.object(ec, "_parent_state", side_effect=AssertionError("no lookup")):
            self.assertFalse(ec.is_orphaned_test_browser(1, r"D:\Profiles\Edge\User Data"))


if __name__ == "__main__":
    unittest.main()
