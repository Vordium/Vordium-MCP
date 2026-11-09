# Changelog

## 0.6.0
- Every order asks you first: before `place_order`, `close_position`, `modify_order` or `set_tpsl` signs, your AI app
  shows the order and nothing is signed unless you confirm (MCP elicitation). `--orders auto` lets orders within the
  agent's on-chain limits go through without asking — your choice, never the default. An app that cannot ask is refused,
  with the reason. Cancels and `stop_all` do not ask.
- New tools: `get_order` (one order by id), `close_position` (all or part of a position, reduce-only), `cancel_all`
  (one market or all), `modify_order` (a signed cancel-and-replace), `set_tpsl` (set, move or remove take-profit and
  stop-loss), `stop_all` (this server stops signing anything but cancels until `vordium-mcp resume`, and every resting
  order is cancelled). Every tool that signs passes the same gate as `place_order` (agent trading on, the agent registered
  and live, trading open, not stopped); `close_position` runs the full pre-trade check; `modify_order` checks the new
  order (its margin is the chain's to judge, since the old order's margin is released); `set_tpsl` checks the agent may
  set take-profit / stop-loss and that each leg sits on the right side of the mark; `modify_order` checks the agent may
  modify, where the chain publishes the agent's permissions.
- The agent key can live in the OS keychain (macOS Keychain, Windows Credential Manager, Secret Service on Linux):
  `vordium-mcp key store|status|delete`. Without `--agent-key-file` the keychain is used. A key file other users can
  read stops the server at start whenever one is named; an encrypted keystore opens with `--agent-key-passphrase-file`.
  A plaintext or failing keychain is never used.
- Tool annotations: read tools read-only and idempotent; tools that sign destructive and not idempotent. Every tool
  publishes an `outputSchema`; every result — a refusal too — carries `summary` and `network` as structured content and as text.
- The tool list is generated from the package (`python -m vordium_mcp.toolsdoc`) and locked in `tools.lock.json`.
- HTTP: listing tools, prompts and resources, `initialize` and `ping` do not spend a connection's request budget.

What to update:
- Use an AI app that supports MCP elicitation for orders, or add `--orders auto` to keep orders going through without
  asking.
- Move the key into the keychain (`vordium-mcp key store --owner 0x… --network testnet|mainnet --from-file
  ~/.vordium/agent.key`) and drop
  `--agent-key-file`; or `chmod 600` the key file.
- To use `modify_order` and `set_tpsl`, allow them for the agent in Vordex → Agents → Edit.

## 0.5.0
- Pre-trade check: before anything is signed, an order is judged against every rule the network runs on it — market
  state and order types, lot, tick, smallest order, largest order and position, price band, margin with the fee,
  reduce-only, open-order count, and the agent's own limits (markets, leverage, largest order and position, exposure,
  daily loss and fee caps, worst price). An order that would be refused is not signed: the answer names each rule with
  the numbers and ready fixes that pass. Rules the public reads cannot settle are listed under `not_checked`.
- New tools `check_order` (the same check, nothing signed) and `my_buying_power` (largest order per side at a given
  leverage, with the agent's limits). `check_order` takes the highest leverage the limits allow when none is named;
  `place_order` keeps 1× as its default.
- `vordium-mcp new-agent-key PATH` makes an agent key on this machine (file mode 600, never overwritten) and prints
  only its address, to register in Vordex with its limits.
- On the test network an agent trades without a session.

What to update (from 0.4):
- Expect `place_order` to answer an order the network would refuse with the rules it breaks and fixes that pass,
  instead of sending it.
- `check_order` and `my_buying_power` need `--owner`.
- On the test network, remove any session step for the agent key from your scripts.

## 0.4.1
- First PyPI release of the 0.4 line (same features as the hosted 0.4.0).

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
