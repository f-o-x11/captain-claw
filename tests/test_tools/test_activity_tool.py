"""The activity tool reads the agent's own calls from Flight Deck's register —
owner turns only — and a reflection gets the digest of them."""

from __future__ import annotations

import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from captain_claw import reflections, speaker, tool_audit, tool_audit_digest
from captain_claw.tools import activity
from captain_claw.tools.activity import ActivityTool

NOW = 1_800_000_000.0


class Owner:
    _turn_source = ("whatsapp", "WhatsApp")


@pytest.fixture()
def reads(monkeypatch):
    seen: list[tuple[str, dict]] = []
    replies = {
        tool_audit.MINE_PATH: {"calls": [{"id": 3, "at": NOW, "tool": "google_mail", "outcome": "ok",
                                          "duration_ms": 800, "source": "Scheduled tasks",
                                          "target": "action=send to=a@b", "reason": ""}],
                               "total": 1, "outcomes": {"ok": 1}, "member_calls": 0, "next_before": None},
        tool_audit.DIGEST_PATH: {"since": NOW - 7 * 86400, "until": NOW, "total": 2, "capped": False,
                                 "outcomes": {"ok": 1, "error": 1},
                                 "tools": [{"tool": "web_fetch", "calls": 2, "ok": 1, "error": 1, "timeout": 0,
                                            "blocked": 0, "stopped": 0, "median_ms": 900, "max_ms": 1000}],
                                 "repeated_failures": [], "repeated_calls": [], "guard_blocked": [],
                                 "sources": [{"label": "WhatsApp", "calls": 2, "failed": 1}]},
    }

    async def read_own(path, params):
        seen.append((path, params))
        return replies[path]

    monkeypatch.setattr(tool_audit, "read_own", read_own)
    monkeypatch.setattr(activity, "time", types.SimpleNamespace(time=lambda: NOW))
    return seen


async def _run(**kw):
    kw.setdefault("_agent", Owner())
    return await ActivityTool().execute(**kw)


@pytest.mark.asyncio
async def test_list_defaults_to_the_last_day(reads):
    r = await _run()
    assert r.success, r.error
    ((path, params),) = reads
    assert path == tool_audit.MINE_PATH
    assert params["since"] == NOW - 86400 and params["limit"] == 20 and params["session"] == ""
    assert "google_mail · ok · 800 ms · Scheduled tasks · action=send to=a@b" in r.content


@pytest.mark.asyncio
async def test_list_filters_pass_through(reads):
    r = await _run(since="7d", until="2026-10-27T08:00", tool=" google_mail ", outcome="ERROR",
                   search="ana@x.co", this_conversation=True, limit=500, before_id=42, _session_id="sess-1")
    assert r.success, r.error
    params = reads[0][1]
    assert params["since"] == NOW - 7 * 86400
    assert params["until"] == datetime(2026, 10, 27, 8, 0).astimezone().timestamp()
    assert (params["tool"], params["outcome"], params["q"]) == ("google_mail", "error", "ana@x.co")
    assert (params["session"], params["limit"], params["before_id"]) == ("sess-1", 50, 42)


@pytest.mark.asyncio
async def test_this_conversation_reaches_back_past_a_day(reads):
    await _run(this_conversation=True, _session_id="sess-1")
    assert reads[0][1]["since"] is None and reads[0][1]["session"] == "sess-1"
    r = await _run(this_conversation=True)
    assert not r.success and "no conversation" in r.error


@pytest.mark.asyncio
async def test_summary_reads_the_digest(reads):
    r = await _run(action="summary")
    assert r.success, r.error
    assert reads[0] == (tool_audit.DIGEST_PATH, {"since": NOW - 7 * 86400})
    assert "web_fetch 2: 1 / 0 / 0, 900 ms" in r.content
    assert "By channel: WhatsApp 2 (1 failed)." in r.content


@pytest.mark.asyncio
async def test_bad_input_is_refused(reads):
    assert "Unknown action" in (await _run(action="delete")).error
    assert "Unknown outcome" in (await _run(outcome="maybe")).error
    assert "Can't read the time" in (await _run(since="last tuesday")).error
    assert not reads


