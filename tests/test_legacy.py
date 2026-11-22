#!/usr/bin/env python3
"""v0.4 batch B (DESIGN.md §6): dialogs, popups + tab ids, frames, select. Pure functions and the
fake browser from test_browser.py — nothing launches. run: python -m pytest -q"""
import unittest
from unittest import mock

from deja_browser import browser as ec
from test_browser import FakeLocator, FakePage, _FakeBrowserCase


class FakeDialog:
    def __init__(self, page, type="confirm", message="sure?"):
        self.page, self.type, self.message, self.done, self.text = page, type, message, None, None

    def accept(self, text=None):
        self.done, self.text = "accepted", text

    def dismiss(self):
        self.done = "dismissed"


class DialogPolicyTest(unittest.TestCase):
    def test_parse(self):
        P = ec.parse_dialog_policy
        self.assertIsNone(P(None))
        self.assertIsNone(P(""))
        self.assertEqual(P("accept"), ("accept", None))
        self.assertEqual(P(" Dismiss "), ("dismiss", None))
        self.assertEqual(P(True), ("accept", None))
        self.assertEqual(P(False), ("dismiss", None))
        self.assertEqual(P("A123"), ("accept", "A123"))                       # prompt answer
        self.assertEqual(P({"accept": True, "text": "accept"}), ("accept", "accept"))
        self.assertEqual(P({"accept": False}), ("dismiss", None))
        for bad in (3, ["accept"], {"text": "x"}, {"accept": True, "text": 5}):
            with self.assertRaises(ec.WbError):
                P(bad)

    def test_decision(self):
        D = ec.dialog_decision
        self.assertEqual(D("alert", None), ("accept", None))
        self.assertEqual(D("beforeunload", None), ("accept", None))
        self.assertEqual(D("confirm", None), ("dismiss", None))
        self.assertEqual(D("prompt", None), ("dismiss", None))
        self.assertEqual(D("confirm", ("accept", "x")), ("accept", None))   # text only answers a prompt
        self.assertEqual(D("prompt", ("accept", "x")), ("accept", "x"))
        self.assertEqual(D("beforeunload", ("dismiss", None)), ("dismiss", None))


