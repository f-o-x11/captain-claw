"""BotPort protocol message types and serialization.

Protocol 1 is the original agent <-> hub protocol (a connection is one
Captain Claw agent). Protocol 2 adds Flight Deck connections (``kind="deck"``):
a deck advertises the agents it exposes, receives the ``network`` roster of
other decks, and proxies dispatched concerns to its own agents. All protocol 2
fields are optional with protocol 1 defaults, and unknown fields are ignored,
so old agents keep working unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


# ── Base ────────────────────────────────────────────────────────


class BaseMessage(BaseModel):
    """Base for all protocol messages."""

    type: str


# ── Connection messages ─────────────────────────────────────────


class RegisterMessage(BaseMessage):
    """CC -> BotPort: register this instance."""

    type: str = "register"
    instance_name: str = ""
    key: str = ""
    secret: str = ""
    capabilities: dict[str, Any] = Field(default_factory=dict)
    # agent:  { personas: [...], tools: [...], models: [...], max_concurrent: int }
    # deck:   { agents: [AgentAd], max_concurrent: int, version: str, features: [...] }
    kind: str = "agent"            # agent | deck
    protocol: int = 1
    deck_id: str = ""              # stable per-deck id (persisted by the deck)
    in_flight: list[str] = Field(default_factory=list)   # concerns this deck is still working on
    awaiting: list[str] = Field(default_factory=list)    # concerns this deck still waits for


class RegisteredMessage(BaseMessage):
    """BotPort -> CC: registration confirmed."""

    type: str = "registered"
    instance_id: str = ""
    botport_version: str = ""
    ok: bool = True
    error: str = ""
    protocol: int = 1
    deck_name: str = ""            # the (key-bound) name the hub knows this deck as
    resumed: bool = False          # an earlier connection of this deck was taken over
    heartbeat_seconds: float = 0.0
    retry: bool = True             # False: do not retry with these settings (bad credentials)


class HeartbeatMessage(BaseMessage):
    """CC -> BotPort: periodic heartbeat."""

    type: str = "heartbeat"
    instance_id: str = ""
    active_concerns: int = 0
    load: float = 0.0  # 0.0-1.0


class HeartbeatAckMessage(BaseMessage):
    """BotPort -> CC: heartbeat acknowledged."""

    type: str = "heartbeat_ack"
    connected_instances: int = 0


# ── Concern messages ────────────────────────────────────────────


class ConcernSubmitMessage(BaseMessage):
    """CC-A -> BotPort: submit a concern for routing."""

    type: str = "concern"
    concern_id: str = ""
    task: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    expertise_tags: list[str] = Field(default_factory=list)
    from_session: str = ""
    # Protocol 2 (decks): explicit addressing and requester identity.
    target_deck: str = ""
    target_agent: str = ""
    from_agent: str = ""
    mode: str = "consult"          # consult | delegate (informational)
    timeout_seconds: float = 0.0   # 0 = hub default


class ConcernAckMessage(BaseMessage):
    """BotPort -> CC-A: concern received and assigned."""

    type: str = "concern_ack"
    concern_id: str = ""
    assigned_to_name: str = ""
    ok: bool = True
    error: str = ""
    target_deck: str = ""
    target_agent: str = ""


class DispatchMessage(BaseMessage):
    """BotPort -> CC-B: dispatch a concern for processing."""

    type: str = "dispatch"
    concern_id: str = ""
    from_instance_name: str = ""
    task: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    persona_hint: str = ""
    # Protocol 2: set by the hub, so the receiving deck can trust them.
    from_deck: str = ""
    from_agent: str = ""
    target_agent: str = ""
    mode: str = "consult"
    timeout_seconds: float = 0.0


class ResultMessage(BaseMessage):
    """CC-B -> BotPort: result of a dispatched concern."""

    type: str = "result"
    concern_id: str = ""
    response: str = ""
    persona_name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error: str = ""


class ConcernResultMessage(BaseMessage):
    """BotPort -> CC-A: relay result back to originator."""

    type: str = "concern_result"
    concern_id: str = ""
    response: str = ""
    from_instance_name: str = ""
    persona_name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error: str = ""
    target_agent: str = ""


# ── Follow-up messages ──────────────────────────────────────────


class FollowUpMessage(BaseMessage):
    """Follow-up on an existing concern (bidirectional via BotPort)."""

    type: str = "follow_up"
    concern_id: str = ""
    message: str = ""
    additional_context: dict[str, Any] = Field(default_factory=dict)


# ── Context negotiation ─────────────────────────────────────────


class ContextRequestMessage(BaseMessage):
    """CC-B -> BotPort -> CC-A: request more context."""

    type: str = "context_request"
    concern_id: str = ""
    questions: list[str] = Field(default_factory=list)


class ContextReplyMessage(BaseMessage):
    """CC-A -> BotPort -> CC-B: reply with additional context."""

    type: str = "context_reply"
    concern_id: str = ""
    answers: dict[str, Any] = Field(default_factory=dict)


# ── Activity messages ────────────────────────────────────────────


class ActivityMessage(BaseMessage):
    """CC -> BotPort: live agent activity update (enriched heartbeat).

    Each step_type replaces the previous value of the same type,
    acting as a compact status line per instance.
    """

    type: str = "activity"
    instance_id: str = ""
    step_type: str = ""  # status | thinking | tool_stream | tool_output
    data: dict[str, Any] = Field(default_factory=dict)
    # data keys vary by step_type:
    #   status:      { text: str }
    #   thinking:    { text: str, tool: str, phase: str }
    #   tool_stream: { chunk: str }
    #   tool_output: { tool_name: str, arguments: dict, output: str }


# ── Lifecycle messages ───────────────────────────────────────────


class CloseConcernMessage(BaseMessage):
    """CC-A -> BotPort: close a concern."""

    type: str = "close_concern"
    concern_id: str = ""


class ConcernClosedMessage(BaseMessage):
    """BotPort -> CC-B: concern was closed by originator."""

    type: str = "concern_closed"
    concern_id: str = ""
    reason: str = ""


class TimeoutNoticeMessage(BaseMessage):
    """BotPort -> CC-A: concern timed out."""

    type: str = "timeout_notice"
    concern_id: str = ""
    reason: str = "idle_timeout"


# ── Deck federation (protocol 2) ─────────────────────────────────


class DeckAgentsMessage(BaseMessage):
    """Deck -> BotPort: full replacement of the agents this deck exposes.

    Each agent: { name, slug, description, expertise_tags: [...], status }.
    """

    type: str = "deck_agents"
    agents: list[dict[str, Any]] = Field(default_factory=list)


class NetworkMessage(BaseMessage):
    """BotPort -> deck: every OTHER deck and the agents it exposes."""

    type: str = "network"
    decks: list[dict[str, Any]] = Field(default_factory=list)
    agents: list[dict[str, Any]] = Field(default_factory=list)  # legacy direct agents


class ProgressMessage(BaseMessage):
    """Assignee -> BotPort -> originator: live progress of a concern.

    Ephemeral (not stored). Also keeps the concern's idle timeout fresh.
    """

    type: str = "progress"
    concern_id: str = ""
    event: str = ""   # accepted | status | thinking | tool | heartbeat
    data: dict[str, Any] = Field(default_factory=dict)


class ResultAckMessage(BaseMessage):
    """Confirms a result frame was received (deck -> hub for concern_result,
    hub -> deck for result), so neither side resends it after a reconnect."""

    type: str = "result_ack"
    concern_id: str = ""


# ── File transfer messages ──────────────────────────────────────


class FileUploadMessage(BaseMessage):
    """CC -> BotPort: upload a file (gzip + base64 encoded).

    Files are stored under workspace-botport/swarm/<swarm_id>/<agent>/.
    """

    type: str = "file_upload"
    swarm_id: str = ""
    concern_id: str = ""       # Link to active concern (for agent identification)
    filename: str = ""
    data: str = ""             # base64-encoded (optionally gzip-compressed) payload
    compressed: bool = False   # True if payload is gzip-compressed before base64
    mime_type: str = ""
    subfolder: str = ""        # Optional subfolder within agent dir


class FileUploadAckMessage(BaseMessage):
    """BotPort -> CC: file upload acknowledged."""

    type: str = "file_upload_ack"
    file_id: str = ""
    filename: str = ""
    path: str = ""             # Relative path within swarm workspace
    size: int = 0
    ok: bool = True
    error: str = ""


class FileListMessage(BaseMessage):
    """CC -> BotPort: request list of available files for a swarm."""

    type: str = "file_list"
    swarm_id: str = ""
    agent_filter: str = ""     # Optional: only files from this agent


class FileListResponseMessage(BaseMessage):
    """BotPort -> CC: list of available files."""

    type: str = "file_list_response"
    swarm_id: str = ""
    files: list[dict[str, Any]] = Field(default_factory=list)
    # Each file: { file_id, filename, path, size, mime_type, agent }


class FileRequestMessage(BaseMessage):
    """CC -> BotPort: request a specific file."""

    type: str = "file_request"
    swarm_id: str = ""
    file_path: str = ""        # Relative path within swarm workspace
    file_id: str = ""          # Alternative: request by file ID


class FileResponseMessage(BaseMessage):
    """BotPort -> CC: file data response (gzip + base64 encoded)."""

    type: str = "file_response"
    swarm_id: str = ""
    filename: str = ""
    path: str = ""
    data: str = ""             # base64-encoded payload
    compressed: bool = False
    mime_type: str = ""
    size: int = 0
    ok: bool = True
    error: str = ""


# ── Parsing ──────────────────────────────────────────────────────

_MESSAGE_MAP: dict[str, type[BaseMessage]] = {
    "register": RegisterMessage,
    "registered": RegisteredMessage,
    "heartbeat": HeartbeatMessage,
    "heartbeat_ack": HeartbeatAckMessage,
    "concern": ConcernSubmitMessage,
    "concern_ack": ConcernAckMessage,
    "dispatch": DispatchMessage,
    "result": ResultMessage,
    "concern_result": ConcernResultMessage,
    "follow_up": FollowUpMessage,
    "context_request": ContextRequestMessage,
    "context_reply": ContextReplyMessage,
    "activity": ActivityMessage,
    "close_concern": CloseConcernMessage,
    "concern_closed": ConcernClosedMessage,
    "timeout_notice": TimeoutNoticeMessage,
    "file_upload": FileUploadMessage,
    "file_upload_ack": FileUploadAckMessage,
    "file_list": FileListMessage,
    "file_list_response": FileListResponseMessage,
    "file_request": FileRequestMessage,
    "file_response": FileResponseMessage,
    "deck_agents": DeckAgentsMessage,
    "network": NetworkMessage,
    "progress": ProgressMessage,
    "result_ack": ResultAckMessage,
}


def parse_message(raw: dict[str, Any]) -> BaseMessage:
    """Parse a raw dict into the appropriate message type.

    Raises ``ValueError`` for unknown or malformed messages.
    """
    msg_type = str(raw.get("type", "")).strip()
    cls = _MESSAGE_MAP.get(msg_type)
    if cls is None:
        raise ValueError(f"Unknown message type: {msg_type!r}")
    return cls.model_validate(raw)


def parse_raw(data: str) -> BaseMessage:
    """Parse a JSON string into the appropriate message type."""
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Message must be a JSON object")
    return parse_message(raw)


def serialize_message(msg: BaseMessage) -> str:
    """Serialize a message to JSON string."""
    return msg.model_dump_json(exclude_none=True)
