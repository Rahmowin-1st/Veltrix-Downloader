#!/data/data/com.termux/files/usr/bin/bash
# Functions shared by the Termux launcher. Source this file; do not execute it.

veltrix_pid_running() {
  kill -0 "$1" 2>/dev/null || return 1
  case "$(ps -p "$1" -o stat= 2>/dev/null | tr -d ' ')" in
    Z*|X*) return 1 ;; # A reparented zombie still answers kill -0.
  esac
  return 0
}

veltrix_child_pids() {
  local parent="$1" child command owner children
  if command -v pgrep >/dev/null 2>&1; then
    children="$(pgrep -P "$parent" 2>/dev/null || true)"
  else
    children="$(ps -e -o pid=,ppid= 2>/dev/null | awk -v parent="$parent" '$2==parent {print $1}')"
  fi
  for child in $children; do
    owner="$(ps -p "$child" -o ppid= 2>/dev/null | tr -d ' ')"
    command="$(ps -p "$child" -o args= 2>/dev/null || true)"
    if [ "$owner" = "$parent" ]; then
      case "$command" in
        *"python bot.py"*) printf '%s\n' "$child" ;;
      esac
    fi
  done
}

veltrix_wait_stopped() {
  local pid="$1" seconds="$2" i
  for ((i=0; i<seconds; i++)); do
    veltrix_pid_running "$pid" || return 0
    sleep 1
  done
  ! veltrix_pid_running "$pid"
}

veltrix_stop_previous() {
  local old_pid="$1" old_command child
  [[ "$old_pid" =~ ^[0-9]+$ ]] || { echo "Invalid supervisor PID file; no process was stopped."; return 1; }
  veltrix_pid_running "$old_pid" || return 0
  old_command="$(ps -p "$old_pid" -o args= 2>/dev/null || true)"
  case "$old_command" in
    *termux_supervisor.sh*) ;;
    *) echo "PID file refers to a different process. No process was stopped. Check .veltrix.pid."; return 1 ;;
  esac

  kill -TERM "$old_pid" 2>/dev/null || true
  veltrix_wait_stopped "$old_pid" "${VELTRIX_STOP_GRACE_SECONDS:-20}" && return 0

  # The old launcher may be stuck waiting for a bot that ignored TERM. Only
  # signal a direct child whose command and parent still match this supervisor.
  for child in $(veltrix_child_pids "$old_pid"); do
    kill -TERM "$child" 2>/dev/null || true
  done
  veltrix_wait_stopped "$old_pid" "${VELTRIX_STOP_CHILD_SECONDS:-10}" && return 0
  for child in $(veltrix_child_pids "$old_pid"); do
    kill -KILL "$child" 2>/dev/null || true
  done
  veltrix_wait_stopped "$old_pid" "${VELTRIX_STOP_FINAL_SECONDS:-5}" && return 0
  echo "Previous supervisor still running (PID=$old_pid). No second worker was started."
  return 1
}
