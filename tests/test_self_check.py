"""self_check: live connection tests and server status, read-only."""

from __future__ import annotations

import asyncio


from captain_claw import self_check as S
from captain_claw.tools.registry import Tool, ToolRegistry, ToolResult
from captain_claw.tools.self_check import SelfCheckTool


class _Echo(Tool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, content="")


class _Provider:
    provider, model = "fake", "fake-1"

    def __init__(self, reply: str = "OK", error: Exception | None = None) -> None:
        self.reply, self.error = reply, error

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        if self.error:
            raise self.error
        return type("R", (), {"content": self.reply})()


class _Agent:
    session = None

    def __init__(self, provider=None, tools=None) -> None:
        self.provider = provider
        self.tools = tools


def test_report_leads_with_what_needs_attention():
    checks = [
        S.Check("server", "Disk", "ok", "plenty"),
        S.Check("connections", "Google Gmail", "fail", "HTTP 401", fix="reconnect Google"),
        S.Check("connections", "Jev", "off", "no key", fix="add a key"),
        S.Check("server", "CPU load", "warn", "busy"),
    ]
    text = S.format_report(checks, took=1.2)
    assert "1 ok · 1 warning(s) · 1 failing · 1 not set up" in text
    attention = text.split("Needs attention:")[1].split("\n\n")[0]
    assert attention.index("Google Gmail") < attention.index("CPU load")
    assert "→ reconnect Google" in attention
    assert "⏸ Jev: no key → add a key" in text


async def test_probe_turns_errors_and_timeouts_into_failures():
    async def boom():
        raise RuntimeError("down")

    async def slow():
        await asyncio.sleep(5)

    [failed] = await S._Probe("connections", "X", boom).run()
    assert failed.status == "fail" and "down" in failed.detail
    [late] = await S._Probe("connections", "Y", slow, timeout=0.05).run()
    assert late.status == "fail" and "no answer" in late.detail


async def test_models_are_pinged(monkeypatch):
    from captain_claw.config import get_config

    monkeypatch.setattr(get_config().model, "allowed", [])
    ok = await S.run_self_check(_Agent(provider=_Provider()), sections=["models"])
    assert [c.status for c in ok] == ["ok"] and "answered in" in ok[0].detail
    broken = await S.run_self_check(_Agent(provider=_Provider(error=RuntimeError("410 retired"))),
                                    sections=["models"])
    assert broken[0].status == "fail" and "410 retired" in broken[0].detail


async def test_server_and_agent_sections_need_no_network(tmp_path):
    checks = await S.run_self_check(_Agent(), sections=["server", "agent"],
                                    workspace=tmp_path, saved_root=tmp_path / "saved")
    names = {c.name for c in checks}
    assert {"Host", "Software", "Disk", "This process", "Clock", "Identity", "Workspace"} <= names
    assert all(c.section in ("server", "agent") for c in checks)
    workspace = next(c for c in checks if c.name == "Workspace")
    assert workspace.status == "ok"
    assert not list((tmp_path / "saved" / "tmp").glob(".self-check-*"))


async def test_google_not_connected_is_not_a_failure(monkeypatch):
    from captain_claw import google_oauth_manager

    async def no(self):
        return False

    monkeypatch.setattr(google_oauth_manager.GoogleOAuthManager, "is_connected", no)
    [check] = await S._google()
    assert check.status == "off" and "connect Google" in check.fix


def test_hidden_tools_say_why(monkeypatch):
    from captain_claw import google_oauth_manager

    monkeypatch.setattr(google_oauth_manager, "is_google_connected_cached", lambda *a, **k: False)
    reg = ToolRegistry()
    reg.register(_Echo("read"))
    reg.register(_Echo("google_mail"), metadata={"requires_google": True})
    checks = S.tool_checks(_Agent(tools=reg), "")
    hidden = [c for c in checks if c.name == "Hidden"]
    assert hidden and "google_mail" in hidden[0].detail and "Google not connected" in hidden[0].detail
    available = next(c for c in checks if c.name == "Available")
    assert available.detail.startswith("1 tools")


async def test_tool_saves_the_report(tmp_path, monkeypatch):
    async def fake_run(agent, **kw):
        return [S.Check("server", "Host", "info", "box"),
                S.Check("connections", "Google", "fail", "HTTP 401", fix="reconnect")]

    monkeypatch.setattr(S, "run_self_check", fake_run)
    result = await SelfCheckTool().execute(save=True, _runtime_base_path=tmp_path,
                                           _saved_base_path=tmp_path / "saved", _session_id="s1")
    assert result.success and "❌ Google" in result.content
    saved = list((tmp_path / "saved" / "output" / "s1").glob("self-check-*.md"))
    assert saved and "Host" in saved[0].read_text()
    assert result.system_hint  # a failure tells the model how to report it


def test_both_tools_are_always_on():
    from captain_claw.config import ToolsConfig

    enabled = ToolsConfig(enabled=["read"]).enabled
    assert "docx" in enabled and "self_check" in enabled
