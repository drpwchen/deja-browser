# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.1] — 2026-11-21

### Added
- `docs/benchmark.md`: a small benchmark of deja-browser against agent-browser, Ego Lite and Claude in Chrome on five public pages, including a run where one model drove deja and Claude in Chrome from the same neutral prompt, and the conditions that favour deja. Linked from the README comparison table. No code changes.

## [0.3.0] — 2026-11-21

### Added
- `captcha_text` action: `{image, input, submit, ok, fields, case, length, refresh, tries}` solves a text-in-an-image CAPTCHA with local `ddddocr`. Without `submit` it reads and fills the code; with `submit` it also submits and retries until the JS expression `ok` is true, refilling `fields` (`{selector: value}`, never echoed back) on each attempt, because a rejected submit usually reloads the form empty.

### Changed
- Text CAPTCHAs (`captcha_text` and `recipe.solve_image_captcha`) now read the image's own pixels through a canvas at its natural size instead of an element screenshot, which could be cropped or rescaled (a 4-character code was read as 3). An element screenshot is only the fallback for a cross-origin image. When the length is known (the input's `maxlength` or `length`), a wrong-length read is refreshed and read again instead of being submitted. Unless `case` is given, attempts try the code as read, then upper case, then lower case (`ddddocr` answers in lower case; some sites compare case-sensitively). `solve_image_captcha` takes the new `case`, `length`, `refresh_selector` and `fields` arguments; existing calls keep working.
- `batch`: a step that ran but returned `success: false` keeps its `data` next to the `error`, so the reason is visible.
- When the CDP port is taken by another browser, `deja-cdp` no longer stops with an error. A browser left behind by deja-browser's own test suite (profile in a temp `deja-browser-test-*` directory whose test run has ended) is closed and the port reused; any other browser is left running and the agent browser starts on the next free port after `port`, recorded in `<profile>/.deja-cdp-port` so later runs (and `deja-cdp status` / `stop`) find it. A stale record is dropped. `deja-cdp launch` / `status` print the port in use.

### Fixed
- The test suite no longer starts a real browser: three `verify` tests ran the real page/script tiers, which launched a browser on port 9222 and could leave it running after the tests ended. Tests now use a random port (never 9222), fail if a test tries to start a browser, and close any browser on a test profile when the run ends.

## [0.2.0] — 2026-11-20

### Added
- `wait` action: `{text | selector | js, timeout=15000, state="visible"}` → `{success, waited_ms}`. A timeout returns `success: false` instead of raising.
- `batch` action (`deja-cdp batch '{"steps":[…]}'`): runs several actions in one process and one CDP connection, so the ~0.5 s driver start is paid once instead of per call. Returns `{ok, results:[{action, ok, data | error, secs}]}`; a failed step (an error, or a `wait` that timed out) skips the rest unless `stop_on_error` is false; `quiet: true` keeps only `{ok, secs}` for a step; nesting is refused; every step goes through the normal host guard. Exit code 1 when any step failed.

