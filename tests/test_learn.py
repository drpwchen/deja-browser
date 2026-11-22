#!/usr/bin/env python3
"""Passive recording (recording.py, the daemon hooks) and `deja-browser learn` (learn.py + recipe.cmd_learn).
No browser, no network: recordings are built by hand (bodies are real files, like `recording {body_dir}`
writes them); the daemon client and the tier runs are mocked.
run: python -m pytest -q"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import browser as B
from deja_browser import client, daemon, draft, learn, recipe as sr, recording as R

TITLES = ["Why Discord is switching from Go to Rust", "Leaving Rust gamedev after 3 years"]


class _Rec:
    """A recording in the schema of recording.py, bodies written to a temp dir."""

    def __init__(self, tmp, url="https://app.test/?q=rust"):
        self.tmp, self.url = Path(tmp), url
        self.responses, self.commands = [], []

    def resp(self, url, body, method="GET", status=200, mime="application/json", page_url=None, post=None,
             rtype="xhr", headers=None):
        i = len(self.responses)
        p = self.tmp / f"{i:04d}.{'json' if 'json' in mime else 'txt'}"
        raw = (json.dumps(body) if not isinstance(body, str) else body).encode()
        p.write_bytes(raw)
        it = {"i": i, "method": method, "url": url, "status": status, "type": rtype, "mime": mime,
              "page_url": page_url or self.url, "req_headers": headers or {}, "body_path": str(p),
              "body_bytes": len(raw)}
        if post is not None:
            it["post_data"] = post
        self.responses.append(it)
        return it

    def cmd(self, action, args, result=None, url_before=None, url_after=None, ok=True):
        c = {"n": len(self.commands) + 1, "action": action, "args": args, "ok": ok,
             "url_before": url_before, "url_after": url_after}
        if result is not None:
            c["result"] = result
        self.commands.append(c)
        return c

    def data(self):
        return {"version": 1, "session": "s", "url": self.url, "responses": self.responses,
                "commands": self.commands}


def _hits(q="rust"):
    return {"hits": [{"title": t, "objectID": str(10045 + i)} for i, t in enumerate(TITLES)], "query": q}


# ---------- recording.py ----------

class ResponseRuleTest(unittest.TestCase):
    def test_site_and_same_site(self):
        self.assertEqual(R.site("hn.algolia.com"), "algolia.com")
        self.assertEqual(R.site("www.bbc.co.uk"), "bbc.co.uk")
        self.assertEqual(R.site("shop.example.com.tw"), "example.com.tw")
        self.assertEqual(R.site("127.0.0.1"), "127.0.0.1")
        self.assertTrue(R.same_site("https://crates.io/crates/serde", "https://crates.io/api/v1/crates/serde"))
        self.assertFalse(R.same_site("https://hn.algolia.com/", "https://uj5wyc0l7x-dsn.algolia.net/1/x"))

    def test_keep_response(self):
        page = "https://hn.algolia.com/?q=rust"
        self.assertTrue(R.keep_response(page, "https://uj5wyc0l7x-dsn.algolia.net/1/q", "xhr", "application/json"))
        self.assertFalse(R.keep_response(page, "https://tracker.example/pixel", "xhr", "image/gif"))
        self.assertTrue(R.keep_response(page, "https://hn.algolia.com/config", "fetch", "text/plain"))
        self.assertTrue(R.keep_response(page, page, "document", "text/html"))
        self.assertFalse(R.keep_response(page, "https://ads.example/frame", "document", "text/html", main_frame=False))
        self.assertFalse(R.keep_response(page, "https://hn.algolia.com/app.js", "script", "text/javascript"))
        self.assertFalse(R.keep_response(page, "https://mail.google.com/sync", "xhr", "application/json"))
        self.assertFalse(R.keep_response("https://mail.google.com/", "https://mail.google.com/x", "document", "text/html"))
        self.assertFalse(R.keep_response(page, "data:application/json,{}", "fetch", "application/json"))

    def test_headers_are_redacted_exactly_like_network_capture(self):
        h = {"Authorization": "Bearer abc", "X-CSRF-Token": "t", "Accept": "application/json", "x-api-key": "k"}
        self.assertEqual(R.safe_headers(h), B._safe_headers(h))
        self.assertEqual(R.safe_headers(h)["Authorization"], "<redacted len=10>")


class RecordingStoreTest(unittest.TestCase):
    def test_ring_buffer_and_byte_budget(self):
        rec = R.Recording("s")
        for i in range(R.MAX_RESPONSES + 5):
            rec.add_response({"url": f"https://a.test/{i}", "mime": "application/json"}, b"{}")
        snap = rec.snapshot()
        self.assertEqual(len(snap["responses"]), R.MAX_RESPONSES)
        self.assertEqual(snap["responses"][0]["url"], "https://a.test/5")
        self.assertEqual(snap["dropped"]["responses"], 5)
        with mock.patch.object(R, "MAX_SESSION_BYTES", 10):
            rec2 = R.Recording("s")
            rec2.add_response({"url": "u1", "mime": "text/plain"}, b"123456")
            rec2.add_response({"url": "u2", "mime": "text/plain"}, b"123456")
            self.assertEqual([r["url"] for r in rec2.snapshot()["responses"]], ["u2"])

    def test_too_big_body_is_not_kept_and_bodies_go_to_body_dir(self):
        rec = R.Recording("s")
        with mock.patch.object(R, "MAX_BODY", 4):
            rec.add_response({"url": "https://a.test/", "mime": "text/html"}, b"<html>")
        rec.add_response({"url": "https://a.test/api", "mime": "application/json"}, b'{"a": 1}')
        with tempfile.TemporaryDirectory() as d:
            snap = rec.snapshot(d)
            big, js = snap["responses"]
            self.assertEqual((big["body_error"], big["body_bytes"]), ("too big", 6))
            self.assertNotIn("body_path", big)
            self.assertEqual(json.loads(Path(js["body_path"]).read_text()), {"a": 1})
        self.assertNotIn("body_path", rec.snapshot()["responses"][1])       # no body_dir: sizes only

    def test_store_lru_and_clear(self):
        st = R.Store()
        with mock.patch.object(R, "MAX_SESSIONS", 2):
            a = st.get("a")
            st.get("b")
            a.used += 100                    # a used most recently
            st.get("c")
        self.assertIsNotNone(st.get("a", create=False))
        self.assertIsNone(st.get("b", create=False))
        self.assertTrue(st.clear("a"))
        self.assertFalse(st.clear("a"))


class CommandSchemaTest(unittest.TestCase):
    def test_fill_values_redacted_for_secret_targets(self):
        self.assertEqual(R.clean_args("fill", {"selector": "#q", "value": "rust"}), {"selector": "#q", "value": "rust"})
        self.assertEqual(R.clean_args("fill", {"selector": "#password", "value": "hunter2"})["value"], "<redacted>")
        a = R.clean_args("fill", {"selector": "#f3", "value": "hunter2"}, secret_target=True)
        self.assertEqual((a["value"], a["redacted"]), ("<redacted>", True))
        self.assertEqual(R.clean_args("captcha_text", {"image": "#i", "fields": {"#pw": "x"}})["fields"],
                         {"#pw": "<redacted>"})

    def test_code_and_long_values_are_cut(self):
        a = R.clean_args("evaluate", {"code": "x" * (R.MAX_CODE + 1)})
        self.assertEqual((len(a["code"]), a["code_truncated"]), (R.MAX_CODE, True))
        self.assertEqual(len(R.clean_args("navigate", {"url": "u" * 900})["url"]), R.MAX_VALUE)

    def test_result_or_preview(self):
        small = R.command(1, "evaluate", {"code": "1"}, True, data={"type": "number", "value": 42})
        self.assertEqual(small["result"], 42)
        big = R.command(2, "evaluate", {"code": "1"}, True, data={"type": "string", "value": "y" * 5000})
        self.assertNotIn("result", big)
        self.assertEqual((len(big["result_preview"]), big["result_chars"]), (R.MAX_PREVIEW, 5002))
        fail = R.command(3, "click", {"selector": "#x"}, False, error="no element")
        self.assertEqual((fail["ok"], fail["error"]), (False, "no element"))

    def test_batch_is_unpacked(self):
        args = {"steps": [{"action": "navigate", "args": {"url": "https://a.test/"}},
                          {"action": "fill", "args": {"selector": "#q", "value": "rust"}},
                          {"action": "click", "args": {"selector": "#go"}}]}
        rep = {"ok": True, "data": {"ok": False, "results": [
            {"action": "navigate", "ok": True, "data": {"url": "https://a.test/"}},
            {"action": "fill", "ok": False, "error": "fill: nope"},
            {"action": "click", "ok": False, "skipped": True}]}}
        rows = R.expand("batch", args, rep)
        self.assertEqual([(a, ok) for a, _, ok, _, _ in rows], [("navigate", True), ("fill", False)])
        self.assertEqual(rows[1][3], "fill: nope")


class DaemonRecordingTest(unittest.TestCase):
    """The worker records each action (browser.wb mocked, no browser: fills count as secret targets)."""

    def setUp(self):
        self.d = daemon.Daemon("k", "tok", "h")

    def tearDown(self):
        self.d.shutdown()

    def ask(self, action, args=None, session="s"):
        return self.d.handle({"token": "tok", "action": action, "args": args or {}, "session": session,
                              "driver": "playwright"})

    def test_commands_recorded_cleared_by_close_session(self):
        def fake_wb(action, args=None, session="d"):
            return {"type": "number", "value": 7} if action == "evaluate" else {"success": True}
        with mock.patch.object(B, "wb", fake_wb):
            self.ask("evaluate", {"code": "3 + 4"})
            self.ask("fill", {"selector": "#q", "value": "rust"})
            self.ask("list_tabs")
            self.ask("batch", {"steps": [{"action": "evaluate", "args": {"code": "1"}}]})
            got = self.ask("recording")["data"]
            self.assertEqual([c["action"] for c in got["commands"]], ["evaluate", "fill", "batch"][:2] + ["evaluate"])
            self.assertEqual(got["commands"][0]["result"], 7)
            self.assertEqual(got["commands"][1]["args"]["value"], "<redacted>")   # not connected: errs on secret
            self.assertEqual(got["commands"][2]["batch"], 0)
            self.assertEqual(self.ask("recording", session="other")["data"]["commands"], [])
            self.ask("close_session")
            self.assertEqual(self.ask("recording")["data"]["commands"], [])

    def test_fill_checked_before_the_job_keeps_a_plain_value(self):
        w = daemon.Worker(self.d, "s", "playwright")
        args = {"steps": [{"action": "fill", "args": {"selector": "#q", "value": "rust"}},
                          {"action": "fill", "args": {"selector": "#pw2", "value": "hunter2"}},
                          {"action": "fill", "args": {"selector": "#gone", "value": "x1y"}}]}
        rep = {"ok": True, "data": {"ok": True, "results": [{"ok": True}, {"ok": True}, {"ok": True}]}}
        w._record("batch", args, rep, None, before={"#q": False, "#pw2": True})
        vals = [c["args"]["value"] for c in self.d.recordings.get("s").snapshot()["commands"]]
        self.assertEqual(vals, ["rust", "<redacted>", "<redacted>"])     # unknown (no browser): counts as secret
        self.assertEqual(self.d.recordings.get("s").redact_values, {"hunter2", "x1y"})  # and their posts get redacted

    def test_recording_clear_and_body_dir_check(self):
        with mock.patch.object(B, "wb", lambda a, args=None, session="d": {"ok": 1}):
            self.ask("read")
        self.assertEqual(self.ask("recording", {"body_dir": "relative/dir"})["kind"], "WbError")
        self.assertTrue(self.ask("recording", {"clear": True})["data"]["cleared"])

    def test_response_handler_applies_ownership_and_rule(self):
        w = daemon.Worker(self.d, "s", "playwright")
        page = mock.Mock(url="https://app.test/?q=rust")
        page.is_closed.return_value = False
        B._tls.tid[id(page)] = (page, "T-MINE")

        def req(url, rtype="xhr", mime="application/json", body=b'{"a": 1}'):
            resp = mock.Mock(status=200, headers={"content-type": mime})
            resp.body.return_value = body
            r = mock.Mock(resource_type=rtype, url=url, method="GET", headers={"authorization": "Bearer x"},
                          post_data=None)
            r.frame.parent_frame = None
            r.response.return_value = resp
            return r
        try:
            with mock.patch.object(B, "_load", return_value={"s": {"tabs": ["T-MINE"]}}):
                w._on_finished(page, req("https://app.test/api/search?q=rust"))
                w._on_finished(page, req("https://cdn.other/x.js", "script"))
                w._on_finished(page, req("https://tracker.other/p", mime="image/gif", body=b"GIF"))
            with mock.patch.object(B, "_load", return_value={"s": {"tabs": ["T-MINE"], "borrowed": ["T-MINE"]}}):
                w._own.clear()
                w._on_finished(page, req("https://app.test/api/two"))       # borrowed tab: not recorded
        finally:
            B._tls.tid.pop(id(page), None)
        resp = self.d.recordings.get("s").snapshot()["responses"]
        self.assertEqual([r["url"] for r in resp], ["https://app.test/api/search?q=rust"])
        self.assertEqual(resp[0]["req_headers"]["authorization"], "<redacted len=8>")


class NoRecordTest(unittest.TestCase):
    """Online banking / web ATM / online trading is never recorded, even with deny_hosts_enabled = false;
    a bank's campaign and sign-up pages are."""

    def test_rule(self):
        for u in ("https://ebank.example-bank.com.tw/login", "https://www.cathaybk.com.tw/MyBank/",
                  "https://webatm.post.gov.tw/", "https://ipost.post.gov.tw/pst/home.html",
                  "https://www.somebank.com.tw/netbank/transfer", "https://etrade.broker.example/order"):
            self.assertTrue(R.no_record(u), u)
        for u in ("https://www.esunbank.com.tw/event/2026card/index.html", "https://event.taishinbank.com.tw/signup",
                  "https://crates.io/api/v1/crates/serde", "about:blank", None):
            self.assertFalse(R.no_record(u), u)

    def test_extra_tokens_from_config(self):
        with mock.patch("deja_browser.config.get", side_effect=lambda s, k, d=None:
                        ["secure.mybroker"] if k == "no_record_extra" else d):
            self.assertTrue(R.no_record("https://secure.mybroker.example/x"))

    def test_responses_and_commands_skipped(self):
        with mock.patch.object(R, "_refused", return_value=False):          # deny list switched off
            self.assertFalse(R.keep_response("https://ebank.b.example/home", "https://ebank.b.example/api/acct",
                                             "xhr", "application/json"))
            self.assertTrue(R.keep_response("https://event.b.example/", "https://event.b.example/api/join",
                                            "xhr", "application/json"))
        rec = R.Recording("s")
        rec.add_command(R.command(1, "navigate", {"url": "https://event.b.example/"}, True,
                                  url_after="https://event.b.example/"))
        rec.add_command(R.command(2, "fill", {"selector": "#acct", "value": "12345678"}, True,
                                  url_before="https://ebank.b.example/home", url_after="https://ebank.b.example/home"))
        snap = rec.snapshot()
        self.assertEqual([c["n"] for c in snap["commands"]], [1])
        self.assertEqual(snap["dropped"]["no_record"], 1)
        self.assertEqual(snap["url"], "https://ebank.b.example/home")


