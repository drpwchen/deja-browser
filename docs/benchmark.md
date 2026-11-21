# How deja-browser compares (small benchmark, read the conditions)

We measured deja-browser against three other ways an agent can drive a browser — agent-browser 0.43.0
(vercel-labs), Ego Lite 0.6.3.17 (Citro Labs) and Claude in Chrome (Anthropic's extension) — on five public
pages: a search page backed by a JSON API (hn.algolia.com), a large Wikipedia table, a two-step server-side form
(quotes.toscrape.com), a JavaScript single-page app (crates.io) and a Cloudflare demo page (nopecha.com).
Each task has three visits with different parameters, three rounds each, on one Windows 11 PC, November 2026.
This is a small benchmark (n = 3, one machine, one model session each for deja and Claude in Chrome), not a general ranking.

## Same model, same prompt (deja vs Claude in Chrome)

Claude Opus 5.5 did the 45 visits once with each tool, from one neutral prompt that let it use each tool the way
the tool's own documentation recommends, including running JavaScript in the page. agent-browser and Ego Lite
were not measured this way.

- On the search, table, form and SPA pages the answer was in what both tools read, on every visit.
- **Repeat visits:** on the search, table, form and SPA pages, Claude in Chrome's tool results were 8.6–24.6x
  deja's in characters (per-page medians of the six repeat visits; single visits ranged 6.7–28.3x). Most of that
  is the tab list the extension appends to every result; excluding that list it was 1.6–2.9x deja's replay
  (single visits 1.5–3.2x). The Cloudflare page is not compared: Claude in Chrome never got past the
  challenge there. (The list is
  counted from its `Tab Context:` label; the blank lines just before it, about 600 characters over the whole
  session, are counted as page content, which slightly favours deja.)
- **First visits cost deja more.** deja spent its first visits writing recipes: 22,274 characters read for the
  five first visits, against 9,854 for Claude in Chrome (2,947 without the tab list). Over all 45 visits, excluding
  the tab list, Claude in Chrome read fewer characters than deja (13,058 vs 29,411); with it, 68,759.
- **Cloudflare:** deja's challenge gate ticked the checkbox once and every visit read the page (9/9; deja used
  its long-lived browser profile). Claude in Chrome stopped at the challenge in every visit and did not try to
  solve it (its answers in 8 of the 9 visits say it would not; in the other it wrote no visible answer), which is
  what its documentation says it does.

## Scripted runs and the first Claude in Chrome run

deja, agent-browser and Ego Lite were driven by fixed scripts; Claude in Chrome was driven by the model under a
prompt that restricted how it read the page.

- **Repeat visits read far less with a recipe than with re-reading the page.** On the table, form and SPA tasks
  a deja replay returned about 54–141 tokens. Re-reading the page with each tool's cheapest read that contained
  the answer every time cost about 2,000–14,600 tokens with agent-browser (about 23–244x; these ranges hold for
  single visits, that is each visit in each round). For Ego Lite the figures are per-task medians of the six
  repeat visits: about 1,300–11,200 tokens, about 12–184x (single visits 9.5–188x). For Claude in Chrome, counted
  the same per-task way, only the table and form tasks qualify (its SPA reads contained the answer in 3 of 6
  visits): about 4,900–11,000 tokens, about 42–180x (single visits on those two tasks 4,800–11,000 tokens,
  35–183x). This compares a recipe against **re-reading the page**; the model-driven run above shows how much
  smaller the gap is when the other tool extracts only what it needs. On the search task the saving was only
  about 2.5x (agent-browser, one repeat visit; there Ego Lite's and Claude in Chrome's re-reads did not contain
  the answer on every visit, so no ratio is given for them).
- **deja is not the fastest.** On the four pages without the Cloudflare challenge, agent-browser was faster on
  every task, first and repeat visits alike (medians, about 0.7–1.5 s per read vs 1–9 s for deja; deja starts a
  new process per call). On the search page's repeat visits it was a near tie (0.85 vs 0.92 s).
  Ego Lite was close to agent-browser (about 0.5–2.4 s per read); Claude in Chrome was the slowest of the four
  (timed from the conversation log, so only the order of magnitude is comparable). Times on the Cloudflare page
  are not compared.
- **First-visit page reads cost about the same for deja and agent-browser.** On the four pages without the Cloudflare
  challenge, reading a page as plain text took a similar number of tokens with both (for example
  ~3,100–3,300 tokens for the search page, where Ego Lite read about the same); Ego Lite's plain text was smaller
  on the table and form pages. Claude in Chrome read more on three of the pages because its prompt in that run
  made it read the accessibility tree too.
- **Scoring artifacts are not tool failures.** Under one rule set for all four tools (the Cloudflare page is
  left out, as above), the plain-text reads of
  deja, agent-browser and Ego Lite contained every expected string, and Claude in Chrome's reads did in 30 of
  33 rows. Those 30 include 9 rows that pass only under a looser check: the expected text is there in
  pieces (split across page elements, or out of order on the search page). The other tools' tree snapshots get
  the same allowance; it favours Claude in Chrome here. In the other 3 rows the model set its own read limit too
  low. One search query is left out of that count
  for every tool: its expected answer had been built from a simplified API call and did not match the page.
  Rebuilt from the page's own query, it matched what both tools read in the model-driven run. The remaining
  differences are each tool's snapshot design (deja truncates at 20,000 characters by default, Ego's default
  snapshot covers the viewport only, agent-browser's `-i` snapshot keeps interactive elements only).
- **Cloudflare checkbox.** When agent-browser and Ego Lite were scripted to click the checkbox the way deja's
  gate does (wait 10 s, click once, wait), Ego Lite got through on every visit (27/27 scored reads; it clicked
  once, later visits passed on their own) and agent-browser did not (0/63 scored reads, 20 clicks). Profiles differ: deja and Ego
  Lite used long-lived browser profiles, agent-browser a fresh one per session, so this does not show that
  agent-browser cannot pass Cloudflare. Ego Lite's own guide says to hand CAPTCHAs to the user.

## Conditions that favour deja

Stated so you can discount them: in the scripted runs the recipe drafts were written in advance by someone who
knows deja (in the model-driven run the model wrote them); deja used a long-lived browser profile while
agent-browser started fresh each run.

The raw data and the scripts that recompute these numbers are not published yet.
