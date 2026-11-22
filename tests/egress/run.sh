#!/bin/bash
# Does deja-browser itself open any connection that is not loopback?  Linux, run as root.
#
#   sudo PY=/path/to/venv/bin/python tests/egress/run.sh
#
# PY needs playwright (and patchright, the default driver) installed; Chromium comes from
# `python -m playwright install chromium` or CHROME=/path/to/chrome. Strace must be installed.
#
# A network namespace with only `lo`; a local test site on 127.0.0.1:8000; Chromium started
# OUTSIDE strace (it is the browser, not this tool) on CDP port 9333. The same actions and recipe
# cycle run twice (MODES, default "inproc daemon"):
#   inproc  DEJA_BROWSER_DAEMON=0: every deja-cdp / deja-browser call runs under `strace -f`
#           (python + the playwright node driver it spawns), logging every connect/sendto.
#   daemon  the background daemon is started first, under its own `strace -f` (it and every driver
#           it spawns); then every CLI call runs under `strace -f` as above and talks to it. Adds
#           the daemon-only path: the passive recording, `learn`, a run of the learned recipe, and a
#           close_session that auto-learns a second visit (its verify replays it; checked: "learned").
#           Fails if any call started a daemon of its own (that one would escape the daemon trace)
#           or if the daemon trace saw no connection at all (it would not prove anything).
# Every traced call has a timeout (STEP_TIMEOUT, default 120 s, then SIGKILL: strace ignores SIGTERM
# while a tracee lives), so a detached process can never keep strace waiting; a timeout fails the run.
# Pass = no AF_INET/AF_INET6 destination other than 127.0.0.1 / ::1, no timeout, checks above.
# SELFTEST=1 adds one deliberate connect to 1.1.1.1 and must report FAIL.
# Not covered: the browser's own traffic, the optional ntfy / hCaptcha features (off here).
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PY=${PY:-python3}
MODES=${MODES:-"inproc daemon"}
STEP_TIMEOUT=${STEP_TIMEOUT:-120}
PORT=9333
RUN=${WORK:-/tmp/deja-browser-egress}/run-$(date +%Y%m%d-%H%M%S)
mkdir -p $RUN/site $RUN/profile
CHROME=${CHROME:-$(ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)}
[ -x "$CHROME" ] || { echo "no Chromium: set CHROME or run: $PY -m playwright install chromium"; exit 2; }
command -v strace >/dev/null || { echo "install strace"; exit 2; }
command -v timeout >/dev/null || { echo "install coreutils (timeout)"; exit 2; }
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

export PYTHONPATH=$ROOT/src DEJA_BROWSER_SECRET_ER_PW=ER_SECRET_MARKER_9f3a
# browser outside strace, headless (WSL has no guaranteed display)
$NS "$CHROME" --user-data-dir=$RUN/profile --remote-debugging-port=$PORT --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check --headless=new --no-sandbox about:blank >/dev/null 2>&1 &
sleep 4

T="strace -f -qq -e trace=connect,sendto,sendmsg -s 200 -o"
CDP="$PY -m deja_browser.browser"
RCP="$PY -m deja_browser.recipe"
daemon_pid() { $PY -c "from deja_browser import client; s = client.live_state(); print(s['pid'] if s else '')"; }

