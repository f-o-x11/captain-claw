# BotPort — connecting Flight Decks

BotPort is the hub that lets agents on one Flight Deck work with agents on
another. Each Flight Deck dials **out** to the hub and keeps one WebSocket
open; the hub routes requests between decks; the receiving deck hands each
request to its own agent, exactly as it would a local consult, and streams the
answer back.

```
 Agent "Planner"          Flight Deck "vps1"            BotPort hub            Flight Deck "pi"          Agent "Researcher"
 flight_deck tool  ──▶  /fd/consult-peer  ══ wss ══▶  routes Agent@deck  ══ ws ══▶  BotPort link  ──▶  own lane on the agent
   "Researcher@pi"   ◀──  ndjson events   ◀═════════   progress + result ◀═══════   proxy (FD)    ◀──   answer
```

* Only the hub needs to be reachable. Decks behind NAT (a Raspberry Pi at home,
  a laptop) work, because they connect out.
* Agents never talk to the hub and never hold its credentials: an agent asks
  its own Flight Deck, which carries the request.
* Any number of decks. An agent on another deck is addressed as
  **`Agent@deck`** (e.g. `Researcher@pi`).

This guide sets up the hub as a plain Python process in **tmux on a Raspberry
Pi**, with Flight Decks on two VPSs and one on the Pi itself — and notes what
changes for other layouts.

---

## 1. Install the hub (Raspberry Pi)

Raspberry Pi OS Bookworm (64-bit recommended) ships Python 3.11, which is what
BotPort needs.

```bash
sudo apt install -y python3-venv tmux git
git clone https://github.com/kstevica/captain-claw.git ~/captain-claw   # or update your checkout
python3 -m venv ~/botport-venv
~/botport-venv/bin/pip install ~/captain-claw/botport
~/botport-venv/bin/botport --version
```

The hub package is small (aiohttp, aiosqlite, pydantic, pyyaml). LLM-assisted
routing of *unaddressed* requests is optional and pulls in `litellm`:
`~/botport-venv/bin/pip install "$HOME/captain-claw/botport[llm]"`.
Deck-to-deck requests are addressed (`Agent@deck`) and never need it.

## 2. Keys — one per deck

```bash
~/botport-venv/bin/botport gen-key vps1
~/botport-venv/bin/botport gen-key vps2
~/botport-venv/bin/botport gen-key pi
```

Each prints an `auth.keys` entry for the hub and the three values to enter in
that Flight Deck. The `instance:` name is **binding**: a deck that connects with
that key *is* that deck — it cannot pretend to be another. A key without an
`instance` cannot be used by a Flight Deck at all (only by a legacy agent), and
no agent may register under a deck's name.

## 3. Configure the hub

```bash
mkdir -p ~/.botport && cp ~/captain-claw/botport/config.example.yaml ~/.botport/config.yaml
chmod 600 ~/.botport/config.yaml
nano ~/.botport/config.yaml
```

Minimum:

```yaml
server:
  host: "0.0.0.0"          # see "Reaching the hub" for 127.0.0.1 behind a proxy / tunnel
  port: 23180
  admin_token: "<openssl rand -hex 24>"   # for the dashboard + REST API
auth:
  enabled: true
  keys:
    - instance: vps1
      key: vps1-1a2b3c4d
      secret: "…"
    - instance: vps2
      key: vps2-5e6f7a8b
      secret: "…"
    - instance: pi
      key: pi-9c0d1e2f
      secret: "…"
logging:
  file: "~/.botport/botport.log"   # rotated; survives a closed tmux pane
```

Check it — this prints the effective configuration (secrets masked), every
warning, and exits non-zero on mistakes:

```bash
~/botport-venv/bin/botport check -c ~/.botport/config.yaml
```

