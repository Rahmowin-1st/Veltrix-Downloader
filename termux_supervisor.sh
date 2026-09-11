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
  python bot.py &
  CHILD_PID=$!
  wait "$CHILD_PID"
  CODE=$?
  CHILD_PID=""

  if [ "$CODE" -eq 0 ]; then
    exit 0
  fi

  echo "$(date -Iseconds) supervisor: bot exited code=$CODE; restart in ${BACKOFF}s"
  sleep "$BACKOFF"
  if [ "$BACKOFF" -lt 30 ]; then
    BACKOFF=$((BACKOFF * 2))
    if [ "$BACKOFF" -gt 30 ]; then BACKOFF=30; fi
  fi
done
