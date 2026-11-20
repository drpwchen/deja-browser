#!/usr/bin/env python3
"""Tests for `deja-browser draft` (draft.py + recipe.cmd_draft): no browser, no network.
Fixtures imitate the two JSON-API tasks of the head-to-head bench (a search API and a package-registry
API) and a page whose data is only in HTML.
run: python -m pytest -q"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import draft, recipe as sr

TITLES = ["Why Discord is switching from Go to Rust", "Leaving Rust gamedev after 3 years",
          "A half-hour to learn Rust"]


def _search_body(q="rust"):
    return {"hits": [{"title": t, "url": f"https://x.test/{i}", "points": 100 - i,
                      "_highlightResult": {"title": {"value": t.replace("Rust", "<em>Rust</em>")}}}
                     for i, t in enumerate(TITLES + ["Other story about Rust"])],
            "query": q, "nbHits": 4}


def _crate_body():
    return {"crate": {"id": "serde", "max_stable_version": "1.0.229", "max_version": "1.0.229",
                      "license_note": None},
            "versions": [{"num": "1.0.229", "license": "MIT OR Apache-2.0"},
                         {"num": "1.0.228", "license": "MIT OR Apache-2.0"}]}


class _Capture:
    """Builds a network_capture result: bodies are real files, like the browser writes them."""

    def __init__(self, tmp, page_url="https://app.test/?q=rust"):
        self.tmp, self.items, self.page_url = Path(tmp), [], page_url

    def add(self, url, body, method="GET", status=200, mime="application/json", headers=None, post=None, ext="json"):
        i = len(self.items)
        p = self.tmp / f"{i:03d}.{ext}"
        raw = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        p.write_bytes(raw)
        it = {"i": i, "method": method, "url": url, "status": status, "type": "fetch", "mime": mime,
              "req_headers": headers or {"accept": "application/json", "authorization": "<redacted len=40>"},
              "body_path": str(p), "body_bytes": len(raw)}
        if post is not None:
            it["post_data"], it["post_data_truncated"] = post, False
        self.items.append(it)
        return self

    def result(self):
        return {"url": self.page_url, "count": len(self.items), "body_dir": str(self.tmp), "requests": self.items}


class FindPathsTest(unittest.TestCase):
    def test_exact_and_substring_and_numbers(self):
        obj = {"a": {"b": "hello world"}, "n": 42, "l": ["x", "hello"]}
        got = dict(draft.find_paths(obj, "hello"))
        self.assertEqual(got, {("a", "b"): False, ("l", "1"): True})
        self.assertEqual(draft.find_paths(obj, "42"), [(("n",), True)])
        self.assertEqual(draft.find_paths(obj, "4"), [])        # a number matches only when its text is equal

    def test_keys_are_not_searched(self):
        self.assertEqual(draft.find_paths({"needle": 1}, "needle"), [])


class PickShapeTest(unittest.TestCase):
    def test_rows_of_one_list_become_path_and_fields(self):
        spec = draft.build_pick([("hits", "0", "title"), ("hits", "2", "title")])
        self.assertEqual(spec, {"path": "hits", "fields": ["title"], "limit": 20})

    def test_two_fields_of_a_row_and_a_deep_row_raise_the_limit(self):
        spec = draft.build_pick([("data", "items", "0", "name"), ("data", "items", "24", "id")])
        self.assertEqual(spec, {"path": "data.items", "fields": ["name", "id"], "limit": 25})

    def test_scalars_become_select(self):
        spec = draft.build_pick([("crate", "max_stable_version"), ("versions", "0", "license")])
        self.assertEqual(spec, {"select": {"max_stable_version": "crate.max_stable_version",
                                           "license": "versions.0.license"}})

    def test_different_lists_fall_back_to_select_and_names_stay_unique(self):
        spec = draft.build_pick([("a", "0", "name"), ("b", "0", "name")])
        self.assertEqual(spec, {"select": {"name": "a.0.name", "b_name": "b.0.name"}})

    def test_a_list_row_that_is_a_bare_string_is_not_a_field(self):
        spec = draft.build_pick([("tags", "1")])
        self.assertEqual(spec, {"select": {"tags": "tags.1"}})


class PickSelectTest(unittest.TestCase):
    def test_select_on_dict_and_on_list_rows(self):
        data = {"crate": {"v": "1"}, "versions": [{"num": "1", "x": 0}, {"num": "2", "x": 0}]}
        self.assertEqual(sr.pick(data, {"select": {"v": "crate.v", "n": "versions.1.num", "gone": "nope.0"}}),
                         {"v": "1", "n": "2", "gone": None})
        self.assertEqual(sr.pick(data, {"path": "versions", "select": {"n": "num"}, "limit": 1}), [{"n": "1"}])

    def test_select_result_has_a_usable_signature(self):
        out = sr.pick({"a": {"b": "x"}}, {"select": {"v": "a.b"}})
        self.assertEqual(sr.signature(out), {"v": "str"})


class RenderUrlPlaceholderTest(unittest.TestCase):
    def test_url_placeholder_is_percent_encoded_plain_is_not(self):
        self.assertEqual(sr._render("/s?q={q|url}&r={q}", {"q": "a b&c"}), "/s?q=a%20b%26c&r=a b&c")
        self.assertEqual(sr._render("/s?q={q|url}", {}), "/s?q={q|url}")     # unknown param left alone


class TemplateTest(unittest.TestCase):
    def test_query_value_and_path_segment(self):
        url, hits, dropped = draft.template_url(
            "https://h.test/api/v1/crates/serde?query=rust&tags=story&hitsPerPage=20", {"q": "rust", "c": "serde"})
        self.assertEqual(url, "https://h.test/api/v1/crates/{c|url}?query={q|url}&tags=story&hitsPerPage=20")
        self.assertEqual((hits, dropped), ({"c": 1, "q": 1}, []))

    def test_credential_looking_query_names_are_dropped_not_copied(self):
        url, _, dropped = draft.template_url(
            "https://h.test/s?q=a&x-api-key=SEKRET1&access_token=SEKRET2&sessionid=SEKRET3&keyword=ok&author=bob", {})
        self.assertEqual(url, "https://h.test/s?q=a&keyword=ok&author=bob")
        self.assertEqual(sorted(dropped), ["access_token", "sessionid", "x-api-key"])
        self.assertNotIn("SEKRET", url)

    def test_json_body(self):
        body, hits, dropped = draft.template_body(
            json.dumps({"query": "rust", "page": 0, "filters": {"lang": "rust"}, "authToken": "t0k", "api_key": "k"}),
            "application/json", {"q": "rust"})
        self.assertEqual(body, {"query": "{q}", "page": 0, "filters": {"lang": "{q}"}})
        self.assertEqual(hits, {"q": 2})
        self.assertEqual(sorted(dropped), ["api_key", "authToken"])

    def test_form_body_stays_a_string(self):
        body, hits, _ = draft.template_body("q=rust+lang&n=3", "application/x-www-form-urlencoded", {"q": "rust lang"})
        self.assertEqual(body, "q={q|url}&n=3")
        self.assertEqual(hits, {"q": 1})


class BuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _search(self, **kw):
        cap = _Capture(self.tmp)
        cap.add("https://app.test/api/v1/ping", {"ok": True})
        cap.add("https://app.test/api/v1/search?query=rust&tags=story&hitsPerPage=20&api_key=SEKRET", _search_body(),
                headers={"accept": "application/json", "x-api-key": "<redacted len=20>", "cookie": "<redacted len=99>"})
        return draft.build(cap.result(), TITLES, {"q": "rust"}, **kw)

    def test_t1_search_api_draft(self):
        rep = self._search()
        self.assertTrue(rep["applicable"])
        d = rep["draft"]
        self.assertEqual(d["request"], {"method": "GET", "headers": {"accept": "application/json"},
                                        "url": "https://app.test/api/v1/search?query={q|url}&tags=story&hitsPerPage=20"})
        self.assertEqual(d["pick"], {"path": "hits", "fields": ["title"], "limit": 20})
        self.assertEqual(d["entry_url"], "https://app.test/")        # the page's query string is not kept
        self.assertEqual(d["params"][0]["name"], "q")
        self.assertFalse(d["writes"])
        self.assertEqual(rep["matched"][TITLES[0]], "hits.0.title")
        self.assertTrue(any("api_key" in w for w in rep["warnings"]))
        self.assertTrue(any("x-api-key" in n and "cookie" in n for n in d["notes"]))
        blob = json.dumps(rep)
        self.assertNotIn("SEKRET", blob)
        self.assertNotIn("redacted", json.dumps(d["request"]))

    def test_t4_package_api_draft_uses_select(self):
        cap = _Capture(self.tmp, "https://reg.test/crates/serde")
        cap.add("https://reg.test/api/v1/crates/serde/owners", {"users": [{"login": "dtolnay"}]})
        cap.add("https://reg.test/api/v1/crates/serde", _crate_body())
        rep = draft.build(cap.result(), ["1.0.229", "MIT OR Apache-2.0"], {"crate": "serde"})
        self.assertTrue(rep["applicable"])
        d = rep["draft"]
        self.assertEqual(d["request"]["url"], "https://reg.test/api/v1/crates/{crate|url}")
        sel = d["pick"]["select"]
        self.assertEqual(sel["license"], "versions.0.license")
        self.assertEqual(sel["max_stable_version"], "crate.max_stable_version")
        self.assertEqual(sr.pick(_crate_body(), d["pick"])["license"], "MIT OR Apache-2.0")
        self.assertEqual(d["name"], "crates")

    def test_expect_picks_the_response_not_the_first_json(self):
        cap = _Capture(self.tmp)
        cap.add("https://app.test/api/config", {"flags": {"a": True}})
        cap.add("https://app.test/api/related?q=rust", {"hits": [{"title": TITLES[0]}]})    # only one expect
        cap.add("https://app.test/api/search?q=rust", _search_body())
        rep = draft.build(cap.result(), TITLES, {"q": "rust"})
        self.assertEqual(rep["picked"]["i"], 2)

    def test_params_decide_between_equal_candidates(self):
        cap = _Capture(self.tmp)
        cap.add("https://app.test/api/static", _search_body())
        cap.add("https://app.test/api/search?q=rust", _search_body())
        rep = draft.build(cap.result(), TITLES, {"q": "rust"})
        self.assertEqual(rep["picked"]["i"], 1)
        self.assertEqual(rep["alternatives"][0]["url"], "https://app.test/api/static")

    def test_ambiguous_expect_is_flagged_and_list_indexes_sort_numerically(self):
        cap = _Capture(self.tmp)
        cap.add("https://reg.test/api/c", _crate_body())
        rep = draft.build(cap.result(), ["1.0.229"])
        self.assertTrue(any("also an exact value at" in w for w in rep["warnings"]))
        rows = {"hits": [{"title": f"row {i}"} for i in range(20)] + [{"title": "needle row"}] * 0}
        rows["hits"][2]["title"] = rows["hits"][17]["title"] = "needle row"
        got = draft.build(_Capture(self.tmp).add("https://reg.test/api/r", rows).result(), ["needle row"])
        self.assertEqual(got["matched"]["needle row"], "hits.2.title")     # not hits.17

    def test_keep_param_keeps_a_named_credential_looking_field(self):
        cap = _Capture(self.tmp).add("https://app.test/q?q=rust&x-api-key=PUBKEY", _search_body())
        rep = draft.build(cap.result(), TITLES, {"q": "rust"}, keep=["x-api-key"])
        self.assertIn("x-api-key=PUBKEY", rep["draft"]["request"]["url"])
        self.assertTrue(any("kept credential-looking" in w for w in rep["warnings"]))
        self.assertFalse(any("dropped" in w for w in rep["warnings"]))

    def test_query_string_inside_a_json_body_is_templated(self):
        cap = _Capture(self.tmp).add("https://app.test/1/query", _search_body(), method="POST",
                                     headers={"content-type": "application/json"},
                                     post=json.dumps({"params": "query=rust&hitsPerPage=30", "n": 1}))
        rep = draft.build(cap.result(), TITLES, {"q": "rust"})
        self.assertEqual(rep["draft"]["request"]["body"], {"params": "query={q|url}&hitsPerPage=30", "n": 1})

    def test_unplaced_param_is_a_warning(self):
        rep = draft.build(_Capture(self.tmp).add("https://app.test/api/s?q=rust", _search_body()).result(),
                          TITLES, {"term": "python"})
        self.assertTrue(any("term" in w and "not in the request" in w for w in rep["warnings"]))
        partial = draft.build(_Capture(self.tmp).add("https://app.test/api/s?q=rust-lang", _search_body()).result(),
                              TITLES, {"term": "rust"})
        self.assertTrue(any("longer value" in w for w in partial["warnings"]))

    def test_post_gets_writes_true_and_a_templated_body(self):
        cap = _Capture(self.tmp)
        cap.add("https://app.test/graphql", _search_body(), method="POST",
                headers={"content-type": "application/json", "authorization": "<redacted len=9>"},
                post=json.dumps({"query": "rust", "token": "T"}))
        rep = draft.build(cap.result(), TITLES, {"q": "rust"})
        d = rep["draft"]
        self.assertEqual(d["request"]["body"], {"query": "{q}"})
        self.assertEqual(d["request"]["headers"], {"content-type": "application/json"})
        self.assertTrue(d["writes"])
        self.assertTrue(any("method is POST" in w for w in rep["warnings"]))

    def test_cut_off_post_body_is_skipped(self):
        cap = _Capture(self.tmp)
        cap.add("https://app.test/graphql", _search_body(), method="POST", post="{}")
        cap.items[0]["post_data_truncated"] = True
        rep = draft.build(cap.result(), TITLES)
        self.assertFalse(rep["applicable"])
        self.assertEqual(rep["skipped"][0]["why"], "captured request body was cut off")

    def test_refused_host_is_skipped(self):
        cap = _Capture(self.tmp).add("https://bank.test/api", _search_body())
        rep = draft.build(cap.result(), TITLES, host_ok=lambda u: "bank" not in u)
        self.assertFalse(rep["applicable"])
        self.assertEqual(rep["skipped"][0]["why"], "refused host")

    def test_no_json_api_is_not_applicable_and_says_where_the_text_is(self):
        cap = _Capture(self.tmp)
        cap.add("https://wiki.test/api/rest_v1/page/html/Foo", "<table><td>55.845</td></table>", mime="text/html", ext="txt")
        cap.add("https://wiki.test/api/stats", {"views": 1})
        rep = draft.build(cap.result(), ["55.845"])
        self.assertFalse(rep["applicable"])
        self.assertNotIn("draft", rep)
        self.assertEqual(rep["found_in_non_json"], ["https://wiki.test/api/rest_v1/page/html/Foo"])
        self.assertIn("script", rep["suggest"])
        self.assertEqual(rep["checked"], {"requests": 2, "json_bodies": 1})

    def test_one_expect_missing_from_every_response_is_not_applicable(self):
        cap = _Capture(self.tmp).add("https://app.test/api/s", _search_body())
        self.assertFalse(draft.build(cap.result(), [TITLES[0], "no such text anywhere"])["applicable"])

    def test_non_2xx_and_missing_bodies_are_ignored(self):
        cap = _Capture(self.tmp).add("https://app.test/api/s", _search_body(), status=403)
        cap.add("https://app.test/api/t", _search_body())
        cap.items[1]["body_path"] = str(self.tmp / "gone.json")
        self.assertFalse(draft.build(cap.result(), TITLES)["applicable"])

    def test_bad_input_raises(self):
        with self.assertRaises(draft.DraftError):
            draft.build({"requests": []}, [])
        with self.assertRaises(draft.DraftError):
            draft.build({"requests": []}, ["ab"])
        with self.assertRaises(draft.DraftError):
            draft.build({"requests": []}, ["abcd"], {"1bad": "x"})
        with self.assertRaises(draft.DraftError):
            draft.build({"nope": 1}, ["abcd"])


class CmdDraftTest(unittest.TestCase):
    """The CLI path, and the promised end state: the draft goes straight through `verify`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _capture_file(self, items=None):
        cap = _Capture(self.tmp)
        cap.add("https://app.test/api/v1/search?query=rust&tags=story", _search_body())
        f = self.tmp / "cap.json"
        f.write_text(json.dumps(cap.result()), encoding="utf-8")
        return f

    def _args(self, f, *extra):
        a = [str(f)]
        for t in TITLES:
            a += ["--expect", t]
        return a + ["--param", "q=rust", *extra]

    def test_draft_then_verify_passes_with_new_values(self):
        f = self._capture_file()
        rep = sr.cmd_draft(self._args(f))
        self.assertTrue(rep["applicable"])
        out = Path(rep["draft_path"])
        self.assertEqual(out, f.with_suffix(".draft.json"))
        self.assertIn("verify", rep["next"])
        self.assertIn('"q": "rust"', rep["next"])
        seen = []

        def http(recipe, args, session):
            req = sr._render(recipe["request"], args)
            seen.append(req["url"])
            return 200, _search_body(args["q"])
        with mock.patch.dict(sr.TIERS, {"http": http, "page": lambda *a: (500, "x")}), \
                mock.patch.object(sr, "_page_chars", return_value=None):
            v = sr.cmd_verify(str(out), {"q": "rust lang"})
        self.assertEqual(v["cheapest"], "http")
        self.assertEqual(seen[0], "https://app.test/api/v1/search?query=rust%20lang&tags=story")
        self.assertEqual(v["results"][0]["preview"][0], {"title": TITLES[0]})
        # and the verified draft saves
        with mock.patch.object(sr, "RECIPES", self.tmp / "store"):
            self.assertEqual(sr.cmd_save(str(out))["saved"], "app.test/search")

    def test_existing_draft_needs_force(self):
        f = self._capture_file()
        sr.cmd_draft(self._args(f))
        with self.assertRaises(sr.RecipeError):
            sr.cmd_draft(self._args(f))
        self.assertTrue(sr.cmd_draft(self._args(f, "--force"))["applicable"])
        other = self.tmp / "named.json"
        self.assertEqual(sr.cmd_draft(self._args(f, "--out", str(other), "--name", "hn-search"))["draft_path"], str(other))
        self.assertEqual(json.loads(other.read_text(encoding="utf-8"))["name"], "hn-search")

    def test_not_applicable_writes_nothing_and_main_exits_2(self):
        cap = _Capture(self.tmp).add("https://app.test/api/s", {"a": 1})
        f = self.tmp / "cap.json"
        f.write_text(json.dumps(cap.result()), encoding="utf-8")
        with mock.patch("builtins.print") as pr:
            rc = sr.main(["draft", str(f), "--expect", "needle text"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(pr.call_args[0][0])["applicable"], False)
        self.assertFalse(f.with_suffix(".draft.json").exists())

    def test_main_applicable_exit_0_and_errors_are_json(self):
        f = self._capture_file()
        with mock.patch("builtins.print") as pr:
            self.assertEqual(sr.main(["draft", *self._args(f)]), 0)
            self.assertTrue(json.loads(pr.call_args[0][0])["applicable"])
            self.assertEqual(sr.main(["draft", str(self.tmp / "missing.json"), "--expect", "abcd"]), 1)
            self.assertFalse(json.loads(pr.call_args[0][0])["ok"])
            self.assertEqual(sr.main(["draft", str(f), "--expect", "ab"]), 1)
            self.assertEqual(sr.main(["draft", str(f), "--expect", "abcd", "--param", "novalue"]), 1)

    def test_refused_entry_host_is_not_drafted(self):
        cap = _Capture(self.tmp, "https://www.paypal.com/x").add("https://app.test/api/s", _search_body())
        f = self.tmp / "cap.json"
        f.write_text(json.dumps(cap.result()), encoding="utf-8")
        with self.assertRaises(sr.RecipeError):
            sr.cmd_draft([str(f), "--expect", TITLES[0]])
        self.assertFalse(f.with_suffix(".draft.json").exists())


if __name__ == "__main__":
    unittest.main()