run_mode() {  # run_mode inproc|daemon  -> logs in $RUN/<mode>/
  MODE=$1; D=$RUN/$MODE; n=0
  mkdir -p $D/recipes $D/data
  cat > $D/c.toml <<EOF
[browser]
executable = "$CHROME"
profile = "$RUN/profile"
port = $PORT
sessions_file = "$D/sessions.json"
[recipes]
dir = "$D/recipes"
[secrets]
backend = "env"
EOF
  export DEJA_BROWSER_CONFIG=$D/c.toml DEJA_BROWSER_HOME=$D/data
  step() {  # step <label> <cmd...>
    n=$((n+1)); echo "== $MODE $n $1" >> $D/steps.txt
    $NS timeout -k 5 $STEP_TIMEOUT $T $D/trace-$n.log "${@:2}" >> $D/steps.txt 2>&1; echo "rc=$?" >> $D/steps.txt
  }
  if [ $MODE = daemon ]; then
    export DEJA_BROWSER_DAEMON=1
    $NS $T $D/trace-daemon.log $PY -m deja_browser.daemon >/dev/null 2>&1 &
    TRACER=$!
    for _ in $(seq 100); do [ -n "$(daemon_pid)" ] && break; sleep 0.1; done
    PID0=$(daemon_pid); echo "daemon pid at start: ${PID0:-none}" >> $D/steps.txt
  else
    export DEJA_BROWSER_DAEMON=0
  fi
  step status        $CDP status
  step navigate      $CDP navigate '{"url":"http://127.0.0.1:8000/index.html"}' --session cap
  step snapshot      $CDP snapshot '{"max_chars":2000}' --session cap
  step snapshot-int  $CDP snapshot '{"interactive_only":true,"refs":true}' --session cap
  step fill          $CDP fill '{"selector":"#u","value":"ER_FILL_MARKER"}' --session cap
  step click         $CDP click '{"selector":"#go","then_snapshot":true}' --session cap
  step read          $CDP read '{"query":"ER_BODY"}' --session cap
  step evaluate      $CDP evaluate '{"code":"document.title"}' --session cap
  step netcap        $CDP network_capture '{"trigger":{"click":"#go"},"wait_ms":1500}' --session cap
  step screenshot    $CDP screenshot "{\"path\":\"$D/shot.png\"}" --session cap
  step pdf           $CDP save_as_pdf "{\"path\":\"$D/page.pdf\"}" --session cap
  step tabs          $CDP list_tabs '{}' --session cap
  step deny          $CDP navigate '{"url":"https://mail.google.com/"}' --session cap
  if [ $MODE = daemon ]; then
    # auto-learn at close_session, before the host has any recipe (else it is skipped by design)
    step auto-nav    $CDP navigate '{"url":"http://127.0.0.1:8000/index.html"}' --session autol
    step auto-fill   $CDP fill '{"selector":"#u","value":"ER_AUTO_MARKER"}' --session autol
    step auto-click  $CDP click '{"selector":"#go"}' --session autol
    step auto-read   $CDP read '{"query":"ER_BODY"}' --session autol
    step auto-close  $CDP close_session '{}' --session autol
    grep -A1 '^== daemon [0-9]* auto-close' $D/steps.txt | grep -q '"learned"' ||
      echo "DAEMON_CHECK: close_session did not report auto-learn" >> $D/steps.txt
    step recording   $RCP recording --session cap
    step learn       $RCP learn --name learned --session cap --expect ER_API_MARKER --param q=ER_FILL_MARKER
    step run-learned $RCP run 127.0.0.1/learned '{"q":"ER_ARG_MARKER"}'
  fi
  cat > $D/draft.json <<'EOF'
{"name":"search","description":"local test","entry_url":"http://127.0.0.1:8000/index.html",
 "params":[{"name":"q","required":true}],
 "request":{"method":"GET","url":"http://127.0.0.1:8000/api.json?q={q}","headers":{"X-Test":"{secret:ER_PW}"}},
 "pick":{"path":"hits","fields":["title","url"]},"writes":false}
EOF
  step verify        $RCP verify $D/draft.json '{"q":"ER_ARG_MARKER"}'
  step save          $RCP save $D/draft.json
  step run           $RCP run 127.0.0.1/search '{"q":"ER_ARG_MARKER"}'
  step run-page      $RCP run 127.0.0.1/search '{"q":"ER_ARG_MARKER"}' --tier page
  step list          $RCP list
  step gate          $RCP gate http://127.0.0.1:8000/second.html
  step close         $CDP close_session '{}' --session cap
  step close-gate    $CDP close_session '{}' --session recipe-gate
  # SELFTEST=1: one deliberate outbound attempt — the run must then FAIL (proves the check works)
  [ "${SELFTEST:-0}" = 1 ] && step selftest $PY -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('1.1.1.1', 443))"
  if [ $MODE = daemon ]; then
    PID1=$(daemon_pid); echo "daemon pid at end: ${PID1:-none}" >> $D/steps.txt
    [ -n "$PID0" ] && [ "$PID0" = "$PID1" ] || echo "DAEMON_CHECK: daemon replaced or missing (start ${PID0:-none}, end ${PID1:-none})" >> $D/steps.txt
    step daemon-stop $PY -c "from deja_browser import client; s = client.live_state(); print(client.shutdown(s) if s else 'none')"
    for _ in $(seq 100); do kill -0 $TRACER 2>/dev/null || break; sleep 0.1; done
    kill -9 $TRACER 2>/dev/null   # strace ignores SIGTERM while a tracee lives
    grep -q "sin_port=htons($PORT)" $D/trace-daemon.log ||
      echo "DAEMON_CHECK: the daemon trace has no connection to the browser (not traced?)" >> $D/steps.txt
  fi
}