- `read` action: `{selector="body", query, context_lines=2, max_chars=20000, offset}` → `{url, title, text, chars, full_chars, truncated, next_offset?, hint?}`. Reads the page's `innerText` by lines; with `query` it keeps only the matching lines (case-insensitive) plus `context_lines` around them, with `…` between gaps — a big page answers in a few hundred characters instead of a full snapshot. A query with no match returns empty `text` and a `hint`.
- `snapshot` takes `offset` and returns `next_offset` when truncated, so a long tree can be read in pages.
- `deja-browser run <id> '{"_limit": N}'`: a reserved argument that keeps at most N list items of the result. It only lowers the recipe's saved `pick.limit` (it cannot raise it), is not a template parameter and does not take part in the shape check. A 20-item recipe replayed with `_limit: 3` prints about a sixth of the characters.
- `deja-browser run … --data-only` prints just the data on success; a failed run still prints the full `{ok: false, tried}` report.
- `deja-browser verify` reports `out_chars` per passing tier and `page_chars` (the entry page's visible text). When the recipe's output is more than half of the page's text it adds a `warning` that a plain read may be cheaper. Not measured (no `page_chars`, no warning) when the page can't be read or is a challenge page.

- `deja-browser draft <capture.json> --expect "<text the answer needs>" [--expect …] [--param name=value …]`: writes a `request` recipe draft from a `network_capture` result, so a site with a JSON API goes from first visit to `verify` without hand-writing the JSON. The response is picked by the `--expect` strings (it must be a 2xx JSON response that contains all of them), not by how it looks. A `--param` value that equals a URL path segment, query value or body string becomes a placeholder; the `pick` is `{path, fields}` when the answers are rows of one list, or `{select}` when they are single values. The report lists where each string was found, other candidate responses and warnings (a value found in several places, an ambiguous field, a POST). When no JSON response holds the answer (the data is in the HTML) it prints `"applicable": false` with a hint to write a `script` recipe, writes no file and exits with code 2. Header values are never copied (except Content-Type / Accept); query and body fields that look like credentials are dropped unless named with `--keep-param`; non-GET drafts get `"writes": true`.
- Recipe `pick` accepts `select`: `{"name": "dotted.path"}` (a number in the path indexes a list) → `{"name": value}`; on a list result it is applied to every row. Existing recipes are unaffected.
- A `{param|url}` placeholder in a recipe is percent-encoded when filled in (`{param}` is still inserted as is).

### Changed
- `deja-browser verify` prints a tier's preview in full only the first time: a later tier with the same result shape prints `"same shape as <tier>"`, and a preview's content is capped at 2,000 characters (it used to print every tier in full, up to 20,000 each).
- `deja-browser run` leaves out `tried` when no tier fell through (it was an empty list); a run that did fall back still lists what failed.
- `snapshot` truncation now cuts on a line boundary (it used to cut mid-line, so `tree_text` can be up to one line shorter than `max_chars`) and adds a `hint` that says how to narrow (`query`, `selector`, `read {query}`) or page on (`offset`). The default cap (20,000) and the existing fields (`tree_text`, `chars`, `full_chars`, `truncated`) are unchanged; `next_offset` and `hint` appear only when truncated.

### Fixed
- `network_capture` with `trigger.navigate` now opens a tab when the session has none (it used to fail with `NoTab: … navigate first`); other triggers still need a tab. The host guard runs before the tab is opened.
- `deja-cdp <action>` failures from the driver (`NoTab`, unsupported action, a missing required argument) print `{"ok": false, "error": "…"}` and exit 1 instead of a Python traceback, like `deja-browser` already did. Unexpected exceptions still raise.

## [0.1.4] — 2026-11-14

### Docs
- README quality pass: requirements and cost, expected quick-start output, a support line, a Limitations section and a Support footer.
- Added `SECURITY.md` and a gitleaks secret-scan workflow.
- No code changes (version strings bumped only).

## [0.1.3] — 2026-11-14

### Changed
- README rewritten for a first-time visitor: one-line pitch, badges, the demo GIF, four key points,
  a Quick start, a worked first recipe, and a comparison with browser-extension bridges. Detail
  sections follow, tightened; all links are absolute so the PyPI page renders them.
- `deja-cdp --help` and `deja-browser --help` show the installed command names.

## [0.1.2] — 2026-11-14

### Added
- Demo GIF at the top of the README: the same Hacker News search as a first visit (navigate +
  snapshot, 29,427 characters of page tree) and as a recipe replay (588 characters of JSON, no page
  opened). Numbers come from a real run.

## [0.1.1] — 2026-11-14

### Changed
- **Final names**: distribution `deja-browser`, import `deja_browser`, commands `deja-cdp` and
  `deja-browser`, config/data dirs `deja-browser`, env vars `DEJA_BROWSER_*`, skill folder
  `skills/deja-browser/`. Config and data dirs, env vars and stored secrets from 0.1.0 keep
  working.

### Added
- Published on PyPI: `pip install deja-browser` (releases are uploaded by GitHub Actions
  with Trusted Publishing).
- `deja-cdp` refuses a browser already listening on the CDP port that runs a different profile
  (another tool's browser, or one started while the config could not be read) instead of silently
  driving it. The browser's per-start id is recorded in the profile, so only the first connect to a
  browser it did not start itself pays for the process lookup (~1 s on Windows); every later connect is
  a string compare. `[browser] check_profile = false` turns it off.
- `AGENTS.md`: an install-and-use guide written for the user's AI agent (asks the user the choices
  that matter, has them log in by hand, runs a smoke test). `skills/deja-browser/SKILL.md` for
  agents that read the Agent Skills format.
- `tests/egress/run.sh`: runs every action and a full recipe cycle in a loopback-only Linux network
  namespace under `strace`, fails on any non-loopback connection; `SELFTEST=1` proves it catches one.
- `[secrets] backend = "command"`: any password-manager CLI (1Password `op`, Bitwarden `bw`, macOS
  `security`, `pass`) as the secret source; no shell, stderr never echoed.
- On launch: the agent profile's password, card and address autofill are turned off
  (`[browser] password_manager = true` to skip), and a password-manager extension installed in the
  profile triggers a warning.

### Changed
- `navigate` and `network_capture` now refuse the same hosts as recipes (guard rail, not a lock).
- Refused hosts now include account sign-in pages (Google, Microsoft, Apple), more mail and payment
  hosts, and Taiwan banks and brokers whose hostnames lack "bank".
- Recipes check the API host of `request.url` too, and `login` never types secrets into a refused
  host, wherever the login page redirected.
- README: why this exists, options at a glance, safety model.

## [0.1.0] — 2026-11-12

First public release.

### Added
- Browser driver: a dedicated Chromium profile (Edge, Chrome, Chromium, Brave; auto-detected)
  over CDP on `127.0.0.1`, with named sessions that close only their own tabs.
- Site recipes: learn-once / replay with an `http` → `page` → `script` tier ladder,
  JSON shape checks (stale responses are reported, never returned), `verify`-before-`save`,
  and a `--allow-write` guard for recipes with side effects.
- CAPTCHA gate: wait → trusted checkbox click → reCAPTCHA audio via local `faster-whisper` →
  notify a human. Text-image CAPTCHAs via local `ddddocr`.
- Opt-in hCaptcha solver through hcaptcha-challenger (GPL-3.0, installed separately) and Google
  Gemini; off by default, sends only the cropped challenge image.
- Secret backends: OS keyring, environment variables, or your own Python function. Recipes
  reference secrets by name only.
- Notifications: ntfy URL, any command, or stderr.
- Refused hosts: mail, banking and brokerage (extendable); ticketing and rail booking by default
  (`allow_ticket_hosts` to lift).
- Config file in the OS config directory; `config.example.toml`.
