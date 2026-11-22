# -*- coding: utf-8 -*-
"""Help text for the two CLIs: a short top-level page plus one entry per action / command / topic.

  deja-cdp help [<action | topic>]        deja-cdp <action> --help
  deja-browser help [<command | topic>]   deja-browser <command> --help

Kept apart from browser.py / recipe.py so a model that runs `--help` reads ~1,000 characters, not
the whole reference: what an agent reads to learn the tool counts against every first visit.
Stdlib only, no imports from the package: printing help never touches a browser or the daemon.
"""

CDP_TOP = """\
deja-cdp - drive the agent browser (its own Chromium profile, CDP on 127.0.0.1).

  deja-cdp <action> '<json args>' --session <task>   one line of JSON out
  deja-cdp help <action | topic>                     args and reply of one action
  deja-cdp launch | status | stop                    the browser itself (launch: log in by hand)

Look:  read {query}   snapshot {refs:true, interactive_only, query, max_chars}
       evaluate {code}   screenshot {path}   save_as_pdf {path}
Act:   navigate {url}   click / fill / select {selector | "@eN"}   wait {text|selector|js|url}
       handoff {message}   (a human does one step: login, CAPTCHA, a judgement)
Tabs:  list_tabs  find_tab  attach_tab  list_browser_tabs  close_session (end every task with it)
More:  batch  dialog  network_capture  captcha_text  cdp

Topics: refs  frames  dialogs  popups  then_snapshot  daemon  library

First visit: snapshot {refs:true} -> act on @eN with then_snapshot:true -> one small read /
evaluate that returns the answer; then `deja-browser learn` makes it a recipe
(deja-browser help learn). Calls go through a background daemon (deja-cdp help daemon).
"""

