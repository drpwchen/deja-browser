#!/usr/bin/env python3
"""Daemon + thin client (v0.4): framing, state file, code hash, routing decisions, worker replies.
No browser: browser.wb is mocked; a real loopback server is used for the protocol round trip."""
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from deja_browser import browser as B
from deja_browser import client, daemon, recipe


class FramingTest(unittest.TestCase):
    def test_round_trip_is_one_ascii_line(self):
        msg = {"action": "read", "args": {"query": "中文\nline"}, "session": "s"}
        raw = client.encode(msg)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        raw.decode("ascii")
        self.assertEqual(client.decode(raw), msg)

    def test_incomplete_or_non_object_is_rejected(self):
        with self.assertRaises(ValueError):
            client.decode(b'{"a": 1}')
        with self.assertRaises(ValueError):
            client.decode(b"[1, 2]\n")


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "k.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_read_remove_only_own(self):
        client.write_state(self.path, {"port": 5, "token": "t", "pid": 42})
        self.assertEqual(client.read_state(self.path)["port"], 5)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        client.remove_state(self.path, 7)          # someone else's pid: kept
        self.assertTrue(self.path.exists())
        client.remove_state(self.path, 42)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_garbage_state_reads_as_none(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(client.read_state(self.path))
        self.path.write_text('{"port": 1}', encoding="utf-8")      # no token
        self.assertIsNone(client.read_state(self.path))


class CodeHashTest(unittest.TestCase):
    def test_changes_with_source_files_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            pkg, cfg = Path(d) / "pkg", Path(d) / "config.toml"
            pkg.mkdir()
            (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
            cfg.write_text("[browser]\nport = 1\n", encoding="utf-8")
            with mock.patch.object(client, "PKG", pkg), mock.patch.object(client.config, "config_path", lambda: cfg):
                h1 = client.code_hash()
                self.assertEqual(h1, client.code_hash())
                (pkg / "a.py").write_text("x = 22\n", encoding="utf-8")
                h2 = client.code_hash()
                self.assertNotEqual(h1, h2)
                cfg.write_text("[browser]\nport = 22\n", encoding="utf-8")
                self.assertNotEqual(h2, client.code_hash())

    def test_key_depends_on_port_and_config(self):
        k = client.key()
        with mock.patch.object(client, "config_port", lambda: 1234):
            self.assertTrue(client.key().endswith("-1234"))
        with mock.patch.object(client.config, "config_path", lambda: Path("/elsewhere/config.toml")):
            self.assertNotEqual(client.key().split("-")[0], k.split("-")[0])


class RoutingDecisionTest(unittest.TestCase):
    def test_enabled(self):
        self.assertTrue(client.enabled({}))
        self.assertTrue(client.enabled({"DEJA_BROWSER_DAEMON": "1"}))
        for off in ("0", "false", "OFF", "no"):
            self.assertFalse(client.enabled({"DEJA_BROWSER_DAEMON": off}))

    def test_relative_output_paths_are_made_absolute(self):
        cwd = os.path.abspath("/work")
        a = client.absolutize("screenshot", {"path": "shot.png"}, cwd)
        self.assertEqual(a["path"], os.path.join(cwd, "shot.png"))
        self.assertEqual(client.absolutize("screenshot", {"path": os.path.join(cwd, "x.png")}, "/other")["path"],
                         os.path.join(cwd, "x.png"))
        b = client.absolutize("batch", {"steps": [{"action": "save_as_pdf", "args": {"path": "o.pdf"}},
                                                  {"action": "network_capture", "args": {"body_dir": "net"}},
                                                  {"action": "read", "args": {"selector": "a.pdf"}}]}, cwd)
        self.assertEqual(b["steps"][0]["args"]["path"], os.path.join(cwd, "o.pdf"))
        self.assertEqual(b["steps"][1]["args"]["body_dir"], os.path.join(cwd, "net"))
        self.assertEqual(b["steps"][2]["args"], {"selector": "a.pdf"})

    def test_recipes_that_may_read_a_secret_run_in_process(self):
        base = {"entry_url": "https://a.example/", "request": {"url": "https://a.example/api?q={q}"}}
        with mock.patch.object(recipe.hcaptcha, "enabled", return_value=False):
            self.assertFalse(recipe.needs_in_process(base))
            self.assertTrue(recipe.needs_in_process(
                {**base, "request": {"url": "https://a.example/api", "headers": {"X-Key": "{secret:API_KEY}"}}}))
            self.assertTrue(recipe.needs_in_process({**base, "login": {"check": "true"}}))
        with mock.patch.object(recipe.hcaptcha, "enabled", return_value=True):
            self.assertTrue(recipe.needs_in_process(base))

    def test_cli_call_falls_back_only_when_not_delivered(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"DEJA_BROWSER_DAEMON": "1"}), redirect_stderr(err):
            with mock.patch.object(client, "call", side_effect=client.Unavailable("no daemon")):
                self.assertIsNone(client.cli_call("read", {}, "s"))
            self.assertIn("running in-process", err.getvalue())
            with mock.patch.object(client, "call", side_effect=client.LostReply("broke")):
                with self.assertRaises(client.LostReply):
                    client.cli_call("click", {}, "s")
        with mock.patch.dict(os.environ, {"DEJA_BROWSER_DAEMON": "0"}), \
                mock.patch.object(client, "call") as c:
            self.assertIsNone(client.cli_call("read", {}, "s"))
            c.assert_not_called()

    def test_call_replaces_a_daemon_running_older_code(self):
        old = {"port": 1, "token": "t", "pid": 5, "code_hash": "old"}
        new = {"port": 2, "token": "u", "pid": 6, "code_hash": "new"}
        with mock.patch.dict(os.environ, {"DEJA_BROWSER_DAEMON": "1"}), \
                mock.patch.object(client, "code_hash", return_value="new"), \
                mock.patch.object(client, "read_state", return_value=old), \
                mock.patch.object(client, "shutdown") as sd, \
                mock.patch.object(client, "spawn", return_value=new) as sp, \
                mock.patch.object(client, "_send", return_value={"ok": True, "data": 1}) as send:
            self.assertEqual(client.call("read", {}, "s")["data"], 1)
        sd.assert_called_once_with(old)
        sp.assert_called_once()
        self.assertIs(send.call_args.args[0], new)

    def test_call_respawns_when_the_daemon_is_gone(self):
        st = {"port": 1, "token": "t", "pid": 5, "code_hash": "h"}
        new = dict(st, port=2)
        sends = [client.Unavailable("cannot connect"), {"ok": True, "data": 2}]
        with mock.patch.dict(os.environ, {"DEJA_BROWSER_DAEMON": "1"}), \
                mock.patch.object(client, "code_hash", return_value="h"), \
                mock.patch.object(client, "read_state", return_value=st), \
                mock.patch.object(client, "spawn", return_value=new), \
                mock.patch.object(client, "_send", side_effect=sends):
            self.assertEqual(client.call("read", {}, "s")["data"], 2)

    def test_a_refused_job_is_not_retried_on_a_new_daemon(self):
        st = {"port": 1, "token": "t", "pid": 5, "code_hash": "h"}
        with mock.patch.dict(os.environ, {"DEJA_BROWSER_DAEMON": "1"}), \
                mock.patch.object(client, "code_hash", return_value="h"), \
                mock.patch.object(client, "read_state", return_value=st), \
                mock.patch.object(client, "spawn") as sp, \
                mock.patch.object(client, "_send", side_effect=client.Unavailable("refused: x", "refused")):
            with self.assertRaises(client.Unavailable):
                client.call("recipe.run", {}, "s")
        sp.assert_not_called()


class FakeDialog:
    def __init__(self, page, type_="confirm", message="sure?"):
        self.page, self.type, self.message, self.done = page, type_, message, None

    def dismiss(self):
        self.done = "dismissed"

    def accept(self):
        self.done = "accepted"


class WorkerTest(unittest.TestCase):
    """Workers with browser.wb mocked: replies, error kinds, dialog records."""

    def setUp(self):
        self.d = daemon.Daemon("k", "tok", "h")

    def ask(self, action, args=None, session="s", token="tok", driver="playwright"):
        return self.d.handle({"token": token, "action": action, "args": args or {}, "session": session,
                              "driver": driver})

    def tearDown(self):
        self.d.shutdown()

    def test_token_and_action_checks(self):
        self.assertEqual(self.ask("read", token="nope")["kind"], "auth")
        self.assertEqual(self.ask("format_disk")["kind"], "refused")
        self.assertEqual(self.ask("read", driver="selenium")["kind"], "refused")
        self.assertEqual(self.ask("daemon.ping")["data"]["code_hash"], "h")

    def test_reply_and_error_kinds(self):
        def fake_wb(action, args=None, session="default"):
            if action == "read":
                return {"text": "hi", "session": session}
            if action == "click":
                raise B.NoTab("no tab")
            if action == "fill":
                raise B.WbError("fill: selector required")
            raise ValueError("boom")
        with mock.patch.object(B, "wb", fake_wb):
            self.assertEqual(self.ask("read", session="x"), {"ok": True, "data": {"text": "hi", "session": "x"}})
            self.assertEqual(self.ask("click")["kind"], "NoTab")
            self.assertEqual(self.ask("fill")["kind"], "WbError")
            r = self.ask("evaluate")
            self.assertEqual((r["kind"], r["error"]), ("Exception", "boom"))
            self.assertIn("Traceback", r["traceback"])

    def test_one_worker_per_session_and_driver(self):
        with mock.patch.object(B, "wb", lambda a, args=None, session="d": {"t": threading.current_thread().name}):
            a = self.ask("read", session="a")["data"]["t"]
            b = self.ask("read", session="b")["data"]["t"]
            a2 = self.ask("read", session="a")["data"]["t"]
            a3 = self.ask("read", session="a", driver="patchright")["data"]["t"]
        self.assertEqual(a, a2)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, a3)
        self.assertIn("deja-worker:a:playwright", a)

    def test_dialog_on_own_tab_is_dismissed_and_reported_others_left_alone(self):
        w = daemon.Worker(self.d, "s", "playwright")
        mine, theirs = mock.Mock(url="http://a.test/"), mock.Mock(url="http://b.test/")
        B._tls.tid[id(mine)] = (mine, "T-MINE")
        B._tls.tid[id(theirs)] = (theirs, "T-THEIRS")
        try:
            with mock.patch.object(B, "_load", return_value={"s": {"tabs": ["T-MINE"]}, "o": {"tabs": ["T-THEIRS"]}}):
                d1, d2, d3 = FakeDialog(mine), FakeDialog(theirs), FakeDialog(mock.Mock(url="http://c.test/"))
                for dlg in (d1, d2, d3):
                    w._on_dialog(dlg)
                d4 = FakeDialog(mine, "beforeunload", "")
                w._on_dialog(d4)
        finally:
            B._tls.tid.pop(id(mine), None)
            B._tls.tid.pop(id(theirs), None)
        self.assertEqual((d1.done, d2.done, d3.done, d4.done), ("dismissed", None, None, "accepted"))
        self.assertEqual([(x["type"], x["handled"], x["tabId"]) for x in w.dialogs],
                         [("confirm", "dismissed", "T-MINE"), ("beforeunload", "accepted", "T-MINE")])
        job = {"done": threading.Event(), "reply": None}
        w._finish(job, {"ok": True, "data": {}})
        self.assertEqual(len(job["reply"]["dialogs"]), 2)
        self.assertEqual(w.dialogs, [])

    def test_idle_worker_retires_and_the_next_request_gets_a_new_one(self):
        import time
        with mock.patch.object(B, "wb", lambda a, args=None, session="d": {"t": threading.get_ident()}),                 mock.patch.object(daemon, "WORKER_IDLE", 0.2):
            first = self.ask("read")["data"]["t"]
            w = self.d.workers[("s", "playwright")]
            w.join(5)                                  # q.get(timeout=1) -> idle > 0.2 s -> retire
            self.assertFalse(w.is_alive())
            self.assertNotIn(("s", "playwright"), self.d.workers)
            second = self.ask("read")["data"]["t"]
            w2 = self.d.workers[("s", "playwright")]
        self.assertIsNot(w2, w)            # a new worker (thread ids are reused on Linux: don't compare those)
        self.assertTrue(first and second)

    def test_idle_daemon_shuts_down(self):
        with mock.patch.object(daemon, "DAEMON_IDLE", 0), mock.patch.object(self.d.stopping, "wait",
                                                                            side_effect=[False, True]):
            self.d.last -= 1
            self.d.idle_watch()
        self.assertTrue(self.d.stopping.is_set())

    def test_recipe_needing_secrets_is_refused_by_the_worker(self):
        rec = {"entry_url": "https://a.example/", "login": {"check": "true"}}
        with mock.patch.object(recipe, "load", return_value=rec), mock.patch.object(recipe, "cmd_run") as run:
            r = self.ask("recipe.run", {"rid": "a.example/x"})
        self.assertEqual(r["kind"], "refused")
        run.assert_not_called()

    def test_secrets_are_refused_in_the_daemon_process(self):
        with self.assertRaises(RuntimeError):
            daemon._refuse_secret("ANY")


