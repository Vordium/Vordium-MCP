# Changelog


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
