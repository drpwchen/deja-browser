# -*- coding: utf-8 -*-
"""Tell the human something (a challenge or a login that needs them, a `handoff`).

[notify]
  ntfy    = "https://ntfy.sh/<your-topic>"     POST to an ntfy topic
  command = ["prog", "--title", "{title}", "--message", "{message}"]   run any program;
            "{python}" is replaced by the current interpreter, ~ is expanded
Neither set -> the message goes to stderr (under the daemon: its log file). Failures are swallowed: a
notification never breaks a run.
"""
import subprocess
import sys
import urllib.parse
import urllib.request

from . import config


def channel():
    """Where notify() sends: "ntfy", "command" or "stderr" (from the config, nothing is sent)."""
    try:
        if config.get("notify", "ntfy"):
            return "ntfy"
        if config.get("notify", "command"):
            return "command"
    except Exception:
        pass
    return "stderr"


def notify(title, message):
    try:
        url = config.get("notify", "ntfy")
        cmd = config.get("notify", "command")
        if url:
            # title/priority as query params: HTTP headers cannot carry non-ASCII titles
            q = urllib.parse.urlencode({"title": title, "priority": "4", "tags": "robot"})
            req = urllib.request.Request(f"{url}{'&' if '?' in url else '?'}{q}",
                                         data=message.encode("utf-8"), method="POST")
            urllib.request.urlopen(req, timeout=20).close()
        elif cmd:
            argv = [str(config.expand(a)) if a.startswith("~") else
                    a.replace("{python}", sys.executable).replace("{title}", title).replace("{message}", message)
                    for a in cmd]
            subprocess.run(argv, timeout=20, capture_output=True)
        else:
            print(f"[deja-browser] {title}: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass
