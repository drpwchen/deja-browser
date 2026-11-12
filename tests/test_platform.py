"""config / secretstore / notify / browser detection — no browser, no network, no real secrets."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edge_recipes import browser, config, notify, secretstore


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
        with mock.patch.dict(os.environ, {"EDGE_RECIPES_HOME": "~/somewhere"}):
            self.assertEqual(config.data_dir(), Path.home() / "somewhere")

    def test_test_run_is_isolated_from_real_config(self):
        self.assertEqual(config.CONFIG, {})


class SecretStoreTest(unittest.TestCase):
    def test_env_backend(self):
        with _cfg({"secrets": {"backend": "env"}}), \
                mock.patch.dict(os.environ, {"EDGE_RECIPES_SECRET_FOO": "v"}):
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
            fake.get_password.assert_called_once_with("edge-recipes", "N")


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
            os.environ.pop("EDGE_RECIPES_BROWSER", None)
            self.assertEqual(browser.find_browser(), "/b")

    def test_none_found_launch_explains(self):
        with mock.patch.object(browser, "EDGE", None), mock.patch.object(browser, "version", return_value=None):
            with self.assertRaises(browser.WbError) as cm:
                browser.launch()
            self.assertIn("executable", str(cm.exception))


class CliHelpTest(unittest.TestCase):
    """--help must print usage and exit 0 without touching a browser (it once raised WbError)."""

    def test_help_flags(self):
        from edge_recipes import recipe
        for main in (browser.main, recipe.main):
            for flag in ("-h", "--help"):
                with mock.patch("sys.stdout"), mock.patch.object(browser, "_ctx", side_effect=AssertionError):
                    self.assertEqual(main([flag]), 0)


if __name__ == "__main__":
    unittest.main()
