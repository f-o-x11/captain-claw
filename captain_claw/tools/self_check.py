"""self_check tool: the agent tests its own connections and reports its server.

The probes live in :mod:`captain_claw.self_check`; this tool picks the
sections, runs them and, when asked, saves the report as a Markdown file the
user can keep or share.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from captain_claw import self_check as S
from captain_claw.tools.registry import Tool, ToolResult
from captain_claw.tools.write import WriteTool


class SelfCheckTool(Tool):
    """Self-assessment and status: live connection tests plus server details."""

    name = "self_check"
    timeout_seconds = 150.0
    description = (
        "Self-assessment and status. Tests live, in parallel, everything you are "
        "connected to, allowed to use and expected to work — your model and every "
        "allowed model (a one-word prompt each), Flight Deck, the owner's Google "
        "(Gmail, Calendar, Drive) and Microsoft 365 (Outlook, Calendar, OneDrive), "
        "Jev, Soniox, deep memory, MCP servers, outgoing mail, web search, the "
        "Telegram and WhatsApp inboxes, BotPort — and reports the server (host, "
        "CPU load, memory, disk, this process, clock) and which tools you can use, "
        "with the reason for each hidden one. Read-only: nothing is sent or changed. "
        "Use it when the user asks what's connected / working / your status, or "
        "when something keeps failing and you need to know why."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {"type": "string", "enum": list(S.SECTIONS)},
                "description": "Only these parts (default: all): server, agent, models, "
                               "connections, tools.",
            },
            "deep": {
                "type": "boolean",
                "description": "Also run a real web search query and start the browser "
                               "(slower; default false).",
            },
            "save": {
                "type": "boolean",
                "description": "Also save the report as a Markdown file for the user.",
            },
        },
        "required": [],
    }

    async def execute(self, sections: list[str] | str | None = None, deep: bool = False,
                      save: bool = False, **kwargs: Any) -> ToolResult:
        if isinstance(sections, str):
            sections = [s.strip().lower() for s in sections.replace(",", " ").split() if s.strip()]
        agent = kwargs.get("_agent")
        base = kwargs.get("_runtime_base_path")
        workspace = Path(base).expanduser() if base else None
        saved_root = WriteTool._resolve_saved_root(kwargs)
        start = time.monotonic()
        checks = await S.run_self_check(
            agent, sections=sections or None, deep=bool(deep),
            session_id=str(kwargs.get("_session_id") or ""),
            workspace=workspace, saved_root=saved_root)
        report = S.format_report(checks, took=time.monotonic() - start)
        if save:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
            target = WriteTool.resolve_local_target(f"output/self-check-{stamp}.md", kwargs)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report + "\n", encoding="utf-8")
            report += f"\n\nSaved: {target}"
        failing = sum(1 for c in checks if c.status == "fail")
        return ToolResult(
            success=True, content=report,
            system_hint=("Tell the user plainly what works and what doesn't, leading with "
                         "anything failing and the fix shown for it. Don't paste the whole "
                         "report unless they asked for the details.") if failing else None)