class RedactPostTest(unittest.TestCase):
    """A login form's post body must never hold the password in the recording (red line: secret values are
    never printed / returned; `recording --verbose` prints post_data)."""

    def test_credential_fields_and_known_secrets(self):
        self.assertEqual(R.redact_post("user=alice&password=hunter22&q=a+b"),
                         "user=alice&password=<redacted>&q=a+b")
        self.assertEqual(R.redact_post("q=a%20b&page=2"), "q=a%20b&page=2")      # nothing to redact: untouched
        self.assertEqual(json.loads(R.redact_post('{"login": "alice", "pwd": "s3cret", "q": "x"}')),
                         {"login": "alice", "pwd": "<redacted>", "q": "x"})
        out = R.redact_post("pw1=Zx9%21pass&user=bob", {"Zx9!pass"})           # field name gives nothing away
        self.assertNotIn("Zx9", out)
        self.assertIn("user=bob", out)
        self.assertNotIn("Zx9!pass", R.redact_post("raw body Zx9!pass end", {"Zx9!pass"}))

    def test_recording_redacts_on_add_and_again_on_snapshot(self):
        rec = R.Recording("s")
        rec.add_secrets(["Zx9!pass", "ab"])                                   # < 3 chars: ignored
        rec.add_response({"method": "POST", "url": "https://a.test/login", "post_data": "u=bob&p1=Zx9%21pass"}, None)
        rec.add_response({"method": "POST", "url": "https://a.test/x", "post_data": "code=Qq77Late"}, None)
        rec.add_secrets(["Qq77Late"])                                         # learned after its post
        posts = [r["post_data"] for r in rec.snapshot()["responses"]]
        self.assertFalse(any("Zx9" in p or "Qq77Late" in p for p in posts), posts)
        self.assertNotIn("ab", rec.redact_values)

    def test_daemon_notes_secret_values_before_the_job(self):
        d = daemon.Daemon("k", "tok", "h")
        try:
            w = daemon.Worker(d, "s", "playwright")
            w._note_secrets("batch", {"steps": [
                {"action": "fill", "args": {"selector": "#user", "value": "alice"}},
                {"action": "fill", "args": {"selector": "#pw", "value": "hunter22"}},
                {"action": "fill", "args": {"selector": "input[name=password]", "value": "hunter33"}},
                {"action": "captcha_text", "args": {"fields": {"#u": "bob", "#p": "Zx9!pass"}}}]},
                before={"#user": False, "#pw": True})
            self.assertEqual(d.recordings.get("s").redact_values, {"hunter22", "hunter33", "bob", "Zx9!pass"})
        finally:
            d.shutdown()

    def test_learn_refuses_a_request_with_a_redacted_value(self):
        with tempfile.TemporaryDirectory() as t:
            r = _Rec(t)
            r.resp("https://app.test/api/login", {"user": {"name": "Alice Example"}}, method="POST",
                   post='{"user": "alice", "pw1": "<redacted>"}',      # a name draft doesn't drop
                   headers={"content-type": "application/json"})
            with self.assertRaises(learn.LearnError) as cm:
                learn.plan_api(r.data(), ["Alice Example"], {}, name="me", write=True)
            self.assertIn("password / secret", cm.exception.reason)


