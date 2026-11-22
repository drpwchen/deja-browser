# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.4.1] — 2026-11-22

### Testing
- A test's made-up API key is now an obviously fake low-entropy string: the repository's secret scan (gitleaks, `generic-api-key`) flagged the one in 0.4.0. It was never a real key. No code changes.

## [0.4.0] — 2026-11-22

This release targets two things: a first visit that costs the model less (a warm daemon, element refs, one `learn` call instead of draft / verify by hand), and pages from older systems (dialogs, frames, popups, `<select>`, flows with no JSON API, a step only a human can do).

### Added

**Background daemon**
- `deja-cdp <action>` (every page and session action, `batch` included) and `deja-browser run` send the request to a daemon (`deja_browser/daemon.py`, client `deja_browser/client.py`) that keeps the Playwright driver and the CDP connection warm, instead of starting a driver and reconnecting on every call. On a test profile, a warm `read` of example.com through the daemon took 0.11 s median (n=10).
- The first call starts it (detached, no window), so that one call takes longer. It listens on 127.0.0.1 only and needs a token from a state file in the data dir (`<data dir>/daemon/`). It restarts itself when the package code or the config file changes; a session's connection is dropped after 10 idle minutes (the browser is never closed), and the daemon exits after 30. Each session runs on its own worker, so a long `wait` in one session does not delay another.
- If the daemon can't be reached, the call runs in-process as before, with a note on stderr; if the reply is lost after the request was sent, the CLI reports an error instead of running the action twice. `DEJA_BROWSER_DAEMON=0` turns the daemon off. The library API (`wb()`) is unchanged and never uses it.

**Element refs and cheaper looks**
- `snapshot {refs: true}` tags controls (buttons, links, text boxes, comboboxes, options, …) and named headings, table cells and images with `[ref=eN]`, inside frames too, and each `[frame …]` header with a ref of its own. `click`, `fill`, `select`, `wait`, `read`, `snapshot` and `screenshot` take `@eN` (or `eN`) wherever they take a selector, and `frame: "@eN"` scopes to a frame, so the model acts on what the snapshot shows instead of writing a CSS selector the tree doesn't carry.
- A ref is the element's role, accessible name and position among elements with the same role and name in its frame (Playwright role locators; nothing is written into the page). Refs are stored per session in a `<sessions file name>.refs/` folder next to the sessions file, so they work from a later CLI process, through the daemon or in-process, and from library callers. Before acting, deja checks that it is the same tab, the page and frame have not navigated, and the same number of elements still match; otherwise the action fails with "stale ref @eN: page changed — take a new snapshot" instead of picking another element. Ids are per session and never reused; an element on an unchanged page keeps its id across snapshots.
- Ref cost, measured on Wikipedia's Periodic table article (705,133 chars of tree): refs add 10 % to the full tree and 39 % to `interactive_only` (181,075 vs 129,899 chars); the first 20,000 chars carry 173 refs. On crates.io: 15,917 vs 14,657 chars, 105 refs. A ref is checked with one role-locator query before the action, which then queries again: a whole `wait {selector: "@eN"}` CLI call (process start included, through the daemon, median of 5) took 0.22 s on crates.io and 0.99 s on the Wikipedia article.
- `then_snapshot` on `click`, `fill`, `select`, `navigate`, `handoff` and `batch`: after the action, the reply carries `snapshot` (default `{interactive_only: true, refs: true}`, or an object of snapshot args with an optional `wait` first), so the model doesn't need a separate call to see the result. A navigation the action started (a link, a form submit, a postback inside a frame) is waited for first; a change made by script / XHR needs `wait`. A failed snapshot is reported there and doesn't fail the action.
- `snapshot {delta: true}`: `{unchanged: true}` when the tree is the same as the last snapshot of this tab, URL and arguments (also across processes), a compact unified diff (`delta: "diff"`) while under half the lines changed, else the full tree (`delta: "full"`).
- `click`, `fill` and `select` with a CSS / text selector that matches more than one element still act on the first, and now say so: `matches: n` and a `warning`.
- `navigate` lists saved recipes for the destination host as `recipes: ["<host>/<name>", …]` (at most 10, from a directory listing), so the agent knows a replay exists.

