# -*- coding: utf-8 -*-
"""Optional hCaptcha image/drag solver: hcaptcha-challenger + Google Gemini. OFF by default.

Turning it on sends the cropped challenge view (the puzzle image, not the page) to Google's
Gemini API, under your own API key. hcaptcha-challenger is GPL-3.0 and is NOT bundled; install
it yourself (the PyPI 0.19.0 release fails the drag challenges; the tested build is git 28ee79b):

  pip install "hcaptcha-challenger @ git+https://github.com/QIN2DIM/hcaptcha-challenger@28ee79b"

Config (all optional except the switch):
  [captcha]
  hcaptcha_solver = "gemini"            # default "off"
  gemini_model = "gemini-3.7-flash"
  gemini_key_secret = "GEMINI_API_KEY"  # secret NAME, resolved through the secret backend
  solver_timeout = 180                  # seconds per challenge

hcaptcha-challenger is async and must hook the challenge's network responses BEFORE the checkbox
is clicked, so solve() does the click itself, from a second CDP connection on a worker thread.
"""
import asyncio
import importlib.util
import os
import threading
import uuid

from . import config, secretstore

DEFAULT_MODEL = "gemini-3.7-flash"


def enabled():
    return str(config.get("captcha", "hcaptcha_solver", "off")).lower() == "gemini"


def available():
    return importlib.util.find_spec("hcaptcha_challenger") is not None


def solve(page, timeout=None):
    """Solve the hCaptcha on this (sync) page. -> {"ran": bool, "signal"?: str, "error"?: str}.
    "ran": False means nothing was clicked (not enabled / not installed / no key), so the caller
    can fall back to its own checkbox click."""
    if not enabled():
        return {"ran": False, "error": "disabled"}
    if not available():
        return {"ran": False, "error": "hcaptcha-challenger not installed"}
    try:
        key = secretstore.get(config.get("captcha", "gemini_key_secret", "GEMINI_API_KEY"))
    except secretstore.SecretMissing as ex:
        return {"ran": False, "error": str(ex)}
    timeout = float(timeout or config.get("captcha", "solver_timeout", 180))
    mark = uuid.uuid4().hex
    # a DOM attribute, not a JS global: patchright evaluates in an isolated world
    page.evaluate("m => document.documentElement.setAttribute('data-er-solve', m)", mark)
    out = {}

    def worker():
        try:
            out.update(asyncio.run(_solve_async(mark, key, timeout)))
        except Exception as ex:  # noqa: BLE001 — reported to the gate, which falls back to a human
            out["error"] = f"{type(ex).__name__}: {ex}"[:300]

    t = threading.Thread(target=worker, name="hcaptcha-solver", daemon=True)
    t.start()
    t.join(timeout + 60)
    if t.is_alive():
        out.setdefault("error", "solver thread did not finish")
    return {"ran": True, **out}


async def _solve_async(mark, key, timeout):
    # hcaptcha-challenger draws helper grids with matplotlib; a GUI backend off the main thread
    # spews Tk errors (seen 2026-11-12). Headless backend unless the user chose one.
    os.environ.setdefault("MPLBACKEND", "Agg")
    from playwright.async_api import async_playwright
    from hcaptcha_challenger import AgentConfig, AgentV
    from .browser import ENDPOINT

    model = config.get("captcha", "gemini_model", DEFAULT_MODEL)
    work = config.data_dir() / "hcaptcha"
    cfg = AgentConfig(
        GEMINI_API_KEY=key,
        EXECUTION_TIMEOUT=timeout, RESPONSE_TIMEOUT=30, RETRY_ON_FAILURE=False,
        CHALLENGE_CLASSIFIER_MODEL=model, IMAGE_CLASSIFIER_MODEL=model,
        SPATIAL_POINT_REASONER_MODEL=model, SPATIAL_PATH_REASONER_MODEL=model,
        cache_dir=work / "cache", challenge_dir=work / "challenge", captcha_response_dir=work / "captcha",
    )
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ENDPOINT)
        page = await _find_page(browser, mark)
        if page is None:
            return {"error": "could not find the page on the second CDP connection"}
        agent = AgentV(page=page, agent_config=cfg)   # hooks network responses: before the click
        await agent.robotic_arm.click_checkbox()
        signal = await agent.wait_for_challenge()
        return {"signal": getattr(signal, "value", str(signal))}


async def _find_page(browser, mark):
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                if await pg.evaluate("() => document.documentElement.getAttribute('data-er-solve')") == mark:
                    return pg
            except Exception:  # noqa: BLE001 — closed / crashed tabs
                continue
    return None
