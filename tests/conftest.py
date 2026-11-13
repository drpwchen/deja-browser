"""Isolate tests from the developer's real config and data dir (set before deja_browser is imported)."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="deja-browser-test-")
os.environ["DEJA_BROWSER_CONFIG"] = os.path.join(_TMP, "no-config.toml")
os.environ["DEJA_BROWSER_HOME"] = os.path.join(_TMP, "data")
