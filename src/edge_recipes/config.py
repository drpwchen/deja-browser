# -*- coding: utf-8 -*-
"""Where things live and what the user configured. Stdlib only (no platformdirs dependency).

Config file: <config dir>/config.toml  (see config.example.toml in the repo). Every key is optional.
  Windows  %APPDATA%\\edge-recipes            data: %LOCALAPPDATA%\\edge-recipes
  macOS    ~/Library/Application Support/edge-recipes  (config and data)
  Linux    $XDG_CONFIG_HOME/edge-recipes       data: $XDG_DATA_HOME/edge-recipes
Overrides: EDGE_RECIPES_CONFIG (path of the config file), EDGE_RECIPES_HOME (data dir).
Paths in the file may use ~ and environment variables.
"""
import os
import sys
import tomllib
from pathlib import Path

APP = "edge-recipes"


def _env_dir(var, fallback):
    v = os.environ.get(var)
    return Path(v) if v else fallback


def config_dir():
    if sys.platform == "win32":
        return _env_dir("APPDATA", Path.home() / "AppData" / "Roaming") / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return _env_dir("XDG_CONFIG_HOME", Path.home() / ".config") / APP


def data_dir():
    if os.environ.get("EDGE_RECIPES_HOME"):
        return expand(os.environ["EDGE_RECIPES_HOME"])
    if sys.platform == "win32":
        return _env_dir("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return _env_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP


def config_path():
    return expand(os.environ["EDGE_RECIPES_CONFIG"]) if os.environ.get("EDGE_RECIPES_CONFIG") \
        else config_dir() / "config.toml"


def expand(p):
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def load(path=None):
    p = Path(path) if path else config_path()
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


CONFIG = load()


def get(section, key, default=None):
    return (CONFIG.get(section) or {}).get(key, default)


def path(section, key, default):
    v = get(section, key)
    return expand(v) if v else default
