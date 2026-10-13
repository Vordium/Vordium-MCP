# Vordium MCP server

Connect any AI that speaks the [Model Context Protocol](https://modelcontextprotocol.io) — Claude, Cursor, or your own
agent — to [Vordium](https://vordium.com). It can read markets, prices, order books, funding and accounts, and, once
the network switches agent trading on, place and cancel orders as an agent whose limits the chain enforces.

- **Testnet first.** The server reads the Vordium testnet unless you name the main network. Testnet tokens have no value.
- **Runs on your machine.** It talks to your MCP client over stdio. No account, no API key.
- **Your keys stay with you.** No tool takes a private key or recovery phrase, and the server refuses any argument that
  looks like one. To trade, you point the server at a file holding an *agent* key; it signs on your machine, and only
  the signature leaves it.
- **The chain enforces the limits.** An agent trades under the policy and per-market limits set by its owner on-chain.
  This server only refuses early: while the network reports agent trading off, `place_order` and `cancel_order` are
  refused before anything is signed.
- **Nothing is remembered.** Every value is read from the network when a tool is called. The server also checks that
  the network it reads is the one it was started for, and refuses every tool if not.

## Install

You need Python 3.9 or later. The simplest way to run it is [uv](https://github.com/astral-sh/uv), which fetches and runs
the package in one step:

```bash
uvx vordium-mcp --version
```

Or install it with pipx or pip:

```bash
pipx install vordium-mcp
```

Reading needs nothing beyond the Python standard library. Trading needs the Vordium SDK for signing:
`uvx --from "vordium-mcp[trade]" vordium-mcp` or `pipx install "vordium-mcp[trade]"`.

## Connect your client

### Claude Desktop

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "vordium": {
      "command": "uvx",
      "args": ["vordium-mcp"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add vordium -- uvx vordium-mcp
```

### Cursor

Add this to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "vordium": {
      "command": "uvx",
      "args": ["vordium-mcp"]
    }
  }
}
```

If you installed with pipx or pip, use `"command": "vordium-mcp"` and `"args": []` instead.

Then ask, for example: *"What does the ETH order book on Vordium look like?"* or *"Is agent trading on?"*

## Tools

| Tool | What it answers |
| --- | --- |
| `get_network` | Which network this is (checked against the network itself) and what the network vouches for now: chain id, genesis, validators, and whether trading, deposits and agent trading are on |
| `get_markets` | Every market with its state, leverage, fees and trading rules |
| `get_oracle` | Oracle and mark prices, for every market or one |
| `get_orderbook` | Bids and asks for one market, best first |
| `get_funding` | The funding rate, interval and next settlement for one market |
| `get_balance` | An account's USDC: available to trade, in positions, reserved by orders, unrealized PnL |
| `get_positions` | An account's open positions |
| `get_orders` | An account's resting orders |
| `get_policy` | Each agent's policy and market limits as the chain stores them, and whether the network enforces them now |
| `get_risk` | Each agent's usage against its policy |
| `place_order` | A market or limit order, signed on your machine with the agent key |
| `cancel_order` | Cancel a resting order, as the agent |

A tool that cannot read the network says so; it never answers from memory. A refusal comes back as a tool error that
names the readings behind it, and says that nothing was signed or sent.

## Trading as an agent

An agent is a key that its owner registers on-chain, gives a session, and limits with a policy. See
[Agents](https://docs.vordium.com/infrastructure/agents/) and [Session keys](https://docs.vordium.com/guides/session-keys/).
The owner's own key never goes into this server.

1. Register the agent and create its session from the owner's wallet.
2. Save the agent's private key in a file only you can read: `chmod 600 ~/.vordium/agent.key`.
3. Start the server with trading allowed:

```json
{
  "mcpServers": {
    "vordium": {
      "command": "uvx",
      "args": ["--from", "vordium-mcp[trade]", "vordium-mcp", "--allow-trading",
               "--owner", "0xYOUR_OWNER_ADDRESS", "--agent-key-file", "~/.vordium/agent.key"]
    }
  }
}
```

Before it signs, the server checks, in this order:

1. The network reports agent trading on (`agent_policy_active`, read live from the network's own answer). If not,
   it refuses at once.
2. You started it with `--allow-trading`, an owner and an agent key file.
3. The key is a registered agent of that owner, not revoked, not expired, with a confirmed session, and is not the
   owner's key.
4. The network reports trading open, and the market is trading.

The chain then applies the agent's policy and limits on every validator. An order this server lets through can still
be refused or dropped by the chain; the server returns the chain's answer unchanged and never reports a fill it did
not see. Read `get_orders` and `get_positions` to see what was applied.

## The main network

The main network is used only when you name it:

```json
"args": ["vordium-mcp", "--network", "mainnet"]
```

Use `--rest` to read from your own node instead of the public one; the server
still checks that the node serves the network you named.

## Read-only over HTTP

`vordium-mcp --http localhost:8770` serves the read tools over streamable HTTP at `/mcp`, for clients that connect to a
URL. It lists no trading tools and takes no key or trading flag. It answers JSON (no event stream), limits each client
to `--rate-per-minute` requests (default 120), refuses browser origins unless allowed with `--allow-origin`, and keeps no
request log. Put it behind a proxy for anything beyond your own machine, and pass `--trust-proxy` there so the limit
applies per client.

## Settings

| Flag | Default |
| --- | --- |
| `--network testnet\|mainnet` | `testnet` |
| `--rest URL` | the network's public REST base |
| `--allow-trading` | off |
| `--owner 0x…` | none |
| `--agent-key-file PATH` | none |
| `--http HOST:PORT` | off (stdio) |
| `--rate-per-minute N` | 120 |
| `--allow-origin URL` | none |

`vordium-mcp --help` lists them, with the environment form of each.

## Develop

```bash
pip install -e ".[trade]" pytest
pytest -q tests                        # no network
# the live testnet test reads the real testnet when switched on; nothing is signed or sent
```

## Links

- Documentation: https://docs.vordium.com/build/mcp/
- Explorer: https://vordscan.io
- Python SDK: https://pypi.org/project/vordium/

MIT licensed.