**Learn a recipe from the visit**
- Passive recording of a first visit (daemon only; `deja_browser/recording.py` has the rule and the schema). While an agent works through `deja-cdp`, the daemon keeps, per session and in memory only, every action it ran (args, ok, URL before / after, the result of `evaluate` / `read` / `select` up to 2,000 chars, the `success` of a `wait`, and for a `selector` / `frame` given as a ref what it resolved to; batch steps unpacked; a `fill` into a password / one-time-code field, also one given as a ref, is stored as `<redacted>`) and the finished responses on the session's own tabs: the main document, a form posted inside a frame, same-site xhr / fetch, and xhr / fetch from other sites that return JSON (a page's own API such as `*.algolia.net`). Request headers are redacted exactly like `network_capture`; in a recorded request body the value of a credential-looking field (urlencoded or JSON: password, token, key, session …) and every value the session typed into a password / one-time-code / card field or passed as `captcha_text` `fields` is stored as `<redacted>`, and `learn` refuses a request that held one. Refused hosts (mail, sign-in, banks, brokers, ticketing; the first four follow `[recipes] deny_hosts_enabled`) and borrowed tabs are never recorded. Online banking, web ATM and online trading pages (a token in host + path such as `ebank`, `netbank`, `webatm`, `mybank`, `etrade`; `[recipes] no_record_extra` adds more) are never recorded either, responses or commands, even with `deny_hosts_enabled = false`: a bank's campaign and sign-up pages are recorded, its account pages are not. Ring buffer of 100 responses / 16 MB per session; bodies over 256 kB are not kept. `close_session` clears it. Daemon action `recording {clear?, body_dir?}`; `deja-browser recording [--session S] [--clear] [--verbose]` prints a summary.
- `deja-browser learn --name N --expect TEXT [--expect …] [--session S] [--param name=value …] [--keep-param K] [--write] [--force] [--verbose]`: one call turns the recording into a verified, saved recipe. It tries, in order: a recorded JSON response that holds every `--expect` becomes a `request` recipe (the same rules as `draft`); else the last `evaluate` whose result holds them becomes a `script` recipe for that page; else the recorded browser actions become a `steps` recipe (below). It then verifies every tier with the recorded values (a tier passes only if its output contains every `--expect`) and saves the cheapest one; nothing is saved when verify fails. Output is at most 400 chars: `{ok, id, tier, kind, params, sample_chars}` or `{ok: false, reason, hint}`.
- Params: `--param`, else the values the agent typed (`fill` / `select`) and the URL values of the visit, wherever one is a whole URL segment, query value, body value or JS string literal (never a part of a longer value). A request value that came from an earlier response (an id from a search result) is refused unless it is passed as `--param`, so a replay never silently reuses it (another recorded response that holds the answer without such a value is tried first: on crates.io the first-ranked `/api/v1/crates/<crate>/<version>/…` is refused and `/api/v1/crates/<crate>` is used); a non-GET request that does not look like a read needs `--write`.
- When the page the `evaluate` read was the answer to a form post-back (server-side forms, ASP.NET `__VIEWSTATE` chains such as quotes.toscrape.com's search), the script replays the recorded chain of posts with `fetch()` from the page it started at: typed values become params, hidden inputs are read again from the page before on every run, and the agent's code runs on the last page.
- Script recipes may have `"at_entry": true`: the script runs on `entry_url` itself, and `entry_url` may hold `{param}` / `{param|url}` in its path or query (never in its host).

**Flows with no JSON API: `steps` recipes and `learn` path 3**
- `steps` recipes, for sites whose flow crosses full-page postbacks, frames and popups (legacy / .NET WebForms systems), which one `script` in one document can't do: `"steps": [{"action", "args", "expect"?, "dialog"?}]`, browser actions (navigate, click, fill, select, wait, evaluate, read; snapshot only last) run in order in the recipe's session, through the daemon or in-process. `{param}` / `{param|url}` are filled into string args, never into JavaScript (`code`, `js`); an evaluate's code that is a function gets the params as its argument. `expect` (`{text | selector | js | url, timeout}`) is checked after its step; a step that fails or an expect not met is reported as stale with its index (`tried: [{tier: "steps", step, action, stale: true, error}]`), with the last dialog receipt when there was one. A click / select / evaluate that starts a navigation (also a postback inside a frame) is waited for before the next step. The last step's value / text is the output, then `pick` and the signature check. New tier `steps` (only for steps recipes); `writes: true` needs `--allow-write`. Refs (`@eN`) and `{secret:…}` are refused in steps at verify, save and run.
- `learn` path 3: when no JSON response and no evaluate / form chain gives a recipe, the recorded browser actions that led to the last `read` or `evaluate` holding every `--expect` become a steps recipe. From the last `navigate` before it, navigate / click / fill / select / evaluate are kept (refs replaced by the selector they resolved to when they ran; a `dialog` arg moved to the step; `select {label}` becomes `select {value}`), failed and look-only commands dropped; a step after which the tab's URL path changed gets `expect {url}`, a click / select otherwise waits for the next step's target. Typed values (or `--param`) become params wherever they are a whole value, including string literals in evaluate code. It is verified with the recorded values in a fresh tab of its own session (`learn-<host>`, closed afterwards) and saved only when that passes. Refused: a password / one-time-code fill, a flow with `attach_tab` / `find_tab` / `handoff` / `captcha_text` / `cdp` / `network_capture`, a credential-looking query field in a navigate URL, and a non-GET request in the flow that does not look like a read, unless `--write`. Verify replays the flow, so `learn --write` performs the write once more.
- On a local test fixture of a hospital-style form (iframe form, cascading department → doctor selects with an 800 ms delay, `confirm()`, a postback inside the frame), a learned recipe gave the right answer for new params through the daemon and in-process, also with an `evaluate` in the frame as the last step (the typed value in its code became `args.pid`); one `deja-browser run` took 2.8–3.5 s wall clock (n=3, CLI process included).

**Pages from older systems**
- JavaScript dialogs, with and without the daemon (and for `wb()` callers): a dialog on one of the session's own tabs, or on a popup one of them opened, is handled the moment it opens and reported. Every action takes `dialog`: `"accept"`, `"dismiss"`, the text to answer a `prompt` with, or `{accept, text}`; without it `alert` and `beforeunload` are accepted and `confirm` / `prompt` dismissed. The action's reply carries `dialog: {type, message, handled}` (`more_dialogs` if several), so a cancelled `confirm` is no longer reported as a plain success. Every handled dialog is also listed under `dialogs` (`[{type, message, url, tabId, at, handled}]`) in the next reply, including one that opened between calls (the daemon keeps listening; in-process, the next call reports it). A dialog on any other tab is left for the user under the daemon; in-process it gets what the driver did before (playwright dismisses it, patchright leaves it). A page with its own `dialog` listener is left to that listener. New `dialog` action: the session's recent dialogs and any whose handling failed. `batch {dialog}` sets the policy for steps that give none. Before, playwright silently cancelled every `confirm`, and under patchright (and after a CLI call had exited) a dialog left open blocked every later connection to the browser. A dialog that opens after an in-process CLI call has exited still can't be handled; the daemon covers that case.
- Popups: a window opened by one of the session's own tabs (`window.open`, `target=_blank`, also `noopener`) becomes the session's own tab, so `close_session` closes it. The reply of the action that opened it (or of the next action, if it opened later) lists `popups: [{tabId, tab, url}]` (a popup whose target has no URL yet is polled for up to 2 s). The current tab stays where it is unless the action has `follow_popup: true` (waits up to `popup_timeout`, default 3000 ms; reply `followed`). Before, a popup was invisible to `list_tabs` and stayed open after `close_session`.
- Short tab ids per session (`tab`: `t1`, `t2`, … never reused within a session) in `navigate`, `list_tabs`, `find_tab` and `attach_tab` replies; `attach_tab {tabId: "t2"}` accepts them.
- Frames: `read` and `snapshot` show the content of every visible child frame (`<iframe>`, `<frameset>`, same- and cross-origin), indented after its parent's content under a header with a ready-to-use selector prefix, e.g. `[frame #work → selector prefix: "#work >> internal:control=enter-frame >> "]`. `query`, `interactive_only`, `max_chars` and `offset` cover frame content too. Pages without child frames take the old path (no extra round trip; warm `read` through the daemon on example.com 0.11 s median, n=10). Before, an iframe's content was invisible (only `- iframe`), and on a frameset page both actions waited 10 s and failed.
- `frame` argument on `read`, `snapshot`, `wait`, `evaluate`, `click`, `fill` and `select`: the frame element's selector, a printed prefix (nested too) or a frame name scopes the action to that frame.
- `select` action: `{selector, value | label | index}` through Playwright `select_option` (frame-piercing selectors work; `value` also matches a label; waits up to `timeout`, default 5000 ms, for the option, so a cascading list that is still loading is fine). The reply has `selected: {value, label}` as now in effect; an option that never appears is an error listing the options.
- `wait {url}` (also `url_contains`): a substring of the tab's URL or of one of its frames' URLs; it survives navigations. `evaluate {arg}`: the argument a function in `code` is called with.

**A step for a human: `handoff`**
- `handoff {message, until?, timeout=600000, then_snapshot?}`: gives the session's current tab to a human for a step and continues. The tab is brought to the front (`Page.bringToFront`), a notification goes out through `[notify]` (ntfy / command / stderr; the reply's `notified` says which), and a small panel with the message and a "Done — hand back to the agent" button appears in the page: a new element outside `<body>` holding a closed shadow root, built without markup, re-created after a navigation in the tab and removed at the end. It returns `{success, how: "button" | "condition" | "timeout", waited_ms, url}` when the button is pressed, `until` (`{text | selector | js | url_contains}`) holds, or the timeout passes. Dialogs on the session's tabs are still answered at once by the action's `dialog` policy and reported (a dialog left open would block every other connection to the browser), so the human can't answer one. Through the daemon the client waits without a timeout and other sessions keep working (in an acceptance run, three reads in another session during a hand-off each took under max(1 s, 3 × the read before it)).