# ---------- learn.py ----------

class CandidateTest(unittest.TestCase):
    def test_sources_names_and_dedupe(self):
        rec = {"commands": [
            {"n": 1, "action": "navigate", "args": {"url": "https://crates.io/crates/serde?tab=readme&x=1"},
             "url_after": "https://crates.io/crates/serde?tab=readme&sort=new", "ok": True},
            {"n": 2, "action": "fill", "args": {"selector": "input[name=\"q\"]", "value": "serde"}, "ok": True},
            {"n": 3, "action": "fill", "args": {"selector": "#pw", "value": "<redacted>", "redacted": True}, "ok": True},
            {"n": 4, "action": "select", "args": {"selector": "#author", "label": "Jane"}, "ok": True,
             "result": {"selected": {"value": "Jane Austen", "label": "Jane"}}}]}
        cl = learn.candidate_list(rec)
        self.assertEqual(cl, [("q", "serde", "typed"), ("author", "Jane Austen", "typed"), ("tab", "readme", "url"),
                              ("sort", "new", "page")])
        self.assertEqual(learn.param_name("select#tag-list"), "tag_list")
        self.assertEqual(learn.param_name("[name=3d]"), "p_3d")
        self.assertEqual(learn.candidates({"commands": [rec["commands"][0]]}, ["https://w.test/wiki/Gold"])["wiki"], "Gold")


class PlanApiTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.r = _Rec(self._t.name)
        self.r.cmd("navigate", {"url": "https://app.test/?q=rust"}, url_after="https://app.test/?q=rust")

    def tearDown(self):
        self._t.cleanup()

    def test_search_api_with_inferred_param(self):
        self.r.resp("https://app.test/", "<html>no titles</html>", mime="text/html", rtype="document")
        self.r.resp("https://api.app.test/search?query=rust&page=0", _hits())
        plan, _ = learn.plan_api(self.r.data(), [TITLES[0]], {}, name="search")
        d = plan["draft"]
        self.assertEqual(d["request"]["url"], "https://api.app.test/search?query={q|url}&page=0")
        self.assertEqual(plan["args"], {"q": "rust"})
        self.assertEqual(d["params"], [{"name": "q", "description": 'recorded with q="rust"', "required": True}])
        self.assertEqual(d["entry_url"], "https://app.test/")
        self.assertFalse(d["writes"])

    def test_never_a_substring_of_a_longer_value(self):
        self.r.resp("https://api.app.test/search", _hits(), method="POST", post='{"query": "rust lang", "x": "and"}',
                    headers={"sec-ch-ua": '"Not.A/Brand";v="24"'})
        with self.assertRaises(learn.LearnError) as cm:
            learn.plan_api(self.r.data(), [TITLES[0]], {"q": "an"}, name="s")
        self.assertIn("--param q", cm.exception.reason)
        plan, _ = learn.plan_api(self.r.data(), [TITLES[0]], {}, name="s")   # inferred "rust": not whole -> dropped
        self.assertEqual(plan["args"], {})
        self.assertEqual(plan["draft"]["request"]["body"]["query"], "rust lang")
        self.assertNotIn("sec-ch-ua", json.dumps(plan["draft"]["request"]))   # header values are never copied

    def test_id_from_an_earlier_response_is_refused_unless_a_param(self):
        self.r.resp("https://app.test/api/search?q=rust", _hits())
        self.r.resp("https://app.test/api/item/10045", {"story": {"title": TITLES[0], "text": "The full story"}})
        with self.assertRaises(learn.LearnError) as cm:
            learn.plan_api(self.r.data(), ["The full story"], {}, name="item")
        self.assertIn("'10045'", cm.exception.reason)
        self.assertIn("--param", cm.exception.hint)
        plan, _ = learn.plan_api(self.r.data(), ["The full story"], {"id": "10045"}, name="item")
        self.assertEqual(plan["draft"]["request"]["url"], "https://app.test/api/item/{id|url}")
        self.assertTrue(plan["draft"]["params"][0]["required"])

    def test_refused_candidate_falls_through_to_the_next_one(self):
        # crates.io 2026-11-22: the smaller /<crate>/<version>/... response ranked first and was refused (its URL
        # holds the version from the earlier /<crate> response), though /<crate> itself holds the answer
        self.r.cmd("navigate", {"url": "https://app.test/crates/serde"}, url_after="https://app.test/crates/serde")
        self.r.resp("https://app.test/api/v1/crates/serde",
                    {"crate": {"name": "serde", "max_version": "1.0.154", "description": "x" * 4000}})
        self.r.resp("https://app.test/api/v1/crates/serde/1.0.154/readme", {"v": "1.0.154"})
        plan, _ = learn.plan_api(self.r.data(), ["1.0.154"], {}, name="ver")
        url = plan["draft"]["request"]["url"]
        self.assertTrue(url.startswith("https://app.test/api/v1/crates/{") and "readme" not in url, url)
        with tempfile.TemporaryDirectory() as t2:      # no other candidate: the first refusal is reported
            only = _Rec(t2)
            only.cmd("navigate", {"url": "https://app.test/crates/serde"}, url_after="https://app.test/crates/serde")
            only.resp("https://app.test/api/v1/crates/serde", {"crate": {"name": "serde", "newest": "v9.9.99"}})
            only.resp("https://app.test/api/v1/crates/serde/v9.9.99/readme", {"v": "1.0.154"})
            with self.assertRaises(learn.LearnError) as cm:
                learn.plan_api(only.data(), ["1.0.154"], {}, name="ver")
            self.assertIn("'v9.9.99'", cm.exception.reason)

    def test_config_values_and_kept_keys_are_not_derived(self):
        self.r.resp("https://app.test/?q=rust", '<script>cfg={appId:"APP12345",key:"aaaa1111bbbb2222cccc"}</script>'
                    .replace("APP12345", '"APP12345"').replace("aaaa1111bbbb2222cccc", '"aaaa1111bbbb2222cccc"'),
                    mime="text/html", rtype="document")
        self.r.resp("https://x-dsn.search.test/1/query?application-id=APP12345&api-key=aaaa1111bbbb2222cccc",
                    _hits(), method="POST", post='{"query": "rust"}')
        plan, rep = learn.plan_api(self.r.data(), [TITLES[0]], {}, keep=["api-key"], name="s")
        self.assertIn("api-key=aaaa1111bbbb2222cccc", plan["draft"]["request"]["url"])
        self.assertFalse(plan["draft"]["writes"])                 # POST to .../query: a read
        plan2, rep2 = learn.plan_api(self.r.data(), [TITLES[0]], {}, name="s")
        self.assertEqual(rep2["dropped"], ["api-key"])

    def test_config_named_value_from_an_earlier_json_response_is_still_derived(self):
        self.r.resp("https://app.test/api/latest", {"version_id": "v2024-55"})
        self.r.resp("https://app.test/api/notes?version_id=v2024-55", {"notes": [TITLES[0]]})
        with self.assertRaises(learn.LearnError) as cm:
            learn.plan_api(self.r.data(), [TITLES[0]], {}, name="notes")
        self.assertIn("v2024-55", cm.exception.reason)

    def test_non_read_post_needs_write(self):
        self.r.resp("https://app.test/api/cart/add", {"ok": True, "title": TITLES[0]}, method="POST", post='{"id": 1}')
        with self.assertRaises(learn.LearnError) as cm:
            learn.plan_api(self.r.data(), [TITLES[0]], {}, name="add")
        self.assertIn("--write", cm.exception.hint)
        plan, _ = learn.plan_api(self.r.data(), [TITLES[0]], {}, name="add", write=True)
        self.assertTrue(plan["draft"]["writes"])

    def test_not_applicable_without_json(self):
        self.r.resp("https://app.test/", f"<li>{TITLES[0]}</li>", mime="text/html", rtype="document")
        plan, rep = learn.plan_api(self.r.data(), [TITLES[0]], {}, name="s")
        self.assertIsNone(plan)
        self.assertEqual(learn.found_in_documents(self.r.data(), [TITLES[0]]), ["https://app.test/"])


