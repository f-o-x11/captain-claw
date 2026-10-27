# Inside an agent — models, tools and guards

[← README](../../README.md) · [All guides](README.md)

## Multi-model support

Mix providers freely — each session independently selects its model.

| Provider | Models |
|---|---|
| OpenAI (API key) | GPT-6 family (per-tier reasoning effort), GPT-5.4, GPT-5.4-mini, GPT-5.4-nano, o3, o4-mini, gpt-image-1.5 |
| OpenAI (Sign in with ChatGPT) | `gpt-5`, `gpt-5-codex`, `gpt-5.1-codex`, `gpt-5.1-codex-mini`, `gpt-5.1-codex-max`, `gpt-5.2-codex`, `gpt-5.3-codex` — billed against your ChatGPT plan, no API key |
| Anthropic | Claude Opus 4.6, Sonnet 4.6, Haiku 4.5 (with prompt caching) |
| Google | Gemini 3.1 Pro/Flash, Gemini 2.5 Pro/Flash (API key or OAuth/Vertex) |
| Google subscription | Gemini via local Antigravity CLI; text only, no API key or credit overages. [Setup and limits](../antigravity-cli.md) |
| DeepSeek | Chat and reasoner models, thinking mode included (orphan tool results and pinned temperatures self-heal since 0.8.0) |
| Ollama | Any local model |
| LiteRT (on-device) | `.litertlm` Gemma models running locally via an isolated subprocess worker |
| OpenRouter | 200+ models via meta-router |

## 57 built-in tools

Shell, file I/O, web fetch/search, browser automation, PDF/DOCX/XLSX/PPTX extraction, image generation (DALL-E), OCR, vision, TTS, STT, email (SMTP/Mailgun/SendGrid), Google Workspace (Drive, Docs, Sheets, Slides, Gmail, Calendar), Microsoft 365 (Outlook mail, Calendar, OneDrive), Jev calibrated judgments (TypeSafe AI), Soniox speech in and out (`audio`), public file links, topic switching by asking (`sessions`), a read-only Telegram inbox, the StopWatch's background picture, bill payments (opt-in), WhatsApp file delivery, intentions (proactive future actions), desktop automation, screen capture with voice commands, persistent cross-session memory (todos, contacts, scripts, APIs, playbooks), datastore (SQLite tables with protection rules), deep memory (Typesense), Basna (read and start multi-agent ensemble runs), personality system, cron scheduling, BotPort fleet discovery, and Termux (Android).

See [USAGE.md](../../USAGE.md#tools-reference) for the full reference.

## Safety guards

Three layers of protection that run before, during, and after agent operations:

- **Input guards** — Validate user intent before the LLM sees it
- **Script guards** — AST-level analysis of generated code before execution
- **Output guards** — Validate tool results for hallucinations and safety
- **Action honesty** (0.8.0) — a fabrication guard rejects replies that claim an email was sent or a calendar event changed when no tool did it, and automated turns may write email only when their own instructions ask for it

Guards support two modes: `stop_suspicious` (block automatically) or `ask_for_approval` (prompt the user).

## The agent web UI

Chat, Computer (retro-themed research workspace with 14 themes), monitor pane, instruction editor, command palette, persona selector, datastore browser, deep memory dashboard, insights browser, nervous system browser, Brain Graph 3D visualization, reflections dashboard, personality editor, playbook editor, and LLM usage analytics.

**Computer** — A standalone research workspace at `/computer` with themed visual generation, exploration trees, folder browser (local + Google Drive), file attachments, PDF export, and public mode with BYOK (Bring Your Own Key).
