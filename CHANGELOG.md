# Changelog

## 0.4.0
- New read tools: `get_status`, `get_fees`, `get_candles`, `get_trades`, `get_account`, `get_fills`, `get_agents`,
  `get_tiers`, `get_season`, `get_points`, `get_leaderboard`, `get_referrals`, `get_tx`, `get_block`, `get_address`.
  `get_funding` takes `history` for past settlements.
- Every result starts with `summary`, one plain sentence, next to the figures and their units. Tools carry titles and
  examples.
- Prompts: market overview, check an account, season standings, explain a transaction, compare markets.
- Resources: the network spec, a fees table and documentation links.
- HTTP: `--hosted` for a public read-only server behind a proxy; `--burst`; `--allow-origin '*'`; CORS preflight;
  a rate-limited request is answered with how long to wait; `/health` carries counters per tool. Over HTTP the server
  gives each connection an `Mcp-Session-Id` at `initialize` and limits each connection, with a looser cap per address.
- MCP protocol 2025-11-25 alongside 2025-06-18, 2025-03-26 and 2024-11-05. A client asking for another version is
  offered the newest; the `MCP-Protocol-Version` header is never refused.

## 0.3.1
- `place_order` reports an order the chain received and then refused: the answer carries `accepted: false`,
  `refused: true` and the chain's own reason, read from its record of refused agent actions. The order's client nonce
  is sent explicitly and returned, so the record is matched to this order only.
- A refusal also carries `refusal_meaning`: the plain sentence the network publishes for that reason
  (`agents.refusalCodes` in the network spec). A network that publishes no codes leaves only the reason.
- A node that checks agent orders when it receives them answers `403` with code `agent_refused`; `place_order` and
  `cancel_order` report that as `refused: true` with its reason and meaning.
## 0.3.0

First public release.

- Reads the Vordium testnet by default; the main network only with `--network mainnet`.
- Tools: `get_network`, `get_markets`, `get_oracle`, `get_orderbook`, `get_funding`, `get_balance`, `get_positions`,
  `get_orders`, `get_policy`, `get_risk`, `place_order`, `cancel_order`.
- Checks that the network it reads is the one it was started for.
- `place_order` and `cancel_order` sign on your machine with a registered agent key, and are refused before anything is
  signed while the network reports agent trading off.
- Read-only streamable HTTP mode (`--http`).
