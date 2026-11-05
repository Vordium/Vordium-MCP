"""The pre-trade check on fixed reads: every rule it judges, the numbers it reports, and fixes that pass."""
from vordium_mcp import pretrade as pt

E18, E8, E6 = 10 ** 18, 10 ** 8, 10 ** 6


class FakeCtx(pt.Ctx):
    """a Ctx built from fixed reads (no network)"""

    def __init__(self, free6=1000 * E6, pos18=0, agent=None, mark8=2400 * E8, **pair):
        self.owner, self.pair_id, self.agent_addr = "0x" + "1" * 40, 1, ("0x" + "a" * 40) if agent else None
        self.specs = {"min_order_notional_6dec": "10000000", "price_band_bps": 500, "price_band_active": True,
                      "order_limits_active": True, "perps_taker_fee_cbps": 450}
        self.pspec = {"pair": {"pair_id": 1, "active": True, "paused": False, "delisted": False, "taker_fee_bps": 0},
                      "tick_8dec": "10000000", "lot_18dec": "100000000000000", "max_leverage_bps_effective": 200000,
                      "maintenance_margin_bps": 250, "max_limit_order_size_18dec": str(8000 * E18),
                      "max_market_order_size_18dec": str(2000 * E18), "max_order_notional_6dec": str(20_000_000 * E6),
                      "max_account_position_18dec": str(40000 * E18), "open_interest_cap_18dec": "0", **pair}
        self.perp = {"lifecycle_state": "active", "reduce_only_mode": False, "post_only_mode": False, "allowed_order_types": 0}
        self.chain_time_ms = 1_800_000_000_000
        self.status = {"trading_open": True, "trading_active": True, "market_frozen": False}
        self.truth = {"agent_policy_active": True}
        self.mark8 = self.oracle8 = mark8
        self.best_bid8, self.best_ask8 = mark8 - 10 * E8 // 10, mark8 + 10 * E8 // 10
        self.free6, self.pos18 = free6, pos18
        self.pos_margin6 = self.pos_entry6 = self.pos_lev = None
        self.open_orders, self.open_cap, self.allowance, self.used = 0, 1000, 20000, 10
        self.agent = agent


def order(**k):
    o = {"pair_id": 1, "side": "buy", "size_18dec": 10 ** 17, "price_8dec": 2390 * E8, "order_type": "limit",
         "post_only": False, "reduce_only": False, "leverage_bps": 100000}
    o.update(k)
    return o


def codes(r):
    return [f["code"] for f in r["failed"]]


def test_a_resting_order_inside_every_rule_passes_with_its_numbers():
    r = pt.check(FakeCtx(), order())
    assert r["can_place"] and r["failed"] == []
    f = r["figures"]
    assert f["notional_6dec"] == 239 * E6 and f["margin_6dec"] == 239 * E6 // 10 and f["fee_6dec"] == 239 * E6 * 450 // E6


def test_each_shape_and_size_rule_refuses_with_the_chain_code():
    assert codes(pt.check(FakeCtx(), order(size_18dec=10 ** 17 + 5 * 10 ** 13))) == ["lot"]
    assert codes(pt.check(FakeCtx(), order(price_8dec=2390 * E8 + 5))) == ["tick"]
    assert codes(pt.check(FakeCtx(), order(size_18dec=10 ** 15))) == ["min_notional"]
    assert codes(pt.check(FakeCtx(), order(leverage_bps=250000))) == ["leverage"]
    assert "order_size_above_max" in codes(pt.check(FakeCtx(free6=10 ** 15), order(size_18dec=8001 * E18)))


def test_margin_is_the_orders_own_reservation_against_free_usdc():
    # 1 ETH at 2,390, 1×: 2,390 USDC of margin against 1,000 free
    r = pt.check(FakeCtx(), order(size_18dec=E18, leverage_bps=10000))
    assert codes(r) == ["insufficient_margin"] and r["failed"][0]["needed"] == "$2,390" and r["failed"][0]["have"] == "$1,000"
    assert pt.check(FakeCtx(), order(size_18dec=E18, leverage_bps=30000))["can_place"]
    assert pt.check(FakeCtx(), order(size_18dec=E18, leverage_bps=10000, reduce_only=True), fixes=False)["can_place"] is False  # nothing to reduce


def test_the_price_band_refuses_only_the_side_that_would_trade_at_once():
    edge = 2400 * E8 * 10500 // 10000
    assert codes(pt.check(FakeCtx(), order(price_8dec=edge + 10 ** 7))) == ["price_band"]
    assert pt.check(FakeCtx(), order(price_8dec=edge - 10 ** 7))["can_place"]
    assert pt.check(FakeCtx(), order(side="sell", price_8dec=3000 * E8))["can_place"]   # far above: passive, never refused


def test_reduce_only_and_post_only():
    assert codes(pt.check(FakeCtx(), order(reduce_only=True))) == ["reduce_only_nothing_to_reduce"]
    assert codes(pt.check(FakeCtx(pos18=E18), order(reduce_only=True))) == ["reduce_only_nothing_to_reduce"]   # same side
    assert pt.check(FakeCtx(pos18=E18), order(side="sell", price_8dec=2410 * E8, reduce_only=True))["can_place"]
    assert codes(pt.check(FakeCtx(), order(post_only=True, price_8dec=2401 * E8))) == ["post_only_would_cross"]