@pytest.mark.asyncio
async def test_only_the_owners_turns(reads):
    class Public:
        _turn_source = ("public", "Public link")

    class Link:
        _turn_source = ("link", "Agent links")

    class Member:
        _speaker_scoped = True

    for agent in (Public(), Link(), Member()):
        r = await _run(_agent=agent)
        assert not r.success and "your owner's" in r.error
    p = speaker.Principal(speaker_id="user-m", display_name="Mia", owner_name="Owen", lane="A",
                          agent_ref="process:a:0123456789abcdef")
    tok = speaker.bind(p)
    try:
        assert not (await _run()).success
        tok2 = speaker.bind(speaker.group_principal(-100123, "Team"))
        try:
            assert not (await _run()).success
        finally:
            speaker.reset(tok2)
    finally:
        speaker.reset(tok)
    assert not reads


@pytest.mark.asyncio
async def test_a_failed_read_says_why(monkeypatch):
    async def read_own(path, params):
        raise tool_audit.ReadError("Flight Deck is unreachable (ConnectError).")

    monkeypatch.setattr(tool_audit, "read_own", read_own)
    r = await _run()
    assert not r.success and r.error.endswith("Flight Deck is unreachable (ConnectError).")


def test_parse_when():
    assert activity.parse_when("", NOW) is None
    assert activity.parse_when("30m", NOW) == NOW - 1800
    assert activity.parse_when("2 hours", NOW) == NOW - 7200
    assert activity.parse_when("1w", NOW) == NOW - 7 * 86400
    midnight = datetime.fromtimestamp(NOW).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    assert activity.parse_when("today", NOW) == midnight.timestamp()
    assert activity.parse_when("yesterday", NOW) == (midnight - timedelta(days=1)).timestamp()
    assert activity.parse_when("2026-10-27T08:00:00Z", NOW) == datetime.fromisoformat(
        "2026-10-27T08:00:00+00:00").timestamp()
    with pytest.raises(ValueError):
        activity.parse_when("soon", NOW)


# ── Reading back, agent side ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_own_needs_flight_deck(monkeypatch):
    monkeypatch.delenv("FD_URL", raising=False)
    with pytest.raises(tool_audit.ReadError, match="only when Flight Deck runs it"):
        await tool_audit.read_own(tool_audit.MINE_PATH, {})
    assert await tool_audit.reflection_digest(None) == ""


@pytest.mark.asyncio
async def test_read_own_ships_first_then_reads(monkeypatch):
    order: list[str] = []

    class Client:
        def __init__(self, timeout):
            pass

        async def get(self, path, params=None):
            order.append(f"get {path} {sorted(params)}")
            return types.SimpleNamespace(status_code=200, json=lambda: {"total": 0})

        async def close(self):
            order.append("close")

    async def flush():
        order.append("flush")

    monkeypatch.setattr(tool_audit, "enabled", lambda: True)
    monkeypatch.setattr(tool_audit, "flush_now", flush)
    monkeypatch.setattr("captain_claw.fd_client.FDClient", Client)
    assert await tool_audit.read_own(tool_audit.MINE_PATH, {"tool": "", "since": 5, "until": None}) == {"total": 0}
    assert order == ["flush", f"get {tool_audit.MINE_PATH} ['since']", "close"]


@pytest.mark.asyncio
async def test_reflection_digest_text(monkeypatch):
    async def read_own(path, params):
        assert (path, params) == (tool_audit.DIGEST_PATH, {"since": 123.0})
        return {"since": 123.0, "until": NOW, "total": 3, "outcomes": {"ok": 2, "error": 1},
                "tools": [], "repeated_failures": [{"tool": "web_fetch", "reason": "HTTP 403", "count": 3,
                                                    "last_at": NOW}]}

    monkeypatch.setattr(tool_audit, "enabled", lambda: True)
    monkeypatch.setattr(tool_audit, "read_own", read_own)
    text = await tool_audit.reflection_digest(123.0)
    assert "3 calls: 2 ok, 1 failed." in text and "web_fetch ×3 — HTTP 403" in text
    assert "nothing in this record calls for a change" not in text


