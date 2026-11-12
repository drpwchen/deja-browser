# -*- coding: utf-8 -*-
"""Tell the human something (only used when a challenge or a login needs them).

[notify]
  ntfy    = "https://ntfy.sh/<your-topic>"     POST to an ntfy topic
  command = ["prog", "--title", "{title}", "--message", "{message}"]   run any program;
            "{python}" is replaced by the current interpreter, ~ is expanded
Neither set -> the message goes to stderr. Failures are swallowed: a notification never breaks a run.
"""
import subprocess
import sys
import urllib.parse
import urllib.request

from . import config


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
            print(f"[edge-recipes] {title}: {message}", file=sys.stderr)
    except Exception:
        pass
