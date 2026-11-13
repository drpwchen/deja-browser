#!/bin/bash
# Does deja-browser itself open any connection that is not loopback?  Linux, run as root.
#
#   sudo PY=/path/to/venv/bin/python tests/egress/run.sh
#
# PY needs playwright (and patchright, the default driver) installed; Chromium comes from
# `python -m playwright install chromium` or CHROME=/path/to/chrome. Strace must be installed.
#
# A network namespace with only `lo`; a local test site on 127.0.0.1:8000; Chromium started
# OUTSIDE strace (it is the browser, not this tool); every deja-cdp / deja-browser call runs under
# `strace -f` (python + the playwright node driver it spawns), logging every connect/sendto.
# Pass = no AF_INET/AF_INET6 destination other than 127.0.0.1 / ::1.
# SELFTEST=1 adds one deliberate connect to 1.1.1.1 and must report FAIL.
# Not covered: the browser's own traffic, the optional ntfy / hCaptcha features (off here).
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PY=${PY:-python3}
RUN=${WORK:-/tmp/deja-browser-egress}/run-$(date +%Y%m%d-%H%M%S)
mkdir -p $RUN/site $RUN/recipes $RUN/profile
CHROME=${CHROME:-$(ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)}
[ -x "$CHROME" ] || { echo "no Chromium: set CHROME or run: $PY -m playwright install chromium"; exit 2; }
command -v strace >/dev/null || { echo "install strace"; exit 2; }
ip netns del ercap 2>/dev/null; ip netns add ercap; ip -n ercap link set lo up
NS="ip netns exec ercap"

# proof of isolation
{ echo "## isolation"; $NS ip -br addr; $NS ip route; echo "route count: $($NS ip route | wc -l)";
  $NS timeout 5 $PY -c "import socket;s=socket.socket();s.settimeout(3);s.connect(('1.1.1.1',443))" 2>&1 | tail -1;
} > $RUN/isolation.txt

# local test site: a form page and a JSON API that echoes the request (secret marker visible locally)
cat > $RUN/site/index.html <<'EOF'
<!doctype html><html><head><meta charset="utf-8"><title>ER_PAGE_MARKER</title></head><body>
<h1>ER_BODY_MARKER</h1><form><input id="u" name="u"><input id="p" type="password" name="p">
<button id="go" type="button" onclick="fetch('/api.json?q='+document.getElementById('u').value)">go</button></form>
<a href="/second.html">second</a></body></html>
EOF
echo '<!doctype html><title>second</title><p>two</p>' > $RUN/site/second.html
echo '{"hits":[{"title":"ER_API_MARKER","url":"http://127.0.0.1:8000/x"}]}' > $RUN/site/api.json
( cd $RUN/site && $NS $PY -m http.server 8000 --bind 127.0.0.1 >/dev/null 2>&1 & )

cat > $RUN/c.toml <<EOF
[browser]
executable = "$CHROME"
profile = "$RUN/profile"
sessions_file = "$RUN/sessions.json"
[recipes]
dir = "$RUN/recipes"
[secrets]
backend = "env"
EOF
export DEJA_BROWSER_CONFIG=$RUN/c.toml PYTHONPATH=$ROOT/src DEJA_BROWSER_SECRET_ER_PW=ER_SECRET_MARKER_9f3a
# browser outside strace, headless (WSL has no guaranteed display)
$NS "$CHROME" --user-data-dir=$RUN/profile --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check --headless=new --no-sandbox about:blank >/dev/null 2>&1 &
sleep 4

