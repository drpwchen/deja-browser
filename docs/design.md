# edge-recipes — design

**Goal:** an agent learns a website's data path once; later runs replay it without anyone (LLM or
human) reading the page. When a CAPTCHA is in the way, the tool tries to clear it itself; a
human is the last resort. It runs on anyone's machine, not just the author's.

## Pieces

| Module | Role |
|---|---|
| `browser.py` | Starts / attaches to a Chromium browser over CDP on `127.0.0.1`, dedicated user-data dir. Named sessions own the tabs they open; `close_session` closes only those. CLI `edge-cdp`. |
| `recipe.py` | Recipe store, the tier ladder (`http` → `page` → `script`), shape checks, login gate, CAPTCHA gate. CLI `edge-recipes`. |
| `hcaptcha.py` | Optional hCaptcha image/drag solver (cloud, opt-in). |
| `config.py` | Reads `config.toml` from the OS config dir. Stdlib only. |
| `secretstore.py` | Secret backends: OS keyring (default), environment, or your own Python function. |
| `notify.py` | "A human is needed" notifications: ntfy URL, any command, or stderr. |

## Recipes

A recipe records what worked on a site: an entry URL, parameters, and one or more ways to get the
data — a plain HTTP request (cookies read live from the browser, never stored), a `fetch()` from
inside a tab on the site, or a script run in the page. Runs start at the cheapest tier that
verified and fall through on failure. Every response is compared with the JSON shape recorded at
verify time; a mismatch is reported as *stale* and never returned as data.

`save` refuses a draft unless the exact same draft passed `verify`. Recipes with side effects
(`"writes": true`) need `--allow-write` on every run.

## CAPTCHA gate

In order, stopping at the first that clears:

1. **wait** — scroll widgets into view; Turnstile, Cloudflare interstitials and invisible
   reCAPTCHA v3 usually pass on a real, headed, long-lived profile.
2. **click** — one trusted input click on the checkbox (reCAPTCHA v2, hCaptcha, Turnstile).
   With the opt-in hCaptcha solver enabled, the solver performs this click itself (it has to
   watch the challenge's network traffic from before the click) and answers the challenge.
3. **audio** — reCAPTCHA v2 image grid → audio challenge → local `faster-whisper`.
4. **human** — notify and wait.

Text-in-image CAPTCHAs are solved on request by a recipe (`solve_image_captcha`, local `ddddocr`).

### hCaptcha (opt-in, cloud)

hCaptcha's current challenges are mostly canvas drag puzzles. Small local vision models were tried
and are not usable (wrong coordinates, or minutes per challenge). The working option is
[hcaptcha-challenger](https://github.com/QIN2DIM/hcaptcha-challenger) with Google Gemini:

- **Off by default.** `[captcha] hcaptcha_solver = "gemini"` turns it on.
- **What leaves your machine:** the cropped challenge view (the puzzle image and its prompt),
  sent to Google's Gemini API under your own key. Not the page, not cookies.
- **License:** hcaptcha-challenger is GPL-3.0. It is not bundled or vendored; you install it
  yourself. The core package stays MIT.
- **Measured (author's machine, small n):** "drag the missing item into the grid" challenges
  13/15 solved, 26–63 s each, with `gemini-3.7-flash` and hcaptcha-challenger git `28ee79b`.
  Hexagon "drag matching shapes into place" puzzles are much weaker: 0/5 on PyPI 0.19.0 and
  1/5 through this package's gate on `28ee79b` (~2 min each) — the model mostly pairs the right
  shapes but drops them off-centre. Treat it as best-effort with a human fallback.

## Guard rails

- **Refused hosts.** Mail, banking and brokerage hosts are refused (built-in list plus
  `deny_hosts_extra`). Ticketing and rail-booking hosts are refused by default because several
  jurisdictions criminalise automated ticket purchase; `allow_ticket_hosts` lifts entries if you
  decide otherwise — your call and your liability.
- **Secrets by name.** Recipes hold `{secret:NAME}` placeholders; values are resolved in-process
  and never printed, returned or written to disk by this tool.
- **Local CDP only.** The debugging port is bound to `127.0.0.1`.
- **No paid CAPTCHA-solving services**, now or later.

## Not done

- A generic local vision-model endpoint (OpenAI-compatible) and an optional GPU-lock hook. They
  only matter once a local model is good enough for a CAPTCHA type; none is today.
- Linux has browser detection but no real-machine test yet.
