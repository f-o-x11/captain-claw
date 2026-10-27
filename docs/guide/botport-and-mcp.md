# BotPort and MCP — decks, agents and Claude, connected

[← README](../../README.md) · [All guides](README.md)

**On this page:** [BotPort](#botport--flight-decks-that-work-together) · [MCP server](#mcp-server--claude-drives-your-fleet) · [MCP client](#mcp-client--your-agents-use-mcp-servers)

## BotPort — Flight Decks that work together

<p align="center">
  <img src="../screenshots/botport-federation.webp" width="100%" alt="Flight Decks that work together: your planner on the VPS asks Researcher@pi at home, and the request flies through your hub">
</p>

Every Flight Deck used to be an island. Now any number of them join one network through **BotPort**, a small hub that is happy on a Raspberry Pi in a tmux pane. Agents address each other as **`Agent@deck`**: your planner on the VPS asks `Researcher@pi` at home, and the answer streams back with live progress, as if both sat on the same deck.

- **Every deck dials out.** A Pi behind home NAT or a laptop joins as easily as a server. Only the hub must be reachable (Tailscale, a tunnel, or its own TLS).
- **Add a deck in a minute, with no restart.** The hub's **Decks** page makes the deck's key and a **join code**. Paste the code into that deck and it's on the network.
- **Everyone can share.** Owners put their own agents on the network from the agent's **⋯**, after a dialog that says plainly what other decks can and can't do with them.
- **Safe by construction.** A request from another deck runs in a lane of its own. It never sends email or triggers Flows, can't fan out to further decks, and obeys your deny list.
- **Built not to lose work.** Decks reconnect, answers wait for a requester that dropped, a worker that dropped gets time to come back, and resubmission is idempotent.

### Add a deck in a minute

<p align="center">
  <img src="../screenshots/botport-decks.webp" width="100%" alt="The hub's Decks page: five Flight Decks online, each with its agents, key and an Add Flight Deck button">
</p>

- The new **Decks** page makes a key and a one-time **join code**; Flight Deck's *Admin console → BotPort → Join code* takes it in one paste.
- Rotate or remove a key and it takes effect at once.
- Every deck has a display name and a description that the whole network, and your agents, see.

<p align="center">
  <img src="../screenshots/botport-join-code.webp" width="50%" alt="A one-time join code, pasted into the deck's Admin console">
</p>

### A dashboard for the wall

<p align="center">
  <img src="../screenshots/botport-network.webp" width="100%" alt="The live network map: KPIs on top, the hub with five decks around it, requests in flight on the right and a ticker below">
</p>

- A live map where every request flies from deck to hub to agent and back.
- KPIs, an in-flight rail, a ticker.
- **Kiosk mode** (`K`), a Requests page, a sign-in screen, a phone layout.

<p align="center">
  <img src="../screenshots/botport-wall.webp" width="100%" alt="Kiosk mode: the BotPort network map full screen on a wall display">
</p>

### Sharing and Swarm

- **Sharing** — an admin chooses which agents a deck offers; users can share the agents they own from the agent's menu.
- **BotPort Swarm** — DAG-based multi-agent orchestration across networked instances. Approval gates, retry with fallback, checkpointing, inter-agent file transfer (up to 50 MB), cron scheduling, and a visual dashboard.

**Set it up:** [docs/botport-deployment.md](../botport-deployment.md), from `pip install` on the Pi to the first request between two decks. Press **K** on the hub's dashboard and it becomes a wall display.

## MCP server — Claude drives your fleet

Captain Claw runs as a Model Context Protocol server over stdio — Claude Desktop and other MCP clients can browse sessions, read conversation history, and send prompts to the full agent.

```bash
captain-claw-mcp    # stdio, configure in claude_desktop_config.json
```

**Fleet access for Claude (new in 0.8.0):** Flight Deck itself is an MCP server at `/fd/mcp-server`. Add it to claude.ai or Claude Desktop as a custom connector (OAuth 2.1 — sign in with your Flight Deck account) or to Claude Code with a personal access token, and Claude can list your agents, hand them tasks and read back the results. See [docs/team-deployment.md](../team-deployment.md#connect-claude-to-your-agents-inbound-mcp).

## MCP client — your agents use MCP servers

The other direction: agents in your fleet call into MCP servers. Add a server once in **Flight Deck → Connections → MCP servers** and every agent the allowlist permits gets the tools auto-registered on boot.

- **HTTP transport** — Streamable-HTTP MCP servers, with optional OAuth2 `client_credentials`, captured `Mcp-Session-Id`, and SSE-response parsing.
- **stdio transport** — `command` + `args` + `env` for local MCP servers shipped via `npx` / `uvx` (filesystem, sqlite, github, postgres, etc.). Children are spawned lazily, auto-respawned on death, and torn down with SIGTERM/SIGKILL on close.
- **Per-agent allowlists** — Restrict each server to specific agent slugs. Disallowed agents get HTTP 404 (existence is opaque).
- **Hot reload** — Agents subscribe to `/fd/mcp/agent/events` (SSE) and re-register proxy tools the moment you change a server — no restart needed.
- **Streaming calls** — `POST /fd/mcp/<name>/call_stream` emits `progress` / `result` / `error` SSE frames for UIs that want live indicators while a long-running tool runs.

See **USAGE.md → Flight Deck → Connections → MCP servers** for the full endpoint reference and config schema.
