"""Against the real Vordium testnet. Opt in with VORDIUM_MCP_LIVE=1; nothing is ever signed or sent."""
import json, os

import pytest

from vordium_mcp.config import load
from vordium_mcp.server import handle, make_chain

pytestmark = pytest.mark.skipif(os.environ.get("VORDIUM_MCP_LIVE") != "1", reason="set VORDIUM_MCP_LIVE=1 to read the testnet")
ANY = "0x0000000000000000000000000000000000000001"


@pytest.fixture(scope="module")
def srv():
    _, cfg = load([], env={})
    return cfg, make_chain(cfg)


def call(srv, name, args=None):
    cfg, chain = srv
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
               chain, cfg)["result"]
    return r["isError"], json.loads(r["content"][0]["text"])


def test_reads(srv):
    err, net = call(srv, "get_network")
    assert not err and net["identity"]["network"] == "testnet" and net["identity"]["testnet"] is True
    err, m = call(srv, "get_markets")
    assert not err and m["count"] > 0
    sym = m["markets"][0]["symbol"]
    for name, args in (("get_oracle", {"symbol": sym}), ("get_orderbook", {"symbol": sym, "depth": 5}),
                       ("get_funding", {"symbol": sym}), ("get_balance", {"address": ANY}),
                       ("get_positions", {"address": ANY}), ("get_orders", {"address": ANY}),
                       ("get_policy", {"address": ANY}), ("get_risk", {"address": ANY})):
        err, doc = call(srv, name, args)
        assert not err, (name, doc)


def test_writes_refuse_while_the_switch_is_off(srv):
    _, net = call(srv, "get_network")
    if net["truth"].get("agent_policy_active") is not False:
        pytest.skip("the testnet reports agent trading on (or cannot vouch); this test covers the off state")
    for name, args in (("place_order", {"symbol": "ETH", "side": "buy", "size": 0.01, "order_type": "market"}),
                       ("cancel_order", {"order_id": 1})):
        err, doc = call(srv, name, args)
        assert err and "agent trading is not switched on" in doc["reason"] and doc["note"] == "Nothing was signed or sent."
