# bazaar-contrib-telegram

Telegram Channel provider for [Bazaar Compute Node](https://github.com/yuchanns/bazaar-compute-node).

## Setup

Create a Telegram bot with BotFather, then install BCN and this provider:

```sh
uv tool install bazaar-compute-node
bcn contrib add telegram --enable
```

Add an agent using an installed Harness provider:

```sh
bcn agent add --name Editor --channel telegram --harness HARNESS_KIND
bcn run
```

## Configuration

Set either field in the agent's `telegram` Channel configuration:

- `token_env`: Environment variable holding the Telegram bot token.
- `token`: Bot token supplied directly by the configer; takes precedence.
