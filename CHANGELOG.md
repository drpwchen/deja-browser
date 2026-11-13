# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.2] — 2026-11-14

### Added
- Demo GIF at the top of the README: the same Hacker News search as a first visit (navigate +
  snapshot, 29,427 characters of page tree) and as a recipe replay (588 characters of JSON, no page
  opened). Numbers come from a real run.

## [0.1.1] — 2026-11-14

### Changed
- **Renamed to deja-browser** (was edge-recipes): distribution `deja-browser`, import
  `deja_browser`, commands `deja-cdp` (was `edge-cdp`) and `deja-browser` (was `edge-recipes`),
  config/data dirs `deja-browser`, env vars `DEJA_BROWSER_*`, skill folder `skills/deja-browser/`.
  The old names keep working: an existing `edge-recipes` config or data dir is used when the new
  one does not exist, `EDGE_RECIPES_*` env vars are read when the new one is unset, and the
  default keyring service / env secret prefix fall back to `edge-recipes` / `EDGE_RECIPES_SECRET_`.

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
- `edge-cdp`: drive a dedicated Chromium profile (Edge, Chrome, Chromium, Brave; auto-detected)
  over CDP on `127.0.0.1`, with named sessions that close only their own tabs.
- `edge-recipes`: learn-once / replay recipes with an `http` → `page` → `script` tier ladder,
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
