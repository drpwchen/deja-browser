# -*- coding: utf-8 -*-
"""[recipes] deny_hosts_enabled: the refused-host list can be switched off; on by default."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deja_browser import browser, config, guard
from deja_browser import recipe as sr

BANK = "https://www.esunbank.com.tw/"
MAIL = "https://mail.google.com/x"


def _cfg(d):
    return mock.patch.object(config, "CONFIG", d)


OFF = {"recipes": {"deny_hosts_enabled": False}}
ON = {"recipes": {"deny_hosts_enabled": True}}


class FakePage:
    """Just enough page for login_gate: the login page redirected to `url`."""
    def __init__(self, url):
        self.url = url
        self.filled = []

    def evaluate(self, js):
        return False

    def goto(self, *a, **k):
        pass

    def fill(self, sel, value, timeout=None):
        self.filled.append(sel)

    def locator(self, sel):
        return mock.MagicMock()

    def wait_for_timeout(self, ms):
        pass


LOGIN = {"check": "false", "user_secret": "U", "pass_secret": "P",
         "user_selector": "#u", "pass_selector": "#p", "submit_selector": "#go"}


class DenySwitchTest(unittest.TestCase):
    def test_default_is_on(self):
        for cfg in ({}, {"recipes": {}}, ON):
            with _cfg(cfg):
                self.assertTrue(guard.deny_hosts_enabled())
                for url in (BANK, MAIL, "https://accounts.google.com/"):
                    with self.assertRaises(guard.HostRefused):
                        guard.check_host(url)

    def test_only_a_real_false_turns_it_off(self):
        for v in ("false", 0, None, "no"):
            with _cfg({"recipes": {"deny_hosts_enabled": v}}):
                self.assertTrue(guard.deny_hosts_enabled(), v)
                with self.assertRaises(guard.HostRefused):
                    guard.check_host(BANK)

    def test_off_allows_deny_hosts(self):
        with _cfg(OFF):
            self.assertFalse(guard.deny_hosts_enabled())
            for url in (BANK, MAIL, "https://card.ubot.com.tw/", "https://www.cathaybk.com.tw/"):
                guard.check_host(url)
                guard.check_host(url, tickets=False)

    def test_off_also_drops_deny_hosts_extra(self):
        with mock.patch.object(guard, "DENY_HOSTS", guard.DENY_HOSTS_DEFAULT + ("example.org",)):
            with _cfg({}):
                with self.assertRaises(guard.HostRefused):
                    guard.check_host("https://example.org/")
            with _cfg(OFF):
                guard.check_host("https://example.org/")

    def test_off_keeps_ticket_hosts(self):
        with _cfg(OFF):
            with self.assertRaisesRegex(guard.HostRefused, "ticketing"):
                guard.check_host("https://tixcraft.com/activity")
            guard.check_host("https://tip.railway.gov.tw/", tickets=False)

    def test_navigate(self):
        with _cfg(ON):
            with self.assertRaisesRegex(browser.WbError, "off-limits"):
                browser._navigate(None, "s", {"url": BANK})
        with _cfg(OFF):
            browser._guard(BANK)
            browser._guard(MAIL)

    def test_recipe_check_host(self):
        with _cfg(ON):
            with self.assertRaises(sr.RecipeError):
                sr._check_host(BANK)
        with _cfg(OFF):
            sr._check_host(BANK)
            sr._check_host(MAIL)

    def test_run(self):
        # writes + no --allow-write stops right after the host checks, before any browser work
        rec = {"entry_url": BANK, "writes": True, "request": {"url": "https://api.esunbank.com.tw/x"}}
        with _cfg(ON):
            with self.assertRaisesRegex(sr.RecipeError, "off-limits"):
                sr.run(rec, {})
        with _cfg(OFF):
            with self.assertRaisesRegex(sr.RecipeError, "side effects"):
                sr.run(rec, {})

    def test_verify(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "d.json"
            p.write_text(json.dumps({"name": "n", "description": "d", "entry_url": BANK,
                                     "request": {"url": BANK}}), encoding="utf-8")
            with mock.patch.object(sr, "run", return_value={"ok": False, "tried": []}) as run:
                with _cfg(ON):
                    with self.assertRaisesRegex(sr.RecipeError, "off-limits"):
                        sr.cmd_verify(p, {})
                    run.assert_not_called()
                with _cfg(OFF):
                    try:
                        sr.cmd_verify(p, {})
                    except sr.RecipeError as ex:
                        self.assertNotIn("off-limits", str(ex))
                    self.assertTrue(run.called)

    def test_draft_entry_url(self):
        with tempfile.TemporaryDirectory() as td:
            cap = Path(td) / "cap.json"
            cap.write_text("[]", encoding="utf-8")
            argv = [str(cap), "--entry-url", BANK]
            with _cfg(ON):
                with self.assertRaisesRegex(sr.RecipeError, "off-limits"):
                    sr.cmd_draft(argv)
            with _cfg(OFF):
                try:
                    sr.cmd_draft(argv)
                except Exception as ex:   # an empty capture fails later; the host check must not
                    self.assertNotIn("off-limits", str(ex))

    def test_login_after_redirect(self):
        with mock.patch.object(sr, "challenge_gate", return_value={"challenge": None}), \
             mock.patch.object(sr, "_secret", return_value="x"):
            page = FakePage("https://www.esunbank.com.tw/login")
            with _cfg(ON):
                with self.assertRaisesRegex(sr.RecipeError, "off-limits"):
                    sr.login_gate(page, LOGIN)
                self.assertEqual(page.filled, [])   # nothing typed into the refused host
            page = FakePage("https://www.esunbank.com.tw/login")
            with _cfg(OFF):
                sr.login_gate(page, LOGIN)
                self.assertEqual(page.filled, ["#u", "#p"])


if __name__ == "__main__":
    unittest.main()
