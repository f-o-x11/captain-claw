"""Claude Haiku on glasses staging (2026-10-22/23): after every report write it
told the user the file "contains a placeholder marker", tried to rewrite it, got
blind-rewrite-blocked, and stopped with "I can't use tools now".

The cause was in-turn write compaction: the model's very next call showed its
own write with ``content`` = ``[written to disk: …]``. These tests pin the
fixes: compaction runs one step late, the marker says the text was saved, and
the guard messages no longer forbid the remaining steps of the request."""

import json

import pytest

from captain_claw import write_guard
from captain_claw.agent import Agent
from captain_claw.llm import LLMProvider, LLMResponse, ToolCall
from captain_claw.session import Session
from captain_claw.tools.registry import ToolRegistry, _loggable_args
from captain_claw.tools.write import WriteTool


class _Dummy(LLMProvider):
    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        return LLMResponse(content="ok")

    async def complete_streaming(self, messages, tools=None, temperature=None, max_tokens=None):
        if False:
            yield ""

    def count_tokens(self, text: str) -> int:
        return len(text)


class _DummySM:
    async def save_session(self, session) -> None:
        return None


def _agent(tmp_path):
    agent = Agent(provider=_Dummy())
    agent._initialized = True
    agent.session = Session(id="s1", name="default")
    agent.session_manager = _DummySM()
    registry = ToolRegistry(base_path=tmp_path)
    registry.register(WriteTool())
    agent.tools = registry
    agent._malformed_retry_count = 0
    agent._blind_write_paths = set()
    return agent


REPORT = "# Battery report\n\n" + "| 18:10 | 53% | 3.72 V |\n" * 40


def _model_writes(agent, call_id, path, content):
    """The assistant message carrying the call, as the turn loop stores it."""
    agent.session.add_message("assistant", "", tool_calls=[{
        "id": call_id, "type": "function",
        "function": {"name": "write", "arguments": json.dumps({"path": path, "content": content})},
    }])
    return ToolCall(id=call_id, name="write", arguments={"path": path, "content": content})


def _stored_content(agent, call_id):
    for msg in agent.session.messages:
        for tc in msg.get("tool_calls") or []:
            if tc.get("id") == call_id:
                return json.loads(tc["function"]["arguments"])["content"]
    raise AssertionError(f"no stored call {call_id}")


@pytest.mark.asyncio
async def test_the_write_just_made_stays_readable_until_the_next_one(tmp_path):
    agent = _agent(tmp_path)
    first = _model_writes(agent, "w1", "battery-report.md", REPORT)
    results = await agent._handle_tool_calls([first])
    assert results[0].get("success") is True
    # What the model's next call sees: its real text, not the marker.
    assert _stored_content(agent, "w1") == REPORT

    second = _model_writes(agent, "w2", "battery-log.md", REPORT)
    await agent._handle_tool_calls([second])
    assert _stored_content(agent, "w1").startswith("[written to disk: battery-report.md")
    assert _stored_content(agent, "w2") == REPORT


@pytest.mark.asyncio
async def test_the_turn_end_flush_compacts_the_last_write(tmp_path):
    agent = _agent(tmp_path)
    await agent._handle_tool_calls([_model_writes(agent, "w1", "report.md", REPORT)])
    agent._flush_write_compactions()
    marker = _stored_content(agent, "w1")
    assert marker.startswith("[written to disk: report.md")
    assert "full text was saved" in marker
    # Re-issued as a file body, the new marker is still refused.
    assert write_guard.is_placeholder_content(marker)
    agent._flush_write_compactions()  # nothing left: a no-op
    assert _stored_content(agent, "w1") == marker


@pytest.mark.asyncio
async def test_a_deferred_write_is_recorded_once(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    recorded = []
    monkeypatch.setattr(agent, "_record_workspace_write",
                        lambda path, lines, kb: recorded.append(path))
    await agent._handle_tool_calls([_model_writes(agent, "w1", "a.md", REPORT)])
    assert recorded == ["a.md"]  # at write time, not at compaction
    await agent._handle_tool_calls([_model_writes(agent, "w2", "b.md", REPORT)])
    agent._flush_write_compactions()
    assert recorded == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_a_blocked_rewrite_points_at_read_not_at_stopping(tmp_path):
    agent = _agent(tmp_path)
    await agent._handle_tool_calls([_model_writes(agent, "w1", "r.md", REPORT)])
    results = await agent._handle_tool_calls(
        [_model_writes(agent, "w2", "r.md", REPORT + "\nregenerated")])
    msg = results[0]["content"]
    assert msg.startswith("BLIND REWRITE BLOCKED")
    assert "not the file's content" in msg
    assert "read it first" in msg
    assert "Do NOT call any more tools" not in msg


def test_tool_logs_leave_out_the_session_and_cut_long_values():
    logged = _loggable_args({
        "path": "r.md",
        "content": "x" * 5000,
        "_session": object(),
        "_runtime_flags": {"a": 1},
    })
    assert set(logged) == {"path", "content"}
    assert logged["path"] == "r.md"
    assert len(logged["content"]) < 400
    assert logged["content"].endswith("(5000 chars)")
