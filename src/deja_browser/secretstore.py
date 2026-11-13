# -*- coding: utf-8 -*-
"""Pluggable secret lookup. Recipes only ever name a secret ({secret:NAME}); the value is fetched
here, stays in this process, and is never printed or returned to the caller.

[secrets] backend =
  "keyring" (default)  OS store via the `keyring` package (Windows Credential Manager, macOS
                       Keychain, Linux Secret Service); service name = [secrets] service, default
                       "deja-browser". Set one: `keyring set deja-browser NAME`.
  "env"                environment variable <[secrets] env_prefix><NAME>, prefix default "DEJA_BROWSER_SECRET_"
  "python"             [secrets] python = "/path/to/file.py:function" — function(name) -> str | None,
                       imported in-process (for an existing personal secret store)
  "command"            [secrets] command = ["op", "read", "op://agent/{name}/password"] — run it
                       ({name} filled in, no shell), stdout minus the trailing newline is the value;
                       non-zero exit = missing. Fits password-manager CLIs: 1Password `op`,
                       Bitwarden `bw get password {name}`, macOS `security find-generic-password -w -s {name}`.
[secrets] set_hint = "how the user stores NAME" ({name} is filled in) — shown when a secret is missing.
"""
import importlib.util
import os
import subprocess

from . import config


class SecretMissing(LookupError):
    pass


def _python_backend(spec):
    file, _, func = spec.rpartition(":")
    if not file or not func:
        raise ValueError('[secrets] python must be "/path/to/file.py:function"')
    p = config.expand(file)
    mod_spec = importlib.util.spec_from_file_location(f"_deja_browser_secret_{p.stem}", p)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return getattr(mod, func)


def _command_backend(name):
    cmd = config.get("secrets", "command")
    if not isinstance(cmd, list) or not cmd:
        raise SecretMissing('[secrets] command must be a list, e.g. ["op", "read", "op://agent/{name}/password"]')
    argv = [str(a).replace("{name}", name) for a in cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise SecretMissing(f"[secrets] command failed to run: {type(ex).__name__}") from None
    # stderr is never echoed: a CLI may print part of what it looked up
    return r.stdout.rstrip("\r\n") if r.returncode == 0 and r.stdout.strip() else None


def _hint(name, backend):
    custom = config.get("secrets", "set_hint")
    if custom:
        return custom.format(name=name)
    if backend == "env":
        return f"set environment variable {config.get('secrets', 'env_prefix', 'DEJA_BROWSER_SECRET_')}{name}"
    if backend == "keyring":
        return f"keyring set {config.get('secrets', 'service', 'deja-browser')} {name}"
    return f"store {name} in your secret backend"


def get(name):
    """-> the secret value; raises SecretMissing (message says how to set it, never contains a value)."""
    backend = config.get("secrets", "backend", "keyring")
    if backend == "env":
        prefixes = [config.get("secrets", "env_prefix")] if config.get("secrets", "env_prefix") \
            else ["DEJA_BROWSER_SECRET_", "EDGE_RECIPES_SECRET_"]   # second = 0.1.0 name
        v = next((os.environ[p + name] for p in prefixes if p + name in os.environ), None)
    elif backend == "python":
        v = _python_backend(config.get("secrets", "python", ""))(name)
    elif backend == "command":
        v = _command_backend(name)
    elif backend == "keyring":
        try:
            import keyring
        except ImportError:
            raise SecretMissing("secrets backend 'keyring' needs `pip install keyring` "
                                "(or set [secrets] backend = \"env\")") from None
        services = [config.get("secrets", "service")] if config.get("secrets", "service") \
            else ["deja-browser", "edge-recipes"]   # second = 0.1.0 name
        v = None
        for service in services:
            v = keyring.get_password(service, name)
            if v is not None:
                break
    else:
        raise SecretMissing(f"unknown [secrets] backend {backend!r}")
    if v is None:
        raise SecretMissing(f"secret {name!r} not found — {_hint(name, backend)}")
    return v
