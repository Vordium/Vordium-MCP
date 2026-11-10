# Changelog

## 0.6.3
- Each network is pinned: the testnet to chain 101102, genesis
  `8b5d414c5cd5d18bc468b64f87ac8e2c2b5ff494c769d5e09dcf50ce75a59792`; the mainnet to chain 101101, genesis
  `2c1c0679fa6ab8358f1d3e8d294a8d1c73ac2e2caf9079f1216abedc3d9fdf4f`.
- Before any tool signs, the network spec, the node's `/status` and the network's own answer must report the pinned
  chain id and genesis, and the Vordium SDK must carry the same pin (it checks `eth_chainId` and the node again).
  Otherwise the tool is refused with the network, the expected and the reported values, and nothing is signed. Reads
  still work; `get_network` shows `pinned` and, on a difference, `pin_warning`.
- `--rest` (your own node) is held to the pin of the network named with `--network`.
- A new genesis for a network takes a new release with the new pin.
- The `trade` extra needs `vordium>=1.4.10` (the first SDK release that pins its networks); with an older SDK, nothing
  is signed.
- An order's client nonce (`place_order`, `close_position`, `modify_order`) comes from the Vordium SDK's nonce
  allocator for the owner, taken when the order is signed: strictly rising across every process on this machine, never
  back when the clock goes back, and checked against the window the network accepts (its block time when it can be
  read). When this computer's clock is too far off, the tool is refused, says how far, and nothing is signed.
- A write whose answer is lost is never signed again with a new nonce. An order is sent again with the same signed
  request; it counts as received (`accepted: true`, `already_received: true`) only when the network then lists an
  order with its client nonce and the signed market, side and size or price. Otherwise, and for writes the network
  does not apply once per nonce (cancel-all, modify, take-profit / stop-loss), the result says it is not known whether
  the chain received it (`accepted: null`, `delivery_unknown: true`) and nothing is sent twice: read `get_orders` first.
- An order whose answer was lost, or answered with a refusal or a 5xx after an earlier copy may have arrived, is
  reported the same way (`accepted: null`, `delivery_unknown: true`): it may have reached the chain, so check the open
  orders before placing it again. It is never reported as refused.
- A cancel whose answer was lost is read back. It is reported as done (`accepted: true`) only when the account's
  records show the order cancelled; as filled before the cancel (`accepted: false`, check the position) when they show
  a fill; otherwise as not known (`accepted: null`). Never as refused.
- A write that could not reach the network on any try is refused with "nothing was sent".
- With no readable block time, the window is checked against this computer's clock.
- Hosted server: calls that read the network run at most 8 at once per address and 32 at once in all; past either, the
  call is answered at once (429 or 503, a JSON-RPC error with `Retry-After`), never queued. Every client together has a
  per-minute cap too.
- Hosted server: one call takes at most 25 s, every read included; a call that reaches the limit is answered as a tool
  error saying so, and its answer is never shared.
- Hosted server: `Mcp-Session-Id` values are issued and checked by the server. A request carrying an id the server did
  not issue is answered 404 (send `initialize` again, without one) and counts against its address.
- Hosted server: an IPv6 client is counted by its /64, an IPv4 client by its address.
- A recovery phrase passed to a tool is recognised word by word, and any text argument over 4096 characters is refused
  before anything else, so a long argument is always judged at once.

## 0.6.2
- A market tool called without a market now says so ("name a market: give its symbol … or its pair_id") and points to
  `get_markets`, instead of reporting that no market matches.
- Hosted server: a reply is never lost when the answer cache fails, and an expired cached answer can be stored again.

## 0.6.1
- MCP Registry listing: `io.github.Vordium/vordium-mcp`, described by `server.json` on the 2025-12-11 schema and
  published from the release workflow. The server itself is unchanged.

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
