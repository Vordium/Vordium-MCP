"""A fake network for the tests: URL -> (status, body). Every request is recorded, so a test can prove what was NOT read."""
import json

from vordium_mcp.config import Config
from vordium_mcp.networks import NETWORKS
from vordium_mcp.server import make_chain

OWNER = "0x00000000000000000000000000000000000000aa"
AGENT = "0x00000000000000000000000000000000000000bb"


def truth(active=False, ok=True, network="testnet", chain_id=101102, genesis="ab" * 32):
    if not ok:
        return {"version": 1, "ok": False, "error": "disagree"}
    return {"version": 1, "ok": True, "network": network, "chain_id": chain_id, "genesis_sha256": genesis,
            "finalized_height": 1000, "validators": {"seated": 7}, "committed": {"agreed_by": 7},
            "agent_policy_active": active, "trading_open": True, "deposits_open": True, "evm_active": False,
            "age_s": 3.0, "max_age_s": 30}


class FakeNet:
    def __init__(self, network="testnet", active=False, **over):
        n = NETWORKS[network]
        self.n = n
        self.calls = []
        test = network == "testnet"
        cid = 101102 if test else 101101
        g = "ab" * 32
        self.routes = {
            n["spec"]: {"testnet": True, "chainId": cid, "genesisSha256": g} if test else {"chainId": cid, "genesisSha256": g},
            n["truth"]: truth(active=active, network=network, chain_id=cid, genesis=g),
            n["rest"] + "/status": {"chain_id": cid, "trading_open": True, "trading_active": True, "trading_blockers": []},
            n["rest"] + "/api/pairs/perps": {"chain_time_ms": 5_000, "count": 2, "pairs": [
                {"pair_id": 1, "symbol": "ETHUSDT", "name": "ETH", "active": True, "paused": False, "delisted": False,
                 "lifecycle_state": "active", "max_leverage_x": 3},
                {"pair_id": 2, "symbol": "BTCUSDT", "name": "BTC", "active": True, "paused": True, "delisted": False,
                 "lifecycle_state": "active", "max_leverage_x": 3}]},
            n["rest"] + "/markets/specs": {"specs": {"min_order_notional_6dec": "10000000", "price_band_active": True,
                                                     "pairs": [{"pair": {"pair_id": 1}, "tick_8dec": "0", "lot_18dec": "0",
                                                                "maintenance_margin_bps": 200}]}},
            n["rest"] + "/oracle/prices": {"prices": [{"pair_id": 1, "symbol": "ETH", "oracle_price": 2500.5, "mark_price": 2500.6},
                                                     {"pair_id": 2, "symbol": "BTC", "oracle_price": 83000.0, "mark_price": 83001.0}]},
            n["rest"] + "/orderbook/1": {"pair_id": 1, "bids": [[2500000000, "100000000000000000"]],
                                         "asks": [[2501500000, "200000000000000000"]]},
            n["rest"] + "/funding/1": {"pair_id": 1, "rate_8dec": -105, "rate_pct": -0.0001, "interval_secs": 3600,
                                       "next_funding": 99, "funding_enabled": True},
            n["rest"] + f"/account/equity/{OWNER}": {"free_balance": 12_500_000, "in_positions": 0, "reserved_orders": 0,
                                                     "unrealized_pnl": 0, "total_equity": 12_500_000},
            n["rest"] + f"/balance/{OWNER}": {"total": 12_500_000, "free_balance": 12_500_000},
            n["rest"] + f"/positions/{OWNER}": [{"pair_id": 1, "size_18dec": "1", "is_long": True}],
            n["rest"] + f"/orders/{OWNER}": [{"order_id": 7, "pair_id": 1}],
            n["rest"] + f"/agents/{OWNER}": {"owner": OWNER, "count": 1, "agents": [
                {"agent": AGENT, "status": "active", "expires_ms": 9_000_000, "policy_v1": {"max_notional": "1"}}]},
            n["rest"] + f"/agents/{OWNER}/{AGENT}": {"found": True, "agent": AGENT, "status": "active",
                                                     "expires_ms": 9_000_000},
            n["rest"] + f"/session/valid?owner={OWNER}&session_key={AGENT}": {"valid": True, "confirmed": True},
        }
        self.routes.update(over)

    def __call__(self, method, url, body=None, timeout=12):
        self.calls.append((method, url))
        if method != "GET":
            return 404, json.dumps({"error": "no writes in the fake network"})
        if url not in self.routes:
            return 404, json.dumps({"error": "not found"})
        v = self.routes[url]
        if isinstance(v, tuple):
            return v
        return 200, json.dumps(v)

    def read_urls(self):
        return [u for m, u in self.calls]


def setup(network="testnet", active=False, cfg=None, **over):
    net = FakeNet(network, active, **over)
    n = NETWORKS[network]
    cfg = cfg or Config(network=network, rest=n["rest"], spec=n["spec"], truth=n["truth"])
    return net, cfg, make_chain(cfg, fetch=net)
