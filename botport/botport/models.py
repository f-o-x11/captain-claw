"""Data models for BotPort."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tag_list(raw: object) -> list[str]:
    """Expertise tags as a clean list (a lone string is one tag, not letters)."""
    if isinstance(raw, str):
        raw = [t for t in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(t).strip()[:64] for t in raw if str(t).strip()][:50]


@dataclass
class PersonaInfo:
    """Advertised persona capabilities."""

    name: str
    description: str = ""
    background: str = ""
    expertise_tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "background": self.background,
            "expertise_tags": list(self.expertise_tags),
        }

    @classmethod
    def from_dict(cls, data: dict) -> PersonaInfo:
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            background=str(data.get("background", "")),
            expertise_tags=_tag_list(data.get("expertise_tags")),
        )


@dataclass
class DeckAgent:
    """An agent a Flight Deck exposes to the network (protocol 2)."""

    name: str
    slug: str = ""
    description: str = ""
    expertise_tags: list[str] = field(default_factory=list)
    status: str = "running"

    @property
    def available(self) -> bool:
        return self.status == "running"

    def matches(self, ref: str) -> bool:
        r = (ref or "").strip().lower()
        return bool(r) and r in (self.name.strip().lower(), self.slug.strip().lower())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "expertise_tags": list(self.expertise_tags),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DeckAgent | None:
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "").strip()[:128]
        if not name:
            return None
        return cls(
            name=name,
            slug=str(data.get("slug") or "").strip()[:128],
            description=str(data.get("description") or "")[:2000],
            expertise_tags=_tag_list(data.get("expertise_tags")),
            status=str(data.get("status") or "running")[:32],
        )


@dataclass
class InstanceInfo:
    """A connection: a Captain Claw agent (legacy) or a Flight Deck."""

    id: str
    name: str
    personas: list[PersonaInfo] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    active_persona: str = ""   # User-selected active persona (empty = auto)
    active_model: str = ""     # User-selected active model (empty = default)
    max_concurrent: int = 5
    active_concerns: int = 0
    status: str = "connected"  # connected | disconnected
    connected_at: str = field(default_factory=_utcnow_iso)
    last_heartbeat: str = field(default_factory=_utcnow_iso)
    disconnected_at: str = ""
    # Live activity: step_type -> {data dict, updated_at}.  Ephemeral, not persisted.
    activity: dict[str, dict] = field(default_factory=dict)
    kind: str = "agent"        # agent | deck
    protocol: int = 1
    deck_id: str = ""
    version: str = ""
    remote_addr: str = ""
    agents: list[DeckAgent] = field(default_factory=list)  # decks only
    auth_key: str = ""         # the key it registered with (never sent anywhere)

    @property
    def is_deck(self) -> bool:
        return self.kind == "deck"

    @property
    def has_capacity(self) -> bool:
        return self.active_concerns < self.max_concurrent and self.status == "connected"

    def find_agent(self, ref: str) -> DeckAgent | None:
        for a in self.agents:
            if a.matches(ref):
                return a
        return None

    def all_expertise_tags(self) -> set[str]:
        tags: set[str] = set()
        for p in self.personas:
            tags.update(t.lower() for t in p.expertise_tags)
        for a in self.agents:
            if a.available:
                tags.update(t.lower() for t in a.expertise_tags)
        return tags

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "personas": [p.to_dict() for p in self.personas],
            "tools": list(self.tools),
            "models": list(self.models),
            "active_persona": self.active_persona,
            "active_model": self.active_model,
            "max_concurrent": self.max_concurrent,
            "active_concerns": self.active_concerns,
            "status": self.status,
            "connected_at": self.connected_at,
            "last_heartbeat": self.last_heartbeat,
            "disconnected_at": self.disconnected_at,
            "activity": dict(self.activity),
            "kind": self.kind,
            "protocol": self.protocol,
            "version": self.version,
            "agents": [a.to_dict() for a in self.agents],
        }

    @classmethod
    def from_dict(cls, data: dict) -> InstanceInfo:
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            personas=[PersonaInfo.from_dict(p) for p in (data.get("personas") or [])],
            tools=list(data.get("tools") or []),
            models=list(data.get("models") or []),
            active_persona=str(data.get("active_persona", "")),
            active_model=str(data.get("active_model", "")),
            max_concurrent=int(data.get("max_concurrent", 5)),
            active_concerns=int(data.get("active_concerns", 0)),
            status=str(data.get("status", "connected")),
            connected_at=str(data.get("connected_at", "")),
            last_heartbeat=str(data.get("last_heartbeat", "")),
        )


@dataclass
class ConcernExchange:
    """A single message in a concern's exchange history."""

    direction: str  # request | response | follow_up | context_request | context_reply
    content: str
    timestamp: str = field(default_factory=_utcnow_iso)
    from_instance: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "content": self.content,
            "timestamp": self.timestamp,
            "from_instance": self.from_instance,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConcernExchange:
        return cls(
            direction=str(data.get("direction", "")),
            content=str(data.get("content", "")),
            timestamp=str(data.get("timestamp", "")),
            from_instance=str(data.get("from_instance", "")),
            metadata=dict(data.get("metadata") or {}),
        )