class PlanScriptTest(unittest.TestCase):
    CODE = "() => [...document.querySelectorAll('.t')].map(e => e.textContent).filter(t => t.includes('Rust'))"

    def rec(self, url="https://w.test/wiki/Rust", code=CODE, result=None, extra=()):
        r = _Rec(tempfile.gettempdir(), url)
        r.cmd("navigate", {"url": url}, url_after=url)
        for c in extra:
            r.cmd(*c)
        r.cmd("evaluate", {"code": code}, result=TITLES if result is None else result, url_before=url, url_after=url)
        return r

    def test_script_with_url_param_and_literal(self):
        plan = learn.plan_script(self.rec().data(), [TITLES[0]], {}, name="titles")
        d = plan["draft"]
        self.assertEqual(d["entry_url"], "https://w.test/wiki/{wiki|url}")
        self.assertTrue(d["at_entry"])
        self.assertIn("includes(args.wiki)", d["script"])           # the whole literal 'Rust' is the param
        self.assertEqual(plan["args"], {"wiki": "Rust"})

    def test_code_literal_templated_only_when_whole(self):
        code, hits = learn.template_code("f('rust', \"rust lang\", 'rus')", {"q": "rust"})
        self.assertEqual((code, hits), ("f(args.q, \"rust lang\", 'rus')", {"q": 1}))

    def test_wrapper_handles_expression_and_function(self):
        s = learn.wrap_script("document.title;")
        self.assertIn("(\ndocument.title\n)", s)
        self.assertIn("typeof v === 'function'", s)

    def test_refusals(self):
        r = self.rec()
        r.resp("https://w.test/wiki/Rust", "<html/>", method="POST", mime="text/html", rtype="document")
        with self.assertRaises(learn.LearnError) as cm:
            learn.plan_script(r.data(), [TITLES[0]], {})
        self.assertIn("first page was not recorded", cm.exception.reason)     # a form chain without its GET
        r = self.rec(url="https://w.test/search", extra=[("select", {"selector": "#author"},
                                                          {"selected": {"value": "Jane Austen"}})])
        with self.assertRaises(learn.LearnError) as cm:
            learn.plan_script(r.data(), [TITLES[0]], {})
        self.assertIn("Jane Austen", cm.exception.reason)
        r = self.rec()
        r.commands[-1]["args"]["frame"] = "#f"
        with self.assertRaises(learn.LearnError):
            learn.plan_script(r.data(), [TITLES[0]], {})
        self.assertIsNone(learn.plan_script(self.rec().data(), ["not on the page"], {}))