CDP = {
    "navigate": """\
navigate {url, newTab, then_snapshot?}
  -> {success, url, tabId, tab, title, status, recipes?}
  Opens url in the session's current tab (newTab: a new one). tab = the short id (t1, t2, ...).
  recipes: ids of saved recipes for this host (at most 10): a replay exists, try
  `deja-browser run <id>` instead of reading the page. Mail, sign-in, bank, broker and payment
  hosts are refused (a guard rail; do not work around it).""",
    "read": """\
read {selector="body", query, context_lines=2, max_chars=20000, offset, frame}
  -> {url, title, text, chars, full_chars, truncated, next_offset?, hint?}
  innerText by lines. query keeps only the matching lines (case-insensitive) plus context_lines
  around them ('...' between gaps). On a big page try read {query} before snapshot. Child frames
  are included (deja-cdp help frames); frame scopes to one. selector may be a ref "@eN".""",
    "snapshot": """\
snapshot {max_chars=20000, selector, interactive_only, query, offset, frame, refs, delta}
  -> {url, title, tree_text, chars, full_chars, truncated, next_offset?, hint?}
  Accessibility tree, cut on a line boundary; when truncated, hint says how to narrow (query,
  selector, interactive_only) or page on (offset = next_offset). Always give max_chars.
  refs:true tags controls and named headings / cells / images with [ref=eN] (deja-cdp help refs).
  delta:true -> {unchanged:true} when the tree equals the last one of this tab, URL and args;
  else {delta:"diff", diff} (unified, 1 line of context) while under half the lines changed;
  else the full tree with delta:"full". Child frames are included (deja-cdp help frames).""",
    "evaluate": """\
evaluate {code, frame, arg}  -> {type, value}
  Runs code in the page's main world; code may be an expression or a function
  (`async () => {...}`), which is called with arg. Return only the fields you need.
  frame scopes to one frame (deja-cdp help frames).""",
    "click": """\
click {selector, then_snapshot?}  -> {success, tag, text}
  Trusted CDP input events. selector: CSS / Playwright selector or a ref "@eN". A selector that
  matches several elements acts on the first and adds {matches: n, warning}. A popup it opens is
  reported (deja-cdp help popups); a dialog it raises is handled (deja-cdp help dialogs).""",
    "fill": """\
fill {selector, value, then_snapshot?}  -> {success, tag, mode}
  Types value (readonly / hidden inputs: JS fallback). selector may be a ref "@eN"; several
  matches -> acts on the first, adds {matches: n, warning}. On a <select>: an option's value or
  its visible label (matched:"label"); reply adds {matched, selected}; anything else is an error
  (use select for clarity).""",
    "select": """\
select {selector, value | label | index, timeout=5000, then_snapshot?}
  -> {success, selected:{value, label}}   (a list if several)
  Playwright select_option; value also matches a label. Waits up to timeout for the option, so
  a cascading list that is still loading is fine; an option that never appears is an error
  listing the options. selector may be a ref "@eN" (the combobox) or frame-piercing.""",
    "wait": """\
wait {text | selector | js | url, timeout=15000, state="visible", frame}  -> {success, waited_ms}
  A timeout is success:false, not an error. text is looked for in every frame unless frame
  scopes it. url (also url_contains): a substring of the tab's URL or of one of its frames' URLs;
  it survives navigations. selector may be a ref "@eN".
  ASP.NET UpdatePanel: after a change that posts back, wait until the partial postback is over:
  wait {js: "!(window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack())"}
  (or wait {selector} for the element the postback fills in).""",
    "handoff": """\
handoff {message, until?, timeout=600000, then_snapshot?}
  -> {success, how: "button"|"condition"|"timeout", waited_ms, url, notified}
  Gives the session's current tab to a human for one step (a login, 2FA, a CAPTCHA, a judgement),
  then continues. The tab comes to the front, a notification goes out ([notify]: ntfy / command /
  stderr; under the daemon stderr is its log; notified says which), and a small panel in the page
  shows the message and a "Done - hand back to the agent" button (a closed shadow root outside
  <body>; the page's own nodes untouched; re-created after a navigation; removed at the end).
  Returns when the button is pressed, until {text | selector | js | url} holds (url_contains is
  a synonym of url), or after timeout (success:false). overlay:false + hint when the panel could
  not be shown. Dialogs on the session's tabs are still answered at once by this action's dialog
  policy (default: confirm / prompt dismissed) and reported: an open dialog would block every
  other connection to the browser. Other sessions keep working.""",
    "screenshot": """\
screenshot {path, format="png", quality, selector}  -> {format, path, sizeBytes, mimeType}
  Writes a file and returns its path, never the image. selector (or "@eN") = one element.""",
    "save_as_pdf": """\
save_as_pdf {path, paper_format, landscape, scale, print_background}
  -> {path, sizeBytes, mimeType, pageTitle}""",
    "list_tabs": """\
list_tabs {}  -> {success, tabs:[{tabId, tab, url, title, active}]}   this session's tabs""",
    "list_browser_tabs": """\
list_browser_tabs {}  -> {success, tabs:[...]}   every tab in the browser""",
    "find_tab": """\
find_tab {url, active}  -> {success, url, tabId, tab, borrowed}
  Makes the session's tab whose URL starts with url the current one. active:true may borrow a
  tab this session did not open (borrowed:true; close_session leaves it open).""",
    "attach_tab": """\
attach_tab {tabId}  -> {success, tabId, tab, url, title, borrowed}
  Borrows an open tab (e.g. the one the user just logged in on); tabId or a short id "t2".
  close_session leaves borrowed tabs open.""",
    "close_session": """\
close_session {}  -> {success, closed}
  Closes the session's own tabs, popups they opened included; borrowed tabs stay open. Also
  clears the session's recording, so run `deja-browser learn` first. Run it at the end of every
  task, also after a failure.""",
    "batch": """\
batch {steps, stop_on_error=true, dialog, then_snapshot?}
  -> {ok, results:[{action, ok, data | error, secs}]}
  steps = [{action, args, quiet?}] run one after another in one call. A failed step (an error,
  or a wait that timed out) skips the rest (skipped:true) unless stop_on_error is false.
  quiet:true keeps only {ok, secs} for a successful step. No nested batch; every step goes
  through the host guard. dialog = the policy for steps that give none. Exit code 1 when a step
  failed.
  deja-cdp batch '{"steps":[{"action":"navigate","args":{"url":"https://example.com"},"quiet":true},
                            {"action":"wait","args":{"text":"Example"}},
                            {"action":"evaluate","args":{"code":"document.title"}}]}' --session S""",
    "dialog": """\
dialog {}  -> {success, recent:[...], pending:[...]}
  The session's recent JavaScript dialogs and any whose handling failed (deja-cdp help dialogs).""",
    "network_capture": """\
network_capture {trigger, url_filter, types, wait_ms, body_dir}
  -> {url, count, body_dir, requests:[{i, method, url, status, type, mime, req_headers,
      post_data, body_path, body_bytes}]}
  The requests one trigger causes, in one call. trigger: {"navigate": url} | {"reload": true} |
  {"click": selector} | {"evaluate": code}. url_filter: substring of the URL. types: resource
  types (default ["xhr", "fetch"]; "all" = everything non-static, needed when the request is a
  page navigation to the API URL). Bodies go to files under body_dir, never inline; credential
  headers come back as <redacted len=N>. trigger.navigate opens a tab if the session has none.
  The output is the input of `deja-browser draft` (usually `learn` is shorter).""",
    "captcha_text": """\
captcha_text {image, input, submit, ok, fields, case, length, refresh, tries=6, digits_only,
              charset, preprocess, min_confidence}
  Text-in-an-image CAPTCHA with local ddddocr (install the ocr extra). Reads the image's own
  pixels, re-reads an answer of the wrong length, tries the code as read, then upper, then lower
  case (unless case is "upper" / "lower"). Without submit it reads and fills input:
  -> {success, filled, code, plausible, length, ...}. With submit (needs input) it submits and
  retries until the JS expression ok is true, refilling fields ({selector: value}, never echoed
  back) each time, since a rejected submit usually reloads the form empty. refresh = selector of
  the "new image" link. Hard images (coloured, noise lines): charset = the allowed characters
  (with length: exactly that many), preprocess:"binarize", min_confidence (0-1) refreshes a
  doubtful read instead of sending it; the result carries confidence and decoder. Never OCR a
  screenshot yourself instead.""",
    "cdp": """\
cdp {method, params}  -> the raw CDP result, on the session's current tab.""",
    "launch": """\
deja-cdp launch  -> {browser, launched, profile, port}
  Starts the agent browser if it isn't running (actions start it on demand too). Use it to let
  the user log in to a site by hand once; the profile keeps the login.""",
    "status": """\
deja-cdp status  -> {up, browser, profile, port}   is CDP up, which browser, which port.""",
    "stop": """\
deja-cdp stop
  Kills only the agent-profile browser (refuses any other owner of the port); daemon workers
  drop their connections first.""",
    # ---- topics ----
    "refs": """\
refs: snapshot {refs:true} tags controls (buttons, links, text boxes, comboboxes, options ...)
  and named headings / cells / images with [ref=eN], inside frames too; each [frame ...] header
  has a ref of its own. "@e5" (or "e5") works wherever a selector does (click, fill, select,
  wait, read, snapshot, screenshot) and as frame:"@e7". A ref is role + accessible name + nth in
  its frame, stored per session next to the sessions file, so it works from a later CLI call,
  with or without the daemon. It is re-checked before use: another tab, a navigated page or
  frame, or a changed number of matching elements is an error "stale ref @e5: page changed -
  take a new snapshot", never a guess. Ids are never reused within a session; the same element
  on an unchanged page keeps its id. A native <option> ref: use select on its combobox.""",
    "frames": """\
frames: read / snapshot show every visible child frame (iframe, frameset; cross-origin too)
  after its parent's content, indented under a header such as
    [frame #work -> selector prefix: "#work >> internal:control=enter-frame >> "]
  Put that prefix in front of a selector to act inside the frame, or pass frame:"#work" (the
  frame element's selector, a printed prefix, a frame name, or a frame ref "@eN"): it scopes
  read / snapshot / wait / evaluate / click / fill / select to that frame.""",
    "dialogs": """\
dialogs: every action takes dialog: "accept" | "dismiss" | "<text to answer a prompt>" |
  {accept, text}: what to do with a JavaScript dialog the action raises. Default: alert and
  beforeunload accepted, confirm and prompt dismissed. The reply then carries
  dialog:{type, message, handled} (more_dialogs if several), so a cancelled confirm is visible:
  redo the action with dialog:"accept" if it should have been accepted. Every dialog handled on
  the session's tabs, also one that opened between calls, is listed with url / tabId / time
  under "dialogs" in the next reply. Dialogs on tabs of other sessions are not this session's
  to answer (the daemon leaves them to the user). In-process (DEJA_BROWSER_DAEMON=0), a dialog
  that opens after a call has exited is not handled, and one that opens while a new process is
  still connecting is dismissed by Playwright and not reported.""",
    "popups": """\
popups: a window opened by one of the session's tabs (window.open, target=_blank) becomes the
  session's own tab; the reply of the action that opened it (or of the next one) lists
  popups:[{tabId, tab, url}]. The current tab stays unless the action has follow_popup:true
  (waits up to popup_timeout=3000 ms; reply "followed": its tab id or null). Switch with
  attach_tab {tabId:"t2"}. close_session closes popups too.""",
    "then_snapshot": """\
then_snapshot (click, fill, select, navigate, handoff, batch): true = after the action, a
  snapshot {interactive_only:true, refs:true} of the session's current tab under "snapshot",
  taken once a navigation the action started (link, submit, postback in a frame) has loaded.
  Or an object of snapshot args, optionally with wait:{text | selector | js, timeout} first (for
  changes made by script / XHR). A failed snapshot is reported there ({error}); the action's
  result stands.""",
    "daemon": """\
daemon: `deja-cdp <action>` (and `deja-browser run`) go through a background daemon that keeps
  the driver and the CDP connection warm instead of starting a driver and reconnecting on every
  call (a warm read of example.com: 0.11 s median of 10). The first call starts it (detached, no window); 127.0.0.1 only, with a token in the data
  dir. It restarts itself when the code or config changes, drops its connections after 10 idle
  minutes and exits after 30. Each session has its own worker, so a long wait in one session
  doesn't delay another. It also records each session's visit for `deja-browser learn` (in
  memory; close_session clears it). If it can't be reached the call runs in-process (a note on
  stderr). DEJA_BROWSER_DAEMON=0 always runs in-process (no recording). The driver is patchright
  under the daemon when it is installed, playwright in-process; EDGE_CDP_DRIVER overrides.
  The connection to the browser times out after 10 s; the error names the likely cause (a
  JavaScript dialog open in a tab) and lists the tabs.""",
    "library": """\
library: from deja_browser.browser import wb
  wb("navigate", {"url": "https://example.com"}, session="my-task")   -> {success, url, tabId, ...}
  wb("evaluate", {"code": "document.title"}, session="my-task")       -> {type, value}
  wb("close_session", {}, session="my-task")
  Same actions and replies as the CLI, always in-process (never the daemon); one CDP connection
  per thread, reused until exit. On Windows a thread reads the Playwright driver's output between
  calls, so a caller that stays connected and idles (time.sleep, waiting on something else) no
  longer stalls new tabs and connections for other clients of the browser.
  Driver: playwright, or patchright with EDGE_CDP_DRIVER=patchright or use_driver(). Sessions map to tabs by CDP targetId in [browser] sessions_file, so a later
  process finds the same tab. Profile, port and browser come from config.toml ([browser]); see
  config.example.toml.""",
}

