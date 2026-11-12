# edge-recipes

Learn a website's data path once in a real browser, then replay it later without an LLM (or a
human) reading the page again.

An agent's first visit explores a site — captures the network calls, finds the JSON endpoint or
the form — and saves what worked as a small parameterised **recipe**. Later runs map a request
onto the recipe's `args` and execute it, falling back to a slower tier only when the cheap one
stops working. When a CAPTCHA gets in the way, the tool tries to clear it itself first; a human
is the last resort.

> **Status: early release (0.1.0).** Tested on Windows 11 with Microsoft Edge and on macOS
> (Apple Silicon, Python 3.14) with Google Chrome. Linux browser detection is implemented but not
> yet exercised on a real machine.

## What it does

- **Dedicated browser profile.** Drives any Chromium browser (Edge, Chrome, Chromium, Brave —
  auto-detected) over CDP on `127.0.0.1` only, with its own user-data dir. Your everyday browser
  profile is never touched. Log in to the sites you want once, by hand, in that profile.
- **Recipes with a tier ladder** (cheapest first, falls through on failure, records what worked):
  - `http` — plain HTTP outside the browser, with cookies read live from the browser (never stored)
  - `page` — `fetch()` inside a tab on the site (native cookies, Cloudflare clearance, CSRF)
  - `script` — the recipe's own JavaScript in the page (DOM read / form fill)
- **Shape checks.** Every response is compared with the JSON shape recorded at verify time; a
  mismatch is reported as *stale*, never returned as data.
- **Write guard.** Recipes with side effects (`"writes": true`) only run with `--allow-write`.
- **Secrets by name.** Recipes reference `{secret:NAME}`; values are resolved in-process from your
  OS keyring (or env / your own function) and are never printed or returned.
- **CAPTCHA gate**, in order:
  1. wait — scroll widgets into view and let Turnstile / Cloudflare clear on their own
  2. click — one trusted input click on the checkbox (reCAPTCHA v2, hCaptcha, Turnstile)
  3. reCAPTCHA v2 image grid → audio challenge → local `faster-whisper` (optional extra)
  4. classic text-in-image CAPTCHAs → local `ddddocr` (optional extra, called by a recipe)
  5. otherwise notify you and wait for a human

  hCaptcha image/drag challenges: **opt-in only** — see *hCaptcha solver* below. Off by default.
- **Refuses** mail, banking and brokerage hosts (built-in list, extendable in config), and
  ticketing / rail-booking hosts by default (see *Acceptable use*).

## Install

Requires Python ≥ 3.11 and a Chromium-based browser.

```sh
pip install -e ".[dev]"            # core + pytest
pip install -e ".[stealth]"        # patchright driver (recommended; used automatically if present)
pip install -e ".[keyring]"        # OS keyring secret backend (the default backend)
pip install -e ".[audio,ocr]"      # reCAPTCHA audio (faster-whisper), text CAPTCHAs (ddddocr)
python -m playwright install-deps  # Linux only, if system libraries are missing
```

`edge-recipes` uses your installed browser; it does not need `playwright install`.

## Quick start

```sh
edge-cdp launch                    # start the dedicated browser; log in to sites by hand if needed
edge-cdp navigate '{"url": "https://example.com"}' --session demo
edge-cdp evaluate '{"code": "document.title"}' --session demo
edge-cdp close_session '{}' --session demo

edge-recipes verify draft.json '{"q": "rust"}'   # try every tier, print shape + preview
edge-recipes save draft.json                     # only after a passing verify of this draft
edge-recipes run hn.algolia.com/search '{"q": "rust"}'
edge-recipes list
edge-recipes gate https://example.com/login      # open a URL, clear any challenge, report
```

From Python:

```python
from edge_recipes.browser import wb
wb("navigate", {"url": "https://example.com"}, session="task")
wb("snapshot", {"max_chars": 4000}, session="task")
wb("close_session", {}, session="task")
```

`edge-cdp --help` and `edge-recipes --help` list every action and the full recipe format.

## Recipe format (abridged)

```json
{
  "version": 1,
  "name": "search",
  "description": "Hacker News full-text search (example)",
  "entry_url": "https://hn.algolia.com/",
  "params": [{"name": "q", "required": true}],
  "request": {"method": "GET", "url": "https://hn.algolia.com/api/v1/search?query={q}"},
  "pick": {"path": "hits", "fields": ["title", "url"], "limit": 20},
  "writes": false
}
```

No recipes ship with the package — you (or your agent) build them per site. Recipes are stored as `<recipes dir>/<host>/<name>.json` and addressed as `<host>/<name>`.

## Configuration

Optional. Copy `config.example.toml` to:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\edge-recipes\config.toml` |
| macOS | `~/Library/Application Support/edge-recipes/config.toml` |
| Linux | `~/.config/edge-recipes/config.toml` |

or point `EDGE_RECIPES_CONFIG` at a file. It covers the browser executable and profile, the
recipes directory, extra denied hosts, the secret backend (`keyring` / `env` / `python`), and
notifications (an ntfy URL or any command; default is stderr).

## hCaptcha solver (opt-in, cloud)

Off by default. When enabled, the CAPTCHA gate hands hCaptcha to
[hcaptcha-challenger](https://github.com/QIN2DIM/hcaptcha-challenger), which asks Google Gemini to
answer the challenge. **This sends the cropped challenge image to Google** under your own API key
(not the page, not cookies). hcaptcha-challenger is GPL-3.0 and is not bundled — install it
yourself:

```sh
pip install "hcaptcha-challenger @ git+https://github.com/QIN2DIM/hcaptcha-challenger@28ee79b"
keyring set edge-recipes GEMINI_API_KEY        # or your configured secret backend
```

```toml
[captcha]
hcaptcha_solver = "gemini"
```

Best effort: on the public demo the "drag the missing item into the grid" type passed 13/15, the
hexagon shape-matching type 1/5 (small samples). Failures fall through to the human step.

## Acceptable use

This is a personal automation tool for sites and accounts you are entitled to use. Respect each
site's terms of service and rate limits. Do not use it for ticket scalping, mass account
creation, credential stuffing, or to defeat protections on systems you do not own. Ticket and
rail-booking sites are refused by default: several jurisdictions criminalise automated ticket
purchase (e.g. Taiwan's Railway Act art. 65). `[recipes] allow_ticket_hosts` can lift that; we
recommend you don't, and the decision and its consequences are yours. The project
does not integrate paid CAPTCHA-solving services and will not.

## Tests

```sh
python -m pytest -q               # pure functions; no browser, no network
```

## License

MIT — see `LICENSE`. Optional dependencies keep their own licenses; GPL-licensed packages are
never vendored into this repository.
