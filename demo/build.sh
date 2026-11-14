#!/usr/bin/env bash
# Rebuild the demo video from a live workspace.
#
# Point RADAR_HOME at a workspace that already has a scanned watchlist. The
# terminal scenes replay text captured from *that* workspace, so the demo
# can never show output the tool did not produce.
#
#   RADAR_HOME=~/radar-home demo/build.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${RADAR_HOME:?set RADAR_HOME to a workspace with scan data}"
export RADAR_HOME
RADAR="${RADAR_BIN:-$ROOT/.venv/bin/radar}"
PY="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
OUT="$RADAR_HOME/data/demo"; PORT="${PORT:-8860}"
mkdir -p "$OUT/caps"

echo "1/5  capturing real CLI output from $RADAR_HOME"
"$RADAR" home                               > "$OUT/caps/home.txt"
"$RADAR" runs | head -4                     > "$OUT/caps/batch.txt"
"$RADAR" stack "${DEMO_COMPANY:-shop_b}" | head -22 > "$OUT/caps/stack.txt"
"$RADAR" changes "${DEMO_WALLED:-walled}" | head -8  > "$OUT/caps/changes.txt"
"$RADAR" signals | head -18                 > "$OUT/caps/signals.txt"

echo "2/5  dashboard on :$PORT"
# A stale server from an earlier run would make every step below talk to
# the wrong process and fail in ways that look like the recorder's fault.
# Refuse to start on a busy port rather than silently lose the bind.
if (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q "[:.]$PORT "; then
  echo "port $PORT is already in use — stop whatever is on it (or set PORT=…) and retry" >&2
  exit 1
fi
"$RADAR" serve --port "$PORT" > "$OUT/serve.log" 2>&1 &
SERVER=$!
# Kill the server's whole process group on exit, not just the shell we spawned.
trap 'kill -- -$(ps -o pgid= -p $SERVER 2>/dev/null | tr -d " ") 2>/dev/null || kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health")" = "200" ] && break; sleep 0.3
done
# And prove it is *our* server answering, not a leftover: health reports the DB path.
if ! curl -s "http://127.0.0.1:$PORT/health" | grep -q "$RADAR_HOME"; then
  echo "the server on :$PORT is not serving $RADAR_HOME — see $OUT/serve.log" >&2
  exit 1
fi
# Warm every page the recorder will visit. The first render of each is the
# slowest, and a cold start under a 30 s locator timeout is a flaky video.
for path in / /signals /vendors "/companies/${DEMO_COMPANY:-shop_b}"; do
  curl -s -o /dev/null "http://127.0.0.1:$PORT$path"
done

echo "3/5  recording"
rm -rf "$OUT/rec"
"$PY" "$ROOT/demo/render_terminal.py"
DEMO_BASE="http://127.0.0.1:$PORT" "$PY" "$ROOT/demo/record_dashboard.py"

echo "4/5  cards"
"$PY" "$ROOT/demo/render_cards.py"

echo "5/5  stitching"
cd "$OUT"; mkdir -p parts
ffmpeg -v error -loop 1 -i card-title.png -t 4.5 -vf "fps=25,scale=1280:800,fade=t=out:st=3.9:d=0.6" -c:v libx264 -pix_fmt yuv420p -y parts/1-title.mp4
ffmpeg -v error -i "$(ls rec/terminal/*.webm | head -1)" -vf "fps=25,scale=1280:800" -c:v libx264 -pix_fmt yuv420p -y parts/2-terminal.mp4
ffmpeg -v error -i "$(ls rec/dash/*.webm | head -1)"     -vf "fps=25,scale=1280:800" -c:v libx264 -pix_fmt yuv420p -y parts/3-dash.mp4
ffmpeg -v error -loop 1 -i card-outro.png -t 6 -vf "fps=25,scale=1280:800,fade=t=in:st=0:d=0.5,fade=t=out:st=5.4:d=0.6" -c:v libx264 -pix_fmt yuv420p -y parts/4-outro.mp4
printf "file '%s'\n" 1-title.mp4 2-terminal.mp4 3-dash.mp4 4-outro.mp4 > parts/list.txt
ffmpeg -v error -f concat -safe 0 -i parts/list.txt -c copy -y martech-radar-demo.mp4
echo "done → $OUT/martech-radar-demo.mp4"