CDP_TOPICS = ("refs", "frames", "dialogs", "popups", "then_snapshot", "daemon", "library")

RECIPE_TOP = """\
deja-browser - site recipes: learn a site's data path once, replay it without reading the page.

  deja-browser list                          saved recipes (<host>/<name>)
  deja-browser run <id> '<json args>' [--allow-write] [--data-only] [--tier T]
  deja-browser learn --name N --expect TEXT [--expect ...] [--param name=value ...] [--session S]
      after doing the task once with deja-cdp: the recorded visit -> a verified, saved recipe
  deja-browser show <id>  |  recording  |  draft  |  verify  |  save  |  gate <url>
  deja-browser help <command | topic>        details (also: <command> --help)

Topics: format (the recipe JSON)  tiers  steps

--expect is text from the actual answer (a value you saw, >= 3 chars), not a field name.
Recipes with "writes": true need --allow-write on run, and the user's go-ahead.
"""

RECIPE = {
    "list": """\
deja-browser list  -> the saved recipes: [{id (<host>/<name>), description, when_to_use, tier,
  params (* = required), verified_at, last_ok, writes}]""",
    "show": """\
deja-browser show <id>  -> the recipe's JSON.""",
    "run": """\
deja-browser run <id> '<json args>' [--tier http|page|script|steps] [--allow-write] [--data-only]
  Replays the recipe: tiers cheapest first (deja-browser help tiers), each result checked against
  the recorded JSON shape; a mismatch is reported as stale, never returned as data.
  "_limit": N in the args keeps at most N list items (only lowers the recipe's pick.limit).
  --data-only prints just the data on success; a failure still prints {ok:false, tried}.
  --allow-write is required for recipes with "writes": true (ask the user first).
  Runs in the deja-cdp daemon unless the recipe uses {secret:...}, a login block or the cloud
  hCaptcha solver: those always run in this process. DEJA_BROWSER_DAEMON=0 forces in-process.
  A stale run: do the task by hand once, then learn (or verify + save) again; don't patch mid-run.""",
    "learn": """\
deja-browser learn --name N --expect TEXT [--expect ...] [--session S] [--param name=value ...]
                   [--keep-param NAME ...] [--description D] [--write] [--force] [--verbose]
  The visit the daemon recorded for session S (default "cli": pass the visit's --session) ->
  draft -> verify with the recorded values -> save the cheapest passing tier. Run it BEFORE
  close_session (that clears the recording); DEJA_BROWSER_DAEMON=0 records nothing.
  --expect: text from the actual answer, >= 3 chars ("1.0.228", not "version"). It must be in a
    recorded JSON response or in the first 2,000 chars of a read / evaluate result (snapshots
    don't count): end the visit with one small evaluate or read {query} returning the answer.
  Uses the first that works: a JSON response -> request recipe; the last evaluate -> script
    recipe (form post-backs replayed with fetch); the actions that led to it -> steps recipe.
  --param name=value: that value becomes {name}; without it, typed values (fill / select) and
    URL values of the visit do. --write: the flow changes something (writes: true; verify
    replays it, so the write happens once more). --force replaces an existing recipe.
  -> {ok, id, tier, kind, params, sample_chars} or {ok:false, reason, hint}; --verbose: all.
  Refused: an id taken from an earlier response (pass it as --param), a password / one-time-code
    fill, a flow with attach_tab / find_tab / handoff / captcha_text / cdp / network_capture.""",
    "recording": """\
deja-browser recording [--session S] [--clear] [--verbose]
  What the daemon recorded for the session (a summary; no bodies): actions with their args and
  results (up to 2,000 chars each; password / one-time-code fills redacted) and the responses on
  the session's own tabs (main document, same-site xhr / fetch, other sites' JSON). In memory
  only, 100 responses / 16 MB per session; close_session or --clear drops it.""",
    "draft": """\
deja-browser draft <capture.json> --expect TEXT [--expect ...] [--param name=value ...]
                   [--name N] [--entry-url U] [--description D] [--out FILE] [--force]
                   [--keep-param NAME ...] [--verbose]
  capture.json = the output of `deja-cdp network_capture`. Writes a request recipe draft from the
  JSON response that holds every --expect (the captured --param value becomes {name}). Exit 2 with
  "applicable": false when no JSON response holds the answer (the data is in the HTML): nothing
  is written; use a script or steps recipe. Credential-looking query / body fields are dropped
  unless named by --keep-param (public client keys). A draft from a POST has "writes": true.
  Prints a summary (the full report with --verbose). Fill in its TODO fields, then verify.
  `learn` does capture + draft + verify + save in one call when the daemon recorded the visit.""",
    "verify": """\
deja-browser verify <draft.json> '<json args>'
  Tries every tier and prints which pass, the result shape, a preview (2,000 chars at most) and
  out_chars; warns when the output is more than half of the page's text. No pass, no save.""",
    "save": """\
deja-browser save <draft.json>
  Saves the draft as <recipes dir>/<host>/<name>.json. Needs a passing verify of this same draft
  (within the last hour). Then tell the user what was saved and how to call it.""",
    "gate": """\
deja-browser gate <url>
  Opens url, clears any challenge (Cloudflare / Turnstile wait, one trusted checkbox click,
  reCAPTCHA audio with local faster-whisper, the opt-in hCaptcha solver) and reports. If it says
  a human is needed, tell the user (or use deja-cdp handoff).""",
    # ---- topics ----
    "format": """\
Recipe file: <recipes dir>/<host>/<name>.json   (id "<host>/<name>"; [recipes] dir in config.toml)
  {
    "version": 1, "name", "description", "when_to_use", "entry_url",
    "params": [{"name", "description", "required", "default"}],
    "request": {"method", "url", "headers", "body"},     API recipe; strings may hold {param},
                                                         {param|url} (percent-encoded) and
                                                         {secret:NAME} (never printed / returned)
    "script": "<async fn body of (args), returns small JSON>",   DOM / form recipe (instead)
    "steps": [{"action", "args", "expect"?, "dialog"?}],         browser flow (instead; help steps)
    "at_entry": true,             optional: the script runs on entry_url itself; entry_url may
                                  then hold {param} / {param|url} in its path or query (not host)
    "pick": {"path": "hits", "fields": ["title", "url"], "limit": 20},   shrink the result
            or {"select": {"version": "crate.max_stable_version"}}: name -> dotted path (a
            number indexes a list); on a list result it is applied to every row
    "writes": false,              true = side effects -> run needs --allow-write
    "driver": "playwright",       optional; default patchright when installed
    "login": {"check", "url", "user_secret", "pass_secret", "user_selector", "pass_selector",
              "submit_selector"},                        optional; secrets by name only
    "tier", "expect": {"signature": {...}}, "verified_at", "last_run", "notes"   written by
                                                         verify / save / run
  }
  learn and draft write this for you; write it by hand only when they can't.""",
    "tiers": """\
Tier ladder (cheapest first; a run falls through on failure and records what worked):
  http   - plain HTTP outside the browser, cookies read live from the agent browser (never stored)
  page   - fetch() inside a tab on entry_url (site cookies, Cloudflare clearance, CSRF all native)
  script - the recipe's own script in the page (DOM read / form fill)
  steps  - the recipe's steps, one browser action after another (only for steps recipes)
Every response is checked against expect.signature; a mismatch is reported as stale, never
returned as data. Challenge pages go through the CAPTCHA gate (deja-browser help gate).""",
    "steps": """\
steps recipes: for sites with no JSON API whose flow crosses full-page postbacks, frames and
popups (legacy / .NET WebForms systems), where one script in one document can't do it. learn
writes them (path 3); by hand:
  "steps": [{"action", "args", "expect"?, "dialog"?}]
  action   navigate, click, fill, select, wait, evaluate, read; snapshot only as the last step.
           Run in order in the recipe's session (through the daemon like any run).
  args     the action's args; {param} / {param|url} are filled into its strings, except
           JavaScript (code, js), which is never string-substituted: an evaluate's code that is a
           function gets the params as its argument (`async (args) => ...`). {secret:...} and
           refs (@eN) are refused: use CSS / role selectors.
  dialog   the policy for a dialog the step raises ("accept", "dismiss", prompt text, {accept, text}).
  expect   {text | selector | js | url, timeout=15000, state?, frame?} checked after the step.
           Not met -> the run fails as stale with the step's index:
           tried: [{tier: "steps", step: i, action, stale: true, error}].
  A click / select / evaluate that starts a navigation (also a postback in a frame) is waited for
  before the next step. The last step (evaluate / read / snapshot) gives the output, then pick and
  the signature check. "writes": true needs --allow-write as always.""",
}

RECIPE_COMMANDS = ("list", "show", "run", "learn", "recording", "draft", "verify", "save", "gate")
RECIPE_TOPICS = ("format", "tiers", "steps")


HELP_FLAGS = ("-h", "--help")


def wants_help(argv):
    """True for no args, `help ...`, `-h` / `--help`, or `<name> ... --help`."""
    return not argv or argv[0] == "help" or any(a in HELP_FLAGS for a in argv)


def topic_of(argv):
    """The entry asked for: `help X` -> X, `X ... --help` -> X, else None (the top page)."""
    if not argv or argv[0] in HELP_FLAGS:
        return None
    if argv[0] == "help":
        return argv[1] if len(argv) > 1 else None
    return argv[0]


def _lookup(table, top, prog, name):
    """-> (text, exit code). Unknown name: the list of what exists, exit 2."""
    if not name:
        return top, 0
    key = name.strip().lstrip("-").lower()
    if key in table:
        return table[key], 0
    return f"{prog}: no help for {name!r}. Available: {', '.join(table)}", 2


def cdp_help(name=None):
    return _lookup(CDP, CDP_TOP, "deja-cdp", name)


def recipe_help(name=None):
    return _lookup(RECIPE, RECIPE_TOP, "deja-browser", name)
