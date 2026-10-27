"""activity tool: the agent reads its own tool calls from Flight Deck's register.

Flight Deck records every tool call an agent makes (``tool_audit.py`` →
``flight_deck/tool_audit_routes.py``). This tool reads that record back, so
"what did you do overnight", "did that email really go out" and "why did the
morning task fail" are answered from what ran, not from memory — and
``summary`` shows which tools keep failing, which calls repeat, and how.

Read-only, and the owner's: the agent reads its own calls made in its owner's
turns. A shared-agent member, a public link or another agent can't use it
(members see their own calls in Flight Deck → Activity).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from captain_claw import tool_audit, tool_audit_digest, turn_source
from captain_claw.tools.registry import Tool, ToolResult

OUTCOMES = ("ok", "error", "timeout", "blocked", "aborted", "interrupted", "running")
_NOT_OWNER_CHANNELS = frozenset({"public", "member", "link"})
_SPAN_RE = re.compile(r"^(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)$")
_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_LIST_DEFAULT_S = 86400
_SUMMARY_DEFAULT_S = 7 * 86400


def parse_when(text: Any, now: float) -> float | None:
    """An epoch time from "24h" / "7d" / "30m" / "today" / "yesterday" or an
    ISO date or time (local when it has no zone); None for nothing."""
    s = str(text or "").strip().lower()
    if not s:
        return None
    m = _SPAN_RE.match(s)
    if m:
        return now - int(m.group(1)) * _UNIT_S[m.group(2)[0]]
    if s in ("today", "yesterday"):
        midnight = datetime.fromtimestamp(now).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight - timedelta(days=1 if s == "yesterday" else 0)).timestamp()
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Can't read the time {text!r} — use 24h, 7d, 30m, today, yesterday "
                         "or an ISO date like 2026-10-27 or 2026-10-27T08:00") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _not_owner(agent: Any) -> bool:
    from captain_claw import speaker

    if speaker.is_speaker_agent(agent) or speaker.current() is not None:
        return True
    src = getattr(agent, "_turn_source", None)
    here = str(src[0] or "") if isinstance(src, tuple) and src else ""
    return turn_source.channel_of(here) in _NOT_OWNER_CHANNELS


class ActivityTool(Tool):
    """The agent's own tool calls, from Flight Deck's register."""

    name = "activity"
    timeout_seconds = 45.0
    description = (
        "Your own record of tool calls, kept by Flight Deck: every call you made in your "
        "owner's turns (chat, WhatsApp, Telegram, scheduled tasks, flows, Autonomous Work), "
        "when, what it was about, how it ended and how long it took. Read-only. "
        "Use 'list' to answer what you did (overnight, in a scheduled task, earlier, in this "
        "conversation) and to check that an action really happened before saying it did — "
        "an email sent, a file written, an event created. Use 'summary' to see which tools "
        "keep failing the same way, which calls you repeat, and what the safety guard "
        "blocked, then change your approach. Calls in shared-agent members' turns are "
        "private to them and not shown."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "summary"],
                "description": "list = the calls themselves, newest first; summary = counts and "
                               "repeated failures per tool.",
            },
            "since": {
                "type": "string",
                "description": "From when: 30m, 24h, 7d, today, yesterday or an ISO date/time "
                               "(default 24h for list, 7d for summary; at most 30 days back for summary).",
            },
            "until": {"type": "string", "description": "Up to when (list only), same forms as since."},
            "tool": {"type": "string", "description": "Only this tool, e.g. google_mail."},
            "outcome": {
                "type": "string",
                "enum": list(OUTCOMES),
                "description": "Only calls that ended this way (aborted = stopped).",
            },
            "search": {"type": "string", "description": "Text in the call's parameters, e.g. an address or a file name."},
            "this_conversation": {"type": "boolean", "description": "Only calls made in this conversation."},
            "limit": {"type": "integer", "description": "How many calls (list; default 20, at most 50)."},
            "before_id": {"type": "integer", "description": "Older calls: the id a previous list gave."},
        },
        "required": [],
    }

    async def execute(self, action: str = "list", since: str | None = None, until: str | None = None,
                      tool: str | None = None, outcome: str | None = None, search: str | None = None,
                      this_conversation: bool = False, limit: int | None = None,
                      before_id: int | None = None, **kwargs: Any) -> ToolResult:
        agent = kwargs.get("_agent")
        if _not_owner(agent):
            return ToolResult(success=False, error=(
                "Your tool-call record is your owner's — it can't be read from this conversation."))
        act = str(action or "list").strip().lower()
        if act not in ("list", "summary"):
            return ToolResult(success=False, error=f"Unknown action {action!r} — use list or summary.")
        if outcome and str(outcome).strip().lower() not in OUTCOMES:
            return ToolResult(success=False, error=f"Unknown outcome {outcome!r} — use one of {', '.join(OUTCOMES)}.")
        now = time.time()
        try:
            start = parse_when(since, now)
            end = parse_when(until, now)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        try:
            if act == "summary":
                d = await tool_audit.read_own(tool_audit.DIGEST_PATH,
                                              {"since": start if start is not None else now - _SUMMARY_DEFAULT_S})
                return ToolResult(content=tool_audit_digest.format_digest(d))
            if start is None and not this_conversation:
                start = now - _LIST_DEFAULT_S
            session = str(kwargs.get("_session_id") or "") if this_conversation else ""
            if this_conversation and not session:
                return ToolResult(success=False, error="This call has no conversation to narrow to.")
            page = await tool_audit.read_own(tool_audit.MINE_PATH, {
                "since": start, "until": end, "tool": str(tool or "").strip(),
                "outcome": str(outcome or "").strip().lower(), "q": str(search or "").strip()[:200],
                "session": session, "before_id": int(before_id) if before_id else None,
                "limit": max(1, min(50, int(limit or 20))),
            })
        except tool_audit.ReadError as exc:
            return ToolResult(success=False, error=f"Can't read your tool-call record: {exc}")
        return ToolResult(content=tool_audit_digest.format_calls(page, since=start))
