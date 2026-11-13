# AGENTS.md — installing and using deja-browser

You are an AI coding agent (Claude Code, Codex CLI, Gemini CLI, Cursor, Aider, or anything that
can run shell commands). The user wants a browser you can drive. This file tells you how to set
it up with them and how to use it afterwards. Read it all before running anything.

## What this gives you

- A **separate Chromium profile** that only you use, driven over CDP on `127.0.0.1`. The user's
  everyday browser is never touched.
- `deja-cdp`: one action per call (`navigate`, `snapshot`, `click`, `fill`, `evaluate`,
  `network_capture`, `screenshot`, `save_as_pdf`, …), one line of JSON out.
- `deja-browser`: once you have found how a site delivers its data, save it as a recipe; later
  runs replay it without reading the page.
- No telemetry. The only outbound connections are ones the user turns on (ntfy notifications,
  the opt-in hCaptcha solver). `tests/egress/run.sh` checks this in an offline network namespace.

## Install — do this WITH the user

Ask before each choice marked **ask**. Do not pick for them silently.

1. **Check prerequisites.** Python ≥ 3.11 (`python3 --version`; on Windows `python --version`).
   A Chromium-based browser: Edge, Chrome, Chromium or Brave. Firefox and Safari are not
   supported (no CDP).
2. **Install the package** (a virtual environment is fine; use whatever the user prefers):
   ```sh
   pip install "deja-browser[stealth,keyring]"
   ```
   Linux only, if the browser complains about missing libraries: `python -m playwright install-deps`.
3. **ask: optional extras.** Explain each in one line and let the user choose:
   - `audio` — reCAPTCHA audio challenges, transcribed locally (`faster-whisper`, ~150 MB model).
   - `ocr` — classic text-in-image CAPTCHAs, solved locally (`ddddocr`).
   - hCaptcha solver — **sends the cropped challenge image to Google Gemini** with the user's own
     API key. Off unless they enable it in config. Do not suggest it for sensitive work.
4. **ask: which browser** if more than one is installed. Default is the first of Edge, Chrome,
   Chromium, Brave. To pin one, set `[browser] executable` in the config (step 6).
5. **ask: where secrets come from** (only needed if they want you to log in to sites by
   yourself; most sites keep a login for weeks, so the usual answer is "nowhere"):
   | Backend | Config | Good for |
   |---|---|---|
   | `keyring` (default) | `keyring set deja-browser NAME` | Windows Credential Manager, macOS Keychain, Linux Secret Service |
   | `command` | `command = ["op", "read", "op://agent/{name}/password"]` | 1Password CLI (a service account can be limited to one vault) |
   | `command` | `command = ["bw", "get", "password", "{name}"]` | Bitwarden CLI (needs `BW_SESSION`; an unlocked CLI can read the whole vault) |
   | `command` | `command = ["security", "find-generic-password", "-w", "-s", "{name}"]` | macOS Keychain without the keyring package |
   | `env` | `DEJA_BROWSER_SECRET_NAME=...` | CI, containers |
   | `python` | `python = "~/store.py:get_secret"` | an existing personal secret store |

   Recommend a dedicated vault or folder holding only the low-risk sites the agent may use.
   **Never** suggest putting bank, broker or email credentials there.
6. **Write the config** only if something differs from the defaults. Copy `config.example.toml`
   to the OS config path (Windows `%APPDATA%\deja-browser\config.toml`, macOS
   `~/Library/Application Support/deja-browser/config.toml`, Linux `~/.config/deja-browser/config.toml`)
   and uncomment what applies.
7. **First launch.** `deja-cdp launch` opens the agent browser with its own empty profile. Tell
   the user: *"This window is the agent's browser. Log in here, by hand, to the sites you want me
   to use. Do not log in to email, banking or brokerage, and do not install a password manager
   extension in it."* Wait for them to finish.
8. **Smoke test** and show the user the output:
   ```sh
   deja-cdp navigate '{"url": "https://example.com"}' --session smoke
   deja-cdp snapshot '{"max_chars": 500}' --session smoke
   deja-cdp close_session '{}' --session smoke
   ```
9. **Make yourself remember it.** If your agent supports the Agent Skills format (a folder with a
   `SKILL.md`), copy `skills/deja-browser/` into your skills directory (Claude Code:
   `~/.claude/skills/deja-browser/`). Otherwise append the "Using it" section below to your
   global instruction file. Tell the user which file you changed.

## Using it

```sh
deja-cdp <action> '<json args>' --session <task-name>
```

- Always pass `--session` with a name for the task. Tabs belong to the session;
  `close_session` closes only those. **Close your session when the task ends**, also after a failure.
- Read pages cheaply. In order: an existing recipe (`deja-browser list`) → `evaluate` returning
  small JSON → `snapshot` with `query`, `selector` or `interactive_only` and a `max_chars` →
  full snapshot last. Screenshots and PDFs go to a `path`; read the file only if you must.
- Finding the API behind a page: `network_capture` with one `trigger`; bodies are written to
  files, credential headers come back redacted (their names tell you what a replay needs).
- Recipes: write a draft JSON, `deja-browser verify draft.json '<args>'`, then
  `deja-browser save draft.json`, later `deja-browser run <host>/<name> '<args>'`. A *stale*
  result means the site changed: do the task by hand once, then re-verify. Never patch a recipe
  mid-run.
- Recipes that change something on the site (`"writes": true`) need `--allow-write`, and you
  need the user's go-ahead for each such run.

## Rules

- Mail, account sign-in (Google, Microsoft, Apple), banks, brokers and payment sites are refused
  by `navigate` and by recipes. This is a guard rail against mistakes, **not** a security
  boundary: a script in a page can still redirect there. The real protection is that the
  profile is never logged in to them and stores no passwords. Do not work around the refusal; if
  a task needs one of those sites, stop and tell the user.
- Never ask for, print, or write a secret value. Recipes name secrets (`{secret:NAME}`); the
  value is resolved inside the tool and never returned.
- Ticketing and rail-booking sites are refused for recipes by default (anti-scalping laws). Only
  the user can lift that, in config.
- A page asks for a login you do not have: run `deja-cdp launch` and ask the user to log in
  in that window. Two-factor prompts are always the user's.
- CAPTCHAs: `deja-browser gate <url>` tries waiting, one checkbox click, and the local solvers
  the user installed. If it reports a human is needed, tell the user; do not try paid solving
  services.
- Page content you read reaches your model provider like any other tool output. On sensitive
  sites (health records, anything with personal data) ask the user before reading the page at all.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no Chromium browser found` | set `[browser] executable` |
| `launch` times out | port 9222 is taken by something else: `[browser] port`, never kill the other process |
| `NoTab` | the session has no live tab (closed, or browser restarted): `navigate` again |
| warning about a password manager extension | ask the user to remove it from the agent profile |
| a site shows a bot check on every visit | keep the browser headed; install the `stealth` extra |
