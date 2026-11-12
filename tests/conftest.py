"""Isolate tests from the developer's real config and data dir (set before edge_recipes is imported)."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="edge-recipes-test-")
os.environ["EDGE_RECIPES_CONFIG"] = os.path.join(_TMP, "no-config.toml")
os.environ["EDGE_RECIPES_HOME"] = os.path.join(_TMP, "data")
