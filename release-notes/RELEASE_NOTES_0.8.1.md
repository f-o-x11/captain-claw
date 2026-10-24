# Captain Claw v0.8.1 Release Notes

**Release title:** A Kiosk for Everyone · Devices That Listen
**Release date:** 2026-10-24

0.8.0 put agents in front of other people. 0.8.1 is about what those people
found when they got there. Nine days, 121 merged pull requests and 160 commits
later, the **kiosk** — the chat-first view a team's non-admins live in — is a
complete place to work: a guide on the first visit, one-click **skill cards**,
a **File library** and a **Data library** that summarise everything the agents
keep, **Scheduled tasks** that deliver to chat, email or Telegram, one
**timeline** per agent with named topics, and a per-agent menu for the model,
the instructions and how hard it thinks. Admins get an **Admin console** of
their own, invitations by email, a waitlist that answers applicants, branding,
and daily plan limits.

The pocket client becomes a family. One firmware tree now builds for three
M5Stack devices — the **StickS3**, the round **StopWatch** and the four-inch
colour e-paper **PaperColor** — that take **signed updates over the air**, keep
**voice notes offline** and send them when the deck is back, speak the answer
**sentence by sentence as the model writes it** with the text following the
voice, and tell the agent the battery, the room's temperature and the time with
every question. And every channel now shares **one conversation**: the agent
knows whether a message came from the web, WhatsApp, the owner's Telegram or
"M5Stack StickS3 (desk)".

The version number is a patch bump, but read [Upgrading](#upgrading) before you
deploy a team deck: **daily plan limits are on by default** (30 messages and
300k tokens a day on the free plan), and MCP servers become private to whoever
added them.

## Highlights

### The kiosk, a complete place to work

- **A guide on the first visit.** A kiosk user's first visit opens *Getting
  started*: creating an agent, giving it work, connecting Google or Microsoft,
  the team Telegram bot, the profile — each step with a real screenshot in the
  viewer's theme (phone screenshots on a phone, swipe to turn the page) and a
  button straight to it. Info, Settings and the empty chat open it again.
- **A chat that reads like a conversation app.** Larger, centred messages; a
  roomier composer with attach, paste and a round send button; and an empty
  chat or a fresh topic opens with *"Good afternoon, Maya."* and the agent's
  top three **skill cards**.
- **"What can I do?"** Skill cards are one-click tasks Flight Deck writes for
  each agent from its archetype, instructions, tools, connected accounts,
  schedules, topics, intentions, insights and playbooks (with the owner's fast
  tier, cached, rewritten when the agent changes). Six sit in the composer;
  **More** opens up to sixteen more on request, with "Suggest again" and a
  nudge when the list is old. A card that needs something — an email to
  answer, claims to check — asks for it first, with a text box, a file or a
  picture.
- **How hard it thinks.** A reasoning-effort button in the composer offers
  *Auto* plus exactly the levels the agent's model has — one scale (off ·
  minimal · low · medium · high · xhigh · max) translated per vendor and
  transport for Claude, OpenAI GPT-5.x/6 and o-series, Gemini 2.5–4 and Gemma,
  Grok, DeepSeek V4, Kimi, GLM, MiniMax, Qwen, Mistral, gpt-oss, Nemotron,
  local servers, Ollama and OpenRouter. A server that refuses the field is
  healed: the request goes again without it. Agent Settings also sets a level
  per **source** (WhatsApp, Telegram, a device, scheduled tasks, …).
