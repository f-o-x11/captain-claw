#!/usr/bin/env bash
# Run the BotPort hub in a tmux session that restarts it if it exits.
#
#   scripts/botport-tmux.sh start | stop | restart | status | attach | logs
#
# BOTPORT_BIN      the botport executable  (default: ~/botport-venv/bin/botport, else `botport` on PATH)
# BOTPORT_CONFIG   the config file         (default: ~/.botport/config.yaml)
# BOTPORT_ENV_FILE environment for the hub (default: ~/.botport/env, optional)
# BOTPORT_SESSION  the tmux session name   (default: botport)
#
# ~/.botport/env (chmod 600) holds BOTPORT_*=… lines — secrets you'd rather
# keep out of config.yaml. It is read here, before every (re)start of the hub
# and at boot, so the hub always runs with what `check` validated. (Variables
# merely exported in your shell do reach the hub too, but cron has none.)
# For boot: crontab -e →  @reboot /path/to/scripts/botport-tmux.sh start
set -euo pipefail

SESSION="${BOTPORT_SESSION:-botport}"
CONFIG="${BOTPORT_CONFIG:-$HOME/.botport/config.yaml}"
ENVFILE="${BOTPORT_ENV_FILE:-$HOME/.botport/env}"
BIN="${BOTPORT_BIN:-}"
if [[ -z "$BIN" ]]; then
  if [[ -x "$HOME/botport-venv/bin/botport" ]]; then BIN="$HOME/botport-venv/bin/botport"; else BIN="$(command -v botport || true)"; fi
fi
if [[ -f "$ENVFILE" ]]; then set -a; . "$ENVFILE"; set +a; fi

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: $1 is not installed" >&2; exit 1; }; }

# "=name" makes tmux match the session exactly (not "botport-dev" for "botport").
running() { tmux has-session -t "=$SESSION" 2>/dev/null; }

check_config() { "$BIN" check -c "$CONFIG" >/dev/null; }

# Read one value from the effective config (file + environment) — "section.key".
cfg() {
  "$BIN" check -c "$CONFIG" 2>/dev/null | awk -v sec="${1%%.*}:" -v key="  ${1#*.}:" '
    $0 == sec { s = 1; next }
    /^[^ #]/ { s = 0 }
    s && index($0, key) == 1 { sub(/^[^:]*: */, ""); gsub(/^'"'"'|'"'"'$/, ""); print; exit }'
}

health_url() {
  local host port scheme
  host="$(cfg server.host)"; port="$(cfg server.port)"; scheme=http
  [[ -n "$(cfg server.tls_cert)" ]] && scheme=https
  case "$host" in ""|0.0.0.0|::|"::0") host=127.0.0.1 ;; esac
  [[ "$host" == *:* ]] && host="[$host]"
  echo "$scheme://$host:${port:-23180}/healthz"
}

start() {
  need tmux
  [[ -n "$BIN" && -x "$BIN" ]] || { echo "error: botport not found (set BOTPORT_BIN)" >&2; exit 1; }
  [[ -f "$CONFIG" ]] || { echo "error: no config at $CONFIG (cp botport/config.example.yaml there)" >&2; exit 1; }
  if running; then echo "botport: already running (tmux session '$SESSION')"; return 0; fi
  check_config || { echo "error: config check failed — run: $BIN check -c $CONFIG" >&2; exit 1; }
  # Hand the hub this shell's BOTPORT_* explicitly: an already-running tmux
  # server would otherwise start it with its own, older environment.
  local env_args=() kv
  while IFS= read -r kv; do env_args+=(-e "$kv"); done < <(env | grep '^BOTPORT_' || true)
  # The loop re-reads the env file and restarts the hub after a crash; `stop`
  # ends the whole session.
  tmux new-session -d "${env_args[@]}" -s "$SESSION" \
    "while true; do if [ -f '$ENVFILE' ]; then set -a; . '$ENVFILE'; set +a; fi; '$BIN' -c '$CONFIG'; code=\$?; echo \"[\$(date -Is)] botport exited (\$code) — restarting in 5s (stop: $0 stop)\"; sleep 5; done"
  echo "botport: started in tmux session '$SESSION' (attach: $0 attach)"
}

stop() {
  if running; then
    # SIGINT first so decks get a clean "going away", then end the session.
    tmux send-keys -t "=$SESSION:" C-c
    sleep 2
    tmux kill-session -t "=$SESSION" 2>/dev/null || true
    echo "botport: stopped"
  else
    echo "botport: not running"
  fi
}

status() {
  if running; then echo "tmux session '$SESSION': up"; else echo "tmux session '$SESSION': down"; fi
  if ! check_config 2>/dev/null; then
    echo "health: not probed — $CONFIG does not pass \`botport check\` (the running hub uses what it loaded)"; return 1
  fi
  local url; url="$(health_url)"
  # -k: a built-in TLS certificate names the public host, not 127.0.0.1.
  if command -v curl >/dev/null 2>&1 && curl -fsSk --max-time 3 "$url"; then echo; else echo "health: no answer at $url"; return 1; fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  # Validate first: a broken config must not take a healthy hub down.
  restart) check_config || { echo "error: config check failed — the running hub was left alone" >&2; exit 1; }; stop; start ;;
  status) status ;;
  attach) need tmux; exec tmux attach -t "=$SESSION" ;;
  logs) need tmux; tmux capture-pane -p -S -200 -t "=$SESSION:" ;;
  *) echo "usage: $0 {start|stop|restart|status|attach|logs}" >&2; exit 2 ;;
esac
