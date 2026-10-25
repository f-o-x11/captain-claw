# BotPort

The routing hub that connects **Flight Decks** (and, for older setups,
individual Captain Claw agents): an agent on one deck consults or delegates to
`Agent@other-deck`, its Flight Deck carries the request to the hub, and the
other deck proxies it to its own agent.

```bash
pip install .            # light: aiohttp, aiosqlite, pydantic, pyyaml
pip install '.[llm]'     # + litellm, for LLM routing of unaddressed requests
botport check -c ~/.botport/config.yaml
botport -c ~/.botport/config.yaml
```

Add Flight Decks on the dashboard (**Decks → Add Flight Deck**): each gets its
own key and a join code to paste into that deck, and connects without a hub
restart. `botport gen-key vps1` still prints an `auth.keys` entry for the
config file, if you prefer that.

* Guide (Raspberry Pi + tmux, VPS decks, TLS / Tailscale / tunnels):
  [../docs/botport-deployment.md](../docs/botport-deployment.md)
* Annotated configuration: [config.example.yaml](config.example.yaml)
* tmux runner with auto-restart: [../scripts/botport-tmux.sh](../scripts/botport-tmux.sh)
* Tests: `python -m pytest botport/tests` (from the repository root)