class PlanFormTest(unittest.TestCase):
    """quotes.toscrape.com-style post-back form: author -> postback -> tag -> Search, __VIEWSTATE chain."""
    CODE = '[...document.querySelectorAll(".quote")].map(q => q.textContent)'

    def rec(self, tmp, second_state="VS-2"):
        r = _Rec(tmp, "https://q.test/search.aspx")
        form = '<form><input type="hidden" name="__VIEWSTATE" value="{}"><select name="author"></select></form>'
        r.resp("https://q.test/search.aspx", form.format("VS-1"), mime="text/html", rtype="document")
        r.resp("https://q.test/filter.aspx", form.format(second_state), method="POST", mime="text/html",
               rtype="document", headers={"Content-Type": "application/x-www-form-urlencoded"},
               post="author=Albert+Einstein&tag=----------&__VIEWSTATE=VS-1")
        r.resp("https://q.test/filter.aspx", "<div class=quote>The world as we have created it</div>", method="POST",
               mime="text/html", rtype="document", headers={"Content-Type": "application/x-www-form-urlencoded"},
               post="author=Albert+Einstein&tag=change&submit_button=Search&__VIEWSTATE=VS-2")
        u0, u1 = "https://q.test/search.aspx", "https://q.test/filter.aspx"
        r.cmd("navigate", {"url": u0}, url_after=u0)
        r.cmd("select", {"selector": "#author", "value": "Albert Einstein"}, {"selected": {"value": "Albert Einstein"}},
              u0, u1)
        r.cmd("select", {"selector": "#tag", "value": "change"}, {"selected": {"value": "change"}}, u1, u1)
        r.cmd("click", {"selector": "input[name=submit_button]"}, None, u1, u1)
        r.cmd("evaluate", {"code": self.CODE}, ["The world as we have created it"], u1, u1)
        return r

    def test_chain_replayed_with_fresh_hidden_fields(self):
        with tempfile.TemporaryDirectory() as d:
            plan = learn.plan_script(self.rec(d).data(), ["The world as we have created it"], {}, name="q")
        self.assertEqual(plan["kind"], "form")
        self.assertEqual(plan["args"], {"author": "Albert Einstein", "tag": "change"})
        s = plan["draft"]["script"]
        self.assertIn('let d0 = await __get("https://q.test/search.aspx");', s)
        self.assertIn('[["author", args.author], ["tag", "----------"], ["__VIEWSTATE", __hidden(d0, "__VIEWSTATE")]]', s)
        self.assertIn('["tag", args.tag], ["submit_button", "Search"], ["__VIEWSTATE", __hidden(d1, "__VIEWSTATE")]', s)
        self.assertNotIn("VS-1", s)
        self.assertTrue(s.rstrip().endswith("})(d2);"))
        self.assertEqual(plan["draft"]["entry_url"], "https://q.test/search.aspx")

    def test_id_from_an_earlier_page_that_is_not_hidden_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.rec(d)
            r.responses[2]["post_data"] = "author=Albert+Einstein&tag=change&item_ref=ID-123456"
            Path(r.responses[1]["body_path"]).write_text('<p data-x="ID-123456"></p>')
            with self.assertRaises(learn.LearnError) as cm:
                learn.plan_script(r.data(), ["The world as we have created it"], {})
        self.assertIn("ID-123456", cm.exception.reason)

    def test_credential_field_and_non_read_post_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.rec(d)
            r.responses[2]["post_data"] += "&password=hunter2"
            with self.assertRaises(learn.LearnError) as cm:
                learn.plan_script(r.data(), ["The world as we have created it"], {})
            self.assertIn("password", cm.exception.reason)
            self.assertNotIn("hunter2", cm.exception.reason + cm.exception.hint)
            r = self.rec(d)
            for x in r.responses[1:]:
                x["url"] = "https://q.test/Default.aspx"
            r.commands[-1]["url_before"] = "https://q.test/Default.aspx"
            with self.assertRaises(learn.LearnError) as cm:
                learn.plan_script(r.data(), ["The world as we have created it"], {})
            self.assertIn("--write", cm.exception.hint)
            plan = learn.plan_script(r.data(), ["The world as we have created it"], {}, write=True)
            self.assertTrue(plan["draft"]["writes"])


