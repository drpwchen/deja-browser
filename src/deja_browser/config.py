# -*- coding: utf-8 -*-
"""Where things live and what the user configured. Stdlib only (no platformdirs dependency).

Config file: <config dir>/config.toml  (see config.example.toml in the repo). Every key is optional.
  Windows  %APPDATA%\\deja-browser            data: %LOCALAPPDATA%\\deja-browser
  macOS    ~/Library/Application Support/deja-browser  (config and data)
  Linux    $XDG_CONFIG_HOME/deja-browser       data: $XDG_DATA_HOME/deja-browser
Overrides: DEJA_BROWSER_CONFIG (path of the config file), DEJA_BROWSER_HOME (data dir).
0.1.0 compatibility: if the new file / dir does not exist the LEGACY_APP one is used, and the
legacy env var prefix is read when the DEJA_BROWSER_* one is unset (see env()).
Paths in the file may use ~ and environment variables.
"""
import os
import sys
import tomllib
from pathlib import Path

APP = "deja-browser"
LEGACY_APP = "edge-recipes"   # 0.1.0 dir name; still read as a fallback


def env(name):
    """DEJA_BROWSER_<name>, else the 0.1.0 spelling of the same variable, else None."""
    return os.environ.get("DEJA_BROWSER_" + name) or os.environ.get("EDGE_RECIPES_" + name) or None


def _env_dir(var, fallback):
    v = os.environ.get(var)
    return Path(v) if v else fallback


def _prefer_new(new, legacy):
    return legacy if not new.exists() and legacy.exists() else new


def config_dir(app=APP):
    if sys.platform == "win32":
        return _env_dir("APPDATA", Path.home() / "AppData" / "Roaming") / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    return _env_dir("XDG_CONFIG_HOME", Path.home() / ".config") / app


def data_dir(app=None):
    if app is None:
        if env("HOME"):
            return expand(env("HOME"))
        return _prefer_new(data_dir(APP), data_dir(LEGACY_APP))
    if sys.platform == "win32":
        return _env_dir("LOCALAPPDATA", Path.home() / "AppData" / "Local") / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    return _env_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / app


def config_path():
    if env("CONFIG"):
        return expand(env("CONFIG"))
    return _prefer_new(config_dir() / "config.toml", config_dir(LEGACY_APP) / "config.toml")


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
