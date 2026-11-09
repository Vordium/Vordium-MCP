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


def test_liquidation_distance_is_signed_and_cross_is_unknown():
    """how far the mark can move AGAINST the position first; 0 or less when already past; cross accounts read '—'"""
    from vordium_mcp.reads import liq_distance
    assert liq_distance(100.0, 95.0, True) == 5.0            # long: liquidated below
    assert liq_distance(100.0, 105.0, False) == 5.0          # short: liquidated above
    assert liq_distance(100.0, 103.0, True) < 0               # a long whose mark is already below its liquidation price
    assert liq_distance(None, 95.0, True) is None and liq_distance(100.0, None, True) is None
    ctx = FakeCtx()
    long_after = pt._liq_after(ctx, 10 ** 17, True, 10 ** 17, 2390 * E8, 239 * E6 // 10, False)
    assert long_after["liq_distance_pct"] is not None and long_after["liq_distance_pct"] > 0
    ctx.liq_mode = 'cross'
    assert pt._liq_after(ctx, 10 ** 17, True, 10 ** 17, 2390 * E8, 239 * E6 // 10, False) == {"liq_price": "—", "liq_distance_pct": None}


def test_margin_mode_per_market_from_the_account_read():
    """404 (no cross margin on the network) = isolated; a failed read = unknown; else THIS market's own mode,
    then the account default — never the network-wide switch"""
    ok = {"default": "cross", "modes": {1: "isolated", 2: "cross"}, "cross": {}}
    assert pt.liq_mode_of("none", None, 1) == "isolated"
    assert pt.liq_mode_of("unknown", None, 1) == "unknown"
    assert pt.liq_mode_of("ok", ok, 1) == "isolated"
    assert pt.liq_mode_of("ok", ok, 2) == "cross"
    assert pt.liq_mode_of("ok", ok, 3) == "cross"                          # no entry: the account default
    assert pt.liq_mode_of("ok", {"default": "isolated", "modes": {}}, 3) == "isolated"


class _Read(dict):
    ok = True


class _Chain:
    """the reads Ctx makes, fixed; margin_modes is the case under test"""

    def __init__(self, mm):
        self.mm = mm

    def require(self, r, what):
        return r["json"]

    def _r(self, j):
        return _Read(json=j)

    def market_specs(self):
        return self._r({"specs": {"pairs": [{"pair": {"pair_id": 1}}]}})

    def markets(self):
        return self._r({"pairs": [{"pair_id": 1}], "chain_time_ms": 1})

    def status(self, cache=True):
        return self._r({})

    def truth(self):
        return {"json": {}}

    def oracle(self):
        return self._r({"prices": [{"pair_id": 1, "mark_price": 2400, "oracle_price": 2400}]})

    def orderbook(self, pair_id):
        return self._r({"bids_8dec": [], "asks_8dec": []})

    def margin_modes(self, owner):
        return self.mm

    def equity(self, owner):
        return self._r({"free_balance": "1000000000"})

    def positions(self, owner):
        return self._r([])

    def read(self, path, cache=True):
        return self._r({})


def test_ctx_reads_this_markets_mode_and_a_failed_read_leaves_it_unknown():
    """the approval card's liquidation figure is '—' unless THIS market is isolated for the account"""
    owner = "0x" + "1" * 40
    assert pt.Ctx(_Chain(("none", None)), owner, 1).liq_mode == "isolated"
    assert pt.Ctx(_Chain(("unknown", None)), owner, 1).liq_mode == "unknown"
    per_pair = ("ok", {"default": "isolated", "modes": {1: "cross"}, "cross": {}})
    assert pt.Ctx(_Chain(per_pair), owner, 1).liq_mode == "cross"
    ctx = pt.Ctx(_Chain(("unknown", None)), owner, 1)
    assert pt._liq_after(ctx, 10 ** 17, True, 10 ** 17, 2390 * E8, 239 * E6 // 10, False) == {"liq_price": "—", "liq_distance_pct": None}


def test_liquidation_rule_reads_each_part_from_its_own_field():
    """The fee from the chain (never typed); gone from every perpetual market = no fee; a failed read is never 'no fee';
    the partial size, step, wait and the vault backstop each only from their own field"""
    from vordium_mcp.reads import liq_rule
    P = lambda **k: {"pair": {"kind": k.pop("kind", "perps")}, **k}
    today = liq_rule({"pairs": [P(liquidation_fee_bps=40), P(liquidation_fee_bps=40), P(kind="spot", liquidation_fee_bps=0)],
                      "isolated_partial_liquidation": True})
    assert today["fee_pct"] == 0.4 and "charges 0.4%" in today["summary"] and "only in part" in today["summary"]
    assert today["backstop_below_maintenance"] is None and today["partial_threshold_usdc"] is None and not today["book_liquidation"]
    gone = liq_rule({"pairs": [P(), P()]})
    assert gone["fee_pct"] == 0 and gone["summary"].startswith("No liquidation fee.")
    assert liq_rule({"pairs": [P(liquidation_fee_bps=0)]})["fee_pct"] == 0
    assert liq_rule({})["fee_pct"] is None and liq_rule(None)["fee_pct"] is None            # nothing read: no claim
    some = liq_rule({"pairs": [P(), P(liquidation_fee_bps=40)]})                                # gone from some only
    assert some["fee_pct"] is None and "in each market's row" in some["summary"] and "0.4" not in some["summary"]
    mixed = liq_rule({"pairs": [P(liquidation_fee_bps=40), P(liquidation_fee_bps=20)]})
    assert mixed["fee_pct"] is None and "in each market's row" in mixed["summary"]
    v1 = {"pairs": [P()], "book_liquidation": True, "liquidation_partial_threshold_6dec": "10000000000",
          "liquidation_partial_bps": 2000, "liquidation_cooldown_ms": 30000, "liquidation_backstop_numerator": 2,
          "liquidation_backstop_denominator": 3}
    new = liq_rule(v1)
    assert new["book_liquidation"] and new["partial_threshold_usdc"] == 10000 and new["partial_pct"] == 20
    assert new["cooldown_s"] == 30 and new["backstop_below_maintenance"] == "2/3"
    assert ("A liquidation is closed with an order against the order book. No liquidation fee. A position worth more than "
            "10000 USDC is closed 20% first, and in full if it is still at its liquidation level within 30 s. Any margin "
            "left stays with the trader. If the book has not closed a position by the time its equity falls below 2/3 "
            "of its maintenance margin, the liquidity vault takes it over at the mark: an isolated position with its "
            "margin; in cross margin, every cross position with the cross margin and the account's free USDC (its open "
            "orders in those markets are cancelled first). Nothing is returned; the vault keeps any profit.") == new["summary"]
    below = liq_rule({**v1, "book_liquidation": False, "isolated_partial_liquidation": True, "pairs": [P(liquidation_fee_bps=40)]})
    assert below["partial_threshold_usdc"] is None and below["backstop_below_maintenance"] is None and below["cooldown_s"] is None
    assert "order book" not in below["summary"] and "vault" not in below["summary"] and "0.4%" in below["summary"]
    bare = liq_rule({"pairs": [P()], "book_liquidation": True})
    assert bare["summary"] == ("A liquidation is closed with an order against the order book. No liquidation fee. "
                               "Any margin left stays with the trader.")


def test_liquidation_share_only_from_its_field():
    """the book rule's threshold without a share field: 'part of it', never an assumed 20%"""
    from vordium_mcp.reads import liq_rule
    r = liq_rule({"pairs": [{"pair": {"kind": "perps"}}], "book_liquidation": True,
                  "liquidation_partial_threshold_6dec": "10000000000", "liquidation_partial_bps": 25000})
    assert r["partial_pct"] is None and "closed part of it first." in r["summary"] and "%" not in r["summary"].split("USDC")[1]
