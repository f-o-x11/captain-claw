#!/usr/bin/env bash
# Run the BotPort hub in a tmux session that restarts it if it exits.
#
#   scripts/botport-tmux.sh start | stop | restart | status | attach | logs
#
# BOTPORT_BIN     the botport executable   (default: ~/botport-venv/bin/botport, else `botport` on PATH)
# BOTPORT_CONFIG  the config file          (default: ~/.botport/config.yaml)
# BOTPORT_SESSION the tmux session name    (default: botport)
# For boot: crontab -e →  @reboot /path/to/scripts/botport-tmux.sh start
set -euo pipefail

SESSION="${BOTPORT_SESSION:-botport}"
CONFIG="${BOTPORT_CONFIG:-$HOME/.botport/config.yaml}"
BIN="${BOTPORT_BIN:-}"
if [[ -z "$BIN" ]]; then
  if [[ -x "$HOME/botport-venv/bin/botport" ]]; then BIN="$HOME/botport-venv/bin/botport"; else BIN="$(command -v botport || true)"; fi
fi

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: $1 is not installed" >&2; exit 1; }; }

health_url() {
  # Port from the effective config (env + file); falls back to the default.
  local port
  port="$("$BIN" check -c "$CONFIG" 2>/dev/null | awk '/^server:/{s=1} s&&/^  port:/{print $2; exit}')"
  echo "http://127.0.0.1:${port:-23180}/healthz"
}

running() { tmux has-session -t "$SESSION" 2>/dev/null; }

start() {
  need tmux
  [[ -n "$BIN" && -x "$BIN" ]] || { echo "error: botport not found (set BOTPORT_BIN)" >&2; exit 1; }
  [[ -f "$CONFIG" ]] || { echo "error: no config at $CONFIG (cp botport/config.example.yaml there)" >&2; exit 1; }
  if running; then echo "botport: already running (tmux session '$SESSION')"; return 0; fi
  "$BIN" check -c "$CONFIG" >/dev/null || { echo "error: config check failed — run: $BIN check -c $CONFIG" >&2; exit 1; }
  # The loop restarts the hub after a crash; `stop` kills the whole session.
  tmux new-session -d -s "$SESSION" \
    "while true; do '$BIN' -c '$CONFIG'; code=\$?; echo \"[\$(date -Is)] botport exited (\$code) — restarting in 5s (stop: $0 stop)\"; sleep 5; done"
  echo "botport: started in tmux session '$SESSION' (attach: $0 attach)"
}

stop() {
  if running; then
    # SIGTERM first so decks get a clean "going away", then end the session.
    tmux send-keys -t "$SESSION" C-c
    sleep 2
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "botport: stopped"
  else
    echo "botport: not running"
  fi
}

status() {
  if running; then echo "tmux session '$SESSION': up"; else echo "tmux session '$SESSION': down"; fi
  local url; url="$(health_url)"
  if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 3 "$url"; then echo; else echo "health: no answer at $url"; return 1; fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  attach) need tmux; exec tmux attach -t "$SESSION" ;;
  logs) need tmux; tmux capture-pane -p -S -200 -t "$SESSION" ;;
  *) echo "usage: $0 {start|stop|restart|status|attach|logs}" >&2; exit 2 ;;
esac
