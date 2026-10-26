"""A refused WhatsApp send is logged with Meta's error code.

Every Cloud API send used to drop Meta's answer, so when Meta de-registered
the bot's number (error 133010) the replies vanished with nothing in the
log. Now each refusal is a WARNING with the code, a de-registered number is
an ERROR that names the fix, and a ``failed`` status webhook (Meta accepted
the send, then couldn't deliver it) is logged the same way.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import whatsapp_bridge as wb

PID = "42"
WAID = "111"
NOT_REGISTERED = {"error": {
    "message": "(#133010) Account not registered", "type": "OAuthException",
    "code": 133010, "fbtrace_id": "x"}}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def graph(monkeypatch):
    """Route the bridge's httpx client to a fake Graph API."""
    calls: list[dict] = []
    reply = {"status": 200, "json": {"messages": [{"id": "wamid.1"}]}, "text": None}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": request.content.decode(errors="replace")})
        if reply["text"] is not None:
            return httpx.Response(reply["status"], text=reply["text"])
        return httpx.Response(reply["status"], json=reply["json"])

    real = httpx.AsyncClient
    monkeypatch.setattr(wb.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("WHATSAPP_ALLOWED_WAIDS", WAID)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PID)
    monkeypatch.setattr(wb, "is_push_muted", lambda w: False)
    return {"calls": calls, "reply": reply}


def _records(caplog, level):
    return [r.getMessage() for r in caplog.records
            if r.name == wb.log.name and r.levelno == level]


def test_a_de_registered_number_is_an_error_naming_the_fix(graph, caplog):
    graph["reply"].update(status=400, json=NOT_REGISTERED)
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        _run(wb._send_whatsapp_text(WAID, "hello"))
    [line] = _records(caplog, logging.ERROR)
    assert "NOT REGISTERED" in line and "133010" in line
    assert f"POST /{PID}/register" in line and WAID in line


def test_other_refusals_are_warnings_with_the_code(graph, caplog):
    graph["reply"].update(status=400, json={"error": {
        "message": "(#131047) Re-engagement message", "code": 131047,
        "error_data": {"details": "Message failed to send because more than 24 hours have passed"}}})
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        _run(wb._send_whatsapp_text(WAID, "hello"))
    assert _records(caplog, logging.ERROR) == []
    [line] = _records(caplog, logging.WARNING)
    assert "HTTP 400" in line and "Meta error 131047" in line and "24 hours" in line


def test_the_code_is_read_from_the_message_when_missing(graph, caplog):
    graph["reply"].update(status=400, json={"error": {
        "message": "(#131030) Recipient phone number not in allowed list"}})
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        _run(wb._send_whatsapp_text(WAID, "hello"))
    [line] = _records(caplog, logging.WARNING)
    assert "Meta error 131030" in line and "not in allowed list" in line


def test_a_body_that_is_not_metas_error_still_logs(graph, caplog):
    graph["reply"].update(status=502, text="<html>Bad gateway</html>")
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        _run(wb._send_whatsapp_text(WAID, "hello"))
    [line] = _records(caplog, logging.WARNING)
    assert "HTTP 502" in line and "Meta error ?" in line and "Bad gateway" in line


def test_an_accepted_send_logs_nothing(graph, caplog):
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        _run(wb._send_whatsapp_text(WAID, "hello"))
    assert len(graph["calls"]) == 1
    assert _records(caplog, logging.WARNING) == _records(caplog, logging.ERROR) == []


def test_read_receipt_and_reaction_refusals_are_logged(graph, caplog):
    graph["reply"].update(status=400, json=NOT_REGISTERED)
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        _run(wb._mark_read_and_typing("wamid.in"))
        assert _run(wb._send_whatsapp_reaction(WAID, "wamid.in", "👍")) is False
    lines = _records(caplog, logging.ERROR)
    assert len(lines) == 2
    assert "read receipt" in lines[0] and "reaction" in lines[1]


def test_the_test_button_says_the_number_needs_registering(graph):
    graph["reply"].update(status=400, json=NOT_REGISTERED)
    ok, why = _run(wb.send_text_checked(WAID, "hi"))
    assert not ok and "isn't registered" in why and f"/{PID}/register" in why


# ── failed status webhooks ────────────────────────────────────────────


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    monkeypatch.setenv("WHATSAPP_ALLOWED_WAIDS", WAID)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PID)
    app = FastAPI()
    app.include_router(wb.router)
    client = TestClient(app)

    def post(value: dict):
        body = json.dumps({"object": "whatsapp_business_account", "entry": [
            {"changes": [{"field": "messages", "value": value}]}]}).encode()
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        return client.post("/whatsapp/webhook", content=body, headers={
            "x-hub-signature-256": f"sha256={sig}", "content-type": "application/json"})

    return post


def test_a_failed_delivery_is_logged_with_its_code(webhook, caplog):
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        resp = webhook({"statuses": [
            {"id": "wamid.out", "status": "delivered", "recipient_id": WAID},
            {"id": "wamid.out2", "status": "failed", "recipient_id": WAID, "errors": [{
                "code": 131047, "title": "Re-engagement message",
                "error_data": {"details": "more than 24 hours have passed"}}]},
        ]})
    assert resp.status_code == 200
    [line] = _records(caplog, logging.WARNING)
    assert "wamid.out2" in line and "131047" in line and "24 hours" in line


def test_a_failed_delivery_for_a_de_registered_number_is_an_error(webhook, caplog):
    with caplog.at_level(logging.WARNING, logger=wb.log.name):
        webhook({"statuses": [{"id": "wamid.out", "status": "failed", "recipient_id": WAID,
                               "errors": [{"code": 133010, "title": "Account not registered"}]}]})
    [line] = _records(caplog, logging.ERROR)
    assert "NOT REGISTERED" in line and f"/{PID}/register" in line
