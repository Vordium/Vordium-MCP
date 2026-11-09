"""Tool annotations and output schemas (MCP 2025-06-18+: outputSchema + structuredContent).

Every result carries ``summary`` (one plain sentence) and ``network``; the server guarantees both. The other fields
listed here are the ones a client can rely on; results may carry more (additionalProperties stays open), and a value
the network does not serve is null, never invented. A refusal is an isError result (its own shape, not this schema).
"""
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
# reads only the configured owner's account through this server (still the network: openWorld)
LOCAL_READ = dict(READ)
# signs and sends: changes what the account holds; never safe to repeat blindly
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

N = {"type": ["number", "null"]}
S = {"type": ["string", "null"]}
B = {"type": ["boolean", "null"]}
A = {"type": "array"}
O = {"type": ["object", "null"]}


def out(**props):
    return {"type": "object",
            "properties": {"summary": {"type": "string", "description": "one plain sentence"},
                           "network": {"type": "string", "description": "testnet or mainnet"}, **props},
            "required": ["summary", "network"]}


WRITE_OUT = dict(sent=B, accepted=B, agent=S, chain_status=N, chain_answer={}, confirmation=O,
                 refused=B, refusal_reason=S, refusal_meaning=S, note=S)

OUTPUT = {
    "get_status": out(live=B, height=N, finalized_height=N, blocks_per_second=N, trading_open=B, deposits_open=B,
                      agent_trading_on=B, trading_blockers=A),
    "get_network": out(identity=O, truth=O),
    "get_markets": out(count=N, markets=A),
    "get_fees": out(count=N, markets=A, liquidation=O),
    "get_oracle": out(prices=A),
    "get_orderbook": out(market=S, pair_id=N, bids=A, asks=A, best_bid=N, best_ask=N, spread=N, empty=B),
    "get_candles": out(market=S, pair_id=N, resolution=S, count=N, candles=A),
    "get_trades": out(market=S, pair_id=N, count=N, trades=A),
    "get_funding": out(market=S, pair_id=N, rate_per_interval_pct=N, interval_secs=N, next_funding=N, history=A),
    "get_account": out(address=S, usdc=O, positions=A, orders=A, explorer=S),
    "get_fills": out(address=S),
    "get_balance": out(address=S, usdc=O),
    "get_positions": out(address=S, count=N, positions=A),
    "get_orders": out(address=S, count=N, orders=A),
    "get_order": out(address=S, order_id=N, found=B, order=O),
    "get_agents": out(owner=S, count=N, agents=A),
    "get_policy": out(owner=S, count=N, agents=A, available=B),
    "get_risk": out(owner=S, count=N, agents=A, available=B),
    "get_tiers": out(agent_limits=O, referral_tiers=O),
    "get_season": out(season_id=N, name=S),
    "get_points": out(address=S, season_id=N, rank=N, qualified=B),
    "get_leaderboard": out(board=S, rows=A),
    "get_referrals": out(address=S, code=S, tier=S),
    "get_tx": out(),
    "get_block": out(),
    "get_address": out(address=S, account_value_usdc=N, open_positions=N, explorer=S),
    "check_order": out(can_place=B, failed=A, fixes=A, figures=O, not_checked=A),
    "my_buying_power": out(markets=A),
    "place_order": out(**WRITE_OUT, client_nonce=N),
    "close_position": out(**WRITE_OUT, client_nonce=N),
    "cancel_order": out(**WRITE_OUT),
    "cancel_all": out(**WRITE_OUT, scope=S),
    "modify_order": out(**WRITE_OUT, replaced_order_id=N, client_nonce=N),
    "set_tpsl": out(**WRITE_OUT, position_id=N, take_profit=N, stop_loss=N),
    "stop_all": out(stopped=B, at=S, cancel_all=O, revoke_on_chain=S),
}
