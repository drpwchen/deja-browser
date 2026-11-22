"""config / secretstore / notify / browser detection — no browser, no network, no real secrets."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import browser, config, guard, notify, secretstore


def _cfg(d):
    return mock.patch.object(config, "CONFIG", d)


class ConfigTest(unittest.TestCase):
    def test_load_toml_and_expand(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.toml"
            p.write_text('[browser]\nprofile = "~/x-profile"\n', encoding="utf-8")
            d = config.load(p)
            with _cfg(d):
                self.assertEqual(config.path("browser", "profile", None), Path.home() / "x-profile")
                self.assertEqual(config.get("browser", "port", 9222), 9222)

    def test_missing_file_is_empty(self):
        self.assertEqual(config.load(Path(tempfile.gettempdir()) / "definitely-not-here.toml"), {})

    def test_data_dir_env_override(self):
        with mock.patch.dict(os.environ, {"DEJA_BROWSER_HOME": "~/somewhere"}):
            self.assertEqual(config.data_dir(), Path.home() / "somewhere")

    def test_legacy_names(self):
        # 0.1.0 names: old env vars and an existing old config/data dir still work
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DEJA_BROWSER_", "EDGE_RECIPES_"))}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["EDGE_RECIPES_HOME"] = "~/old-home"
            self.assertEqual(config.data_dir(), Path.home() / "old-home")
            os.environ["DEJA_BROWSER_HOME"] = "~/new-home"
            self.assertEqual(config.data_dir(), Path.home() / "new-home")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, env, clear=True),                 mock.patch.object(config.sys, "platform", "linux"):
            os.environ["XDG_CONFIG_HOME"] = td
            os.environ["XDG_DATA_HOME"] = td
            self.assertEqual(config.config_path(), Path(td) / "deja-browser" / "config.toml")
            (Path(td) / "edge-recipes").mkdir()
            (Path(td) / "edge-recipes" / "config.toml").write_text("", encoding="utf-8")
            self.assertEqual(config.config_path(), Path(td) / "edge-recipes" / "config.toml")
            self.assertEqual(config.data_dir(), Path(td) / "edge-recipes")
            (Path(td) / "deja-browser").mkdir()
            (Path(td) / "deja-browser" / "config.toml").write_text("", encoding="utf-8")
            self.assertEqual(config.config_path(), Path(td) / "deja-browser" / "config.toml")
            self.assertEqual(config.data_dir(), Path(td) / "deja-browser")

    def test_test_run_is_isolated_from_real_config(self):
        import conftest
        self.assertEqual(config.CONFIG, {"browser": {"port": conftest.TEST_PORT}})
        self.assertTrue(str(config.config_path()).startswith(conftest._TMP))

    def test_test_browser_never_uses_9222(self):
        import conftest
        self.assertNotEqual(conftest.TEST_PORT, 9222)
        self.assertEqual(browser.CONFIG_PORT, conftest.TEST_PORT)
        self.assertNotIn("--remote-debugging-port=9222", browser.launch_args())
        self.assertEqual(conftest.TEST_PREFIX, browser.TEST_PROFILE_PREFIX)


class SecretStoreTest(unittest.TestCase):
    def test_env_backend(self):
        with _cfg({"secrets": {"backend": "env"}}), \
                mock.patch.dict(os.environ, {"DEJA_BROWSER_SECRET_FOO": "v"}):
            self.assertEqual(secretstore.get("FOO"), "v")

    def test_missing_message_has_hint_not_value(self):
        with _cfg({"secrets": {"backend": "env", "set_hint": "store it with mytool {name}"}}):
            with self.assertRaises(secretstore.SecretMissing) as cm:
                secretstore.get("NOPE_XYZ")
            self.assertIn("mytool NOPE_XYZ", str(cm.exception))

    def test_python_backend(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "store.py"
            f.write_text("def look(n):\n    return {'A': 'x'}.get(n)\n", encoding="utf-8")
            with _cfg({"secrets": {"backend": "python", "python": f"{f}:look"}}):
                self.assertEqual(secretstore.get("A"), "x")
                with self.assertRaises(secretstore.SecretMissing):
                    secretstore.get("B")

    def test_keyring_backend(self):
        fake = mock.Mock()
        fake.get_password.return_value = "k"
        with _cfg({}), mock.patch.dict(sys.modules, {"keyring": fake}):
            self.assertEqual(secretstore.get("N"), "k")
            fake.get_password.assert_called_once_with("deja-browser", "N")

    def test_legacy_secret_names(self):
        fake = mock.Mock()
        fake.get_password.side_effect = lambda service, name: "old" if service == "edge-recipes" else None
        with _cfg({}), mock.patch.dict(sys.modules, {"keyring": fake}):
            self.assertEqual(secretstore.get("N"), "old")
        with _cfg({"secrets": {"service": "mine"}}), mock.patch.dict(sys.modules, {"keyring": fake}):
            with self.assertRaises(secretstore.SecretMissing):
                secretstore.get("N")
        with _cfg({"secrets": {"backend": "env"}}),                 mock.patch.dict(os.environ, {"EDGE_RECIPES_SECRET_FOO": "old"}):
            self.assertEqual(secretstore.get("FOO"), "old")


    def test_command_backend(self):
        code = "import sys; print({'A': 'x y'}.get(sys.argv[1], ''), end='' if sys.argv[1] == 'A' else '\\n'); " \
               "sys.exit(0 if sys.argv[1] == 'A' else 1)"
        with _cfg({"secrets": {"backend": "command", "command": [sys.executable, "-c", code, "{name}"]}}):
            self.assertEqual(secretstore.get("A"), "x y")
            with self.assertRaises(secretstore.SecretMissing) as cm:
                secretstore.get("B")
            self.assertNotIn("x y", str(cm.exception))

    def test_command_backend_needs_a_list(self):
        with _cfg({"secrets": {"backend": "command", "command": "op read x"}}):
            with self.assertRaises(secretstore.SecretMissing):
                secretstore.get("A")


class GuardProfileTest(unittest.TestCase):
    def test_harden_profile_sets_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td, _cfg({}):
            p = Path(td) / "Default" / "Preferences"
            p.parent.mkdir(parents=True)
            p.write_text('{"keep": 1, "profile": {"name": "x"}}', encoding="utf-8")
            changed = guard.harden_profile(td)
            self.assertIn("credentials_enable_service", changed)
            import json
            d = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(d["keep"], 1)
            self.assertEqual(d["profile"], {"name": "x", "password_manager_enabled": False})
            self.assertFalse(d["autofill"]["credit_card_enabled"])
            self.assertEqual(guard.harden_profile(td), [])

    def test_harden_profile_new_profile_and_opt_out(self):
        with tempfile.TemporaryDirectory() as td:
            with _cfg({"browser": {"password_manager": True}}):
                self.assertEqual(guard.harden_profile(td), [])
            with _cfg({}):
                self.assertTrue(guard.harden_profile(td))
                self.assertTrue((Path(td) / "Default" / "Preferences").exists())

    def test_password_managers_resolves_localized_names(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "Default" / "Extensions" / "abc" / "1.0_0"
            (v / "_locales" / "en").mkdir(parents=True)
            (v / "manifest.json").write_text('{"name": "__MSG_extName__", "default_locale": "en"}', encoding="utf-8")
            (v / "_locales" / "en" / "messages.json").write_text('{"extName": {"message": "Bitwarden"}}', encoding="utf-8")
            w = Path(td) / "Default" / "Extensions" / "zot" / "5.0"
            w.mkdir(parents=True)
            (w / "manifest.json").write_text('{"name": "Zotero Connector"}', encoding="utf-8")
            self.assertEqual(guard.password_managers(td), [("abc", "Bitwarden")])

    def test_check_host_lists(self):
        for url in ("https://accounts.google.com/", "https://www.cathaybk.com.tw/", "https://www.fubon.com/",
                    "https://ipost.post.gov.tw/", "https://www.sinotrade.com.tw/", "https://outlook.office.com/mail"):
            with self.assertRaises(guard.HostRefused):
                guard.check_host(url)
        guard.check_host("https://tip.railway.gov.tw/", tickets=False)   # browsing a timetable is fine
        with self.assertRaises(guard.HostRefused):
            guard.check_host("https://tip.railway.gov.tw/")
        guard.check_host("https://scholar.google.com/")
        guard.check_host("https://pubmed.ncbi.nlm.nih.gov/")

    def test_navigate_refuses_before_touching_the_browser(self):
        with self.assertRaises(browser.WbError):
            browser._navigate(None, "s", {"url": "https://mail.google.com/"})

class NotifyTest(unittest.TestCase):
    def test_command_placeholders(self):
        with _cfg({"notify": {"command": ["{python}", "~/n.py", "--t", "{title}", "--m", "{message}"]}}), \
                mock.patch.object(notify.subprocess, "run") as run:
            notify.notify("T", "M")
            argv = run.call_args[0][0]
            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(argv[1], str(Path.home() / "n.py"))
            self.assertEqual(argv[3:], ["T", "--m", "M"])

    def test_ntfy_url_puts_title_in_query(self):
        with _cfg({"notify": {"ntfy": "https://ntfy.example/topic"}}), \
                mock.patch.object(notify.urllib.request, "urlopen") as op:
            notify.notify("Ti tle", "body")
            req = op.call_args[0][0]
            self.assertTrue(req.full_url.startswith("https://ntfy.example/topic?title=Ti+tle"))
            self.assertEqual(req.data, b"body")

    def test_default_stderr_and_never_raises(self):
        with _cfg({}), mock.patch.object(sys, "stderr") as err:
            notify.notify("a", "b")
            self.assertTrue(err.write.called)
        with _cfg({"notify": {"command": ["x"]}}), \
                mock.patch.object(notify.subprocess, "run", side_effect=OSError):
            notify.notify("a", "b")   # swallowed


class BrowserDetectTest(unittest.TestCase):
    def test_config_wins(self):
        with _cfg({"browser": {"executable": "~/my/chrome"}}):
            self.assertEqual(browser.find_browser(), str(Path.home() / "my" / "chrome"))

    def test_first_existing_candidate(self):
        with _cfg({}), mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(browser, "_candidates", return_value=["/a", "/b", "/c"]), \
                mock.patch.object(browser.os.path, "exists", side_effect=lambda p: p in ("/b", "/c")):
            os.environ.pop("DEJA_BROWSER_BROWSER", None)
            self.assertEqual(browser.find_browser(), "/b")

    def test_none_found_launch_explains(self):
        with mock.patch.object(browser, "EDGE", None), mock.patch.object(browser, "version", return_value=None):
            with self.assertRaises(browser.WbError) as cm:
                browser.launch()
            self.assertIn("executable", str(cm.exception))


class CliHelpTest(unittest.TestCase):
    """--help must print usage and exit 0 without touching a browser (it once raised WbError)."""

    def test_help_flags(self):
        from deja_browser import recipe
        for main in (browser.main, recipe.main):
            for flag in ("-h", "--help"):
                with mock.patch("sys.stdout"), mock.patch.object(browser, "_ctx", side_effect=AssertionError):
                    self.assertEqual(main([flag]), 0)

    def _out(self, main, argv):
        import io
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), mock.patch.object(browser, "_ctx", side_effect=AssertionError), \
                mock.patch.object(browser, "_main_via_daemon", side_effect=AssertionError):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_top_level_help_is_short(self):
        from deja_browser import clihelp, recipe
        for main in (browser.main, recipe.main):
            rc, out = self._out(main, ["--help"])
            self.assertEqual(rc, 0)
            self.assertLess(len(out), 1500)      # a model reads this on every first visit
        self.assertEqual(self._out(browser.main, [])[0], 2)
        self.assertIn("help <action", clihelp.CDP_TOP)

    def test_every_action_and_command_has_help(self):
        import inspect
        import re
        from deja_browser import clihelp, recipe
        cdp = set(browser.SESSION_ACTIONS) | set(browser.PAGE_ACTIONS) | {"launch", "status", "stop"}
        self.assertEqual(cdp | set(clihelp.CDP_TOPICS), set(clihelp.CDP))
        for name in cdp | set(clihelp.CDP_TOPICS):   # named on the top page, so a model finds it
            self.assertRegex(clihelp.CDP_TOP, rf"\b{name}\b")
        cmds = set(re.findall(r'cmd == "(\w+)"', inspect.getsource(recipe.main)))
        self.assertEqual(cmds, set(clihelp.RECIPE_COMMANDS))
        self.assertEqual(cmds | set(clihelp.RECIPE_TOPICS), set(clihelp.RECIPE))
        for name in cmds | set(clihelp.RECIPE_TOPICS):
            self.assertRegex(clihelp.RECIPE_TOP, rf"\b{name}\b")

    def test_help_per_action(self):
        from deja_browser import recipe
        for argv in (["help", "snapshot"], ["snapshot", "--help"], ["click", "{}", "-h"], ["help", "refs"]):
            rc, out = self._out(browser.main, argv)
            self.assertEqual(rc, 0, argv)
            self.assertNotIn("Topics:", out)          # the entry, not the top page
        self.assertIn("refs:true", self._out(browser.main, ["help", "snapshot"])[1])
        rc, out = self._out(browser.main, ["help", "nosuch"])
        self.assertEqual(rc, 2)
        self.assertIn("navigate", out)
        rc, out = self._out(recipe.main, ["learn", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--expect", out)
        self.assertEqual(self._out(recipe.main, ["help", "format"])[0], 0)
        rc, out = self._out(recipe.main, ["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", out)


if __name__ == "__main__":
    unittest.main()