**Help**
- `deja-cdp help <action | topic>` and `deja-browser help <command | topic>` (also `<action> --help`): the args and reply of one action or command, and the topics refs, frames, dialogs, popups, then_snapshot, daemon, library / format, tiers, steps. An unknown name lists what exists.

**Text CAPTCHAs and the refused-host list**
- `captcha_text` / `solve_image_captcha`: `charset`, `preprocess` and `min_confidence` for hard text CAPTCHAs. `charset` restricts `ddddocr`'s per-frame scores to the allowed characters and, with a known `length`, decodes the best reading with exactly that many characters; `preprocess: "binarize"` makes the background white and everything else black; `min_confidence` refreshes a doubtful read instead of submitting it. On 85 hand-labelled samples (digits in random colours, crossed by noise lines) whole-code accuracy went from 37% to 76% (4 digits) and from 55% to 96% (6 digits). Nothing changes unless you pass them.
- `[recipes] deny_hosts_enabled` (default `true`): `false` switches off the refused-host list (mail, account sign-in, banks, brokers, payments, and `deny_hosts_extra`) for `navigate` and for recipe `run` / `verify` / `draft` / `gate` and the login step, e.g. to let an agent fill a bank's public sign-up form. With it off, separation (a profile never logged in there, no stored passwords for those sites) is the only protection. Ticketing hosts are unaffected. Nothing changes unless you set it.

