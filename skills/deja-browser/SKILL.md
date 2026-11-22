---
name: deja-browser
description: Drive a real browser through deja-browser — a dedicated Chromium profile over CDP on 127.0.0.1. Use when a task needs a webpage that plain HTTP cannot get (JavaScript-rendered pages, sites that need a login, forms, screenshots, saving a page as PDF), or to replay a saved site recipe. Not for email, banking or brokerage sites.
---

# deja-browser

The agent's own browser profile; the user logged in to its sites by hand. Use it only when an
API or plain HTTP can't get the page. `deja-cdp help <action>` gives any action's args.

```sh
deja-cdp <action> '<json args>' --session <task>     # one line of JSON out
```

## Flow

1. Recipe for this host? `deja-browser list` (`navigate` replies list them too) →
   `deja-browser run <host>/<name> '<json args>'`. No page read.
2. Else do the task once:
   - `navigate {url}`, then `read {query}` or `snapshot {refs:true, interactive_only:true, max_chars:3000}`.
   - Act on the refs: `click {selector:"@e5"}`, `fill {selector:"@e7", value}`,
     `select {selector:"@e9", label}`; `then_snapshot:true` shows the result in the same reply.
   - Finish with one small `evaluate {code}` that returns just the answer (with no JSON API, it
     learns a script recipe; a final `read` learns a slower step-by-step one).
3. Will this come back? `deja-browser learn --name <name> --session <task> --expect "<value>"`
   (before `close_session`). `--expect` = text copied from the actual answer, ≥ 3 chars
   (`"1.0.228"`, not `"version"`). `--param q=<value you typed>` names a param. On `ok:false`
   follow its `hint`; on success tell the user what was saved.
4. After a `navigate`: `deja-cdp close_session '{}' --session <task>`, also after a failure. A
   `run` alone needs none (it keeps its own `recipe-<host>` tab for the next replay).

## Legacy pages

- Frames: `read` / `snapshot` include iframe and frameset content under a
  `[frame … selector prefix: "…"]` header; `frame:"#work"` (or a frame ref) scopes an action.
- Dialogs: an action's reply carries `dialog: {type, message, handled}`; by default `confirm` /
  `prompt` are dismissed. Pass `dialog:"accept"` (or the prompt text) when it should go through.
- Popups: replies list `popups: [{tab, url}]`; switch with `attach_tab {tabId:"t2"}`.
- Partial postbacks: `wait {selector}` (`deja-cdp help wait`).
- Flows with no JSON API still `learn`: they become `steps` recipes.

## A human is needed

Try first: `captcha_text` for an image-text CAPTCHA (hard images: give `charset`, `length`,
`preprocess`, `min_confidence`; `deja-cdp help captcha_text`), `deja-browser gate <url>` for other
challenges. Login, 2FA, a failed challenge or a judgement: `handoff {message:"Log in, then
press Done"}` gives the user the tab and a Done button; continue on `success:true`.

## Rules

- Refused hosts (mail, account sign-in, banks, brokers, payments) are a guard rail; never work
  around one — tell the user. Only the user may switch the list off
  (`[recipes] deny_hosts_enabled = false`); never change it yourself.
- Recipes that change something (`"writes": true`, `learn --write`) run only with
  `--allow-write` and the user's go-ahead for each run.
- Never ask for or print secret values.
- Sensitive pages (health, personal data): ask first; what you read goes to your model provider.
