#!/usr/bin/env python3
"""Element refs (snapshot {refs}), multi-match warnings, then_snapshot, snapshot delta, recipe hints on
navigate. Playwright fully mocked. run: python -m pytest -q"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import browser as ec
from test_browser import _FakeBrowserCase

TREE = """- heading "Orders" [level=1]
- table:
  - rowgroup:
    - row "A-001 刪除":
      - cell "A-001"
      - cell "刪除":
        - button "刪除"
    - row "A-002 刪除":
      - cell "A-002"
      - cell "刪除":
        - button "刪除"
- paragraph:
  - textbox "病歷號": A123
  - textbox
  - textbox
- 'button "a: b"'
- button "say \\"hi\\""
- button /usr/
- heading [level=2]
- list:
  - listitem: item 1
- link "下一頁":
  - /url: result.html"""


def role_sel(role, name=None, nth=None, prefix="", scope=None):
    rec = {"r": role, "n": name, "p": prefix, "s": scope}
    return ec.ref_locator(rec) + (f" >> nth={nth}" if nth is not None else "")


class ParseTest(unittest.TestCase):
    def test_plain_quoted_and_escaped_keys(self):
        P = ec.parse_aria_line
        self.assertEqual(P('- button "OK"'), {"role": "button", "name": "OK", "at": 13})
        self.assertEqual(P('  - textbox "病歷號": A123')["name"], "病歷號")
        self.assertEqual(P('- link "下一頁":')["at"], len('- link "下一頁"'))
        self.assertEqual(P('- heading "X" [level=1]')["at"], len('- heading "X" [level=1]'))
        q = P("- 'button \"a: b\"'")
        self.assertEqual((q["role"], q["name"]), ("button", "a: b"))
        self.assertEqual(q["at"], len("- 'button \"a: b\""))           # inside the YAML quotes
        self.assertEqual(P("- 'link \"it''s\"':")["name"], "it's")
        self.assertEqual(P('- button "say \\"hi\\""')["name"], 'say "hi"')
        self.assertEqual(P("- button /usr/")["name"], "/usr/")        # literal: we never render regexes
        self.assertEqual(P("- textbox"), {"role": "textbox", "name": None, "at": 9})

    def test_non_nodes(self):
        for ln in ("- text: hello", "  - /url: https://x", "plain", "- paragraph: a: b", ""):
            p = ec.parse_aria_line(ln)
            self.assertTrue(p is None or p["role"] in ("text", "paragraph"), ln)
        self.assertIsNone(ec.parse_aria_line("  - /url: x"))

    def test_ref_id(self):
        self.assertEqual(ec.ref_id("@e12"), "e12")
        self.assertEqual(ec.ref_id("e3"), "e3")
        for v in ("#e3", "e", "@x1", "button", None, 5, "e3 >> nth=0"):
            self.assertIsNone(ec.ref_id(v), v)


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.lines = TREE.splitlines()
        self.cands = ec.ref_records(self.lines, {"p": ""})

    def rec(self, needle, k=0):
        hits = [c for ln, c in zip(self.lines, self.cands) if needle in ln and c]
        return hits[k][0]

    def test_duplicates_get_nth_and_count(self):
        self.assertEqual([self.rec('button "刪除"', k)["i"] for k in (0, 1)], [0, 1])
        self.assertEqual(self.rec('button "刪除"')["c"], 2)
        self.assertEqual(self.rec('cell "刪除"', 1)["i"], 1)

    def test_unnamed_nodes_count_every_node_of_their_role(self):
        tb = [c[0] for ln, c in zip(self.lines, self.cands) if c and c[0]["r"] == "textbox"]
        self.assertEqual([(r["n"], r["i"], r["c"]) for r in tb], [("病歷號", 0, 1), (None, 1, 3), (None, 2, 3)])

    def test_which_lines_get_refs(self):
        tagged = [ln.strip() for ln, c in zip(self.lines, self.cands) if c]
        self.assertIn('- heading "Orders" [level=1]', tagged)
        self.assertNotIn("- heading [level=2]", tagged)           # unnamed content node: no ref
        roles = {ec.parse_aria_line(t)["role"] for t in tagged}
        self.assertFalse(roles & {"table", "row", "rowgroup", "list", "listitem", "paragraph", "text"}, roles)
        self.assertIn('- link "下一頁":', tagged)

    def test_annotate_inserts_before_the_colon_and_inside_quotes(self):
        n = iter(range(1, 100))
        out = ec.annotate_refs(self.lines, {"p": ""}, lambda rec: f"e{next(n)}")
        txt = "\n".join(out)
        self.assertIn('- textbox "病歷號" [ref=', txt)
        self.assertRegex(txt, r'textbox "病歷號" \[ref=e\d+\]: A123')
        self.assertRegex(txt, r"- 'button \"a: b\" \[ref=e\d+\]'")
        self.assertRegex(txt, r'- link "下一頁" \[ref=e\d+\]:\n  - /url')
        self.assertEqual(out[1], "- table:")


class AllocatorTest(unittest.TestCase):
    def test_ids_are_stable_session_wide_and_reset_per_url(self):
        store = {"next": 1, "tabs": {}}
        a = ec.ref_allocator(store, "T1", "https://x/1")
        r = {"p": "", "r": "button", "n": "OK", "i": 0, "c": 1}
        self.assertEqual(a(dict(r)), "e1")
        self.assertEqual(a(dict(r)), "e1")                            # same element, same id
        self.assertEqual(a({**r, "c": 2}), "e2")                      # count changed: a new identity
        b = ec.ref_allocator(store, "T2", "https://x/1")
        self.assertEqual(b(dict(r)), "e3")                            # other tab: never the same id
        a2 = ec.ref_allocator(store, "T1", "https://x/1")
        self.assertEqual(a2(dict(r)), "e1")                           # a later snapshot, same page
        a3 = ec.ref_allocator(store, "T1", "https://x/2")
        self.assertEqual(a3(dict(r)), "e4")                           # another URL: old refs dropped, no reuse
        self.assertEqual(set(store["tabs"]["T1"]["refs"]), {"e4"})

    def test_cap(self):
        store = {"next": 1, "tabs": {}}
        a = ec.ref_allocator(store, "T", "u", cap=2)
        ids = [a({"r": "button", "n": str(i), "i": 0, "c": 1}) for i in range(4)]
        self.assertEqual(ids, ["e1", "e2", None, None])
        self.assertTrue(a.full)

    def test_prune_closed_tabs_keeps_the_counter(self):
        store = {"next": 9, "tabs": {"A": {"u": "x", "refs": {"e1": {}}}, "B": {"u": "y", "refs": {"e2": {}}}}}
        ec.prune_ref_store(store, {"B"})
        self.assertEqual((list(store["tabs"]), store["next"]), (["B"], 9))

    def test_locator(self):
        self.assertEqual(role_sel("button", "OK"), 'internal:role=button[name="OK"s]')
        self.assertEqual(role_sel("textbox"), "internal:role=textbox")
        self.assertEqual(role_sel("button", 'say "hi" \\', prefix="#w >> internal:control=enter-frame >> ",
                                  scope="#form"),
                         '#w >> internal:control=enter-frame >> #form >> nth=0 >> '
                         'internal:role=button[name="say \\"hi\\" \\\\"s]')


class DeltaTest(unittest.TestCase):
    def test_unchanged_diff_full(self):
        old = "\n".join(f"- line {i}" for i in range(20))
        self.assertEqual(ec.snapshot_delta(old, old), ("unchanged", ""))
        new = old.replace("- line 7", "- line 7 changed")
        kind, d = ec.snapshot_delta(old, new)
        self.assertEqual(kind, "diff")
        self.assertIn("-- line 7", d)
        self.assertIn("+- line 7 changed", d)
        self.assertNotIn("---", d.splitlines()[0])                    # no file headers
        self.assertEqual(ec.snapshot_delta(old, "\n".join(f"- other {i}" for i in range(20)))[0], "full")

    def test_key_ignores_paging(self):
        self.assertEqual(ec.delta_key({"refs": True, "max_chars": 5, "offset": 9, "delta": True}),
                         ec.delta_key({"refs": True}))
        self.assertNotEqual(ec.delta_key({"refs": True}), ec.delta_key({"refs": True, "query": "x"}))


class RecipeHintTest(unittest.TestCase):
    def test_listing(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d) / "a.example"
            h.mkdir()
            for i in range(12):
                (h / f"r{i:02d}.json").write_text("{}", encoding="utf-8")
            (h / "notes.txt").write_text("x", encoding="utf-8")
            got = ec.saved_recipes("https://a.example/path?q=1", root=d)
            self.assertEqual(len(got), ec.MAX_RECIPE_HINTS)
            self.assertEqual(got[0], "a.example/r00")
            self.assertEqual(ec.saved_recipes("https://b.example/", root=d), [])
            self.assertEqual(ec.saved_recipes("about:blank", root=d), [])
            self.assertEqual(ec.saved_recipes(None, root=d), [])


class RefActionTest(_FakeBrowserCase):
    """snapshot {refs} -> click / fill / select / frame with @eN, through wb() and the session store."""

    def setUp(self):
        super().setUp()
        self.nav("https://a.example/orders")
        self.page = self.ctx.pages[-1]
        self.page.aria["body"] = TREE
        for sel in (role_sel("button", "刪除"), role_sel("textbox"), role_sel("textbox", "病歷號")):
            self.page.dom[sel] = {"count": 3 if "textbox" == sel[-7:] else (2 if "刪除" in sel else 1)}
        for k in (0, 1, 2):
            self.page.dom[role_sel("button", "刪除", k)] = {"text": "刪除"}
            self.page.dom[role_sel("textbox", None, k)] = {}
        self.page.dom[role_sel("textbox", "病歷號", 0)] = {}

    def snap(self, **a):
        return ec.wb("snapshot", {"refs": True, **a}, session="s1")

    def ref_of(self, tree, needle, k=0):
        import re
        return [re.search(r"\[ref=(e\d+)\]", ln).group(1) for ln in tree.splitlines()
                if needle in ln and "[ref=" in ln][k]

    def test_click_second_duplicate_and_fill_unnamed(self):
        t = self.snap()["tree_text"]
        r = ec.wb("click", {"selector": "@" + self.ref_of(t, 'button "刪除"', 1)}, session="s1")
        self.assertTrue(r["success"])
        self.assertNotIn("warning", r)
        self.assertEqual(self.page.clicked[-1], role_sel("button", "刪除", 1))
        ec.wb("fill", {"selector": self.ref_of(t, "- textbox [ref", 1), "value": "x"}, session="s1")
        self.assertEqual(self.page.values[role_sel("textbox", None, 2)], "x")

    def test_refs_survive_a_new_process(self):
        t = self.snap()["tree_text"]
        rid = self.ref_of(t, 'button "刪除"', 0)
        stored = json.loads(ec._refs_path("s1").read_text(encoding="utf-8"))
        self.assertEqual(stored["tabs"][self.page.tid]["u"], "https://a.example/orders")
        self.assertEqual(stored["tabs"][self.page.tid]["refs"][rid], {"r": "button", "n": "刪除", "i": 0, "c": 2})
        ec._tls.tid = {}                       # nothing cached in memory: the file is the only source
        ec.wb("click", {"selector": rid}, session="s1")
        self.assertEqual(self.page.clicked[-1], role_sel("button", "刪除", 0))

    def test_same_snapshot_twice_same_ids(self):
        self.assertEqual(self.snap()["tree_text"], self.snap()["tree_text"])

    def test_stale_after_navigation_and_count_change(self):
        t = self.snap()["tree_text"]
        rid = self.ref_of(t, 'button "刪除"', 1)
        self.page.dom[role_sel("button", "刪除")]["count"] = 3                   # a row was added
        with self.assertRaisesRegex(ec.WbError, r"stale ref @%s: page changed .*\(3 elements now match" % rid):
            ec.wb("click", {"selector": "@" + rid}, session="s1")
        self.page.dom[role_sel("button", "刪除")]["count"] = 2
        self.nav("https://a.example/other")
        with self.assertRaisesRegex(ec.WbError, "stale ref .*now at https://a.example/other"):
            ec.wb("click", {"selector": "@" + rid}, session="s1")
        self.assertEqual(self.page.clicked, [])
        self.page.aria["body"] = '- button "OK"'
        self.snap()                                                           # new page: old ids dropped
        with self.assertRaisesRegex(ec.WbError, "stale ref"):
            ec.wb("click", {"selector": "@" + rid}, session="s1")

    def test_unknown_ref_and_ref_from_another_tab(self):
        with self.assertRaisesRegex(ec.WbError, "unknown ref @e999"):
            ec.wb("click", {"selector": "@e999"}, session="s1")
        rid = self.ref_of(self.snap()["tree_text"], "Orders")
        self.nav("https://a.example/orders", newTab=True)
        with self.assertRaisesRegex(ec.WbError, r"stale ref .*from tab t1.*attach_tab"):
            ec.wb("click", {"selector": "@" + rid}, session="s1")

    def test_query_does_not_match_the_annotation(self):
        r = self.snap(query="ref=")
        self.assertEqual(r["tree_text"], "")
        r = self.snap(query="病歷號", interactive_only=True)
        self.assertRegex(r["tree_text"], r'^\s*- textbox "病歷號" \[ref=e\d+\]: A123$')

    def test_close_session_forgets_refs(self):
        self.snap(delta=True)
        self.assertTrue(ec._refs_path("s1").exists())
        ec.wb("close_session", {}, session="s1")
        self.assertFalse(ec._refs_path("s1").exists())
        self.assertFalse(ec._refs_path("s1", ".trees").exists())

    def test_ref_file_name_is_safe_and_next_to_the_sessions_file(self):
        p = ec._refs_path("a/b:c")
        self.assertEqual(p.parent, ec.SESSIONS_FILE.parent / "sessions.refs")
        self.assertNotIn("/", p.name)
        self.assertNotIn(":", p.name)
        with mock.patch.object(ec, "SESSIONS_FILE", Path.home() / ".my-agent-sessions.json"):
            self.assertEqual(ec.refs_dir(), Path.home() / ".my-agent-sessions.refs")   # not ~/refs

    def test_frame_ref(self):
        rec = {"k": "frame", "p": "#work >> internal:control=enter-frame >> "}
        store = {"next": 1, "tabs": {self.page.tid: {"u": self.page.url, "refs": {"e1": rec}}}}
        ec._write_json(ec._refs_path("s1"), store)
        with self.assertRaisesRegex(ec.WbError, "@e1 is a frame"):
            ec.wb("click", {"selector": "@e1"}, session="s1")
        with self.assertRaisesRegex(ec.WbError, "no frame element matches '#work'"):   # the fake has no frames
            ec.wb("read", {"frame": "@e1"}, session="s1")
        self.page.dom[role_sel("textbox", "病歷號")] = {}
        with self.assertRaisesRegex(ec.WbError, "is not a frame"):
            ec.wb("read", {"frame": "@" + self.ref_of(self.snap()["tree_text"], "病歷號")}, session="s1")


class MultiMatchTest(_FakeBrowserCase):
    def test_warning_only_for_more_than_one(self):
        self.nav()
        p = self.ctx.pages[-1]
        p.dom[".del"] = {"count": 3, "text": "刪除"}
        p.dom["#one"] = {"text": "x"}
        r = ec.wb("click", {"selector": ".del"}, session="s1")
        self.assertEqual(r["matches"], 3)
        self.assertIn("acted on the first", r["warning"])
        self.assertNotIn("matches", ec.wb("click", {"selector": "#one"}, session="s1"))
        r = ec.wb("fill", {"selector": ".del", "value": "v"}, session="s1")
        self.assertEqual(r["matches"], 3)


class ThenSnapshotDeltaTest(_FakeBrowserCase):
    def test_then_snapshot_default_and_object(self):
        self.nav()
        p = self.ctx.pages[-1]
        p.dom["#go"] = {"text": "go"}
        p.aria["body"] = '- heading "H" [level=1]\n- button "go"\n- text: x'
        r = ec.wb("click", {"selector": "#go", "then_snapshot": True}, session="s1")
        self.assertRegex(r["snapshot"]["tree_text"], r'^- button "go" \[ref=e\d+\]$')   # interactive_only
        r = ec.wb("click", {"selector": "#go", "then_snapshot": {"query": "x"}}, session="s1")
        self.assertEqual(r["snapshot"]["tree_text"], "- text: x")
        r = ec.wb("navigate", {"url": "https://a.example/2", "then_snapshot": True}, session="s1")
        self.assertIn("[ref=", r["snapshot"]["tree_text"])
        r = ec.wb("batch", {"steps": [{"action": "click", "args": {"selector": "#go"}}], "then_snapshot": True},
                  session="s1")
        self.assertTrue(r["ok"])
        self.assertIn("[ref=", r["snapshot"]["tree_text"])
        r = ec.wb("click", {"selector": "#go", "then_snapshot": "yes"}, session="s1")
        self.assertIn("error", r["snapshot"])

    def test_then_snapshot_failure_does_not_fail_the_action(self):
        self.nav()
        p = self.ctx.pages[-1]
        p.dom["#go"] = {}
        with mock.patch.object(ec, "_snapshot", side_effect=ec.WbError("boom")):
            r = ec.wb("click", {"selector": "#go", "then_snapshot": True}, session="s1")
        self.assertTrue(r["success"])
        self.assertEqual(r["snapshot"], {"error": "boom"})

    def test_delta_unchanged_diff_full_and_per_args(self):
        self.nav()
        p = self.ctx.pages[-1]
        p.aria["body"] = "\n".join(f'- button "b{i}"' for i in range(10))
        first = ec.wb("snapshot", {"delta": True}, session="s1")
        self.assertEqual(first["delta"], "full")
        self.assertIn("tree_text", first)
        r = ec.wb("snapshot", {"delta": True}, session="s1")
        self.assertTrue(r["unchanged"])
        self.assertNotIn("tree_text", r)
        p.aria["body"] = p.aria["body"].replace('"b3"', '"b3x"')
        r = ec.wb("snapshot", {"delta": True}, session="s1")
        self.assertEqual(r["delta"], "diff")
        self.assertIn('+- button "b3x"', r["diff"])
        r = ec.wb("snapshot", {"delta": True, "query": "b3"}, session="s1")           # other args: own base
        self.assertEqual(r["delta"], "full")
        p.aria["body"] = "\n".join(f'- link "z{i}"' for i in range(10))
        self.assertEqual(ec.wb("snapshot", {"delta": True}, session="s1")["delta"], "full")
        self.nav("https://a.example/elsewhere")                                       # other URL: no base
        self.assertEqual(ec.wb("snapshot", {"delta": True}, session="s1")["delta"], "full")

    def test_refs_snapshot_is_a_delta_base_with_stable_ids(self):
        self.nav()
        p = self.ctx.pages[-1]
        p.aria["body"] = "\n".join(f'- button "b{i}"' for i in range(10))
        ec.wb("snapshot", {"refs": True}, session="s1")
        r = ec.wb("snapshot", {"refs": True, "delta": True}, session="s1")
        self.assertTrue(r.get("unchanged"))
        p.aria["body"] += '\n- button "new"'
        r = ec.wb("snapshot", {"refs": True, "delta": True}, session="s1")
        self.assertEqual(r["delta"], "diff")
        self.assertEqual([ln for ln in r["diff"].splitlines() if ln[:1] in "+-"], ['+- button "new" [ref=e11]'])


class NavWatchTest(unittest.TestCase):
    """then_snapshot waits for a navigation the action started (e.g. a postback inside a frame)."""

    class Page:
        def __init__(self):
            self.handlers, self.ticks, self.script = {}, 0, []

        def on(self, ev, fn):
            self.handlers.setdefault(ev, []).append(fn)

        def remove_listener(self, ev, fn):
            self.handlers[ev].remove(fn)

        def emit(self, ev, arg):
            for fn in list(self.handlers.get(ev, [])):
                fn(arg)

        def wait_for_timeout(self, ms):
            self.ticks += 1
            if self.script:
                self.script.pop(0)()

    def test_waits_for_the_frame_that_asked(self):
        page, frame = self.Page(), mock.Mock()
        w = ec._NavWatch(page)
        req = mock.Mock(frame=frame, is_navigation_request=lambda: True)
        page.script = [lambda: page.emit("request", req), lambda: None, lambda: page.emit("framenavigated", frame)]
        self.assertEqual(w.settle(5000), 1)
        frame.wait_for_load_state.assert_called_once()
        self.assertEqual(page.ticks, 3)                          # stopped polling once it navigated
        self.assertFalse(any(page.handlers.values()))            # listeners removed

    def test_no_navigation_costs_one_short_wait(self):
        page = self.Page()
        self.assertEqual(ec._NavWatch(page).settle(5000), 0)
        self.assertEqual(page.ticks, 1)


class NavigateRecipesTest(_FakeBrowserCase):
    def test_navigate_lists_saved_recipes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.example").mkdir()
            (Path(d) / "a.example" / "search.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(ec, "RECIPES_DIR", Path(d)):
                self.assertEqual(self.nav("https://a.example/x")["recipes"], ["a.example/search"])
                self.assertNotIn("recipes", self.nav("https://b.example/x"))


if __name__ == "__main__":
    unittest.main()