Where settings come from: `-c FILE` (or `$BOTPORT_CONFIG`), else
`./botport.yaml`, else `./config.yaml` *if it is a BotPort config* (Captain
Claw's own `config.yaml` in the repository root is ignored), else
`~/.botport/config.yaml`. Environment variables override the file:
`BOTPORT_<SECTION>__<KEY>`, e.g. `BOTPORT_SERVER__ADMIN_TOKEN=…`,
`BOTPORT_AUTH__ENABLED=true`. To keep secrets out of the file, put such lines
in `~/.botport/env` (`chmod 600`): the tmux script below loads it for `check`,
for every (re)start of the hub and at boot — variables only exported in your
shell would not reach a hub started by cron.

## 4. Run it in tmux

```bash
~/captain-claw/scripts/botport-tmux.sh start    # tmux session "botport", restarts the hub if it exits
~/captain-claw/scripts/botport-tmux.sh status   # is it up? (/healthz)
~/captain-claw/scripts/botport-tmux.sh attach   # watch it (detach: Ctrl-b d)
~/captain-claw/scripts/botport-tmux.sh restart  # after editing the config
~/captain-claw/scripts/botport-tmux.sh stop
```

The script uses `~/botport-venv/bin/botport`, `~/.botport/config.yaml` and
(if present) `~/.botport/env`; override with `BOTPORT_BIN=…`,
`BOTPORT_CONFIG=…`, `BOTPORT_ENV_FILE=…`. `restart` checks the configuration
first and leaves a running hub alone when it does not pass. To start it at boot:
`crontab -e` and add

```
@reboot /home/pi/captain-claw/scripts/botport-tmux.sh start
```

What the hub prints when it is up:

```
BotPort v0.8.1 (protocol 2) listening on http://0.0.0.0:23180
  Flight Decks / agents connect to: ws://<this-host>:23180/ws
  Dashboard: http://<this-host>:23180/  (admin token required)
  Health:    http://<this-host>:23180/healthz
  Auth: enabled (3 keys)
```

It never silently moves to another port: if 23180 is taken it says so and
exits (the decks are configured for that port). `SIGTERM` / Ctrl-C closes the
decks' sockets with "going away" and they reconnect on their own when the hub
is back.

## 5. Reaching the hub from the VPSs

The deck's key and secret travel in the first frame, and requests carry your
agents' work — **never use plain `ws://` across the internet**. Pick one:

| Option | Hub config | Deck URL | Notes |
|---|---|---|---|
| **Tailscale / WireGuard** (simplest) | `host: "0.0.0.0"` (or the tailnet IP) | `ws://100.x.y.z:23180/ws` | Encrypted by the VPN, nothing exposed publicly. Recommended. |
| **Cloudflare Tunnel** | `host: "127.0.0.1"`, `behind_proxy: true` | `wss://botport.example.com/ws` | `cloudflared tunnel` → `http://localhost:23180`; WebSockets work out of the box. |
| **Port forward + Caddy** | `host: "127.0.0.1"`, `behind_proxy: true` | `wss://botport.example.com/ws` | Router forwards 443 to the Pi; `Caddyfile`: `botport.example.com { reverse_proxy 127.0.0.1:23180 }` (needs a domain / DDNS). |
| **Port forward + built-in TLS** | `tls_cert`, `tls_key` set | `wss://botport.example.com:23180/ws` | Certificates you renew yourself. |

With `behind_proxy: true`, bind the hub to `127.0.0.1` (so only the proxy
reaches it): it then takes the client address from the last `X-Forwarded-For`
hop — believed only from `server.trusted_proxies`, loopback by default — and no
longer treats loopback as "local", so set `admin_token` to use the dashboard.

The Pi's own Flight Deck connects locally, `ws://127.0.0.1:23180/ws`, when the
hub listens on `0.0.0.0` or `127.0.0.1` without built-in TLS. Bound to the
tailnet IP, use that IP; with built-in TLS, use the public `wss://` name (the
certificate does not name 127.0.0.1).

## 6. Connect each Flight Deck

In each Flight Deck: **Admin console → BotPort**.

| Field | vps1 | vps2 | pi |
|---|---|---|---|
| Hub URL | `wss://botport.example.com/ws` | `wss://botport.example.com/ws` | `ws://127.0.0.1:23180/ws` |
| Deck name | `vps1` | `vps2` | `pi` |
| Key / Secret | from `gen-key vps1` | from `gen-key vps2` | from `gen-key pi` |

Then choose **what this deck offers**:

* **Exposed agents** — which of this deck's agents other decks may use. Nothing
  is exposed until you pick (or switch on "every agent"). Flight Deck's own
  workers are never exposed.
* **Accept requests from** — `*` (any deck on the hub) or a list of deck names.
* **Let this deck's agents consult remote agents** — on by default.
* **Max concurrent remote tasks** and **timeout**.
* **Denied tools** — tools remote requests may not use (e.g. `shell`, `scripts`,
  `write`, `edit`), on top of `flight_deck`, `consult_peer` and `botport`, which
  remote turns never get.

"Test hub" checks the address (it reads `/healthz`, without connecting as the
deck). The status card shows connected / retrying (with the reason) and the
network: every other deck and the agents it exposes.

Headless decks can be configured by environment instead (values set this way
show as locked in the UI):

| Variable | Meaning |
|---|---|
| `FD_BOTPORT_URL` | hub URL (setting it also enables the link) |
| `FD_BOTPORT_DECK_NAME` | this deck's name |
| `FD_BOTPORT_KEY`, `FD_BOTPORT_SECRET` | credentials from `botport gen-key` |
| `FD_BOTPORT_EXPOSE` | comma-separated agent names/slugs, or `*` |
| `FD_BOTPORT_ACCEPT_FROM` | comma-separated deck names, or `*` |
| `FD_BOTPORT_ALLOW_OUTBOUND` | `true` / `false` |
| `FD_BOTPORT_MAX_CONCURRENT` | remote tasks run at once (default 4) |
| `FD_BOTPORT_DENY_TOOLS` | extra tools remote turns may not use |
| `FD_BOTPORT_ENABLED` | `false` to switch the link off |
| `FD_BOTPORT_NO_PROXY` | `1` to ignore `HTTPS_PROXY` for the hub connection |

The secret is stored encrypted (Fernet, `fd_crypto`) when `cryptography` is
installed. On a deck with sign-in off (`FD_AUTH_ENABLED=false`), the BotPort
settings can only be changed from the machine itself. Internet-facing decks
should run with sign-in on (see [team-deployment.md](team-deployment.md)).

## 7. Use it

Agents see remote agents through the `flight_deck` tool:

```
flight_deck list_agents
  Fleet agents (2): …
  Agents on other Flight Decks (2) — consult/delegate them by their full Agent@deck name:
  - Researcher@pi [remote · Flight Deck 'pi' via BotPort] — Finds things
  - Coder@vps2 [remote · Flight Deck 'vps2' via BotPort]
```

So "ask Researcher@pi to find X" works — `consult` waits for the answer (with
live progress), `delegate` hands the task off and the answer arrives as a
message when it is done. A bare name reaches a remote agent only when exactly
one deck has it; local agents always win an ambiguous name.

What a remote request can and cannot do on the receiving deck:

* It runs in **its own lane** of the target agent (`BP` + 80 bits of a hash of
  the asking deck's hub-verified name): never in the owner's main chat, never
  in another deck's history. The lane sees no owner profile, context packs,
  topic recall or members' data.
* It can **never write or send email**, never triggers the agent's Flows, and
  its reply goes only back to the requester (not to the agent's channels). An
  answer that drew on members' private data is not sent off the deck.
* It never gets the network tools (`flight_deck`, `consult_peer`, `botport` —
  no A → B → A loops, no fan-out on someone else's behalf) or the tools bound
  to the owner's own session and memory (`cron`, `sessions`, `history`,
  `topics`, `personality`, `terminal`, `insights`, `intentions`,
  `shared_agent_usage`) — the agent itself enforces this for every `BP` lane —
  plus whatever you deny (for an agent exposed to someone else's deck, consider
  `shell, scripts, write, edit`).
* Only Flight Decks are identified by the hub. A legacy agent (or a hub swarm)
  asking is let in only when *Accept requests from* is `*`, and gets a lane of
  its own.
* File attachments do not cross decks yet: put the content in the message.

## 8. Reliability — what happens when things drop

| Event | What happens |
|---|---|
| A deck's connection drops | It reconnects by itself: about 1 s after a drop that ended a healthy session; repeated failures back off up to 60 s (with jitter). |
| A request's acknowledgement is lost in a drop | The deck reports it as not-yet-acknowledged and resubmits it; the hub keeps the work running. |
| The **asking** deck drops mid-task | The answer waits on the hub (`result_retention_seconds`, 15 min) and is delivered when it reconnects. |
| The **working** deck drops mid-task | The task keeps running there; the hub waits `reconnect_grace_seconds` (90 s); the result is sent after the reconnect. |
| The working deck **restarts** | The hub sees the task is gone and tells the requester at once. |
| A request is lost in a blip | Submission is idempotent: the deck resubmits; a dispatch never acknowledged is re-sent. |
| The hub restarts | Decks reconnect; requests that were in flight fail with "the hub restarted — retry". |
| No progress for `idle_timeout_seconds` | Both sides are told; the worker stops. |
| The requester gives up | The worker is told to stop and hangs up on its agent. |
| Two Flight Deck processes share one deck identity | The one taken over says so ("took over the link (code 4000)") and waits 5 min instead of taking the link back. |

## 9. Check that it works

```bash
curl -s http://127.0.0.1:23180/healthz                       # {"ok": true, "protocol": 2, …}
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:23180/api/status | python3 -m json.tool
```

`/api/status` lists the decks (a disconnected one for about
`reconnect_grace_seconds` + 2 min after it dropped), their agents, load, and
the hub's counters. The dashboard at `http://<hub>:23180/` shows the same (it asks
for the admin token once).

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Authentication failed" | Key or secret wrong. `botport check` shows the configured keys (masked). After 10 failures a minute the address is blocked for that key for 5 min ("Too many failed registrations"). |
| "This key is not bound to a deck name" | Give the key an `instance:` (the deck's name) in the hub's `auth.keys`. |
| "the hub speaks BotPort protocol 1" | The hub is older than the decks: update BotPort on the Pi. |
| "A different Flight Deck is already connected as 'x'" | Two decks use the same name — or a deck's data directory was reset and its old connection has not timed out yet (it does within ~1 min). |
| Link flips with "another connection of this deck took over (code 4000)" | Two Flight Deck processes run with the same deck settings. |
| "hub answered 404" | URL path: it ends in `/ws`. |
| "hub answered HTTP 4xx/5xx" or TLS errors | The proxy / tunnel does not pass WebSockets, or the certificate is wrong. |
| "connection refused" / timeouts | Hub not running, wrong port, firewall, or the tunnel is down. `scripts/botport-tmux.sh status` on the Pi. |
| Remote agent missing from `list_agents` | Not exposed on its deck, not running, its deck is offline, or this deck has "consult remote agents" off. |
| "does not accept requests from 'x'" | The target deck's *Accept requests from* list. |
| Dashboard / API answer 401 | Set `server.admin_token` and send it (`Authorization: Bearer …`); without a token the API answers only on the Pi itself. |

## Upgrading from per-agent BotPort

Before Flight Deck carried BotPort, each agent could connect to the hub on its
own (the Spawner's "BotPort Connection"). That still works — the hub accepts
those connections, and decks can address them by name — but it is now
**legacy**: new agents have it off, and remote work goes through the deck's
link, where the safeguards above apply.

Process agents pick the new behaviour up with the code. **Docker agents run
the code baked into their image**: rebuild the agent image so their
`flight_deck` tool knows `Agent@deck` names and so a remote request's lane
leaves the agent's own tool bindings alone (an older image still answers in a
separate lane, but rebinds its global tools to it).
