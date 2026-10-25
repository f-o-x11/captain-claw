# Captain Claw v0.8.2 Release Notes

**Release title:** Decks That Work Together
**Release date:** 2026-10-25

Until now every Flight Deck was an island. Your agents could work as a team,
but only with the agents on the same machine. 0.8.2 connects the islands.

**BotPort** is the hub that does it, rebuilt around Flight Decks. Run it on
anything that can stay on (a Raspberry Pi in a tmux pane is plenty), and every
Flight Deck that joins can lend its agents to the others. The planner on your
VPS asks `Researcher@pi`, the research agent on the Pi at home, and the answer
streams back with live progress, as if the two sat on the same deck. Decks dial
out, so a deck behind home NAT or on a laptop joins just as easily as a server.

Adding a deck takes a minute and no restart: press **Add Flight Deck** on the
hub's new **Decks** page, paste the **join code** it shows into that deck, and
press Save. Every deck gets a display name and a description that the whole
network sees. The hub's dashboard is now a **live map of the network**, made for
a wall screen: each request flies from the asking deck through the hub to the
agent, and the answer flies back.

And it isn't only for admins. **Anyone can share the agents they own**, from
the agent's menu or the kiosk's **⋯**, after a dialog that says plainly what
other decks will and won't be able to do with it.

Also in 0.8.2: devices play **long answers through** (the old 66-second
ceiling is gone; Flight Deck speaks up to about 1,500 characters and shows
the rest), the PaperColor keeps its colours clean with an **hourly white
pass**, and research reports now carry **the pictures** from the pages they
cite.