# ---------- cmd_learn / draft output ----------

class CmdLearnTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.store = Path(self._t.name) / "store"
        self.bodies = Path(self._t.name) / "bodies"
        self.bodies.mkdir()

    def tearDown(self):
        self._t.cleanup()

    def recording(self):
        r = _Rec(self.bodies)
        r.cmd("navigate", {"url": "https://app.test/?q=rust"}, url_after="https://app.test/?q=rust")
        r.resp("https://api.app.test/search?query=rust", _hits())
        return r.data()

    def learn(self, argv, rec, tiers_ok=True):
        def verify(d, given, expects=()):
            if not tiers_ok:
                return [{"tier": "http", "ok": False, "tried": [{"tier": "http", "status": 403}]}], [], {}
            return [{"tier": "http", "ok": True, "out_chars": 99}], ["http"], {"http": [{"title": "str"}]}
        with mock.patch.dict("os.environ", {"DEJA_BROWSER_DAEMON": "1"}), \
                mock.patch.object(client, "call_running", return_value={"ok": True, "data": rec}), \
                mock.patch.object(sr, "_verify_tiers", side_effect=verify), \
                mock.patch.object(sr, "RECIPES", self.store):
            return sr.cmd_learn(argv)

    def test_success_is_short_and_saved(self):
        out, rc = self.learn(["--session", "s", "--name", "search", "--expect", TITLES[0]], self.recording())
        self.assertEqual(rc, 0)
        self.assertEqual(out, {"ok": True, "id": "app.test/search", "tier": "http", "kind": "api",
                               "params": {"q": "rust"}, "sample_chars": 99})
        self.assertLessEqual(len(json.dumps(out)), sr.LEARN_OUT)
        saved = json.loads((self.store / "app.test" / "search.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["verified_args"], {"q": "rust"})
        out, rc = self.learn(["--name", "search", "--expect", TITLES[0]], self.recording())
        self.assertEqual((rc, out["reason"]), (1, "recipe app.test/search exists"))

    def test_failed_verify_saves_nothing(self):
        out, rc = self.learn(["--name", "search", "--expect", TITLES[0]], self.recording(), tiers_ok=False)
        self.assertEqual(rc, 1)
        self.assertIn("http: status 403", out["reason"])
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), sr.LEARN_OUT)
        self.assertFalse(self.store.exists())

    def test_nothing_recorded_and_no_daemon(self):
        out, rc = self.learn(["--name", "x", "--expect", "abc"], {"responses": [], "commands": []})
        self.assertEqual(rc, 1)
        self.assertIn("no recording", out["reason"])
        with mock.patch.dict("os.environ", {"DEJA_BROWSER_DAEMON": "0"}):
            out, rc = sr.cmd_learn(["--name", "x", "--expect", "abc"])
        self.assertIn("network_capture", out["hint"])
        with mock.patch.dict("os.environ", {"DEJA_BROWSER_DAEMON": "1"}), \
                mock.patch.object(client, "call_running", side_effect=client.Unavailable("no daemon is running")):
            out, rc = sr.cmd_learn(["--name", "x", "--expect", "abc"])
        self.assertIn("no daemon is running", out["reason"])

    def test_html_only_answer_says_so(self):
        r = _Rec(self.bodies)
        r.resp("https://app.test/", f"<li>{TITLES[0]}</li>", mime="text/html", rtype="document")
        out, rc = self.learn(["--name", "x", "--expect", TITLES[0]], r.data())
        self.assertIn("page HTML", out["reason"])
        self.assertIn("evaluate", out["hint"])

    def test_compact_cuts_long_text(self):
        out = learn.compact({"ok": False, "reason": "r" * 500, "hint": "h" * 500})
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 400)


class DraftOutputTest(unittest.TestCase):
    def test_default_draft_output_is_a_summary(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Rec(d)
            r.resp("https://api.app.test/search?query=rust", _hits())
            cap = Path(d) / "cap.json"
            cap.write_text(json.dumps({"url": "https://app.test/", "requests": r.responses}), encoding="utf-8")
            argv = [str(cap), "--expect", TITLES[0], "--param", "q=rust"]
            short = sr.cmd_draft(argv)
            full = sr.cmd_draft(argv + ["--force", "--verbose"])
        for k in ("applicable", "draft_path", "next", "matched"):
            self.assertIn(k, short)
        self.assertIn("draft", full)
        self.assertLess(len(json.dumps(short)), len(json.dumps(full)))
        self.assertLessEqual(len(json.dumps(short)), 700)


if __name__ == "__main__":
    unittest.main()
