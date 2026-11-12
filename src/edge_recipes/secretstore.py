# -*- coding: utf-8 -*-
"""Pluggable secret lookup. Recipes only ever name a secret ({secret:NAME}); the value is fetched
here, stays in this process, and is never printed or returned to the caller.

[secrets] backend =
  "keyring" (default)  OS store via the `keyring` package (Windows Credential Manager, macOS
                       Keychain, Linux Secret Service); service name = [secrets] service, default
                       "edge-recipes". Set one: `keyring set edge-recipes NAME`.
  "env"                environment variable <[secrets] env_prefix><NAME>, prefix default "EDGE_RECIPES_SECRET_"
  "python"             [secrets] python = "/path/to/file.py:function" — function(name) -> str | None,
                       imported in-process (for an existing personal secret store)
[secrets] set_hint = "how the user stores NAME" ({name} is filled in) — shown when a secret is missing.
"""
import importlib.util
import os

from . import config


class SecretMissing(LookupError):
    pass


def _python_backend(spec):
    file, _, func = spec.rpartition(":")
    if not file or not func:
        raise ValueError('[secrets] python must be "/path/to/file.py:function"')
    p = config.expand(file)
    mod_spec = importlib.util.spec_from_file_location(f"_edge_recipes_secret_{p.stem}", p)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return getattr(mod, func)


def _hint(name, backend):
    custom = config.get("secrets", "set_hint")
    if custom:
        return custom.format(name=name)
    if backend == "env":
        return f"set environment variable {config.get('secrets', 'env_prefix', 'EDGE_RECIPES_SECRET_')}{name}"
    if backend == "keyring":
        return f"keyring set {config.get('secrets', 'service', 'edge-recipes')} {name}"
    return f"store {name} in your secret backend"


def get(name):
    """-> the secret value; raises SecretMissing (message says how to set it, never contains a value)."""
    backend = config.get("secrets", "backend", "keyring")
    if backend == "env":
        v = os.environ.get(config.get("secrets", "env_prefix", "EDGE_RECIPES_SECRET_") + name)
    elif backend == "python":
        v = _python_backend(config.get("secrets", "python", ""))(name)
    elif backend == "keyring":
        try:
            import keyring
        except ImportError:
            raise SecretMissing("secrets backend 'keyring' needs `pip install keyring` "
                                "(or set [secrets] backend = \"env\")") from None
        v = keyring.get_password(config.get("secrets", "service", "edge-recipes"), name)
    else:
        raise SecretMissing(f"unknown [secrets] backend {backend!r}")
    if v is None:
        raise SecretMissing(f"secret {name!r} not found — {_hint(name, backend)}")
    return v