### Changed
- `deja-cdp --help` and `deja-browser --help` print a short overview (about 1,100 and 800 chars) instead of the full reference (13,177 and 8,048 chars); the detail moved to `help <name>`.
- `skills/deja-browser/SKILL.md` rewritten around the new flow: replay a recipe if one exists, else do the task with refs and `then_snapshot`, then `learn` once.
- `deja-browser draft` prints a summary by default (request, params, pick, matched paths, the first three warnings, the draft path and the next command); `--verbose` prints the full report as before. The draft file itself is unchanged. The report also lists `dropped` credential fields and `redacted_headers`.
- `deja-browser run` keeps recipes that use `{secret:…}`, a `login` block, or the cloud hCaptcha solver in the calling process; the daemon never reads a secret.
- Under the daemon, the driver for `deja-cdp` actions is patchright when it is installed (as for recipes); `EDGE_CDP_DRIVER=playwright` still selects playwright. In-process runs keep the old default (playwright).
- `navigate` adds a new tab to the session before loading it, so a page that fails to load no longer leaves a tab outside the session.
- `fill` on a `<select>`: a value that is an option's value selects it as before; a value that is an option's visible label now selects that option (`matched: "label"`); anything else is an error ("nothing was selected"). Before, it returned `success: true` and left the select empty. The reply adds `matched` and `selected`.
- `wait {text}` without `frame` looks in every frame of the page, not only the top document.
- `snapshot {interactive_only: true}` keeps controls whose names Playwright prints YAML-quoted (e.g. `- 'button "a: b"'`); they were dropped.