for m in $MODES; do run_mode $m; done

pkill -f "user-data-dir=$RUN/profile" ; pkill -f "http.server 8000"
ip netns del ercap

# analysis: every inet connect/sendto destination, all modes
TR="$RUN/*/trace-*.log"
{
  for m in $MODES; do echo "## $m: steps $(grep -c '^== ' $RUN/$m/steps.txt)   trace files: $(ls $RUN/$m/trace-*.log | wc -l)"; done
  echo "## total connect() calls: $(cat $TR | grep -c 'connect(')"
  echo "## inet/inet6 destinations (any address):"
  cat $TR | grep -oE 'sin6?_addr=inet_(addr|pton)\([^)]*\)|sin_port=htons\([0-9]+\)' | paste - - | sort | uniq -c
  echo "## non-loopback destinations:"
  cat $TR | grep -E 'connect\(|sendto\(|sendmsg\(' | grep -E 'AF_INET' | grep -vE '127\.0\.0\.1|"::1"|::ffff:127' | head -20
  echo "(end)"
  # a send with no address goes to a connected socket, i.e. where its connect() went (checked above):
  # the CLI <-> daemon requests carry the markers over 127.0.0.1 that way. Count addressed sends only.
  echo "## markers seen in any non-loopback addressed send: $(cat $TR | grep -E 'sendto|sendmsg' | grep -E 'AF_INET' | grep -vE '127\.0\.0\.1|"::1"|::ffff:127' | grep -c MARKER)"
  echo "## steps that failed (deny = the refused mail.google.com navigate, expected):"
  for m in $MODES; do awk '/^== /{s=$0} /^rc=[1-9]/{print s, $0}' $RUN/$m/steps.txt; done
  echo "## daemon checks:"; cat $RUN/*/steps.txt | grep -E '^(DAEMON_CHECK|daemon pid)' || echo "(none)"
} > $RUN/summary.txt
cat $RUN/isolation.txt $RUN/summary.txt
echo "logs: $RUN"
bad=$(cat $TR | grep -E 'connect\(|sendto\(|sendmsg\(' | grep -E 'AF_INET' | grep -vcE '127\.0\.0\.1|"::1"|::ffff:127')
hung=$(cat $RUN/*/steps.txt | grep -cE '^rc=(124|137)$')
dchk=$(cat $RUN/*/steps.txt | grep -c '^DAEMON_CHECK')
[ "$bad" = 0 ] && [ "$hung" = 0 ] && [ "$dchk" = 0 ] && echo "PASS: no non-loopback connection ($MODES)" ||
  { echo "FAIL: $bad non-loopback calls, $hung timed-out steps, $dchk daemon check failures"; exit 1; }
