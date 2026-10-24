"""BotPort command line.

    botport [run] [-c CONFIG] [--host H] [--port P] [--data-dir D]
    botport check [-c CONFIG]        validate the configuration and exit
    botport gen-key NAME             print a new key/secret entry for a deck
    botport --version
"""

from __future__ import annotations

import argparse
import secrets
import sys
from typing import Any

import yaml

from botport import __version__

_COMMANDS = ("run", "check", "gen-key")


def _add_config_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", "-c", default="", help="Path to config YAML (default: ./config.yaml, then ~/.botport/config.yaml)")
    p.add_argument("--host", default="", help="Bind host (overrides config)")
    p.add_argument("--port", "-p", type=int, default=0, help="Bind port (overrides config)")
    p.add_argument("--data-dir", default="", help="Database / files directory (overrides config)")


def _load(args: argparse.Namespace):
    from botport.config import BotPortConfig

    cfg = BotPortConfig.from_yaml(args.config) if args.config else BotPortConfig.load()
    if getattr(args, "host", ""):
        cfg.server.host = args.host
    if getattr(args, "port", 0):
        cfg.server.port = args.port
    if getattr(args, "data_dir", ""):
        cfg.server.data_dir = args.data_dir
    return cfg


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:3] + "…" if len(value) > 6 else "***"


def _effective(cfg: Any) -> dict[str, Any]:
    data = cfg.model_dump(mode="json")
    data["server"]["admin_token"] = _mask(data["server"].get("admin_token", ""))
    data["llm"]["api_key"] = _mask(data["llm"].get("api_key", ""))
    for k in data["auth"].get("keys", []):
        k["secret"] = _mask(k.get("secret", ""))
    return data


def cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = _load(args)
    except Exception as exc:
        print(f"ERROR: cannot load configuration: {exc}", file=sys.stderr)
        return 1
    print(f"# BotPort {__version__} — effective configuration")
    print(f"# source: {cfg.source or '(defaults + environment)'}   data dir: {cfg.data_dir}")
    print(yaml.safe_dump(_effective(cfg), default_flow_style=False, sort_keys=False))
    problems = cfg.problems()
    for w in cfg.warnings():
        print(f"WARNING: {w}")
    for p in problems:
        print(f"ERROR: {p}")
    if problems:
        return 1
    print("OK")
    return 0


def cmd_gen_key(args: argparse.Namespace) -> int:
    from botport.server import DECK_NAME_RE

    name = args.name.strip()
    if not DECK_NAME_RE.match(name):
        print("ERROR: name must be letters, digits, '-', '_' or '.' (max 63, no spaces or '@')",
              file=sys.stderr)
        return 1
    key = f"{name}-{secrets.token_hex(4)}"
    secret = secrets.token_urlsafe(32)
    print("# Add to auth.keys in the BotPort config (and set auth.enabled: true):")
    print(yaml.safe_dump([{"instance": name, "key": key, "secret": secret}],
                         default_flow_style=False, sort_keys=False).rstrip())
    print()
    print(f"# In Flight Deck '{name}' → Settings → BotPort (or FD_BOTPORT_* environment):")
    print(f"#   deck name: {name}")
    print(f"#   key:       {key}")
    print(f"#   secret:    {secret}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from botport.server import run

    try:
        cfg = _load(args)
    except Exception as exc:
        print(f"ERROR: cannot load configuration: {exc}", file=sys.stderr)
        return 1
    problems = cfg.problems()
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        print("Fix the configuration (see `botport check`).", file=sys.stderr)
        return 1
    return run(cfg)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V", "version"):
        print(f"botport {__version__}")
        return 0

    command = "run"
    if argv and argv[0] in _COMMANDS:
        command = argv.pop(0)

    if command == "gen-key":
        p = argparse.ArgumentParser(prog="botport gen-key", description="Generate a deck key/secret entry")
        p.add_argument("name", help="Deck name, e.g. vps1")
        return cmd_gen_key(p.parse_args(argv))

    p = argparse.ArgumentParser(
        prog=f"botport {command}" if command != "run" else "botport",
        description="BotPort — routing hub that connects Flight Decks (and agents)",
        epilog="Commands: run (default), check, gen-key NAME, --version",
    )
    _add_config_args(p)
    args = p.parse_args(argv)
    if command == "check":
        return cmd_check(args)
    return cmd_run(args)
