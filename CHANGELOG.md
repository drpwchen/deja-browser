# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