# Valid concern states.
CONCERN_STATES = frozenset({
    "pending",
    "assigned",
    "in_progress",
    "responded",
    "closed",
    "failed",
    "timeout",
})

# Terminal states (no further transitions).
TERMINAL_STATES = frozenset({"closed", "failed", "timeout"})


@dataclass
class Concern:
    """A routed task between two CC instances."""

    id: str
    from_instance: str
    from_instance_name: str = ""
    from_session: str = ""
    assigned_instance: str | None = None
    assigned_instance_name: str = ""
    assigned_session: str | None = None
    task: str = ""
    context: dict = field(default_factory=dict)
    expertise_tags: list[str] = field(default_factory=list)
    status: str = "pending"
    messages: list[ConcernExchange] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    timeout_at: str = ""
    metadata: dict = field(default_factory=dict)
    # Protocol 2 addressing (persisted inside metadata, see to_dict/store).
    target_deck: str = ""
    target_agent: str = ""
    from_agent: str = ""
    mode: str = "consult"
    # Runtime-only bookkeeping (not persisted).
    deadline_at: float = 0.0          # monotonic hard deadline, 0 = none
    counted_active: bool = False      # assignee's active_concerns was incremented
    assignee_offline_since: float = 0.0
    finished_at: float = 0.0          # monotonic time it became terminal

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.status not in TERMINAL_STATES

    def touch(self) -> None:
        self.updated_at = _utcnow_iso()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_instance": self.from_instance,
            "from_instance_name": self.from_instance_name,
            "from_session": self.from_session,
            "assigned_instance": self.assigned_instance,
            "assigned_instance_name": self.assigned_instance_name,
            "assigned_session": self.assigned_session,
            "task": self.task,
            "context": dict(self.context),
            "expertise_tags": list(self.expertise_tags),
            "status": self.status,
            "messages": [m.to_dict() for m in self.messages],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "timeout_at": self.timeout_at,
            "metadata": dict(self.metadata),
            "target_deck": self.target_deck,
            "target_agent": self.target_agent,
            "from_agent": self.from_agent,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Concern:
        return cls(
            id=str(data.get("id", "")),
            from_instance=str(data.get("from_instance", "")),
            from_instance_name=str(data.get("from_instance_name", "")),
            from_session=str(data.get("from_session", "")),
            assigned_instance=data.get("assigned_instance"),
            assigned_instance_name=str(data.get("assigned_instance_name", "")),
            assigned_session=data.get("assigned_session"),
            task=str(data.get("task", "")),
            context=dict(data.get("context") or {}),
            expertise_tags=list(data.get("expertise_tags") or []),
            status=str(data.get("status", "pending")),
            messages=[ConcernExchange.from_dict(m) for m in (data.get("messages") or [])],
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            timeout_at=str(data.get("timeout_at", "")),
            metadata=dict(data.get("metadata") or {}),
            target_deck=str(data.get("target_deck", "")),
            target_agent=str(data.get("target_agent", "")),
            from_agent=str(data.get("from_agent", "")),
            mode=str(data.get("mode", "consult") or "consult"),
        )
