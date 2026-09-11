#!/data/data/com.termux/files/usr/bin/bash
set -uo pipefail
cd "$(dirname "$0")"

CHILD_PID=""

cleanup() {
  if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
  fi
}
trap cleanup TERM INT EXIT

BACKOFF=2
while true; do
  START_TS="$(date +%s)"
  python bot.py &
  CHILD_PID=$!
  wait "$CHILD_PID"
  CODE=$?
  END_TS="$(date +%s)"
  RUNTIME=$((END_TS - START_TS))
  CHILD_PID=""

  # Even a clean child exit is unexpected for a 24/7 polling bot. Restart it.
  # The supervisor itself still exits cleanly when termux_start.sh sends TERM.

  # A long healthy run means the previous crash storm is over.
  if [ "$RUNTIME" -ge 120 ]; then
    BACKOFF=2
  fi

  echo "$(date -Iseconds) supervisor: bot exited code=$CODE after ${RUNTIME}s; restart in ${BACKOFF}s"
  sleep "$BACKOFF"
  if [ "$BACKOFF" -lt 30 ]; then
    BACKOFF=$((BACKOFF * 2))
    if [ "$BACKOFF" -gt 30 ]; then BACKOFF=30; fi
  fi
done