T="strace -f -qq -e trace=connect,sendto,sendmsg -s 200 -o"
n=0
step() {  # step <label> <cmd...>
  n=$((n+1)); echo "== $n $1" >> $RUN/steps.txt
  $NS $T $RUN/trace-$n.log "${@:2}" >> $RUN/steps.txt 2>&1; echo "rc=$?" >> $RUN/steps.txt
}
CDP="$PY -m deja_browser.browser"
RCP="$PY -m deja_browser.recipe"
step status        $CDP status
step navigate      $CDP navigate '{"url":"http://127.0.0.1:8000/index.html"}' --session cap
step snapshot      $CDP snapshot '{"max_chars":2000}' --session cap
step snapshot-int  $CDP snapshot '{"interactive_only":true}' --session cap
step fill          $CDP fill '{"selector":"#u","value":"ER_FILL_MARKER"}' --session cap
step click         $CDP click '{"selector":"#go"}' --session cap
step evaluate      $CDP evaluate '{"code":"document.title"}' --session cap
step netcap        $CDP network_capture '{"trigger":{"click":"#go"},"wait_ms":1500}' --session cap
step screenshot    $CDP screenshot "{\"path\":\"$RUN/shot.png\"}" --session cap
step pdf           $CDP save_as_pdf "{\"path\":\"$RUN/page.pdf\"}" --session cap
step tabs          $CDP list_tabs '{}' --session cap
step deny          $CDP navigate '{"url":"https://mail.google.com/"}' --session cap
cat > $RUN/draft.json <<'EOF'
{"name":"search","description":"local test","entry_url":"http://127.0.0.1:8000/index.html",
 "params":[{"name":"q","required":true}],
 "request":{"method":"GET","url":"http://127.0.0.1:8000/api.json?q={q}","headers":{"X-Test":"{secret:ER_PW}"}},
 "pick":{"path":"hits","fields":["title","url"]},"writes":false}
EOF
step verify        $RCP verify $RUN/draft.json '{"q":"ER_ARG_MARKER"}'
step save          $RCP save $RUN/draft.json
step run           $RCP run 127.0.0.1/search '{"q":"ER_ARG_MARKER"}'
step run-page      $RCP run 127.0.0.1/search '{"q":"ER_ARG_MARKER"}' --tier page
step list          $RCP list
step gate          $RCP gate http://127.0.0.1:8000/second.html
step close         $CDP close_session '{}' --session cap
step close-gate    $CDP close_session '{}' --session recipe-gate
# SELFTEST=1: one deliberate outbound attempt — the run must then FAIL (proves the check works)
[ "${SELFTEST:-0}" = 1 ] && step selftest $PY -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('1.1.1.1', 443))"

pkill -f "user-data-dir=$RUN/profile" ; pkill -f "http.server 8000"
ip netns del ercap

# analysis: every inet connect/sendto destination, per step
{
  echo "## steps: $n   trace files: $(ls $RUN/trace-*.log | wc -l)"
  echo "## total connect() calls: $(cat $RUN/trace-*.log | grep -c 'connect(')"
  echo "## inet/inet6 destinations (any address):"
  cat $RUN/trace-*.log | grep -oE 'sin6?_addr=inet_(addr|pton)\([^)]*\)|sin_port=htons\([0-9]+\)' | paste - - | sort | uniq -c
  echo "## non-loopback destinations:"
  cat $RUN/trace-*.log | grep -E 'connect\(|sendto\(|sendmsg\(' | grep -E 'AF_INET' | grep -vE '127\.0\.0\.1|"::1"|::ffff:127' | head -20
  echo "(end)"
  echo "## markers seen in any non-loopback send: $(cat $RUN/trace-*.log | grep -E 'sendto|sendmsg' | grep -vE '127\.0\.0\.1' | grep -c MARKER)"
  echo "## steps that failed (12 = the refused mail.google.com navigate, expected):"
  awk '/^== /{s=$0} /^rc=[1-9]/{print s, $0}' $RUN/steps.txt
} > $RUN/summary.txt
cat $RUN/isolation.txt $RUN/summary.txt
echo "logs: $RUN"
bad=$(cat $RUN/trace-*.log | grep -E 'connect\(|sendto\(|sendmsg\(' | grep -E 'AF_INET' | grep -vcE '127\.0\.0\.1|"::1"|::ffff:127')
[ "$bad" = 0 ] && echo "PASS: no non-loopback connection" || { echo "FAIL: $bad non-loopback calls"; exit 1; }
