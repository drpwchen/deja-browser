# -*- coding: utf-8 -*-
"""Guard rails for the agent browser. Not a security boundary.

What actually protects you is separation: the agent profile is never logged in to mail, banking
or brokerage, and it stores no passwords. The pieces here keep an agent from wandering into those
sites by accident and keep stored secrets away from them:

- check_host(url)          refuse mail / bank / broker hosts (and ticketing, for recipes)
- harden_profile(profile)  turn off the browser's own password and card autofill in the profile
- password_managers(profile)  list password-manager extensions installed in the profile

An agent that can run shell commands can edit your config; a deny list cannot stop a determined
attacker. It stops accidents and makes the policy explicit.
"""
import json
import os
import re
import urllib.parse
from pathlib import Path

from . import config

# substring of the host. Mail and account sign-in pages first (a Google / Microsoft sign-in opens
# mail, files and calendars at once), then banks, brokers and payments. Extend with
# [recipes] deny_hosts_extra.
DENY_HOSTS_DEFAULT = (
    # mail and account sign-in
    "mail.google.", "gmail.", "accounts.google.", "outlook.live.", "outlook.office.", "login.live.",
    "login.microsoftonline.", "mail.yahoo.", "icloud.com", "appleid.apple.", "proton.me",
    # banks, brokers, payments (generic)
    "bank", "securities", "paypal.", "venmo.", "wise.com", "revolut.",
    # Taiwan banks and brokers whose hostnames do not contain "bank"
    "cathaybk.", "fubon.", "sinopac.", "sinotrade.", "yuanta.", "post.gov.tw", "capital.com.tw",
    "kgi.com", "masterlink.", "concords.", "jihsun.", "tcb-bank.", "hncb.",
)
DENY_HOSTS = DENY_HOSTS_DEFAULT + tuple(config.get("recipes", "deny_hosts_extra", []))

# ticketing / rail booking: automated purchase is anti-scalping territory (e.g. Taiwan Railway
# Act art. 65, event-ticket laws), so recipes for these are refused by default. [recipes]
# allow_ticket_hosts lifts entries ("railway.gov.tw", or "*" for all) — your call, your liability.
TICKET_HOSTS_DEFAULT = ("ticketmaster.", "livenation.", "stubhub.", "viagogo.", "seetickets.", "axs.com",
                        "tixcraft.", "kktix.", "ibon.com.tw", "ticket.com.tw", "eplus.jp", "pia.jp",
                        "l-tike.", "interpark.", "yes24.", "melon.com", "damai.cn", "12306.cn",
                        "railway.gov.tw", "thsrc.com.tw", "eki-net.", "smart-ex.jp", "irctc.")


def ticket_hosts(allow):
    allow = set(allow or ())
    return () if "*" in allow else tuple(d for d in TICKET_HOSTS_DEFAULT if d not in allow)


TICKET_HOSTS = ticket_hosts(config.get("recipes", "allow_ticket_hosts", []))


class HostRefused(RuntimeError):
    pass


def host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def check_host(url, tickets=True):
    """Raise HostRefused for a denied host. tickets=False skips the ticketing list (plain browsing:
    looking at a timetable is fine; automated purchase through a recipe is what the list is for)."""
    h = host(url)
    if any(d in h for d in DENY_HOSTS):
        raise HostRefused(f"{h}: mail / account sign-in / banks / brokers are off-limits for the agent "
                          "browser (see [recipes] deny_hosts_extra)")
    if tickets and any(d in h for d in TICKET_HOSTS):
        raise HostRefused(f"{h}: ticketing / rail booking sites are refused by default (anti-scalping "
                          "laws); see [recipes] allow_ticket_hosts")


# ---------- the profile ----------

# Chromium preference keys (Edge, Chrome, Brave share them): saved passwords, payment cards, and
# addresses. With these off the browser neither offers to save nor fills anything in by itself.
_HARDEN = {("credentials_enable_service",): False,
           ("profile", "password_manager_enabled"): False,
           ("autofill", "credit_card_enabled"): False,
           ("autofill", "profile_enabled"): False}


def harden_profile(profile):
    """Turn off password / card / address autofill in <profile>/Default/Preferences. Call only while
    the browser is NOT running (it rewrites the file on exit). -> list of keys changed.
    [browser] password_manager = true skips this."""
    if config.get("browser", "password_manager", False):
        return []
    p = Path(profile) / "Default" / "Preferences"
    try:
        prefs = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return []   # unreadable: leave the browser's own file alone
    changed = []
    for keys, val in _HARDEN.items():
        node = prefs
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        if node.get(keys[-1]) != val:
            node[keys[-1]] = val
            changed.append(".".join(keys))
    if changed:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".deja-browser.tmp")
        tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    return changed


_PM = re.compile(r"password|bitwarden|1password|lastpass|dashlane|keeper|nordpass|enpass|roboform|"
                 r"proton pass|icloud", re.I)


def _ext_name(vdir):
    m = json.loads((vdir / "manifest.json").read_text(encoding="utf-8-sig"))
    name = m.get("name") or ""
    mm = re.fullmatch(r"__MSG_(\w+)__", name)
    if mm:
        for loc in (m.get("default_locale"), "en", "en_US"):
            f = vdir / "_locales" / (loc or "") / "messages.json"
            if loc and f.exists():
                msgs = {k.lower(): v for k, v in json.loads(f.read_text(encoding="utf-8-sig")).items()}
                name = (msgs.get(mm.group(1).lower()) or {}).get("message", name)
                break
    return name


def password_managers(profile):
    """-> [(extension_id, name)] for password-manager extensions installed in any profile dir.
    Unlocked, such an extension offers its whole vault to anything that can click in the page."""
    out = []
    for ext_root in Path(profile).glob("*/Extensions"):
        for eid in ext_root.iterdir():
            versions = sorted(d for d in eid.iterdir() if d.is_dir()) if eid.is_dir() else []
            if not versions:
                continue
            try:
                name = _ext_name(versions[-1])
            except (OSError, ValueError):
                continue
            if _PM.search(name):
                out.append((eid.name, name))
    return out
