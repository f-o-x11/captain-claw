# Captain Claw v0.8.3 Release Notes

**Release title:** Plugged In, On the Record
**Release date:** 2026-10-27

0.8.2 let Flight Decks work with each other. 0.8.3 lets the rest of your world
in, and keeps a record of everything your agents do with it.

**Agent links** connect any system to an agent. Give your CRM, shop or
monitoring its own key and it talks to the agent over a WebSocket, plain HTTP
or signed webhooks, with files both ways. Or let Flight Deck dial out to an
orchestrator's `wss://` address, so an agent behind a home router needs no
public address at all. The agent can write to the system on its own with the
new `linked_system` tool.

**WhatsApp Web** links *your own* WhatsApp to Flight Deck as a linked device.
Your agents read your chats, files and voice notes, and, only if you turn it
on, send to the chats you whitelist. Pick a few chats and an agent answers
there when someone calls it by name. It is opt-in, off by default, and
experimental (see [the warning](#whatsapp-web-your-own-whatsapp-experimental)).

**The tool-call register** writes down every tool call every agent makes:
what it was, what started it, and how it ended, in an append-only, HMAC-chained
log. You see your own agents' calls under **Activity**; admins see everything
under **Audit**. Agents can read their own calls back with the new `activity`
tool ("did that email really go out?").

Also in 0.8.3: nudges and proposals that wait for your approval reach
**WhatsApp, Telegram, your devices and the bell**, with Approve and Reject
right there; **idle agents can sleep** and wake when they're needed; the kiosk
gets **Shared folders** with **OneDrive** beside Google Drive; and agents gain
**six new tools**, among them Word documents with **real tracked changes**.

**Restart Flight Deck, then every agent.** See [Upgrading](#upgrading).

## Highlights

### Agent links: connect any system to an agent

```
 Your system (CRM, shop, monitor)        Flight Deck                    Agent
 WebSocket   /link/ws          ──▶   cc_link_… key → the link's  ──▶   a 🔗 session of its own
 HTTP  POST  /link/messages    ──▶   queue, events, files               linked_system tool
 signed webhook                ◀──   answers, files, messages     ◀──   (writes on its own)
```

- **Connections → Systems** (in the kiosk too) adds a link to one of your
  agents. The system gets its own `cc_link_…` key, shown once.
- **Three ways in:**
  - a WebSocket at `/link/ws`;
  - HTTP: `POST /link/messages` (add `?wait=` to get the answer in the same
    call), `GET /link/events`, `GET /link/files/<id>`;
  - a webhook Flight Deck signs with HMAC-SHA256 and never follows redirects
    for.
- **Or Flight Deck dials out.** A link can name an orchestrator's `wss://`
  address and the key it issued; Flight Deck keeps the connection open while
  the agent runs. The Systems screen shows *Who connects* and the connection's
  state.
- **The agent can write first.** The new `linked_system` tool (`list`, `send`)
  messages a linked system on the agent's own initiative, if the link allows it
  (*the agent can message it*, on by default).
- **Kept apart.** A link's turns run in a session of their own, are never
  mirrored into your chat, and may write email only if the link's own
  instructions ask for it. Keys are stored only as a hash; the webhook secret
  and an outbound link's remote key are sealed. Shared-agent members can't use
  `linked_system`.
- Webhooks and outbound links go only to public `https://` and `wss://`
  addresses unless `FD_LINK_PRIVATE_ADDRESSES=1`.

### WhatsApp Web: your own WhatsApp (experimental)

> **Read this first.** WhatsApp Web support uses an unofficial client
> ([neonize](https://github.com/krypton-byte/neonize)). That is against
> WhatsApp's terms of service, and WhatsApp can ban an account that uses one.
> It is off until an admin allows it and each user consents, and it isn't
> available in the desktop app.

- **Link your own number** in **Connections → WhatsApp Web** by scanning a QR
  code or typing a pairing code, like any linked device. Flight Deck keeps the history, sealed at rest,
  for as long as you choose (7, 30, 90, 365 days, or everything).
- **Your agents read it** with the new `whatsapp_web` tool: chats, recent
  messages, search, contacts, and attachments, with voice notes transcribed
  when Soniox is set up. Each turn starts with a line on what came in since the
  agent's last turn. Only the owner's agents read it; shared-agent members
  never do.
- **Sending is yours to switch on.** Off by default. When on, an agent sends
  only to chats you whitelisted, which must already exist (it never starts a
  conversation), within a daily limit (30, up to 200), 6 a minute per chat,
  and never the same text to the same chat twice in 24 hours. Every message
  carries your signature (*— sent by my assistant* unless you change it). It
  never reacts, marks messages read or changes your presence. The fabrication
  guard now also catches a WhatsApp send that didn't happen.
- **Auto-replies in chats you choose.** Tick *auto-reply* on a whitelisted
  chat, pick one of your agents and its trigger words. The agent answers when
  it's named, called by a trigger word or `@word`, or replied to, and may keep
  talking for 15 minutes after its last message. It sees only that chat's last
  30 messages and may run one web search; never your mail, files, calendar,
  memory or other chats. It answers at most 10 times an hour per chat, ignores
  messages older than 10 minutes, and never answers another assistant. Nobody
  in the chat can change its rules.
- **See why it answered, or didn't.** *Auto-reply activity* lists each
  decision with a one-line reason, the reply or the reason for silence, the
  model's reasoning, the search, tokens and time.
- Install with `pip install 'captain-claw[whatsapp-web]'`. Kill switches:
  `FD_WHATSAPP_WEB=off`, `FD_WHATSAPP_WEB_SEND=off`,
  `FD_WHATSAPP_WEB_AUTOREPLY=off`.

The WhatsApp Cloud API channel you already have is unchanged, except that a
refused send now logs Meta's error code, and a de-registered number is an error
that names the fix (`POST /<phone-number-id>/register`). The Connections test
button says so too.

### The tool-call register: every call, on the record

- **Every tool call** by every agent the deck runs is recorded: the agent, the
  time, the tool, its parameters with secrets masked, what started it (a chat,
  a channel, a schedule, a link…), and how it ended: `ok`, `error`, `blocked`,
  `timeout`, `aborted` or `interrupted`. Calls are recorded *before* the safety
  checks, so refused calls show up too.
- **Where you see it:**
  - **Activity** in the kiosk's Settings, and **⋯ → Activity** on each agent:
    your own agents' calls.
  - **Admin console → Audit**: every call on the deck, an integrity check of
    the chain, and the retention (90 days by default, 7 to 3,650).
  - CSV and JSON-lines export.
- **Tamper-evident.** Rows are append-only and HMAC-chained, and gaps show.
  Someone with access to the host *and* the deck's key could still rebuild the
  chain, and tools a model provider runs on its own side aren't recorded.
- **A shared agent's owner** sees members' calls without their parameters or
  errors.
- **Agents read their own record.** The new `activity` tool (`list`,
  `summary`) answers "what did you do overnight?" from the register, not from
  memory. It sees only the agent's own calls from its owner's turns, never
  members' calls or file and mail bodies, and is refused in public-link,
  member, Telegram-group and link turns. Self-reflection now also gets a
  digest of repeated failures and repeated calls.

### Nudges and proposals reach you

- **Autonomous Work's nudges and the proposals waiting for your approval** now
  go where you are:
  - **WhatsApp** (as before, now with proposals);
  - **Telegram** (new, off until you turn it on);
  - **your devices** (new, on): a beep, a banner, and a *Waiting* count;
  - **the bell** in Flight Deck, with **Approve / Reject** on each proposal.
- Choose them on the new **Notifications** card in Connections (the kiosk's
  Settings and the admin console have it too). Quiet hours send the count
  without the beep.
- The bell lists every waiting proposal, including ones made before this
  release, once each. **⤢ Show bigger** opens it in a large dialog, and
  nudges and proposals render as Markdown (pictures dropped, links shown as
  text).
- Devices only ever get the count and a beep; the text stays in Flight Deck.

### Idle agents sleep

- An admin can let idle agents sleep to free memory: **Admin console →
  Settings → Agents → Put idle agents to sleep**, from 10 minutes to a week
  (off by default).
- A sleeping agent wakes **five minutes before** its scheduled tasks and cron
  jobs, and **when something needs it**: a chat, a channel message, a device,
  a link, a member, another agent, a Flow or an auto-reply.
- The kiosk shows *Asleep*, process cards an `asleep` badge, and the
  Processes page counts sleeping agents apart.
- Never put to sleep: run workers, Iskra beings, agents with a Telegram,
  Slack or Discord bot of their own, an agent with a run in flight, and Docker
  agents.

### The kiosk: Shared folders, with OneDrive

- A fourth tile, **Folders**, beside Scheduled, Library and Data: the shared
  VFS as a library. Recent files; My folders, Agent runs, Shared with me,
  Linked and Google Drive; preview, edit and save, rename, drag-and-drop
  upload, search, and a `vfs:` address to copy for your agents. The dialog
  opens with a card that explains what the VFS is.
- **OneDrive folders mount like Google Drive**, read-only: your own or one
  shared with you, through your Microsoft connection. Agents reach it as
  `vfs:<name>/`.
- **Fullscreen** on the File library, the Data library (and an open table) and
  Shared folders.
- **⌘/Ctrl+Enter queues a message.** Enter still sends; ⌘/Ctrl+Enter adds the
  message to the lane's queue, switches the queue to Auto and opens it.
- The browser tab's icon is the deck's **Branding** logo.

### Six new tools

| Tool | What it does |
|---|---|
| `docx` | Creates Word files from Markdown, reads them, and edits them as **real Word tracked changes**; lists, accepts and rejects changes and comments. A user's upload is never changed: the redline is a new file. Members can use it, and their changes carry their own name. Checked in Microsoft Word. |
| `self_check` | Tests the agent's live connections and models and reports its server. Read-only. |
| `reasoning_effort` | Reads or changes the agent's own reasoning effort when you ask, from any channel; it changes the same setting you would. Owner-only. |
| `activity` | The agent's own tool calls, read back from the register (above). |
| `linked_system` | Messages a linked system (above). |
| `whatsapp_web` | Reads, and with your consent sends to, your own WhatsApp (above). |

`write` and `edit` now refuse `.docx` files and point to `docx`. That makes
**63 built-in tools** per agent.

### Mail

- When the repeat check refuses a send, it now says that a new edition of a
  recurring report isn't a repeat: put the date in the subject and send again.
  A real same-day duplicate is still refused.

### Devices

- **Firmware 0.19.1** (StickS3, StopWatch, PaperColor A) and **1.0.21**
  (PaperColor B):
  - a beep, a banner (StopWatch and StickS3) and a magenta **count badge** on
    every screen when something waits for you in Flight Deck;
  - the device **renews its link to the deck** by itself: 30 seconds after its
    agent goes missing (backing off to every 10 minutes), and after 20 minutes
    idle, never in the middle of something.
- Older firmware ignores the new count, so the deck can go first.

### Security fix

- A removed agent whose folder was kept can no longer pass that folder, and
  its files, to another user who creates an agent with the same name. Flight
  Deck now marks each agent folder with its owner. An unmarked leftover folder
  that holds files and has no owner on record is refused to everyone: pick
  another name, or purge it. Purging an agent now also drops its File and Data
  library rows.

## Upgrading

1. **Each Flight Deck** — pull, and `pip install .` where Captain Claw is
   installed non-editable. Add `pip install 'captain-claw[whatsapp-web]'` only
   if you'll allow WhatsApp Web.
2. **Restart Flight Deck, then every agent** — including Docker and standalone
   agents. Flight Deck creates its new tables on start; agents need the
   restart for the six tools, the register and link turns. Then hard-refresh
   every browser.
3. **Devices** — publish the new firmware from `firmware/device` with
   `python tools/publish.py <env> --deck https://<deck>`; devices take it from
   *Menu → Software update* or by themselves.
4. **Opt in, as an admin** — *Allow WhatsApp Web*, the idle-sleep time, and
   the audit retention, all in the admin console. Users turn on Telegram
   notifications themselves.

BotPort hubs need nothing new: 0.8.3 keeps protocol 2.

**Changed**

- An agent folder left over from a removed agent, with files and no owner on
  record, can't be reused by anyone (see [Security fix](#security-fix)).
- `write` and `edit` refuse `.docx`; agents use `docx`.
- `FD_LINK_WEBHOOKS_PRIVATE` is now `FD_LINK_PRIVATE_ADDRESSES`; the old name
  still works.

**New settings**

| Setting | Default | What it does |
|---|---|---|
| `FD_LINK_PRIVATE_ADDRESSES` | off | Allow `ws://`, `http://` and private hosts for webhooks and outbound links |
| `FD_WHATSAPP_WEB` / `_SEND` / `_AUTOREPLY` | — | `off` switches WhatsApp Web, its sending, or its auto-replies off deck-wide |
| *Allow WhatsApp Web* (admin) | off | Lets users link their own WhatsApp |
| *Put idle agents to sleep* (admin) | off | Idle time before an agent sleeps, 10 min to a week |
| `FD_AGENT_IDLE_STOP_S` / `FD_AGENT_IDLE_TICK_S` | — / `60` | Idle time in seconds, overriding the admin setting; how often the controller looks |
| Audit retention (admin) | 90 days | How long tool calls are kept, 7 to 3,650 days |
| `CLAW_TOOL_AUDIT` (agent) | on | `off` stops an agent recording its tool calls |
| Notifications: WhatsApp / Telegram / devices (per user) | on / off / on | Where nudges and waiting proposals go |
| WhatsApp Web sending, daily limit, signature, retention (per user) | off, 30, *— sent by my assistant*, everything | |
| *The agent can message it* (per link) | on | Whether `linked_system` may write to that system |

New Flight Deck stores create themselves on start: `agent_links`,
`agent_link_events`, `agent_link_files`, `tool_audit.db` with its key file, and
each user's WhatsApp Web store. No manual migration.