class _DialogCase(_FakeBrowserCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(ec, "cached_target_id", lambda pg: getattr(pg, "tid", None))
        p.start()
        self.patches.append(p)
        st = ec._tls
        self.saved = (st.sink, st.recent, st.pending, st.scope, st.dialog_hook, st.late_in_result)
        st.sink, st.recent, st.pending, st.scope, st.dialog_hook, st.late_in_result = [], {}, [], None, None, True

    def tearDown(self):
        st = ec._tls
        st.sink, st.recent, st.pending, st.scope, st.dialog_hook, st.late_in_result = self.saved
        super().tearDown()

    def page_with_confirm(self, kind="confirm"):
        """Clicking #q raises a dialog on the page (as the context listener would deliver it)."""
        self.nav()
        page = self.ctx.pages[-1]
        page.dom["#q"] = {"text": "query"}
        page.fired = []

        def click_hook(sel):
            if sel == "#q":
                d = FakeDialog(page, kind, "確定查詢?")
                page.fired.append(d)
                ec._on_ctx_dialog(d)
        page.click_hook = click_hook
        return page


class DialogReceiptTest(_DialogCase):
    def test_default_dismisses_confirm_and_says_so(self):
        page = self.page_with_confirm()
        r = ec.wb("click", {"selector": "#q"}, session="s1")
        self.assertEqual(page.fired[0].done, "dismissed")
        self.assertEqual(r["dialog"], {"type": "confirm", "message": "確定查詢?", "handled": "dismissed"})
        self.assertEqual(r["dialogs"][0]["tabId"], page.tid)          # full record, reported once
        page.texts["body"] = "x"
        self.assertNotIn("dialogs", ec.wb("read", {}, session="s1"))

    def test_accept_policy_and_prompt_text(self):
        page = self.page_with_confirm()
        r = ec.wb("click", {"selector": "#q", "dialog": "accept"}, session="s1")
        self.assertEqual((page.fired[0].done, r["dialog"]["handled"]), ("accepted", "accepted"))
        page2 = self.page_with_confirm("prompt")
        ec.wb("click", {"selector": "#q", "dialog": "A123"}, session="s1")
        self.assertEqual((page2.fired[0].done, page2.fired[0].text), ("accepted", "A123"))

    def test_batch_level_policy_applies_to_steps(self):
        page = self.page_with_confirm()
        r = ec.wb("batch", {"dialog": "accept", "steps": [{"action": "click", "args": {"selector": "#q"}}]},
                  session="s1")
        self.assertEqual(page.fired[0].done, "accepted")
        self.assertEqual(r["results"][0]["data"]["dialog"]["handled"], "accepted")
        self.assertEqual(len(r["dialogs"]), 1)                       # outermost reply only

    def test_late_dialog_default_policy_reported_next_and_in_status(self):
        page = self.page_with_confirm()
        late = FakeDialog(page, "alert", "LATE")
        ec._on_ctx_dialog(late)                                       # no action running
        self.assertEqual(late.done, "accepted")
        page.texts["body"] = "x"
        r = ec.wb("read", {}, session="s1")
        self.assertNotIn("dialog", r)
        self.assertEqual([(d["type"], d["message"], d["handled"]) for d in r["dialogs"]],
                         [("alert", "LATE", "accepted")])
        st = ec.wb("dialog", {}, session="s1")
        self.assertEqual([d["message"] for d in st["recent"]], ["LATE"])
        self.assertEqual(st["pending"], [])

    def test_foreign_tab_driver_default_in_process_left_alone_by_daemon(self):
        self.nav()
        other = FakePage(self.ctx, "https://other.example/", tid="OTHER")
        d = FakeDialog(other)
        ec.handle_dialog(d, "s1")                                     # in-process, playwright: dismiss
        self.assertEqual(d.done, "dismissed")
        with mock.patch.object(ec, "_DRIVER", "patchright"):
            d2 = FakeDialog(other)
            ec.handle_dialog(d2, "s1")
            self.assertIsNone(d2.done)
        d3, sink = FakeDialog(other), []
        ec.handle_dialog(d3, "s1", leave_foreign=True, sink=sink)
        self.assertEqual((d3.done, sink), (None, []))

    def test_popup_of_own_tab_is_owned_and_caller_listener_wins(self):
        self.nav()
        mine = self.ctx.pages[-1]
        popup = FakePage(self.ctx, "https://a.example/pop", tid="POP")
        popup.opener = lambda: mine
        d = FakeDialog(popup, "alert", "hi")
        ec.handle_dialog(d, "s1", leave_foreign=True)
        self.assertEqual(d.done, "accepted")
        popup.listeners = lambda ev: [print]                           # the caller's own page listener
        d2 = FakeDialog(popup)
        self.assertIsNone(ec.handle_dialog(d2, "s1"))
        self.assertIsNone(d2.done)

    def test_caller_listener_on_the_real_page_object_wins(self):
        """The sync Page has no listeners(); the caller's page.on("dialog") lives on its impl emitter."""
        self.nav()
        page = self.ctx.pages[-1]
        page.listeners = None                                          # like the sync wrapper: absent
        page._impl_obj = mock.Mock(listeners=lambda ev: [print] if ev == "dialog" else [])
        d = FakeDialog(page)
        self.assertIsNone(ec.handle_dialog(d, "s1"))
        self.assertIsNone(d.done)
        page._impl_obj = mock.Mock(listeners=lambda ev: [], _opener=None)
        ec.handle_dialog(d, "s1")
        self.assertEqual(d.done, "dismissed")

    def test_failed_action_names_the_dialog(self):
        self.nav()
        page = self.ctx.pages[-1]

        def boom(sel):
            ec._on_ctx_dialog(FakeDialog(page, "confirm", "really?"))
            raise ec.WbError("click: gone")
        page.dom["#q"] = {}
        page.click_hook = boom
        with self.assertRaises(ec.WbError) as cm:
            ec.wb("click", {"selector": "#q"}, session="s1")
        self.assertIn("really?", str(cm.exception))

    def test_bad_policy_is_an_error_before_anything_runs(self):
        self.nav()
        with self.assertRaises(ec.WbError):
            ec.wb("click", {"selector": "#q", "dialog": 7}, session="s1")


class TabIdTest(unittest.TestCase):
    def test_ids_are_short_stable_and_never_reused(self):
        e = {"tabs": [], "borrowed": [], "current": None}
        self.assertEqual(ec.assign_tab_id(e, "A"), "t1")
        self.assertEqual(ec.assign_tab_id(e, "B"), "t2")
        self.assertEqual(ec.assign_tab_id(e, "A"), "t1")
        del e["ids"]["A"]                                   # tab gone
        self.assertEqual(ec.assign_tab_id(e, "C"), "t3")
        self.assertEqual(ec.resolve_tab_ref(e, "t2"), "B")
        self.assertIsNone(ec.resolve_tab_ref(e, "t9"))
        self.assertEqual(ec.resolve_tab_ref(e, "ABCDEF0123"), "ABCDEF0123")   # a targetId passes through
        self.assertEqual(ec.resolve_tab_ref(e, "tab"), "tab")

    def test_adopt_popups_follows_owned_openers_only(self):
        m = {"s": {"tabs": ["MAIN", "BOR"], "borrowed": ["BOR"], "current": "MAIN"},
             "o": {"tabs": ["OTHER"], "borrowed": [], "current": "OTHER"}}
        live = {"MAIN": {}, "BOR": {}, "OTHER": {},
                "P1": {"openerId": "MAIN"}, "P2": {"openerId": "P1"},          # popup of a popup
                "PB": {"openerId": "BOR"}, "PO": {"openerId": "OTHER"},       # borrowed / other session
                "X": {"openerId": None}}
        e = m["s"]
        self.assertEqual(sorted(ec.adopt_popups(m, e, live)), ["P1", "P2"])
        self.assertEqual(e["tabs"], ["MAIN", "BOR", "P1", "P2"])
        self.assertEqual(e["current"], "MAIN")                             # not switched
        self.assertEqual(ec.adopt_popups(m, e, live), [])                    # once only


class PopupTest(_DialogCase):
    def popup(self, opener, url="https://a.example/pop"):
        p = FakePage(self.ctx, url)
        p.opener_tid = opener.tid
        self.ctx.all_pages.append(p)
        return p

    def test_click_registers_the_popup_with_a_short_id(self):
        r = self.nav()
        self.assertEqual(r["tab"], "t1")
        main = self.ctx.pages[-1]
        main.dom["#pick"] = {}
        main.click_hook = lambda sel: self.popup(main)
        c = ec.wb("click", {"selector": "#pick"}, session="s1")
        self.assertEqual(c["popups"], [{"tabId": "T2", "tab": "t2", "url": "https://a.example/pop"}])
        tabs = ec.wb("list_tabs", {}, session="s1")["tabs"]
        self.assertEqual([(t["tab"], t["active"]) for t in tabs], [("t1", True), ("t2", False)])
        a = ec.wb("attach_tab", {"tabId": "t2"}, session="s1")
        self.assertEqual((a["tabId"], a["tab"], a["borrowed"]), ("T2", "t2", False))
        with self.assertRaises(ec.NoTab):
            ec.wb("attach_tab", {"tabId": "t7"}, session="s1")
        self.assertEqual(ec.wb("close_session", {}, session="s1")["closed"], 2)
        self.assertEqual(self.ctx.pages, [])

    def test_late_popup_is_reported_by_the_next_action_and_follow_popup_switches(self):
        self.nav()
        main = self.ctx.pages[-1]
        self.popup(main)                                   # appeared after the last call
        main.texts["body"] = "x"
        r = ec.wb("read", {}, session="s1")
        self.assertEqual([p["tab"] for p in r["popups"]], ["t2"])
        main.dom["#pick"] = {}
        main.click_hook = lambda sel: self.popup(main, "https://a.example/pop2")
        c = ec.wb("click", {"selector": "#pick", "follow_popup": True}, session="s1")
        self.assertEqual(c["followed"], "t3")
        self.assertEqual(ec.wb("list_tabs", {}, session="s1")["tabs"][-1]["active"], True)

    def test_follow_popup_without_one_says_so(self):
        self.nav()
        main = self.ctx.pages[-1]
        main.dom["#b"] = {}
        c = ec.wb("click", {"selector": "#b", "follow_popup": True, "popup_timeout": 200}, session="s1")
        self.assertIsNone(c["followed"])
        self.assertNotIn("popups", c)



class FramePureTest(unittest.TestCase):
    def test_frame_hop(self):
        H = ec.frame_hop
        self.assertEqual(H({"tag": "iframe", "id": "work", "idUnique": True}), "#work")
        self.assertEqual(H({"tag": "iframe", "id": "a:b", "idUnique": True}), "iframe[id='a:b']")
        self.assertEqual(H({"tag": "frame", "id": "dup", "idUnique": False, "name": "main", "nameUnique": True}),
                         "frame[name='main']")
        self.assertEqual(H({"tag": "frame", "name": "it's", "nameUnique": True}), "frame[name='it\\'s']")
        self.assertEqual(H({"tag": "iframe", "name": "x", "nameUnique": False, "index": 2}), "iframe >> nth=2")
        self.assertEqual(H({"tag": "iframe", "index": 0}), "iframe >> nth=0")

    def test_frame_arg_scopes_click_fill_select(self):
        pfx = "#fr" + ec.FRAME_HOP
        with mock.patch.object(ec, "_resolve_frame", return_value=(object(), pfx)) as rf:
            self.assertEqual(ec._fsel(None, {"frame": "#fr"}, "#p"), pfx + "#p")
            self.assertEqual(ec._fsel(None, {}, "#p"), "#p")
            self.assertEqual(ec._fsel(None, {"frame": "#fr"}, pfx + "#p"), pfx + "#p")   # already pierces
            self.assertEqual(rf.call_count, 1)

    def test_header_and_frame_arg(self):
        pfx = "#work" + ec.FRAME_HOP
        self.assertEqual(ec.frame_header("#work", pfx),
                         '[frame #work → selector prefix: "#work >> internal:control=enter-frame >> "]')
        self.assertEqual(ec.frame_arg_selector("#work"), ("#work", pfx))
        self.assertEqual(ec.frame_arg_selector(pfx), ("#work", pfx))
        self.assertEqual(ec.frame_arg_selector(pfx.rstrip()), ("#work", pfx))
        nested = pfx + "iframe >> nth=1" + ec.FRAME_HOP
        self.assertEqual(ec.frame_arg_selector(nested), (pfx + "iframe >> nth=1", nested))
        with self.assertRaises(ec.WbError):
            ec.frame_arg_selector("  ")

    def test_assemble_indents_children_and_drops_empty_ones(self):
        secs = [{"depth": 0, "lines": ["top"]},
                {"depth": 1, "header": "[frame A]", "lines": ["a1"]},
                {"depth": 2, "header": "[frame A/B]", "lines": ["b1", "b2"]},
                {"depth": 1, "header": "[frame EMPTY]", "lines": []}]
        self.assertEqual(ec.assemble_sections(secs).splitlines(),
                         ["top", "[frame A]", "  a1", "  [frame A/B]", "    b1", "    b2"])

    def test_query_lines(self):
        self.assertEqual(ec.query_lines(["a", "b", "HIT", "c", "d", "e", "f", "hit"], "hit", 1),
                         ["b", "HIT", "c", "…", "f", "hit"])


class FakeFrame:
    def __init__(self, text, info=None, parent=None, aria=None, has_body=True):
        self.text, self.info, self.parent_frame = text, info or {}, parent
        self.child_frames, self.aria, self.has_body, self.url = [], aria, has_body, "https://a.example/f"
        if parent is not None:
            parent.child_frames.append(self)

    def frame_element(self):
        return mock.Mock(evaluate=lambda js: dict(self.info, visible=self.info.get("visible", True)))

    def evaluate(self, js, arg=None):
        if js == ec._JS_BODY_TEXT:
            return self.text
        if js == ec._JS_HAS_BODY:
            return self.has_body
        if js == ec._JS_TEXT_PRESENT:
            return arg in self.text
        raise AssertionError(js)

    def locator(self, sel):
        frame = self

        class L:
            first = None

            def aria_snapshot(self, timeout=None):
                return frame.aria or "".join(f"- text: {ln}\n" for ln in frame.text.splitlines()).rstrip()
        loc = L()
        loc.first = loc
        return loc


class FrameWalkTest(_FakeBrowserCase):
    def framed_page(self, frameset=False):
        self.nav()
        page = self.ctx.pages[-1]
        main = FakeFrame("" if frameset else "Top text", has_body=not frameset)
        work = FakeFrame("Frame text\nPMR", {"tag": "iframe", "id": "work", "idUnique": True}, main)
        FakeFrame("Inner deep", {"tag": "iframe", "index": 0}, work)
        FakeFrame("ad", {"tag": "iframe", "index": 1, "visible": False}, main)
        page.main_frame = main
        page.frames = [main] + main.child_frames + work.child_frames
        page.wait_for_timeout = lambda ms: None
        return page

    def test_read_shows_frame_content_under_prefix_headers(self):
        self.framed_page()
        r = ec.wb("read", {}, session="s1")
        pw = "#work" + ec.FRAME_HOP
        self.assertEqual(r["text"].splitlines(), [
            "Top text", ec.frame_header("#work", pw), "  Frame text", "  PMR",
            "  " + ec.frame_header("iframe >> nth=0", pw + "iframe >> nth=0" + ec.FRAME_HOP), "    Inner deep"])
        self.assertNotIn("ad", r["text"].split())                   # hidden frame left out
        q = ec.wb("read", {"query": "deep"}, session="s1")
        self.assertEqual(q["text"].splitlines()[-1], "    Inner deep")
        self.assertIn("iframe >> nth=0", q["text"].splitlines()[0])
        self.assertNotIn("Top text", q["text"])

    def test_frameset_snapshot_and_truncation_counts_frames(self):
        self.framed_page(frameset=True)
        s = ec.wb("snapshot", {}, session="s1")
        self.assertTrue(s["tree_text"].startswith("[frame #work"))
        self.assertIn("  - text: Frame text", s["tree_text"])
        t = ec.wb("read", {"max_chars": 40}, session="s1")
        self.assertTrue(t["truncated"])
        self.assertLessEqual(len(t["text"]), 40)

    def test_wait_text_checks_every_frame(self):
        self.framed_page()
        self.assertTrue(ec.wb("wait", {"text": "Inner deep", "timeout": 100}, session="s1")["success"])
        self.assertFalse(ec.wb("wait", {"text": "missing", "timeout": 50}, session="s1")["success"])

    def test_single_document_page_takes_the_old_path(self):
        self.nav()
        page = self.ctx.pages[-1]
        page.texts["body"] = "plain"
        page.frames = [FakeFrame("ignored")]
        self.assertEqual(ec.wb("read", {}, session="s1")["text"], "plain")


class SelectTest(_FakeBrowserCase):
    OPTS = [["", "--"], ["PMR", "復健科"], ["IM", "內科"]]

    def page(self, **extra):
        self.nav()
        page = self.ctx.pages[-1]
        page.dom["#dept"] = {"options": [list(o) for o in self.OPTS], "selected": "", **extra}
        return page

    def test_select_choice(self):
        C = ec.select_choice
        self.assertEqual(C(self.OPTS, "PMR"), ("value", "PMR"))
        self.assertEqual(C(self.OPTS, " 復健科 "), ("label", "復健科"))
        self.assertEqual(C(self.OPTS, ""), ("value", ""))
        self.assertIsNone(C(self.OPTS, "牙科"))
        self.assertEqual(C([["a", "b"], ["b", "x"]], "b"), ("value", "b"))      # a value wins over a label

    def test_fill_on_select_by_value_label_and_a_bad_value(self):
        page = self.page()
        r = ec.wb("fill", {"selector": "#dept", "value": "IM"}, session="s1")
        self.assertEqual((r["success"], r["matched"], r["selected"]), (True, "value", {"value": "IM", "label": "內科"}))
        r = ec.wb("fill", {"selector": "#dept", "value": "復健科"}, session="s1")
        self.assertEqual((r["matched"], r["selected"]["value"]), ("label", "PMR"))
        with self.assertRaises(ec.WbError) as cm:
            ec.wb("fill", {"selector": "#dept", "value": "牙科"}, session="s1")
        self.assertIn("'PMR'='復健科'", str(cm.exception))
        self.assertEqual(page.dom["#dept"]["selected"], "PMR")                  # unchanged

    def test_fill_on_hidden_select_sets_it_by_js(self):
        page = self.page(hidden=True)
        r = ec.wb("fill", {"selector": "#dept", "value": "內科"}, session="s1")
        self.assertEqual((r["fallback"], r["selected"]["value"], page.dom["#dept"]["selected"]), ("js", "IM", "IM"))

    def test_select_action(self):
        self.page()
        self.assertEqual(ec.wb("select", {"selector": "#dept", "label": "復健科"}, session="s1")["selected"],
                         {"value": "PMR", "label": "復健科"})
        self.assertEqual(ec.wb("select", {"selector": "#dept", "value": "IM"}, session="s1")["selected"]["label"], "內科")
        self.assertEqual(ec.wb("select", {"selector": "#dept", "index": 0}, session="s1")["selected"]["value"], "")
        with self.assertRaises(ec.WbError) as cm:
            ec.wb("select", {"selector": "#dept", "label": "牙科", "timeout": 10}, session="s1")
        self.assertIn("options:", str(cm.exception))
        for bad in ({"selector": "#dept"}, {"selector": "#dept", "value": "x", "label": "y"}, {"label": "y"}):
            with self.assertRaises(ec.WbError):
                ec.wb("select", bad, session="s1")


class DialogHookTest(unittest.TestCase):
    def test_each_connection_gets_its_own_handler_object(self):
        """The driver caches its wrapping shim on the handler object; a shared function would give a
        patchright connection playwright's shim when both drivers run in one process (daemon)."""
        handlers = []

        class Ctx:
            def on(self, ev, h):
                handlers.append((ev, h))
        ctxs = [Ctx(), Ctx()]
        saved = ec._tls.hooked
        try:
            for c in ctxs:
                with mock.patch.object(ec, "_connect", return_value=mock.Mock(contexts=[c])):
                    ec._tls.hooked = None
                    self.assertIs(ec._ctx(), c)
                    ec._ctx()                                 # same context: not registered twice
        finally:
            ec._tls.hooked = saved
        self.assertEqual([ev for ev, _ in handlers], ["dialog", "dialog"])
        self.assertIsNot(handlers[0][1], handlers[1][1])
        self.assertIsNot(handlers[0][1], ec._on_ctx_dialog)


if __name__ == "__main__":
    unittest.main()