class LoopbackTest(unittest.TestCase):
    """The real TCP server + client._send, browser.wb mocked."""

    def test_round_trip_over_loopback(self):
        d = daemon.Daemon("k", "tok", "h")
        srv = daemon._Server(("127.0.0.1", 0), daemon._Handler)
        srv.daemon_obj, d.server = d, srv
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        st = {"port": srv.server_address[1], "token": "tok"}
        try:
            with mock.patch.object(B, "wb", lambda a, args=None, session="d": {"echo": args, "a": a}):
                r = client._send(st, {"action": "read", "args": {"q": "中文"}, "session": "s", "driver": "playwright"})
                self.assertEqual(r["data"], {"echo": {"q": "中文"}, "a": "read"})
                with self.assertRaises(client.Unavailable) as cm:
                    client._send(dict(st, token="bad"), {"action": "read", "args": {}, "session": "s"})
                self.assertEqual(cm.exception.kind, "auth")
        finally:
            d.shutdown()
            t.join(5)
        with self.assertRaises(client.Unavailable):        # nothing listens any more: not delivered
            client._send(st, {"action": "read", "args": {}, "session": "s"})


class MainRoutingTest(unittest.TestCase):
    def run_main(self, argv, rep):
        out = io.StringIO()
        with mock.patch.object(client, "cli_call", return_value=rep) as cc, redirect_stdout(out), \
                redirect_stderr(io.StringIO()) as err:
            rc = B.main(argv)
        return rc, out.getvalue().strip(), err.getvalue(), cc

    def test_daemon_reply_is_printed_with_dialogs(self):
        dl = [{"type": "alert", "message": "m", "handled": "dismissed"}]
        rc, out, _, cc = self.run_main(["read", '{"query":"x"}', "--session", "S"],
                                       {"ok": True, "data": {"text": "t"}, "dialogs": dl})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"text": "t", "dialogs": dl})
        self.assertEqual(cc.call_args.args[:3], ("read", {"query": "x"}, "S"))

    def test_daemon_errors_keep_the_in_process_shape(self):
        rc, out, _, _ = self.run_main(["click", "{}"], {"ok": False, "kind": "NoTab", "error": "no tab"})
        self.assertEqual((rc, json.loads(out)), (1, {"ok": False, "error": "no tab"}))
        rc, out, err, _ = self.run_main(["click", "{}"], {"ok": False, "kind": "Exception", "error": "x",
                                                          "traceback": "Traceback: x"})
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("Traceback", err)
        rc, out, _, _ = self.run_main(["batch", '{"steps":[{"action":"read"}]}'],
                                      {"ok": True, "data": {"ok": False, "results": []}})
        self.assertEqual(rc, 1)

    def test_lost_reply_is_an_error_not_a_rerun(self):
        out = io.StringIO()
        with mock.patch.object(client, "cli_call", side_effect=client.LostReply("broke")), \
                mock.patch.object(B, "wb") as wb, redirect_stdout(out):
            rc = B.main(["click", '{"selector":"#go"}'])
        self.assertEqual(rc, 1)
        self.assertIn("may or may not have run", json.loads(out.getvalue())["error"])
        wb.assert_not_called()

    def test_no_daemon_runs_in_process(self):
        out = io.StringIO()
        with mock.patch.object(client, "cli_call", return_value=None), \
                mock.patch.object(B, "wb", return_value={"x": 1}) as wb, redirect_stdout(out):
            self.assertEqual(B.main(["read", "{}"]), 0)
        wb.assert_called_once_with("read", {}, session="cli")
        self.assertEqual(json.loads(out.getvalue()), {"x": 1})

    def test_status_and_stop_never_use_the_daemon_for_the_action(self):
        with mock.patch.object(client, "cli_call") as cc, mock.patch.object(B, "version", return_value=None), \
                redirect_stdout(io.StringIO()):
            B.main(["status"])
        cc.assert_not_called()
        with mock.patch.object(client, "disconnect_all") as dc, mock.patch.object(B, "stop", return_value={}), \
                redirect_stdout(io.StringIO()):
            B.main(["stop"])
        dc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
