#!/usr/bin/env python3
"""Tests for the CDP browser layer (Edge / Playwright fully mocked — nothing launches,
nothing connects). run: python -m pytest -q"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edge_recipes import browser as ec


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
        return "- heading \"Hi\"\n" * 50


class FakePage:
    def __init__(self, ctx, url="about:blank", tid=None):
        self.context, self.url, self.closed = ctx, url, False
        self.tid = tid or f"T{len(ctx.all_pages) + 1}"
        self.dom = {}           # selector -> {"readonly": bool, "text": str}
        self.values, self.clicked, self.evals = {}, [], []
        self.pdf_raises = False

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


class EdgeCdpTest(unittest.TestCase):
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
        self.assertEqual(len(snap["tree_text"]), 30)
        self.assertEqual(ec.wb("cdp", {"method": "Page.getLayoutMetrics"}, session="s3")["method"],
                         "Page.getLayoutMetrics")

    def test_unsupported_action(self):
        with self.assertRaises(ec.WbError):
            ec.wb("network", {"cmd": "start"}, session="s1")


class LaunchTest(unittest.TestCase):
    def test_launch_noop_when_cdp_up(self):
        with mock.patch.object(ec, "version", return_value={"Browser": "Edg/156"}), \
             mock.patch.object(ec.subprocess, "Popen") as popen:
            self.assertEqual(ec.launch(), ({"Browser": "Edg/156"}, False))
            popen.assert_not_called()

    def test_launch_starts_dedicated_profile_on_loopback(self):
        seq = iter([None, None, {"Browser": "Edg/156"}])
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ec, "version", side_effect=lambda: next(seq)), \
             mock.patch.object(ec, "PROFILE", Path(td) / "agent-profile"), \
             mock.patch.object(ec.os.path, "exists", return_value=True), \
             mock.patch.object(ec.time, "sleep"), \
             mock.patch.object(ec.subprocess, "Popen") as popen:
            v, ours = ec.launch()
            self.assertTrue(ours)
            argv = popen.call_args[0][0]
            self.assertIn("--remote-debugging-address=127.0.0.1", argv)
            self.assertIn("--remote-debugging-port=9222", argv)
            self.assertIn("--no-first-run", argv)
            self.assertTrue(any(a.startswith("--user-data-dir=") and a.endswith("agent-profile") for a in argv))

    def test_stop_refuses_foreign_listener(self):
        with mock.patch.object(ec, "listener_pid", return_value=1234), \
             mock.patch.object(ec.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="msedge.exe --user-data-dir=C:\\Users\\X\\AppData\\Edge Default")
            r = ec.stop()
            self.assertIsNone(r["killed"])
            self.assertEqual(run.call_count, 1)                 # no taskkill


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


if __name__ == "__main__":
    unittest.main()