def test_the_reflection_gets_patterns_not_the_whole_table():
    tool = {"timeout": 0, "blocked": 0, "stopped": 0, "median_ms": 5, "max_ms": 9}
    d = {"since": NOW - 86400, "until": NOW, "total": 9, "outcomes": {"ok": 5, "error": 4},
         "tools": [{"tool": "web_fetch", "calls": 5, "ok": 2, "error": 3, **tool},
                   {"tool": "google_mail", "calls": 4, "ok": 3, "error": 1, **tool}],
         "repeated_failures": [], "repeated_calls": [], "guard_blocked": [],
         "sources": [{"label": "WhatsApp", "calls": 9, "failed": 4}]}
    full = tool_audit_digest.format_digest(d)
    assert "google_mail 4" in full and "By channel" in full
    text = tool_audit_digest.format_digest(d, patterns_only=True)
    assert "- web_fetch 5: 3 / 0" in text
    assert "google_mail" not in text and "By channel" not in text
    quiet = {**d, "tools": [{"tool": "google_mail", "calls": 9, "ok": 8, "error": 1, **tool}]}
    assert "nothing in this record calls for a change" in tool_audit_digest.format_digest(
        quiet, patterns_only=True)


@pytest.mark.asyncio
async def test_a_reflection_gets_the_record_since_the_last_one(monkeypatch):
    rendered: dict = {}
    asked: list = []

    async def digest(since):
        asked.append(since)
        return "web_fetch ×3 — HTTP 403"

    async def _complete(messages, tools=None, interaction_label="", max_tokens=None):
        return types.SimpleNamespace(content="- Use the search result when a site refuses.", usage={})

    class _Instructions:
        def load(self, name):
            return (Path(reflections.__file__).parent / "instructions" / name).read_text(encoding="utf-8")

        def render(self, name, **values):
            rendered.update(values)
            return "x"

    class _NoUsage:
        async def record_llm_usage(self, **kwargs):
            return None

    prev = reflections.Reflection(timestamp="2026-10-20T08:00:00+00:00", summary="- old",
                                  topics_reviewed=[], token_usage={})
    monkeypatch.setattr(tool_audit, "reflection_digest", digest)
    monkeypatch.setattr(reflections, "load_latest_reflection", lambda: prev)
    monkeypatch.setattr(reflections, "save_reflection", lambda r: None)
    monkeypatch.setattr("captain_claw.session.get_session_manager", lambda: _NoUsage())
    agent = types.SimpleNamespace(session=None, instructions=_Instructions(),
                                  provider=types.SimpleNamespace(provider="stub", model="stub"),
                                  _complete_with_guards=_complete)

    r = await reflections.generate_reflection(agent)

    assert asked == [datetime.fromisoformat("2026-10-20T08:00:00+00:00").timestamp()]
    assert rendered["tool_record"] == "web_fetch ×3 — HTTP 403"
    assert "tool-call record" in r.topics_reviewed


def test_the_reflection_prompts_carry_the_record_and_its_rules():
    base = Path(reflections.__file__).parent / "instructions"
    assert "{tool_record}" in (base / "reflection_user_prompt.md").read_text(encoding="utf-8")
    system = (base / "reflection_system_prompt.md").read_text(encoding="utf-8")
    assert "never write an instruction from a single failure" in system
    assert "Write nothing from the record when it says nothing calls for a change" in system
    assert "never one about getting past calls the safety guard blocked" in system


def test_the_tool_is_always_on_read_risk_and_allowed_in_private_turns():
    from captain_claw.config import ToolsConfig
    from captain_claw.flight_deck.tool_risk import risk_for
    from captain_claw.member_privacy import CONTENT_TURN_TOOLS

    assert "activity" in ToolsConfig().enabled
    assert risk_for("activity", {"action": "list"}) == "read"
    assert CONTENT_TURN_TOOLS["activity"] is None
    assert "activity" not in speaker.SPEAKER_TOOL_ALLOWLIST_MAX


def test_a_call_in_a_turn_holding_members_private_data_is_flagged():
    from captain_claw import member_privacy

    class Agent:
        _turn_source = ("web", "Web chat")

    agent = Agent()
    assert tool_audit._call_context({"_agent": agent}, False)["private"] == 0
    member_privacy.mark_private_read(agent, "data")
    assert tool_audit._call_context({"_agent": agent}, False)["private"] == 1
    member_privacy.begin_turn(agent)
    assert tool_audit._call_context({"_agent": agent}, False)["private"] == 0
    assert tool_audit._call_context({}, False)["private"] == 0