- **One timeline per agent.** *New topic* no longer wipes the screen: the
  conversation stays, with a divider the agent names (*"New topic · Rome
  trip"*) once the topic is left behind. A **Topics** menu jumps back to any
  topic or resumes it; the new `sessions` tool does the same by asking ("go back
  to the Rome trip") on surfaces with no menu. Turns from other channels show
  where they came from — *via WhatsApp*, *via M5Stack StickS3 (desk)* — and the
  loaded window is capped at 2,000 rows with *Earlier* and *Later messages*, so
  a long-lived tab stays quick.
- **A File library.** Flight Deck reads every file the user's agents keep —
  stopped agents too — and has the fast tier write a title, a summary, one of
  twelve categories and tags. Pictures are described by a model that can see
  (text in them read word for word); recordings and videos are transcribed
  with Soniox and summarised. Search runs through names, summaries, transcripts
  and text; a preview pane shows the selected picture, player, PDF first page,
  rendered Markdown, table or code. From a file: open, download, edit, present
  as a deck, publish a **public link**, or share it from the phone.
- **A Data library.** The same for the agents' datastore tables: each column
  profiled (how full, ranges, sums, dates, common values), a title, summary and
  key facts written from the profile, and a live data grid with readable
  headers, links, formatted numbers and a record panel per row. Export as CSV
  (formula-safe), JSON or XLSX.
- **Scheduled tasks.** Kiosk users schedule their own agents: a prompt on a
  timer (`daily`, `weekdays 08:30`, `weekly mon,wed 18:00`, …, in the user's
  time zone) whose result goes to the agent's chat, the user's own Gmail or
  Outlook, and/or Telegram. Each task runs in a session of its own (*"⏰ Morning
  brief"*), every run is logged and searchable, and *Continue in chat* picks the
  conversation up.
- **Each agent has a ⋯ menu** — **Info** (its intentions and topics),
  **Instructions** (the owner's standing instructions, applied from the next
  reply), **Settings** (provider, model, key and endpoint from the providers an
  agent can actually run on; vision auto/on/off; how it thinks; reasoning effort
  per source) and **Remove**, which deletes the agent, its chat log and its
  scheduled tasks for good. A power switch per agent starts and stops it.
- **A new agent picker.** A gallery with search by role, task or skill, family
  chips, and cards that show the model class and thinking style; a detail panel
  explains the pick — what it does, what it runs on, how it thinks, what it can
  do — before you name it.
- **A message queue you can read.** One card on top says what is happening and
  offers the next step; below it *Up next* (numbered, reorderable, editable) and
  a folded *Done* list with times and tokens; undo for remove, skip and clear.
- **Info and limits.** The Info dialog shows the user's corner of the deck in
  one read: the tier set their agents run on (never a key), their agents and
  models, the profile text their agents receive, their plan, today's message
  and token meters, and which accounts are connected.
- **On a phone** the kiosk is one screen at a time — the agent list, or one
  chat full screen with a back arrow — with dialogs as sheets, 16 px fields iOS
  doesn't zoom into, and a viewport that follows the keyboard. The Android
  HUD app (`android/hud`, `docs/android-app.md`) wraps the deck's `/hud/` for
  phones.
- **Who gets the kiosk is the admin's choice** — *Off*, *Everyone* or *Selected
  users*, a *Kiosk mode* box per user, and a default for new accounts.
  `--simple-chat` now only seeds *Everyone* until an admin picks.

### Running a deck for people

- **The Admin console** — a layout of its own for setting the deck up and
  looking after it, every page at `<deck>/console/<page>`: an **Overview**
  (people, agents running, messages today, 30-day spend, requests to review, a
  setup checklist, the machine's load), **Models** (tier sets as cards, publish
  with "changed since published", team keys), **Archetypes**, **Connections**
  (the deck-wide part above the admin's own), **Users**, **Usage & limits** and
  **Settings**. Admins start there; *Switch to user view* goes to the kiosk.
- **People in, the right way.** A **Users** page with Users · Invitations ·
  Waitlist tabs, search and filters. **Invite by email** from the admin's own
  Gmail or Outlook — the invitee picks their own password from a one-time link.
  With sign-up closed, *No account?* leads to a **waitlist** under the deck's
  name; approving an applicant creates the account and emails them the sign-in
  link.
- **Passwords.** Admins set a user's password (and sign them out everywhere);
  an admin-set password must be changed at the first sign-in; everyone gets a
  Password card in their Profile with "sign out my other devices".
- **Your deck, your name.** An instance name, a logo and a short description on
  a redesigned sign-in page, with a *Powered by Captain* footer an admin can
  switch off.
- **Daily plan limits.** Plans are *free*, *premium* and *enterprise* (*pro* is
  renamed), each with messages and AI tokens per UTC day, counted where Flight
  Deck relays a human turn (web, WhatsApp, Telegram, devices, the HUD) and
  reported by agents for every LLM call — **prompt-cache reads don't count**.
  The kiosk shows an allowance bar under the lane tabs and closes the composer
  when it's spent. Admins grant **free resets**, up to a deck-wide cap; a user
  spends one to start the day over.
- **MCP servers are private to whoever added them.** One person's OAuth sign-in
  (a CRM, a fund database) is no longer every user's: only the owner's agents
  use a server and only the owner sees, calls or manages it. An admin can share
  one with everyone (*deck-wide*), from Connections or **Admin → Integrations**.
- **MCP OAuth sign-in.** Remote MCP servers that follow the MCP authorization
  spec connect with discovery, **Dynamic Client Registration**, PKCE and a
  resource indicator, in a popup — with refresh, sign-out and revocation.
- **Public file links.** A Share button on any agent file publishes a snapshot
  at `<deck>/s/<token>` (rendered without scripts, HTML sandboxed, views and
  expiry shown, stop any time); the `public_link` tool lets an agent do the same
  when you ask it to "share that file".
- **The desktop app** opens straight into the Admin console or the locked
  kiosk, signs in to Google and Microsoft through the system browser (RFC 8252
  loopback), and no longer starts runaway agent servers.

### Devices: StickS3, StopWatch and PaperColor

The StickS3 shipped in 0.8.0. Since then `firmware/device/` builds one tree for
three devices, and all three grew up together:

| | StickS3 | StopWatch | PaperColor |
|---|---|---|---|
| screen | 240×135 LCD | 466×466 round AMOLED, touch | 4" E Ink Spectra 6, 400×600, six inks |
| talk | front button | yellow button (or the on-screen record button) | A (B build) / C (A build) |
| extras | 18 animated characters | vibration, clock face, raise to wake, background picture | two RGB status lights, room temperature and humidity, RTC, microSD |
| build | `pio run -e sticks3` | `pio run -e stopwatch` | `pio run -e papercolor_b` (or `papercolor`) |
| firmware | 0.18.1 | 0.18.1 | 1.0.16 (B) · 0.18.1 (A) |

- **Updates over the air, signed.** `tools/publish.py` builds, signs (ECDSA
  P-256, the private key never leaves your machine) and uploads a build to your
  deck; devices take it from *Menu → Software update* or by themselves on USB
  power, verify the SHA-256 and signature, and **roll back** if the new build
  can't reach Wi-Fi and the deck within 90 s. Every firmware since 0.8.1 went
  out this way.
- **Press to talk, press again to send** — or stop talking: 2.5 s of quiet sends
  the recording on every device, whatever its microphone's noise floor. A blip
  starts a recording and a lower one ends it; a rising chime comes before the
  spoken answer and a falling one after. On the PaperColor a press from sleep
  starts recording at once, while Wi-Fi and the deck connect; on the StickS3
  and the StopWatch a wake press only wakes it and the next press talks.
- **Answers as they're written.** The deck forwards the model's text as it
  streams; the device shows the answer sentence by sentence and **reads each
  sentence aloud** while the rest is still being written, so the first words
  are heard early. Only the answer streams — never the agent's own rephrasing,
  titling or tool rewrites. On the StopWatch the sentence being spoken is lit
  and the page turns with the voice; the StickS3's pages follow it too. Pause
  and play, replay, and a **stop** button while the agent works.
- **Voice notes offline.** Out of Wi-Fi, or with the deck down, talking keeps a
  voice note (8 kHz ADPCM: about 13 minutes in the StopWatch's and PaperColor's
  flash, 5½ on the StickS3, far more on the PaperColor's microSD); once the deck
  is back the device asks, then sends them as one turn the agent reads in
  order, with when each was recorded. With no
  saved network in reach a device is an offline note-taker within seconds.
- **Note-taking mode.** Hold the talk button (StopWatch: yellow, PaperColor B:
  A) and every recording is kept as a note — written to flash or the card as
  it's recorded, no length limit — until you hold it again and they all go to
  the agent at once.
- **The agent knows where you are.** Every question carries the device's
  readings — battery, Wi-Fi, how it's held and moving, the room's temperature
  and humidity, the local time — as a `[Context: …]` line, never as your words.
  Battery time left (or until full) is learned across deep sleep and shown on
  the watch face and status pages.
- **Sound by voice.** "Volume up", "beep off" (English or Croatian) never reach
  the agent: the deck catches them, the device changes and says the result.
- **Robust on weak Wi-Fi.** A turn holds through a dropped link and the answer
  is delivered after the reconnect; "new topic, how warm is it?" goes straight
  to the answer.
- **StopWatch** — idle it is a **clock face** with the agent's animated
  character; raise your wrist to wake it. Send the agent a picture and ask for
  it as the watch background (`watch_background` tool): it is cropped to the
  circle and drawn, frosted, behind every screen; swipe right from the clock for
  the picture alone, left for an **info page** (notes waiting, Wi-Fi, deck,
  agent). Status as gauges, touch in every picker, haptics, two-level menu,
  screens that slide and fade, a working ring that breathes — and smooth
  playback (a 0.5 s stutter and a reboot on every send are fixed).
- **PaperColor** — a voice-first e-paper that redraws only when it has been put
  down. The **B** build treats the screen as a dashboard: clock, date, room
  climate, battery with time left, notes waiting and room left, the links —
  redrawn every five minutes — the last answer on B at the largest size that
  fits, the options QR code on C, and the two **status lights** carrying the
  turn (red recording, a blink per agent step, green with the voice, which side
  is down when it's offline). microSD notes, hot-plugged; colours in all six
  inks.
- **StickS3** — 18 characters (Car, Matrix, Orc, Elf, Dwarf, C64, Amiga, Cat,
  Dog, Dinosaur, Alien, Caveman, HAL 9000, KITT, Pac-Man, Captain, Bot, Bird),
  three text sizes, a paged status page, screen-off and power-off timeouts,
  press to talk and offline notes.

Buttons, setup, building and publishing: `firmware/device/README.md`. The
design walk-through of the watch: `docs/stopwatch-ux-review.md`.

### One conversation across every channel

- **Every message knows its source** — `web`, `app`, `glasses`, `whatsapp`,
  `telegram`, `device:<kind>`, `scheduled`, … with a label a person reads. The
  model sees *"[via WhatsApp] …"* in its history; the kiosk shows *via M5Stack
  StickS3 (desk)*.
- **The owner's own Telegram joins the main conversation** — the one the web
  chat, WhatsApp, glasses and devices already share — and holds the lane like
  any other turn (`telegram.owner_in_main`, on).
- **Telegram grew up.** One **team bot** per deck with admin approval (and
  auto-approve); narration sent live, one message per blurb; voice notes,
  audio, video, round videos and GIFs reach the agent (voice transcribed); a
  read-only **Telegram inbox** the agents read like mail, sealed at rest, with
  "3 new from Ana" in each turn's context; and agents in **Telegram groups**
  through the bot's add-to-group link, answering @mentions with a group's own
  limited session.
- **Soniox keys live in Flight Deck** — each user's own, or a team key an admin
  shares — and agents never hold one. The new **`audio`** tool transcribes a
  file and speaks text to an MP3 that WhatsApp, Telegram and the devices
  deliver.

### Agents

- **Native vision.** A model that sees images gets the turn's pictures inline
  instead of a separate vision model's description; tool screenshots (browser,
  screen capture, `cv`) go to it too. `model.vision` auto / on / off, per agent
  in Flight Deck; a server that refuses images is healed in the same call;
  HEIC and TIFF uploads become JPEG.
- **The everyday working set is always on** — shell, glob, web fetch and search,
  the extract tools, insights, contacts, APIs, todo and `image_vision`,
  whatever an archetype's list says.
- **Jev keeps work going** — when a turn runs out of steps, Jev judges whether
  it's still making progress and extends it; when a guard refuses a repeated
  call, Jev picks the next step instead of a fixed message.
- **No phantom placeholders.** A model's own just-written file is no longer
  compacted before its next call (Claude Haiku had taken the marker for a
  broken file and given up).
- **Chats survive a deck restart** — they follow an agent to its new port,
  refresh an expired token, and say *"Agent unreachable — reconnecting…"*
  instead of a bubble per retry.

### Payments (opt-in, off by default)

With `FD_PAYMENTS=1` and `pip install .[payments]`, Captain prepares bill
payments it never makes: an **HUB3 / EPC QR barcode** from an invoice for your
own bank app, a **Revolut Business draft** you send yourself, or a **pay-by-bank
link** your bank approves with its own SCA (Enable Banking). It can buy from an
online shop with a **one-time card** you approve (Stripe Link, a Revolut
single-use card; ACP and AP2 checkout), confirm bills from a read-only **bank
statement**, remind you before a bill is due, and spot **recurring bills**.
Card numbers are scrubbed at every entry point, and only you clear a blocking
warning. Plans and decisions: `docs/payments-phase0-plan.md` to
`docs/payments-phase4-plan.md`.

### Also

- A **one-click deploy** button (RepoCloud) and a local code-lookup MCP example,
  both contributed — thank you.
- The chat's daily allowance bar sits under the lane tabs.
- The kiosk's agent menu lost Files and Data entries that repeated the Library
  and Data tiles (agents the libraries can't index keep *Files & data*).
- `image_vision` is always available, and a failed description no longer
  forbids it.
- The tool-call log drops injected keys and cuts long values (one agent's
  `process.log` had reached 3.7 GB).

## Upgrading

**Before you deploy**

1. **Daily plan limits are on.** On a deck with sign-in, every non-admin is
   counted against their plan from the first day: **free** 30 messages and
   300,000 tokens per UTC day, **premium** 300 and 3,000,000, **enterprise**
   unlimited (0 = unlimited). Admins, the desktop app and decks without
   sign-in are exempt. Check **Admin → Plans** (or *Console → Usage & limits*)
   and each user's plan before your team's next morning. Users on *pro* move to
   *premium* by themselves.
2. **Pull, then `pip install .`** wherever Captain Claw is installed
   non-editable (the deck serves the bundle from site-packages), **restart Flight
   Deck, then every agent, then hard-refresh.** Agents need the restart for
   topic dividers, source marks, device context lines, streamed device answers
   and reasoning effort.

**After you deploy**

- **Kiosk mode** is now set in **Admin → Settings → Kiosk mode**;
  `--simple-chat` / `FD_SIMPLE_CHAT` only seeds *Everyone* until you pick.
- **Admins open in the Admin console.** The old "Where do you want to start?"
  dialog is gone; the full Flight Deck is one click away.
- **MCP servers** are private to their creator. On first start, servers you
  signed in to with OAuth go to whoever signed in; other existing servers stay
  deck-wide, as they behaved. Review them in **Admin → Integrations → MCP
  servers**.
- **Soniox:** an admin moves the server's key into Flight Deck with **Admin →
  Integrations → Soniox → Move server key here** (`SONIOX_API_KEY` still works
  as the last fallback). Flight Deck no longer passes the key to agents.
- **Telegram:** the owner's private chat with their agent now continues the
  main conversation. `telegram.owner_in_main: false` keeps it separate.
- **Devices:** the StickS3 needs one last USB flash for over-the-air updates
  (it moves back to two app slots; settings survive). Generate a signing key
  once (`python tools/publish.py keygen` in `firmware/device`), then publish
  each build with `python tools/publish.py <env> --deck https://<deck>` and an
  admin token (`--token`, `FD_TOKEN`, or
  `~/.captain-claw/firmware-signing/deck-token`). **PaperColor B
  publishes as the `papercolor` kind**, so once a B build is on the deck every
  PaperColor that takes updates moves to B.
- **File library** previews of PDFs need `pypdfium2` (in `.[payments]`);
  picture and recording summaries need a vision-capable model, a Soniox key and
  `ffmpeg`.
- **Telegram inbox** seals text with a key in `<FD home>/data_key` (or
  `FD_DATA_KEY_FILE`); back it up with the database. `cryptography` is now a
  declared dependency.

**Removed and changed**

- The kiosk's right-hand files/datastore column is gone — the File and Data
  libraries and *Files & data* replace it.
- The admin start picker is gone; admins start in the console.
- The StickS3's Smiley character is gone (its users get the Captain).
- The PaperColor's notes folder is `captain-notes` (moved from `claw-notes` at
  start-up); only files the device named are ever read or removed.
- Cache reads no longer count toward any token figure Flight Deck bills.

**New settings**

| Setting | Default | What it does |
|---|---|---|
| `FD_PAYMENTS` / `FD_PAYMENTS_STATEMENTS` | off | Payments; read-only bank statements |
| `FD_PAYMENTS_SYNC_TIMES` | two daily slots | When bank statements are read |
| `FD_ENABLEBANKING_APP_ID` / `_KEY` | — | Pay-by-bank links and statements (Enable Banking) |
| `FD_LINK_CLIENT_ID` / `_SECRET` / `FD_LINK_TEST` | — | Stripe Link one-time cards |
| `FD_AP2` | off | AP2 protocol checkout |
| `FD_PUBLIC_FILE_LINKS` / `FD_PUBLIC_FILE_LINK_MAX_MB` | on / `50` | Public file links; size cap |
| `FD_AGENT_WS_WAIT_S` | `30` | How long a chat waits for a restarting agent |
| `FD_DATA_KEY_FILE` | `<FD home>/data_key` | Key that seals the Telegram inbox |
| `FD_DESKTOP` | — | Set by the desktop app (loopback sign-in) |
| `DEVICE_VOICE_MALE` / `DEVICE_VOICE_FEMALE` | Adrian / Emma | Device voices |
| `model.vision` | `auto` | Send pictures to the model itself (`on` for local Qwen builds) |
| `context.inline_images` | 4 per call | Pictures per call, newest tool pictures, size cap |
| `telegram.owner_in_main` | `true` | Owner's Telegram in the main conversation |
| `telegram.groups` / `group_turns_per_hour` | `true` / `30` | Agents in Telegram groups |
| `tools.jev.check_progress` / `review_blocks` | `true` | Jev extends working turns / reviews refused calls |
| `STREAM_NARRATION` | on | `off` folds Telegram narration into one answer |
| `~/.captain-claw/reasoning_effort.json` | — | Reasoning effort per source (also from agent Settings) |

New tables and databases (`file_library.db`, `data_library.db`, `payments.db`,
`daily_usage`, `usage_reset_credits`, `scheduler_runs`, `telegram_links`,
`telegram_inbox*`, `waitlist`, invitations, `public_shares`, …) create
themselves on restart. No manual migration.
