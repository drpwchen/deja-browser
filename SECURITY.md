# Security policy

deja-browser drives a real browser profile and can hold references to secrets, so a bug here can
expose a session or a credential.

## Reporting a vulnerability

Please report it privately through GitHub: the repository's **Security** tab, then **Report a
vulnerability**. Do not open a public issue for it.

Useful in a report: the version, your OS and browser, and the smallest steps that reproduce it.
Never attach cookies, tokens, a browser profile directory or a real `config.toml`.

## What counts

- A way to make the tool open a connection other than to `127.0.0.1` or a feature you enabled
  (see `tests/egress/run.sh`).
- A secret value printed, logged, returned to the agent or written into a recipe.
- The CDP port or profile reachable from outside the local machine.
- Bypassing the refused-host list in a way the README says it prevents. (The README already notes
  it is a guard rail, not a lock.)

## Supported versions

Only the latest release receives fixes.