**Upgrade the hub first** (Flight Decks need protocol 2). See
[Upgrading](#upgrading).

## Highlights

### BotPort: Flight Decks that work together

```
 Agent "Planner"         Flight Deck "vps1"           BotPort hub            Flight Deck "pi"        Agent "Researcher"
 flight_deck tool  ──▶  /fd/consult-peer  ══ wss ══▶  routes Agent@deck  ══ ws ══▶  BotPort link  ──▶  its own lane
   "Researcher@pi"   ◀──  live progress    ◀═════════   progress + result ◀═══════   (proxy)       ◀──  the answer
```

- **Agent@deck.** An agent on another deck is addressed as `Researcher@pi`.
  `list_agents` shows every shared agent on the network, with its deck's
  display name: *Flight Deck 'pi' (Kitchen Pi)*. **Consult** for a quick
  answer with live progress, **delegate** for work whose result lands in the
  asking agent's chat.
- **Every deck dials out.** Only the hub must be reachable: a Raspberry Pi
  at home, a laptop or a deck behind NAT joins as easily as a VPS. Tailscale,
  a Cloudflare Tunnel, Caddy or the hub's own TLS keep it private.
- **Agents never hold the hub's credentials.** An agent asks its own Flight
  Deck, which carries the request. The receiving deck hands it to its own
  agent the same way it would a local consult.
- **Built not to lose work.**
  - A deck that drops reconnects by itself and supersedes its old socket.
  - An answer waits on the hub (15 min) for a requester that dropped.
  - A worker that dropped mid-task gets 90 s to come back.
  - Lost acknowledgements are resubmitted, and submission is idempotent.
  - A dispatch that never arrived is sent again.
  - Timeouts tell both sides.
  - Two Flight Deck processes sharing one identity back off instead of
    fighting over the link.
- **Safe by construction.** A request from another deck runs in a lane of its
  own (`BP…`), never in the owner's chat or another deck's history:
  - It never writes or sends email and never triggers Flows.
  - It never gets the network tools (no A → B → A loops) or the tools bound
    to the owner's session and memory.
  - On top of that, the admin's **deny list** applies.
  - *Accept requests from* matches only deck names the hub verified.
  - An answer that drew on members' private data is not sent off the deck.
- **Ops that a Pi in tmux deserves.**
  - `botport check`, `botport gen-key`, `botport --version`.
  - `/healthz`, `/api/status`, a rotating log file.
  - `scripts/botport-tmux.sh` (start, status, logs, attach, restart, stop;
    restart at boot).
  - Environment overrides for every setting.
  - The hub never silently moves to another port.

The whole road, from `pip install` on the Pi to the first request between two
decks, is in [docs/botport-deployment.md](docs/botport-deployment.md).

### Add a Flight Deck in a minute, without a restart

- **Decks** page on the hub's dashboard → **Add Flight Deck**. Choose:
  - a display name;
  - the deck's network name (suggested for you);
  - a description of what's on it.
- The hub makes the deck's key and shows it **once**:
  - a **join code** (the address, name, key and secret in one line);
  - the same values field by field;
  - the `FD_BOTPORT_*` variables for a deck without a screen.
- On the deck: **Admin console → BotPort → Join code**, paste, **Save**. The
  hub's dialog turns green the moment the deck connects.
- **Rotate** a secret or **Remove** a deck and it takes effect at once: the
  deck using that key is disconnected, and its Status card says why
  (*this deck's secret was replaced on the hub*).
- The hub stores only a fingerprint of each secret. Keys in `botport.yaml`
  keep working exactly as before. An empty `auth.keys` is now fine.
- Display names and descriptions reach:
  - the hub's map and drawers;
  - every deck's **Network** card;
  - the list your agents get.

### A dashboard built for the wall

- **Network** — the hub in the middle, every Flight Deck around it with its
  shared agents, load and status. Every request is drawn as it travels: asker →
  hub → agent, progress and the answer back the same way, failures in red.
  Busy decks glow. Around the map:
  - KPIs: decks online, shared agents, in flight, last 24 h, success rate and
    median answer time;
  - an in-flight rail with each agent's latest step;
  - recent results;
  - a live ticker.
- **Kiosk mode** — the ⛶ button, **K**, or `?kiosk=1`. Full screen, scaled to
  the display, cursor hidden when idle. Put it on a TV in the office and watch
  your agents work with each other.
- **Requests** — everything recent, filterable by state and deck, searchable;
  a drawer with the route, the live step, the task and the whole conversation.
- **Decks** — add, name, rotate and remove (above).
- A real sign-in screen for the admin token. A phone layout. Swarm restyled.

### Everyone shares

- **Users share the agents they own** from the agent's menu, or the kiosk's
  **⋯**. A network badge marks shared agents.
- **"Share on network?"** is a designed dialog that tells the truth:
  - what other decks can do with the agent;
  - what stays out of their reach (your chats, your profile, sending email,
    Flows, scheduled tasks, the terminal, whatever the admin blocks);
  - what is worth knowing (it answers with its own files, datastore and
    connected inbox, and its use counts toward this deck);
  - which decks are on the network now.
- **Admins stay in control.**
  - *Let users share their own agents* (`FD_BOTPORT_USER_SHARING`).
  - A Stop button on every owner's share.
  - The admin's own *Exposed agents* list, as before.
- A share lapses when the agent changes hands and goes with the agent when it
  is removed.

### Hardening

- Flight Deck sends an agent's web token **only to that agent's own
  address** (loopback, or a Docker agent's container names and IPs). The same
  rule covers the WebSocket proxy, Flows, the glasses bridge and agent
  notifications. Tokens are compared in constant time.
- The hub:
  - **Admin token** for the dashboard and REST API. Without one, the API
    answers only the hub's own machine, opened by a loopback name (no DNS
    rebinding).
  - Constant-time key checks, and a failure limiter per address and key.
  - Deck identity bound to its key.
  - `X-Forwarded-For` believed only from `trusted_proxies`.
  - Fixed: a swarm path traversal and a dashboard XSS. Gzip bodies are
    bounded.
  - Swarm disk quotas.
  - Deck changes from the dashboard must be JSON (no cross-site form posts).

### Devices

- **Long answers play through.** The answer's voice now plays through a ring
  instead of one buffer, so it no longer stops at about 66 seconds. Flight
  Deck speaks up to about 1,500 characters of an answer (`MAX_SPOKEN_CHARS`);
  the rest is shown, not read. A long answer arrives faster than it is spoken,
  so the device asks the deck to hold (`audio_hold`) and to go on
  (`audio_go`); a pause holds it. Replay comes from the ring while the whole
  clip is still there, otherwise from the deck. Firmware **0.18.2**
  (StopWatch, StickS3, PaperColor A) and **1.0.17** (PaperColor B).
- **PaperColor: a white pass.** Six-ink paper that only ever goes from one
  picture to the next loses colour and keeps faint shadows. Once an hour, and
  once after a real restart, it goes all white before the dashboard. A screen
  you asked for stays quick. Firmware **1.0.18** (PaperColor B).

### Research reports with pictures

`web_fetch` and `web_fetch_batch` keep the pictures a page shows that could
matter to the research:

- they keep pictures with a caption, telling alt text or a title, inside the
  content;
- they drop logos, icons, avatars, ads, navigation, "related" teasers and
  repeats;
- each kept picture is fitted for documents and listed with its caption,
  section and source.

Reports use them in each format:

- **.md** links the original;
- **.html** embeds it, so the file stands alone;
- **.docx** places it with `python-docx`.

`read` shows embedded pictures as short placeholders, so an HTML report never
floods the context. Settings: `tools.web_fetch.save_images`, `images_per_page`
(4), `images_per_batch` (16).

## Upgrading

**Order matters: the hub first, then the decks.** A Flight Deck on 0.8.2
refuses a hub older than protocol 2 and waits before retrying.

1. **Hub** — `cd ~/captain-claw && git pull`, then
   `~/botport-venv/bin/pip install ~/captain-claw/botport` and
   `scripts/botport-tmux.sh restart`. Run `botport check -c ~/.botport/config.yaml`
   first if you edited the config. Set `server.admin_token` if you will open
   the dashboard from another machine.
2. **Each Flight Deck** — pull, `pip install .` where Captain Claw is
   installed non-editable, **restart Flight Deck, then every agent** (agents
   on old code don't see remote agents), then hard-refresh.
3. **Connect the decks** — on the hub's **Decks** page, **Add Flight Deck**
   for each one and paste its join code into *Admin console → BotPort*. Decks
   with keys in `botport.yaml` connect with those as before.
4. **Devices** — publish the new firmware from `firmware/device` with
   `python tools/publish.py <env> --deck https://<deck>`; devices take it from
   *Menu → Software update* or by themselves.

**Changed**

- The per-agent BotPort link (the Spawner's *Direct BotPort link*) is now
  **legacy and off** for new agents; remote work goes through the deck's link,
  where the safeguards apply. Old links keep working; switch them off.
- Without `server.admin_token`, the hub's dashboard and API answer only on the
  hub's own machine, opened as `http://127.0.0.1:23180/` or
  `http://localhost:23180/` (with auth on). With a token, nothing changes.
- The hub makes dashboard keys only with `auth.enabled: true`; on an open hub,
  any deck joins without one.
- `auth.enabled: true` with an empty `auth.keys` is a warning now, not an
  error.

**New settings**

| Setting | Default | What it does |
|---|---|---|
| `server.admin_token` (hub) | — | Token for the hub's dashboard and REST API |
| `server.behind_proxy` / `trusted_proxies` | off / loopback | A TLS proxy or tunnel on the hub's host; whose `X-Forwarded-For` to believe |
| `server.tls_cert` / `tls_key` | — | Serve `wss://` from the hub itself |
| `server.data_dir` | `~/.botport` | Database, log and swarm files |
| `server.swarm_workspace_mb` / `min_free_disk_mb` | `1024` / `512` | Swarm file quota; disk the hub never fills |
| `auth.max_failures_per_minute` / `block_seconds` | `10` / `300` | Failed-registration limiter |
| `concerns.reconnect_grace_seconds` / `result_retention_seconds` | `90` / `900` | Wait for a worker that dropped; keep answers for a requester that dropped |
| `FD_BOTPORT_URL`, `_DECK_NAME`, `_KEY`, `_SECRET` | — | A deck's link, pinned by environment |
| `FD_BOTPORT_EXPOSE`, `_ACCEPT_FROM`, `_ALLOW_OUTBOUND` | — / `*` / on | What it shares, whom it serves, whether its agents ask others |
| `FD_BOTPORT_MAX_CONCURRENT`, `_DENY_TOOLS` | `4` / — | Remote tasks at once; extra tools remote requests may not use |
| `FD_BOTPORT_USER_SHARING` | on | Users may share their own agents |
| `FD_BOTPORT_ENABLED`, `FD_BOTPORT_NO_PROXY` | — | Switch the link off; ignore `HTTP(S)_PROXY` for it |
| `tools.web_fetch.save_images` / `images_per_page` / `images_per_batch` | on / `4` / `16` | Pictures kept for research |

The hub's new `decks` table creates itself on start; a deck's shares live in
Flight Deck's settings. No manual migration.