### Fixed
- `fill`'s fallback for readonly / hidden inputs works with frame-piercing selectors (`… >> internal:control=enter-frame >> …`); it raised a traceback.
- `network_capture` with `trigger.evaluate` runs the code in the page's main world under patchright too (it ran in patchright's isolated world, so page globals were invisible), like the `evaluate` action.
- Connecting to the browser now times out after 10 s with an error that names the likely cause (a JavaScript dialog open in one of the listed tabs) instead of hanging.
- Finding a session's tab no longer opens a CDP session to every tab in the browser on each call (one `Target.getTargets`, then only the session's tab is resolved).
- On Windows, a library caller (`wb()`) that stays connected and idles (`time.sleep`, waiting on something else) no longer stalls the whole browser. The Playwright driver writes to a blocking pipe that sync Playwright reads only inside a call; once it filled, the driver froze, and because Playwright pauses every new tab and out-of-process frame until its client answers, other clients' navigations timed out. A thread now reads the driver's output between calls. On a test profile, an idle caller plus another process opening tabs: stalled at the first operation in 3 of 3 runs before, 12 of 12 operations in 3 of 3 runs after, for playwright and patchright, new tabs, cross-origin frames, a heavy page, 30 idle seconds and daemon workers.
- A library caller whose Playwright driver crashed (for instance, the browser closed while a call was connecting) returned an error with the stock transport but waited forever with the Windows output-pipe reader above; it now gets the error too.
- With playwright 1.65 (which moved its pipe transport) the Windows output-pipe reader was silently not installed; it is found in either place now.
- The sessions file is written with a unique temp name, merges only the caller's session (two processes no longer overwrite each other's sessions) and retries when Windows briefly locks it (`PermissionError` from antivirus / indexer).

### Testing
- `tests/egress/run.sh` now runs its action and recipe cycle twice, in-process and through the daemon (the daemon started under its own `strace -f`, plus the recording, `learn` and a run of the learned recipe), with a per-call timeout so a detached daemon can't hang the tracer. It fails if a call starts a daemon of its own or the daemon trace shows no connection. Passed on WSL2 Ubuntu (headless Chromium, both modes); `SELFTEST=1` failed as it should.
- The dialog, popup, frame, `select`, ref, `learn` path 3 and `handoff` behaviour above was checked case by case (PASS / FAIL, through the daemon and in-process) against a local fixture that imitates legacy / ASP.NET pages: an iframe and a frameset, a popup that writes back to its opener, cascading selects, `confirm()`, a form that posts back inside its frame (`__VIEWSTATE`) and one that writes. The fixture and its acceptance script are development tools and are not part of the package or this repository; they are not a real .NET application, and no hospital system was tested.

### Docs
- README: the comparison is now one table of deja-browser, agent-browser, Ego Lite and Claude in Chrome (what each reuses on a repeat visit, characters and seconds on first and repeat visits, Cloudflare, old-style pages, what was not measured, and where deja is behind). `docs/benchmark.md` has the 0.4 round.

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
