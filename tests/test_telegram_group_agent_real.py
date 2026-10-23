"""A real Agent answering in a Telegram group (web/telegram_group_chat.py):
the model is offered the web tools only, told it's in a group, and sees none of
the owner's insights — whatever the owner's agent knows."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from captain_claw import speaker
from captain_claw.agent import Agent
from captain_claw.config import get_config
from captain_claw.llm import LLMProvider, LLMResponse
from captain_claw.web import telegram_group_chat as gc

GROUP = -100555
SECRET = "the owner's locker code is 4321"


class _Provider(LLMProvider):
    def __init__(self):
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append({"messages": messages, "tools": tools})
        return LLMResponse(content="Try a risotto.")

    async def complete_streaming(self, messages, tools=None, temperature=None, max_tokens=None):
        if False:
            yield ""

    def count_tokens(self, text):
        return len(text.split()) or 1


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    import captain_claw.conversation_topics as _ct
    from captain_claw import session as _session

    home = tmp_path / "home"
    (home / ".captain-claw").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FD_DATA_DIR", str(tmp_path / "fd-data"))
    for var in ("CLAW_VFS_ROOT", "CLAW_VFS_USER", "FD_OWNER_ID", "FD_URL"):
        monkeypatch.delenv(var, raising=False)
    cfg = get_config()
    for section, attr in (
        ("memory", "path"), ("session", "path"), ("insights", "db_path"),
        ("conversation_topics", "db_path"), ("nervous_system", "db_path"),
        ("sister_session", "db_path"), ("cognitive_metrics", "db_path"),
        ("datastore", "path"), ("autonomous_work", "db_path"),
    ):
        monkeypatch.setattr(getattr(cfg, section), attr, str(home / ".captain-claw" / f"{section}.db"))
    monkeypatch.setattr(_session, "_manager", _session.SessionManager(home / ".captain-claw" / "s.db"))
    monkeypatch.setattr(_ct, "_MANAGER", None)
    monkeypatch.setattr(speaker, "_IN_FLIGHT", 0)
    monkeypatch.setattr(speaker, "_MAIN_LOOP", None)
    return home


async def _keep_cache(*_a, **_k):
    return None


def _real_agent(monkeypatch, tmp_path, provider):
    import captain_claw.agent_context_mixin as acm
    from captain_claw.instructions import InstructionLoader

    monkeypatch.setattr("captain_claw.tools.registry._registry", None)
    agent = Agent(provider=provider)
    agent.instructions = InstructionLoader(
        base_dir=Path(acm.__file__).resolve().parent / "instructions",
        personal_dir=tmp_path / "personal",
    )
    agent._build_playbook_context_note_sync = lambda q: ""
    agent._initialized = True
    agent.memory = None
    return agent


async def test_a_group_turn_offers_the_web_only_and_hides_the_owner(monkeypatch, tmp_path):
    from captain_claw.session import get_session_manager

    provider = _Provider()
    agent = _real_agent(monkeypatch, tmp_path, provider)
    agent._register_default_tools()
    owner_tools = set(agent.tools.list_tools())
    assert {"web_search", "web_fetch", "shell", "insights"} <= owner_tools

    async def build(session, send, **kw):
        agent.session = session
        agent.session_manager = get_session_manager()
        return agent

    async def get_state(key):
        return None

    async def set_state(key, value):
        pass

    server = types.SimpleNamespace(
        agent=types.SimpleNamespace(
            session_manager=types.SimpleNamespace(get_app_state=get_state, set_app_state=set_state),
            _fleet_identity={"name": "Chef"}, tools=None),
        _build_scoped_agent=build,
        _register_speaker_keys=lambda a: a.tools.register_speaker_session(a.session.id),
    )
    gc.chats_of(server).groups[str(GROUP)] = {"title": "Family", "session_id": ""}
    group_agent = await gc._group_agent(server, str(GROUP))
    assert group_agent is agent
    # Whatever the owner's insights say, a group never hears it.
    monkeypatch.setattr(get_config().insights, "enabled", True)
    monkeypatch.setattr(get_config().insights, "inject_in_context", True)
    agent._insights_context_cache = [{"content": SECRET, "importance": 10, "category": "fact"}]
    agent._suppress_memory_context = False  # control: the owner's agent would say it
    assert "4321" in agent._build_insights_context_note()
    agent._suppress_memory_context = True
    monkeypatch.setattr(agent, "_refresh_insights_context_cache", _keep_cache)

    tok = speaker.bind(agent._speaker_principal)
    speaker.turn_started()
    try:
        answer = await agent.complete('[Telegram group "Family": Ana is asking you.]\nAna: dinner ideas?')
    finally:
        speaker.turn_ended()
        speaker.reset(tok)
    assert answer == "Try a risotto."
    offered = set()
    for call in provider.calls:
        for t in call["tools"] or []:
            offered.add(t.get("name") or t.get("function", {}).get("name"))
    assert offered <= {"web_search", "web_fetch"} and offered
    text = "\n".join(str(m.get("content") if isinstance(m, dict) else m) for c in provider.calls
                     for m in c["messages"])
    assert speaker.GROUP_MODE_NOTE in text
    assert "4321" not in text
