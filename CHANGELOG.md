# Changelog

## 0.3.0

First public release.

- Reads the Vordium testnet by default; the main network only with `--network mainnet`.
- Tools: `get_network`, `get_markets`, `get_oracle`, `get_orderbook`, `get_funding`, `get_balance`, `get_positions`,
  `get_orders`, `get_policy`, `get_risk`, `place_order`, `cancel_order`.
- Checks that the network it reads is the one it was started for.
- `place_order` and `cancel_order` sign on your machine with a registered agent key, and are refused before anything is
  signed while the network reports agent trading off.
- Read-only streamable HTTP mode (`--http`).
