---
name: deja-browser
description: Drive a real browser through deja-browser — a dedicated Chromium profile over CDP on 127.0.0.1. Use when a task needs a webpage that plain HTTP cannot get (JavaScript-rendered pages, sites that need a login, forms, screenshots, saving a page as PDF), or to replay a saved site recipe. Not for email, banking or brokerage sites.
---

# deja-browser

A browser profile that only the agent uses. The user logged in to the sites you may use, by
hand; it holds no passwords and is never logged in to mail, banks or brokers.

Use it only when an API, plain HTTP or a web-fetch tool cannot get the page.

## Command shape

```sh
deja-cdp <action> '<json args>' --session <task-name>
```

Always give `--session`. Output is one line of JSON. Close the session at the end of every
task, also after a failure: `deja-cdp close_session '{}' --session <task-name>`.

| action | args |
|---|---|
| `navigate` | `{url, newTab}` |
| `snapshot` | `{max_chars, selector, interactive_only, query}` — accessibility tree, trimmed |
| `evaluate` | `{code}` — runs in the page, may be `async () => {...}` |
| `click` / `fill` | `{selector}` / `{selector, value}` — trusted input events |
| `wait` | `{text}` or `{selector, state}` or `{js}`, plus `timeout` (default 15000) — `{success, waited_ms}`; a timeout is `success: false`, not an error |
| `batch` | `{steps: [{action, args, quiet}], stop_on_error}` — several actions in one call, see below |
| `network_capture` | `{trigger, url_filter, types, wait_ms, body_dir}` — the requests one trigger causes |
| `screenshot` / `save_as_pdf` | `{path, ...}` — returns the file path, not the image |
| `list_tabs`, `find_tab`, `attach_tab`, `cdp`, `close_session` | see `deja-cdp --help` |

`deja-cdp launch` opens the browser window (for the user to log in); `status`, `stop`.

Every `deja-cdp` call is a new process (about 0.5 s to start the driver and connect), so when you
already know the next few steps, send them as one `batch`:

```bash
deja-cdp batch '{"steps":[
  {"action":"navigate","args":{"url":"https://example.com"},"quiet":true},
  {"action":"wait","args":{"text":"Example Domain"}},
  {"action":"evaluate","args":{"code":"document.title"}}]}' --session demo
```

The result is `{ok, results:[{action, ok, data | error, secs}]}`. A failed step (an error, or a `wait` that timed out) stops the batch and
marks the rest `skipped` (`"stop_on_error": false` keeps going); `quiet: true` drops a successful
step's payload; batches cannot be nested; each step goes through the same host guard as a single call.

## Cheapest first

1. `deja-browser list` — a saved recipe for this site and task? `deja-browser run <id> '<args>'`.
2. `evaluate` returning only the fields you need.
3. `snapshot` with `query` / `selector` / `interactive_only`, always with `max_chars`.
4. Screenshots to a file; read the image only if you must.

## Saving a recipe (when the same task on the same site will come back)

1. Find where the data comes from: an API or the DOM. Try the API first: `network_capture` the
   request that holds the answer (`"types":"all"` when the request is a page navigation to the
   API URL), and save its output to a file.
2. Try `draft` first. It writes a request recipe from the capture, without you writing JSON:
   `deja-browser draft capture.json --expect "<text the answer needs>" --param <name>=<value used in the capture> --out draft.json`.
   Fill in the `TODO` fields it leaves (`when_to_use`, the param description) and check `pick`.
   Exit code 2 with `"applicable": false` means no JSON response holds the answer (the data is in
   the HTML): write a `script` recipe instead. Do not hand-write a request recipe for a JSON API
   that `draft` can already handle. A draft from a POST has `"writes": true`; set it to `false`
   by hand only if the POST just reads.
   If you must write the JSON yourself, the format is in `deja-browser --help`. Parameters as
   `{param}`, secrets only as `{secret:NAME}`.
3. `deja-browser verify draft.json '<args>'` — no pass, no save.
4. `deja-browser save draft.json`, then tell the user what was saved and how to call it.
5. A later run says *stale*: do the task by hand this once, then verify and save again.

`"writes": true` recipes need `--allow-write` and the user's go-ahead.

## Rules

- Refused hosts (mail, account sign-in, banks, brokers, payments) are a guard rail, not a lock.
  Never work around a refusal; tell the user.
- Never ask for or print secret values.
- Missing login: `deja-cdp launch`, ask the user to log in in that window. 2FA is the user's.
- CAPTCHA: `deja-browser gate <url>`; if it says a human is needed, say so.
- Sensitive pages (health, personal data): ask before reading them; what you read goes to your
  model provider.