AGENT = {"found": True, "status": "active", "expires_ms": 1_900_000_000_000, "expired_by_head_time": False,
         "policy_v1": {"enabled": True, "expires_ms": 1_900_000_000_000, "caps_bitmask": 19, "side_mode": 0,
                       "max_leverage_bps": 100000, "max_total_exposure_6dec": 5000 * E6, "daily_loss_limit_6dec": 500 * E6,
                       "daily_fee_cap_6dec": 0, "ops_per_window": 0, "ops_window_ms": 0, "oracle_band_bps": 300},
         "market_limits": [{"pair": 1, "allowed": True, "max_leverage_bps": 100000, "max_order_18dec": str(441800000000000000),
                            "max_position_18dec": str(2209000000000000000)}], "risk_v1": None, "accounting": None}


def test_an_agent_over_its_leverage_is_refused_with_the_agent_limit_as_the_fix():
    """The live case: buy 0.1 ETH at 20× through an agent that allows 10× on ETH."""
    r = pt.check(FakeCtx(agent=AGENT), order(leverage_bps=200000))
    assert codes(r) == ["agent_refused/max_leverage"] and r["failed"][0]["have"] == "20×"
    fix = r["fixes"][0]
    assert fix["label"] == "use 10× (agent limit)" and fix["order"]["leverage_bps"] == 100000 and fix["passes"]
    assert pt.leverage_cap(FakeCtx(agent=AGENT)) == 100000


def test_agent_market_size_and_band_rules():
    ctx = FakeCtx(agent=AGENT)
    assert "agent_refused/max_order" in codes(pt.check(ctx, order(size_18dec=5 * 10 ** 17)))
    assert codes(pt.check(ctx, order(price_8dec=2300 * E8))) == ["agent_refused/price_outside_band"]
    other = dict(AGENT, market_limits=[])
    assert codes(pt.check(FakeCtx(agent=other), order())) == ["agent_refused/market_not_allowed"]


def test_combined_fix_passes_as_a_whole():
    """several rules fail at once: one order with every fix applied in turn, judged again, is offered first"""
    r = pt.check(FakeCtx(agent=AGENT), order(size_18dec=10 ** 15, price_8dec=2700 * E8, leverage_bps=200000))
    assert not r["can_place"]
    top = r["fixes"][0]
    assert top["label"].startswith("together:") and top["passes"] and pt.check(FakeCtx(agent=AGENT), top["order"])["can_place"]


def test_buying_power_takes_margin_and_agent_limits_together():
    ctx = FakeCtx(agent=AGENT)
    assert pt.max_size(ctx, "buy", 100000) == 441800000000000000            # the agent's largest order wins
    assert pt.max_size(FakeCtx(free6=100 * E6), "buy", 10000) == 416 * 10 ** 14   # 100 USDC at 1× at the mark, on the lot


def test_a_locked_agent_may_still_close_and_yesterdays_loss_counts_for_nothing():
    today = FakeCtx().chain_time_ms // 86400000
    locked = dict(AGENT, risk_v1={"lock_active": True, "day": today, "realised_pnl_day_6dec": str(-600 * E6), "fees_day_6dec": "0"})
    long1 = FakeCtx(agent=locked, pos18=E18 // 10)
    assert pt.check(long1, order(side="sell", price_8dec=2410 * E8, size_18dec=E18 // 10, reduce_only=True))["can_place"]
    assert "agent_refused/locked_reduce_only" in codes(pt.check(long1, order()))
    flip = pt.check(long1, order(side="sell", price_8dec=2410 * E8, size_18dec=E18 * 15 // 100))   # 0.1 long → 0.05 short
    assert "agent_refused/locked_reduce_only" in codes(flip)
    yesterday = dict(AGENT, risk_v1={"lock_active": False, "day": today - 1, "realised_pnl_day_6dec": str(-600 * E6)})
    assert pt.check(FakeCtx(agent=yesterday), order())["can_place"]


def test_fix_prices_carry_at_most_six_decimals_and_the_signer_rounding_is_mirrored():
    ctx = FakeCtx(tick_8dec="10", mark8=442 * 10 ** 3)                   # a 7–8 decimal market (tick 1e-7)
    assert pt.price_step(ctx) == 100
    fx = pt.check(ctx, order(price_8dec=10 ** 6, size_18dec=10 ** 22))["fixes"]
    assert all(int(f["order"]["price_8dec"]) % 100 == 0 for f in fx if f["order"] and "price_8dec" in f["order"])
    s = pt.snap(FakeCtx(), order(size_18dec=123450000000000000, price_8dec=239012345678))
    assert s["size_18dec"] == 123400000000000000 and s["price_8dec"] == 239010000000
    assert pt.snap(FakeCtx(), order(side="sell", price_8dec=239012345678))["price_8dec"] == 239020000000
