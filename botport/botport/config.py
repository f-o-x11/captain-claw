"""BotPort configuration management."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict

log = logging.getLogger(__name__)

try:
    DEFAULT_HOME = Path("~/.botport").expanduser()
except RuntimeError:
    DEFAULT_HOME = Path("/tmp/.botport")
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "config.yaml"
LOCAL_CONFIG_FILENAME = "config.yaml"
LOCAL_BOTPORT_FILENAME = "botport.yaml"
DEFAULT_PORT = 23180

# Top-level sections of a BotPort config, and ones that only a Captain Claw
# (agent / Flight Deck) config has — a ./config.yaml with the latter is not ours.
_BOTPORT_SECTIONS = frozenset({"server", "routing", "concerns", "auth"})
_FOREIGN_SECTIONS = frozenset({"model", "web", "tools", "botport", "session", "memory", "telegram"})


def looks_like_botport_config(path: Path) -> bool:
    """Is ``path`` a BotPort config (not, say, Captain Claw's own config.yaml
    in the repository root)?"""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return True  # let from_yaml report the parse error
    if not isinstance(data, dict):
        return True
    keys = set(data)
    return bool(keys & _BOTPORT_SECTIONS) and not (keys & _FOREIGN_SECTIONS)


class ServerConfig(BaseModel):
    """Web server settings."""

    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    dashboard_enabled: bool = True
    # Bearer token for the dashboard REST API and dashboard pages. Empty:
    # open when auth is disabled (dev mode), loopback-only when auth is on.
    admin_token: str = ""
    # Set when a reverse proxy on this host terminates TLS: every request then
    # arrives from 127.0.0.1, so loopback no longer proves a local caller.
    behind_proxy: bool = False
    # WebSocket ping interval. Short enough to keep NAT/proxy mappings alive
    # and to notice a half-open socket within ~2 intervals.
    ws_heartbeat_seconds: float = 25.0
    # A new socket must send `register` within this many seconds.
    register_timeout_seconds: float = 15.0
    max_message_mb: int = 16
    # Serve TLS directly (wss://). Leave empty behind a TLS reverse proxy.
    tls_cert: str = ""
    tls_key: str = ""
    # Where the database and swarm files live. Empty: ~/.botport.
    data_dir: str = ""
    # Browser origins allowed to call the REST API cross-origin (e.g. a Flight
    # Deck UI on another host). Empty: no CORS headers (same-origin only).
    cors_origins: list[str] = Field(default_factory=list)
    # With behind_proxy: the addresses of the proxies whose X-Forwarded-For
    # is believed (anyone else's is ignored).
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    # Swarm file uploads (legacy agents): per-swarm cap, and the free space
    # always left on the disk (an SD card that fills up takes the DB with it).
    swarm_workspace_mb: int = 1024
    min_free_disk_mb: int = 512

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)


class RoutingConfig(BaseModel):
    """Routing strategy settings."""

    strategy: str = "auto"  # informational: tags -> LLM (if enabled) -> least loaded
    fallback: str = "reject"  # reject | queue_until_available


class ConcernConfig(BaseModel):
    """Concern lifecycle settings."""

    idle_timeout_seconds: int = 600
    max_follow_ups: int = 20
    max_concurrent_per_instance: int = 10
    timeout_check_interval_seconds: int = 10
    # Default / maximum lifetime a requester may ask for (seconds).
    default_timeout_seconds: int = 600
    max_timeout_seconds: int = 3600
    # A deck that drops mid-concern gets this long to reconnect before the
    # concerns it was working on fail.
    reconnect_grace_seconds: int = 90
    # Results for a requester that is offline are held this long and
    # delivered when it reconnects.
    result_retention_seconds: int = 900
    # Finished concerns stay in memory this long (the DB keeps them for
    # logging.retention_days).
    memory_retention_seconds: int = 300


class AuthKeyEntry(BaseModel):
    """A single authorized instance key.

    For a Flight Deck the ``instance`` name is binding: a deck that connects
    with this key IS that deck, whatever name it sends.
    """

    instance: str = ""
    key: str = ""
    secret: str = ""


class AuthConfig(BaseModel):
    """Authentication settings."""

    enabled: bool = False
    keys: list[AuthKeyEntry] = Field(default_factory=list)
    # Failed registrations allowed per client address per minute before the
    # address is refused for block_seconds.
    max_failures_per_minute: int = 10
    block_seconds: int = 300

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_keys(cls, data: Any) -> Any:
        """YAML parses 'keys:' with only comments as None — coerce to []."""
        if isinstance(data, dict) and data.get("keys") is None:
            data["keys"] = []
        return data


class LoggingConfig(BaseModel):
    """Logging settings."""

    level: str = "info"
    concern_history: bool = True
    retention_days: int = 30
    # Also write logs here (rotated). Empty: console only.
    file: str = ""
    file_max_mb: int = 10
    file_backups: int = 5
    color: str = "auto"  # auto | always | never


class LLMConfig(BaseModel):
    """LLM settings for smart routing via litellm.

    Model names use litellm's ``provider/model`` format, e.g.:
      - ``ollama/llama3.2``
      - ``gemini/gemini-2.0-flash``
      - ``openai/gpt-4o-mini``
      - ``anthropic/claude-3-haiku-20240307``

    For backward compatibility the old ``provider`` + ``model`` fields are
    merged automatically (e.g. provider="gemini", model="gemini-2.0-flash"
    → model="gemini/gemini-2.0-flash").
    """

    enabled: bool = False
    provider: str = ""  # legacy — merged into model if set
    model: str = "ollama/llama3.2"
    api_key: str = ""
    base_url: str = ""  # empty = let litellm use its provider defaults
    temperature: float = 0.3
    max_tokens: int = 1000
    timeout: int = 30

    @model_validator(mode="after")
    def _merge_provider_into_model(self) -> "LLMConfig":
        """Merge legacy ``provider`` field into ``model`` for litellm compat."""
        if self.provider and "/" not in self.model:
            self.model = f"{self.provider}/{self.model}"
            self.provider = ""
        return self


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class BotPortConfig(BaseSettings):
    """Main BotPort configuration.

    Precedence: command-line flags > ``BOTPORT_*`` environment variables
    (nested with ``__``, e.g. ``BOTPORT_AUTH__ENABLED=true``) > YAML file >
    defaults.
    """

    server: ServerConfig = Field(default_factory=ServerConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    concerns: ConcernConfig = Field(default_factory=ConcernConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    model_config = SettingsConfigDict(
        env_prefix="BOTPORT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Where this config came from (not a setting; set by load/from_yaml).
    _source: str = ""

    @property
    def source(self) -> str:
        return self._source

    @property
    def data_dir(self) -> Path:
        raw = self.server.data_dir or os.environ.get("BOTPORT_DATA_DIR", "")
        return Path(raw).expanduser() if raw else DEFAULT_HOME

    @classmethod
    def from_yaml(cls, path: Path | str) -> BotPortConfig:
        """Load configuration from a YAML file (environment variables still win)."""
        p = Path(path).expanduser()
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{p}: top level must be a mapping")
        env = EnvSettingsSource(
            cls, env_prefix="BOTPORT_", env_nested_delimiter="__",
        )()
        cfg = cls.model_validate(_deep_merge(data, env))
        cfg._source = str(p.resolve())
        return cfg

    @classmethod
    def load(cls) -> BotPortConfig:
        """Load configuration with fallback chain: local -> home -> defaults.

        A file that exists but does not parse is an error, not a silent
        fallback to defaults (which would start an open hub on the wrong port).
        """
        explicit = os.environ.get("BOTPORT_CONFIG", "")
        if explicit:
            return cls.from_yaml(explicit)

        named = Path(LOCAL_BOTPORT_FILENAME)
        if named.is_file():
            cfg = cls.from_yaml(named)
            log.info("Config loaded from %s", named.resolve())
            return cfg

        local = Path(LOCAL_CONFIG_FILENAME)
        if local.is_file():
            if looks_like_botport_config(local):
                cfg = cls.from_yaml(local)
                log.info("Config loaded from %s", local.resolve())
                return cfg
            log.warning(
                "Ignoring %s: it is not a BotPort config (Captain Claw's?). Use -c, "
                "BOTPORT_CONFIG, ./botport.yaml or %s", local.resolve(), DEFAULT_CONFIG_PATH,
            )

        if DEFAULT_CONFIG_PATH.is_file():
            cfg = cls.from_yaml(DEFAULT_CONFIG_PATH)
            log.info("Config loaded from %s", DEFAULT_CONFIG_PATH)
            return cfg

        log.info("No config file found; using defaults (+ BOTPORT_* environment)")
        cfg = cls()
        cfg._source = ""
        return cfg

    def save(self, path: Path | str | None = None) -> None:
        """Save configuration to YAML."""
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", exclude_none=True)
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    def problems(self) -> list[str]:
        """Configuration mistakes worth refusing to start over."""
        out: list[str] = []
        if not (0 < self.server.port < 65536):
            out.append(f"server.port {self.server.port} is not a valid TCP port")
        if bool(self.server.tls_cert) != bool(self.server.tls_key):
            out.append("server.tls_cert and server.tls_key must be set together")
        for f in (self.server.tls_cert, self.server.tls_key):
            if f and not Path(f).expanduser().is_file():
                out.append(f"TLS file not found: {f}")
        if self.auth.enabled:
            if not self.auth.keys:
                out.append("auth.enabled is true but auth.keys is empty — nobody could connect")
            seen_keys: set[str] = set()
            seen_names: set[str] = set()
            for i, k in enumerate(self.auth.keys):
                if not k.key.strip() or not k.secret.strip():
                    out.append(f"auth.keys[{i}] needs both key and secret")
                if k.key.strip() in seen_keys:
                    out.append(f"auth.keys[{i}]: duplicate key {k.key!r}")
                seen_keys.add(k.key.strip())
                name = k.instance.strip().lower()
                if name and name in seen_names:
                    out.append(f"auth.keys[{i}]: instance name {k.instance!r} is used twice")
                if name:
                    seen_names.add(name)
        return out

    def warnings(self) -> list[str]:
        """Risky but legal settings, printed at startup."""
        out: list[str] = []
        exposed = self.server.host not in ("127.0.0.1", "localhost", "::1")
        if not self.auth.enabled and exposed:
            out.append(
                "auth is DISABLED and the hub listens on "
                f"{self.server.host}: anyone who can reach port {self.server.port} can "
                "register and drive the connected agents. Enable auth before exposing it."
            )
        if self.auth.enabled:
            for k in self.auth.keys:
                if k.secret and len(k.secret.strip()) < 16:
                    out.append(f"auth key {k.key!r} has a short secret (<16 chars)")
                if not k.instance.strip():
                    out.append(f"auth key {k.key!r} has no `instance`: a Flight Deck cannot use it "
                               "(only a legacy agent, under the name it sends)")
        if self.server.behind_proxy and self.server.host not in ("127.0.0.1", "localhost", "::1"):
            out.append("behind_proxy is on but the hub listens on "
                       f"{self.server.host}: bind it to 127.0.0.1 so only the proxy reaches it")
        if self.server.dashboard_enabled and not self.server.admin_token and self.auth.enabled:
            out.append(
                "server.admin_token is empty: the dashboard and REST API answer only "
                "loopback requests" + (" (and behind_proxy disables even that)"
                                       if self.server.behind_proxy else "")
            )
        if self.server.dashboard_enabled and not self.server.admin_token and not self.auth.enabled and exposed:
            out.append("the dashboard and REST API are open to anyone who can reach the hub")
        if self.llm.enabled:
            import importlib.util

            if importlib.util.find_spec("litellm") is None:
                out.append("llm.enabled is true but litellm is not installed "
                           "(pip install 'botport[llm]'); LLM routing is skipped")
        if self._source and self.auth.enabled:
            try:
                mode = Path(self._source).stat().st_mode
                if mode & 0o077:
                    out.append(
                        f"{self._source} holds secrets but is readable by other users "
                        f"(chmod 600 {self._source})"
                    )
            except OSError:
                pass
        return out


# ── Global singleton ─────────────────────────────────────────────

_config: BotPortConfig | None = None


def get_config() -> BotPortConfig:
    global _config
    if _config is None:
        _config = BotPortConfig.load()
    return _config


def set_config(config: BotPortConfig) -> None:
    global _config
    _config = config
