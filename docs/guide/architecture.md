# Architecture

[← README](../../README.md) · [All guides](README.md)

Where each part of Captain Claw lives in the code.

| Component | Path |
|---|---|
| Agent (14-mixin composition) | `captain_claw/agent.py` |
| LLM providers | `captain_claw/llm/` |
| 57 tools + registry | `captain_claw/tools/` |
| Flight Deck (FastAPI + React) | `captain_claw/flight_deck/` |
| Device bridge (StickS3 / StopWatch / PaperColor, `/device/ws`) + OTA store + backgrounds | `captain_claw/flight_deck/device_bridge.py`, `device_firmware.py`, `device_backgrounds.py` |
| Device firmware (PlatformIO, all three devices) | `firmware/device/` |
| Kiosk File and Data libraries | `captain_claw/flight_deck/file_library.py`, `data_library.py` |
| Scheduled tasks | `captain_claw/flight_deck/fd_scheduler.py` |
| Admin console | `flight-deck/src/components/console/` |
| Reasoning effort per vendor | `captain_claw/llm/reasoning.py` |
| Payments (opt-in) | `captain_claw/payments/` |
| HUD for smart glasses (`/hud/`) | `flight-deck/src/hud/` |
| Bat (plan, loop, judge, store) | `captain_claw/flight_deck/bat_*.py` |
| Fabrication guard | `captain_claw/fabrication_guard.py` |
| Lupa research desk (on the Flight Deck API) | `lupa/` |
| DAG orchestrator | `captain_claw/session_orchestrator.py` |
| Semantic memory (vector + BM25) | `captain_claw/semantic_memory.py` |
| Deep memory (Typesense) | `captain_claw/deep_memory.py` |
| Insights (fact extraction) | `captain_claw/insights.py` |
| Nervous system (dreaming) | `captain_claw/nervous_system.py` |
| Cognitive tempo | `captain_claw/cognitive_tempo.py` |
| MCP server | `captain_claw/mcp_serve.py` |
| BotPort client (agent, legacy) | `captain_claw/botport_client.py` |
| BotPort link (Flight Deck ⇄ other decks) | `captain_claw/flight_deck/botport_link.py`, `botport_routes.py` |
| BotPort hub | `botport/botport/` |
| Web UI + REST API | `captain_claw/web/` |
| Prompt templates (~100 files) | `captain_claw/instructions/` |
| Config (Pydantic) | `captain_claw/config.py` |

For every command, tool and config key, see [USAGE.md](../../USAGE.md).
