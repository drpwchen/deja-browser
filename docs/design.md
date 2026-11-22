# deja-browser — design

**Goal:** an agent learns a website's data path once; later runs replay it without anyone (LLM or
human) reading the page. When a CAPTCHA is in the way, the tool tries to clear it itself; a
human is the last resort. It runs on anyone's machine, not just the author's.

## Pieces

| Module | Role |
|---|---|
| `browser.py` | Starts / attaches to a Chromium browser over CDP on `127.0.0.1`, dedicated user-data dir. Named sessions own the tabs they open (popups they open included); `close_session` closes only those. Element refs, frames, dialogs, `handoff`. CLI `deja-cdp`. |
| `daemon.py`, `client.py` | Background daemon that keeps the driver and the CDP connection warm (127.0.0.1, token file, one worker per session; restarts itself when the code or config changes). The CLI talks to it through a stdlib-only client and falls back to running in-process. |
| `recording.py`, `learn.py`, `draft.py` | Per-session recording of a first visit (memory only, secrets redacted) and `learn`, which turns it into a recipe; `draft` does the same from an explicit `network_capture`. |
| `driverpipe.py` | Windows only: drains the Playwright driver's output between calls so an idle library caller can't stall the whole browser. |
| `recipe.py` | Recipe store, the tier ladder (`http` → `page` → `script`, or `steps`), shape checks, login gate, CAPTCHA gate. CLI `deja-browser`. |
| `hcaptcha.py` | Optional hCaptcha image/drag solver (cloud, opt-in). |
| `config.py` | Reads `config.toml` from the OS config dir. Stdlib only. |
| `guard.py` | Guard rails: refused hosts (shared by `navigate` and recipes), turning off the profile's own password / card / address autofill on launch, warning about password-manager extensions in the profile. Not a security boundary — separation is. |
| `secretstore.py` | Secret backends: OS keyring (default), a command (password-manager CLIs), environment, or your own Python function. |
| `notify.py` | "A human is needed" notifications: ntfy URL, any command, or stderr. |

## Recipes

A recipe records what worked on a site: an entry URL, parameters, and one or more ways to get the
data — a plain HTTP request (cookies read live from the browser, never stored), a `fetch()` from
inside a tab on the site, or a script run in the page. Runs start at the cheapest tier that
verified and fall through on failure. Every response is compared with the JSON shape recorded at
verify time; a mismatch is reported as *stale* and never returned as data.

`save` refuses a draft unless the exact same draft passed `verify`. Recipes with side effects
(`"writes": true`) need `--allow-write` on every run.

Flows with no JSON API that cross full-page postbacks, frames and popups (older / ASP.NET-style systems)
are saved as `steps` recipes: browser actions replayed in order, each with an optional check
(`expect`); a step that fails is reported as stale with its index.

## First visit, then `learn`

The first visit only does the task. While the agent works through the daemon, the session's actions and
the responses on its own tabs are kept in memory (credential-looking values and anything typed into a
password field stored as `<redacted>`; refused hosts and online-banking / trading pages never recorded).
`close_session` runs `learn` itself when the host has no recipe yet (auto-learn, on by default; the check
values come from the visit's last read / evaluate result, and a visit that sent a request that may change
something is skipped). `deja-browser learn --name N --expect TEXT` does the same by hand. It tries, in order: a recorded JSON response that holds the
answer (a `request` recipe), the last `evaluate` that returned it (a `script` recipe), the recorded
actions (a `steps` recipe). It verifies with the recorded values and saves only the cheapest tier that
passes; output is a few hundred characters, so learning costs the model almost nothing. A value that came
from an earlier response (an id from a search result) is never silently reused as a constant.

## No silent success

An action that reports success must have done the thing. Before 0.4, testing pages that imitate older
systems showed four correctness problems: a `confirm()` cancelled by the driver while `click` returned
success; a dialog opening after a CLI call had exited, which blocked every later connection to the
browser; iframe content invisible to `read` / `snapshot` with no hint; and `fill` on a `<select>` by its
visible label returning success with nothing selected. Now:

- **Dialogs** on the session's tabs are answered at once (the action's `dialog` policy, default: accept
  `alert`, dismiss `confirm` / `prompt`) and reported in the reply; a dialog left open would block every
  other client of the browser.
- **Popups** opened by the session's tabs join the session (listed in the reply, closed by
  `close_session`).
- **Frames** are read and snapshotted with their content, each under a header with a selector prefix
  that reaches into it; `select` reports the option actually in effect.
- **Connecting** times out after 10 s and names the likely cause instead of hanging.

The fixture is a set of local pages, not a real .NET application; no hospital system was tested.

## A step for a human

`handoff` gives the session's tab to a person (brought to the front, a notification, a "Done — hand back
to the agent" button in the page) and continues when it is pressed, a condition holds, or a timeout
passes. Meant for two-factor codes and judgement calls, so a flow doesn't restart.

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
  `deny_hosts_extra`); `deny_hosts_enabled = false` (default `true`) switches that list off, leaving
  separation as the only protection. Ticketing and rail-booking hosts are refused by default because several
  jurisdictions criminalise automated ticket purchase; `allow_ticket_hosts` lifts entries if you
  decide otherwise — your call and your liability.
- **Secrets by name.** Recipes hold `{secret:NAME}` placeholders; values are resolved in-process
  and never printed, returned or written to disk by this tool.
- **Local CDP only.** The debugging port is bound to `127.0.0.1`.
- **No paid CAPTCHA-solving services**, now or later.

## Not done

- A generic local vision-model endpoint (OpenAI-compatible) and an optional GPU-lock hook. They
  only matter once a local model is good enough for a CAPTCHA type; none is today.
- Linux is tested headless only (WSL2), and the 0.4 daemon not yet on macOS.
- A recipe's first run in a session opens a new tab: 1.4–2.1 s on three of the four benchmark pages,
  against 0.4–1.0 s once that tab is open.
