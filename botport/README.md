# BotPort

The routing hub that connects **Flight Decks** (and, for older setups,
individual Captain Claw agents): an agent on one deck consults or delegates to
`Agent@other-deck`, its Flight Deck carries the request to the hub, and the
other deck proxies it to its own agent.

```bash
pip install .            # light: aiohttp, aiosqlite, pydantic, pyyaml
pip install '.[llm]'     # + litellm, for LLM routing of unaddressed requests
botport gen-key vps1     # an auth.keys entry + the values for that Flight Deck
botport check -c ~/.botport/config.yaml
botport -c ~/.botport/config.yaml
```

* Guide (Raspberry Pi + tmux, VPS decks, TLS / Tailscale / tunnels):
  [../docs/botport-deployment.md](../docs/botport-deployment.md)
* Annotated configuration: [config.example.yaml](config.example.yaml)
* tmux runner with auto-restart: [../scripts/botport-tmux.sh](../scripts/botport-tmux.sh)
* Tests: `python -m pytest botport/tests` (from the repository root)
