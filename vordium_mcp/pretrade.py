"""Pre-trade check: every rule the chain runs on a perpetual order, judged from live reads before anything is signed.

One function, used for an order request (it is never sent for approval when a rule fails), for the check_order tool and
for buying power. Units are the chain's own: size × 10^18, price × 10^8, USDC × 10^6, leverage and bands in bps.

The chain's verdict is its intake answer (the intake runs the chain's own placement check on committed state), so a
rule here reads the same committed state the intake does. Two things can still differ at the very edge: the mark used
for the price band and margin (the public read carries 6 decimals, the chain 8), and anything that changes in the
second between this read and the order. Rules the network does not publish enough to judge are listed under
`not_checked`, never guessed.
"""
import math

E18, E8, E6 = 10 ** 18, 10 ** 8, 10 ** 6


def _int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    if isinstance(v, float) and math.isfinite(v):
        return int(v)
    return None


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def units(v, scale):
    """an integer at `scale` decimals as a short decimal string (trailing zeros trimmed)"""
    if v is None:
        return "—"
    neg, v = v < 0, abs(int(v))
    whole, frac = divmod(v, 10 ** scale)
    s = f"{whole:,}" + (("." + str(frac).rjust(scale, "0").rstrip("0")) if frac else "")
    return ("-" if neg else "") + s


def usd(v6):
    return "—" if v6 is None else f"${units(v6, 6)}"


class Unreadable(RuntimeError):
    """a read the check needs did not answer: the check says so instead of judging"""


def liq_mode_of(mstate, ms, pair_id):
    """the account's margin mode for one market, from chain.margin_modes: "none" (the network has no cross
    margin) = isolated; a failed read = unknown; else this market's own mode, or the account default."""
    if mstate == "none":
        return "isolated"
    if mstate != "ok" or not isinstance(ms, dict):
        return "unknown"
    return (ms.get("modes") or {}).get(int(pair_id), ms.get("default")) or "unknown"


class Ctx:
    """Everything the chain's placement check reads, for one owner and one market, read once."""

    def __init__(self, chain, owner, pair_id, agent=None):
        self.owner, self.pair_id, self.agent_addr = owner.lower(), int(pair_id), (agent or "").lower() or None
        need = lambda r, what: chain.require(r, what)
        doc = need(chain.market_specs(), "market rules")
        specs = doc.get("specs") if isinstance(doc.get("specs"), dict) else doc   # the network-wide rules sit under "specs"
        self.specs = specs
        self.pspec = next((p for p in specs.get("pairs") or []
                           if _int((p.get("pair") or {}).get("pair_id", p.get("pair_id"))) == self.pair_id), None)
        if self.pspec is None:
            raise Unreadable(f"market {self.pair_id} is not in the network's market rules")
        perps = need(chain.markets(), "market list")
        self.perp = next((p for p in perps.get("pairs") or [] if _int(p.get("pair_id")) == self.pair_id), {}) or {}
        self.chain_time_ms = _int(perps.get("chain_time_ms"))
        self.status = need(chain.status(cache=False), "network status")
        self.truth = chain.truth().get("json") or {}
        prices = need(chain.oracle(), "prices")
        row = next((p for p in prices.get("prices") or [] if _int(p.get("pair_id")) == self.pair_id), {}) or {}
        m, o = _num(row.get("mark_price")), _num(row.get("oracle_price"))
        self.mark8 = round(m * E8) if m and m > 0 else None
        self.oracle8 = round(o * E8) if o and o > 0 else None
        if self.mark8 is None:
            raise Unreadable(f"the network gives no price for market {self.pair_id} right now")
        book = need(chain.orderbook(self.pair_id), "order book")
        top = lambda k: _int(((book.get(k) or [[None]])[0] or [None])[0])
        self.best_bid8, self.best_ask8 = top("bids_8dec"), top("asks_8dec")   # an empty side stays None (nothing to cross)
        # THIS pair's margin mode for the account. Cross has ONE account-level liquidation price, so the isolated
        # formula below would be wrong for it; a failed read leaves the mode unknown. Either way the figure reads "—".
        self.liq_mode = liq_mode_of(*chain.margin_modes(owner), self.pair_id)
        eq = need(chain.equity(owner), "account balance")
        self.free6 = _int(eq.get("free_balance"))
        if self.free6 is None:
            raise Unreadable("the account's free balance could not be read")
        pos = need(chain.positions(owner), "positions")
        p = next((x for x in (pos if isinstance(pos, list) else pos.get("positions") or []) if _int(x.get("pair_id")) == self.pair_id), None)
        if p:
            sz = _int(p.get("size")) or 0
            self.pos18 = sz if p.get("is_long") else -sz
            self.pos_margin6, self.pos_entry6, self.pos_lev = _int(p.get("margin")), _int(p.get("entry_price")), _num(p.get("leverage"))
        else:
            self.pos18, self.pos_margin6, self.pos_entry6, self.pos_lev = 0, None, None, None
        acct = need(chain.read(f"/markets/account/{owner}", cache=False), "account limits")
        self.open_orders, self.open_cap = _int(acct.get("open_orders")), _int(acct.get("open_orders_cap"))
        req = acct.get("requests") or {}
        self.allowance, self.used = _int(req.get("allowance")), _int(req.get("used"))
        self.agent = None
        if self.agent_addr:
            self.agent = need(chain.agent(owner, self.agent_addr), "agent record")
            if isinstance(self.agent, dict) and "record" in self.agent:
                self.agent = self.agent["record"]

    # the pair's own numbers
    def g(self, k):
        return _int(self.pspec.get(k))

    @property
    def pair(self):
        return self.pspec.get("pair") or {}

    def taker_cbps(self):
        bps = _int(self.pair.get("taker_fee_bps")) or 0
        return bps * 100 if bps > 0 else (_int(self.specs.get("perps_taker_fee_cbps")) or 0)


def _rule(rule, code, words, need=None, have=None):
    return {"rule": rule, "code": code, "words": words, "needed": need, "have": have}


def check(ctx, order, fixes=True):
    """Judge one order. `order`: pair_id, side "buy"|"sell", size_18dec, price_8dec (0 = market), order_type
    "limit"|"market", tif "gtc"|"ioc"|"fok", post_only, reduce_only, leverage_bps. Returns can_place, failed rules
    (needed vs have), not_checked rules, the order's figures and ready fixes."""
    side = order["side"]
    buy = side == "buy"
    size = int(order["size_18dec"])
    price = int(order.get("price_8dec") or 0)
    market = order.get("order_type") == "market" or price == 0
    ro, po = bool(order.get("reduce_only")), bool(order.get("post_only"))
    lev = int(order.get("leverage_bps") or 0)
    fails, unchecked = [], []
    F = fails.append

    # ── the network, the market ──
    st = ctx.status
    if st.get("trading_open") is False or st.get("trading_active") is False:
        F(_rule("trading", "trading_paused", "Trading is not open on the network right now."))
    if st.get("market_frozen") is True and not ro:
        F(_rule("trading", "market_frozen", "New positions are paused on the network right now."))
    pr = ctx.pair
    if pr.get("paused") is True or ctx.perp.get("paused") is True:
        F(_rule("market", "pair_paused", "This market is paused."))
    if pr.get("delisted") is True or ctx.perp.get("delisted") is True:
        F(_rule("market", "pair_delisted", "This market is delisted."))
    life = ctx.perp.get("lifecycle_state")
    lat = _int(pr.get("list_at_timestamp_ms")) or _int(ctx.perp.get("list_at_timestamp_ms")) or 0
    if (life and life != "active") or (lat and ctx.chain_time_ms and lat > ctx.chain_time_ms) or pr.get("active") is False:
        F(_rule("market", "pair_staged", "This market has not opened yet."))
    if ctx.perp.get("reduce_only_mode") is True and not ro:
        F(_rule("market", "pair_reduce_only", "This market only takes reduce-only orders right now."))
    if ctx.perp.get("post_only_mode") is True and not po:
        F(_rule("market", "pair_post_only", "This market only takes post-only orders right now."))
    if _int(ctx.perp.get("allowed_order_types")):
        unchecked.append("allowed order types (the market limits them and the network does not publish how)")
    if market and not ctx.mark8:
        F(_rule("price", "no_price", "This market has no price yet, so a market order cannot be valued."))

    # ── shape ──
    lot, tick = ctx.g("lot_18dec") or 0, ctx.g("tick_8dec") or 0
    if size <= 0:
        F(_rule("size", "zero_size", "The size is zero."))
    if lot and size % lot:
        F(_rule("size", "lot", f"The size must be a multiple of {units(lot, 18)}.", units(size - size % lot, 18), units(size, 18)))
    if not market and tick and price % tick:
        F(_rule("price", "tick", f"The price must be a multiple of {units(tick, 8)}.", units(price - price % tick, 8), units(price, 8)))
    maxlev = ctx.g("max_leverage_bps_effective") or 0
    if lev <= 0 or (maxlev and lev > maxlev):
        F(_rule("leverage", "leverage", f"This market allows up to {maxlev / 10000:g}×.", f"≤ {maxlev / 10000:g}×", f"{lev / 10000:g}×"))

    # ── value and sizes ──
    pval = ctx.mark8 if market else price
    notional6 = size * pval // (E8 * E18 // E6) if pval else None   # size18 × price8 / 10^20
    minn = _int(ctx.specs.get("min_order_notional_6dec")) or 0
    if notional6 is not None and minn and notional6 < minn and not ro:
        F(_rule("value", "min_notional", f"The order is worth less than the smallest order ({usd(minn)}).", usd(minn), usd(notional6)))
    limits_on = ctx.specs.get("order_limits_active") is not False
    mlim, mmkt = ctx.g("max_limit_order_size_18dec") or 0, ctx.g("max_market_order_size_18dec") or 0
    if limits_on and not market and mlim and size > mlim:
        F(_rule("size", "order_size_above_max", f"This market's largest limit order is {units(mlim, 18)}.", units(mlim, 18), units(size, 18)))
    if limits_on and market and mmkt and size > mmkt:
        F(_rule("size", "market_order_above_max", f"This market's largest market order is {units(mmkt, 18)}.", units(mmkt, 18), units(size, 18)))
    maxn = ctx.g("max_order_notional_6dec") or 0
    if limits_on and maxn and notional6 is not None and notional6 > maxn:
        F(_rule("value", "max_notional", f"This market's largest order is worth {usd(maxn)}.", usd(maxn), usd(notional6)))
    if (ctx.g("open_interest_cap_18dec") or 0) and not ro:
        unchecked.append("open-interest cap (the network does not publish open interest in base units)")
    after = ctx.pos18 + (size if buy else -size)
    maxpos = ctx.g("max_account_position_18dec") or 0
    if limits_on and maxpos and not ro and abs(after) > maxpos and abs(after) > abs(ctx.pos18):
        F(_rule("position", "position_limit", f"This market's largest position per account is {units(maxpos, 18)}.",
                units(maxpos, 18), units(abs(after), 18)))

    # ── reduce-only ──
    if ro:
        # measured (truth test): a reduce-only order on the same side as the position gets the same answer as no position
        if ctx.pos18 == 0 or (ctx.pos18 > 0) == buy:
            F(_rule("reduce-only", "reduce_only_nothing_to_reduce", "Reduce-only needs an open position on the other side "
                    "in this market." if ctx.pos18 else "Reduce-only needs an open position in this market."))
        elif size > abs(ctx.pos18):
            unchecked.append("reduce-only larger than the position (the chain takes it; the extra does not fill)")

    # ── margin ──
    res8 = pval
    if not market and pval and ctx.mark8:
        # a marketable order reserves at the worse of its limit and the mark
        crosses = (buy and ctx.best_ask8 is not None and price >= ctx.best_ask8) or (not buy and ctx.best_bid8 is not None and price <= ctx.best_bid8)
        if crosses:
            # measured on the chain's own records: a marketable sell 1% under the mark reserved at the mark, a buy at its limit
            res8 = max(price, ctx.mark8)
    margin6 = 0 if ro or not lev or not res8 else size * res8 * 10000 // (E18 * E8 // E6 * lev)
    fee6 = notional6 * ctx.taker_cbps() // E6 if notional6 is not None else None
    if not ro and ctx.free6 is not None and margin6 > ctx.free6:
        F(_rule("margin", "insufficient_margin", f"The order needs {usd(margin6)} of margin; {usd(ctx.free6)} is free.",
                usd(margin6), usd(ctx.free6)))

    # ── price band and post-only ──
    band = _int(ctx.specs.get("price_band_bps")) or 0
    if not market and band and ctx.specs.get("price_band_active") is not False and ctx.mark8:
        hi, lo = ctx.mark8 * (10000 + band), ctx.mark8 * (10000 - band)
        if (buy and price * 10000 > hi) or (not buy and price * 10000 < lo):
            edge = hi // 10000 if buy else -(-lo // 10000)
            edge -= edge % tick if (buy and tick) else 0
            if not buy and tick and edge % tick:
                edge += tick - edge % tick
            F(_rule("price", "price_band", f"A {'buy' if buy else 'sell'} cannot be priced more than {band / 100:g}% "
                    f"{'above' if buy else 'below'} the mark ({units(ctx.mark8, 8)}).", units(edge, 8), units(price, 8)))
    if po and not market:
        if (buy and ctx.best_ask8 and price >= ctx.best_ask8) or (not buy and ctx.best_bid8 and price <= ctx.best_bid8):
            F(_rule("post-only", "post_only_would_cross", "A post-only order cannot trade at once against the book.",
                    f"{'below' if buy else 'above'} {units(ctx.best_ask8 if buy else ctx.best_bid8, 8)}", units(price, 8)))

    # ── counts ──
    if ctx.open_orders is not None and ctx.open_cap and ctx.open_orders >= ctx.open_cap:
        F(_rule("open orders", "too_many_open_orders", f"The account already has {ctx.open_cap:,} open orders.", f"< {ctx.open_cap:,}", f"{ctx.open_orders:,}"))
    if ctx.allowance is not None and ctx.used is not None and ctx.used + 1 > ctx.allowance:
        F(_rule("requests", "rate_limited", "The account's request allowance is used up (it grows by 1 per USDC traded).",
                f"≤ {ctx.allowance:,}", f"{ctx.used + 1:,}"))

    # ── the agent that signs it ──
    if ctx.agent_addr:
        _agent_rules(ctx, order, fails, unchecked, size, after, market, notional6, lev, buy, ro)

    figures = {"notional": usd(notional6), "notional_6dec": notional6, "margin": usd(margin6), "margin_6dec": margin6,
               "fee_est": usd(fee6), "fee_6dec": fee6, "position_after": units(after, 18),
               "mark": units(ctx.mark8, 8) if ctx.mark8 else "—", "free": usd(ctx.free6)}
    figures.update(_liq_after(ctx, after, buy, size, res8, margin6, ro))
    out = {"can_place": not fails, "failed": fails, "not_checked": unchecked, "figures": figures}
    out["fixes"] = _fixes(ctx, order, fails) if fails and fixes else []
    if not fails and figures.get("liq_distance_pct") is not None and figures["liq_distance_pct"] < 3:
        out["warning"] = (f"After this fill the mark would already be at or past the liquidation price {figures['liq_price']}."
                          if figures["liq_distance_pct"] <= 0 else
                          f"After this fill the liquidation price would be {figures['liq_price']}: the mark can move only "
                          f"{figures['liq_distance_pct']:.1f}% against you first.")
    return out


def _agent_rules(ctx, order, F, unchecked, size, after, market, notional6, lev, buy, ro):
    a, add = ctx.agent or {}, F.append
    now = ctx.chain_time_ms
    pol = a.get("policy_v1")
    if not a.get("found", True) or a.get("status") != "active" or a.get("expired_by_head_time") or not _int(a.get("expires_ms")):
        add(_rule("agent", "agent_refused/registration_not_live", "The agent is not live on the network (ended, stopped or never registered)."))
        return
    if (ctx.truth.get("agent_policy_active") is False):
        unchecked.append("agent policy (the network's agent policy switch is off)")
        return
    if not pol:
        add(_rule("agent", "agent_refused/no_policy", "The agent has no limits on the network, so it may not trade."))
        return
    if pol.get("enabled") is False:
        add(_rule("agent", "agent_refused/policy_disabled", "The agent's limits are switched off."))
    pexp = _int(pol.get("expires_ms")) or 0
    if pexp and now and pexp <= now:
        add(_rule("agent", "agent_refused/policy_expired", "The agent's limits have ended."))
    caps = _int(pol.get("caps_bitmask")) or 0
    if not (caps & (16 if ro else 1)):
        add(_rule("agent", "agent_refused/capability_not_granted", f"The agent may not place {'reduce-only' if ro else 'new'} orders."))
    ml = next((m for m in a.get("market_limits") or [] if _int(m.get("pair")) == ctx.pair_id and m.get("allowed")), None)
    if not ml:
        add(_rule("agent", "agent_refused/market_not_allowed", "The agent may not trade this market."))
        return
    f, t = _int(pol.get("active_from_ms_of_day")) or 0, _int(pol.get("active_to_ms_of_day")) or 0
    if (f or t) and now:
        d = now % 86400000
        inside = (f <= d < t) if f < t else (d >= f or d < t)
        if not inside:
            add(_rule("agent", "agent_refused/outside_active_window", "The agent may only trade at set hours of the day."))
    # reducing: reduce-only, or the position keeps its side and shrinks (a flip through zero opens the other side)
    reducing = ro or (after * ctx.pos18 >= 0 and abs(after) < abs(ctx.pos18))
    if (_int(pol.get("side_mode")) or 0) == 1 and not reducing:
        add(_rule("agent", "agent_refused/reduce_only_or_side", "The agent may only reduce positions."))
    mo = _int(ml.get("max_order_18dec")) or 0
    if mo and size > mo:
        add(_rule("agent", "agent_refused/max_order", f"The agent's largest order here is {units(mo, 18)}.", units(mo, 18), units(size, 18)))
    mp = _int(ml.get("max_position_18dec")) or 0
    if mp and abs(after) > mp and abs(after) > abs(ctx.pos18):
        add(_rule("agent", "agent_refused/max_position", f"The agent's largest position here is {units(mp, 18)}.", units(mp, 18), units(abs(after), 18)))
    pl, mlv = _int(pol.get("max_leverage_bps")) or 0, _int(ml.get("max_leverage_bps")) or 0
    cap = min(x for x in (pl, mlv) if x) if (pl or mlv) else 0
    if cap and lev > cap:
        add(_rule("agent", "agent_refused/max_leverage", f"The agent may use up to {cap / 10000:g}× here.", f"≤ {cap / 10000:g}×", f"{lev / 10000:g}×"))
    ex = _int(pol.get("max_total_exposure_6dec")) or 0
    if ex and notional6 is not None and notional6 > ex:
        add(_rule("agent", "agent_refused/max_total_exposure", f"The agent's total exposure is capped at {usd(ex)}.", usd(ex), usd(notional6)))
    elif ex:
        unchecked.append("agent total exposure with the account's other positions (the network does not publish how it is summed)")
    risk = a.get("risk_v1") or {}
    if risk.get("lock_active") and not reducing:
        add(_rule("agent", "agent_refused/locked_reduce_only", "The agent is locked to reducing positions (its daily loss "
                  "or fee limit was reached)."))
    today = (now // 86400000) if now else None
    same_day = today is not None and _int(risk.get("day")) == today           # yesterday's counters do not count today
    dl, fc = _int(pol.get("daily_loss_limit_6dec")) or 0, _int(pol.get("daily_fee_cap_6dec")) or 0
    lost = -(_int(risk.get("realised_pnl_day_6dec")) or 0) if same_day else 0
    if dl and lost >= dl and not reducing:
        add(_rule("agent", "agent_refused/daily_loss_limit", f"The agent reached its daily loss limit ({usd(dl)}).", usd(dl), usd(lost)))
    fees = (_int(risk.get("fees_day_6dec")) or 0) if same_day else 0
    if fc and fees >= fc and not reducing:
        add(_rule("agent", "agent_refused/daily_fee_cap", f"The agent reached its daily fee limit ({usd(fc)}).", usd(fc), usd(fees)))
    acc = a.get("accounting") or {}
    opw, win = _int(pol.get("ops_per_window")) or 0, _int(pol.get("ops_window_ms")) or 0
    if opw and win and now and _int(acc.get("ops_count_window")) is not None:
        start = _int(acc.get("window_start_ms")) or 0
        if now < start + win and _int(acc.get("ops_count_window")) >= opw:
            add(_rule("agent", "agent_refused/rate_limit", f"The agent used its {opw:,} actions for this window.", f"< {opw:,}", f"{_int(acc.get('ops_count_window')):,}"))
    band = _int(pol.get("oracle_band_bps")) or 0
    if band and not market and ctx.oracle8:
        if abs(int(order.get("price_8dec") or 0) - ctx.oracle8) * 10000 > ctx.oracle8 * band:
            add(_rule("agent", "agent_refused/price_outside_band", f"The agent may only price within {band / 100:g}% of the oracle price ({units(ctx.oracle8, 8)}).",
                      f"{units(ctx.oracle8 * (10000 - band) // 10000, 8)}–{units(ctx.oracle8 * (10000 + band) // 10000, 8)}", units(int(order.get('price_8dec') or 0), 8)))


def _liq_after(ctx, after, buy, size, res8, margin6, ro):
    """The liquidation price after a full fill (the isolated formula the network shows on positions) and its distance."""
    mm = (ctx.g("maintenance_margin_bps") or 0) / 10000
    if not after or not res8 or not ctx.mark8 or ro or getattr(ctx, 'liq_mode', 'isolated') != 'isolated':
        return {"liq_price": "—", "liq_distance_pct": None}
    add = size if (after > 0) == buy else 0
    old = abs(ctx.pos18) if (after > 0) == (ctx.pos18 > 0) else 0
    entry = ((ctx.pos_entry6 or 0) / E6 * old / E18 + res8 / E8 * add / E18) / ((old + add) / E18) if (old + add) else res8 / E8
    margin = ((ctx.pos_margin6 or 0) if old else 0) / E6 + (margin6 / E6 if add else 0)
    s = abs(after) / E18
    p = (entry * s - margin) / (s * (1 - mm)) if after > 0 else (entry * s + margin) / (s * (1 + mm))
    mark = ctx.mark8 / E8
    if p <= 0:
        return {"liq_price": "None", "liq_distance_pct": None}
    # signed like get_account: how far the mark can move against the position first (0 or less: at or past it)
    return {"liq_price": f"{p:,.6g}", "liq_distance_pct": round(((mark - p) if after > 0 else (p - mark)) / mark * 100, 2)}


def max_size(ctx, side, lev_bps, price8=0, agent=True):
    """The largest size that passes the size, position, margin and agent limits at this leverage (0 when none)."""
    buy = side == "buy"
    p8 = price8 or ctx.mark8 or 0
    lot = ctx.g("lot_18dec") or 1
    if not p8 or not lev_bps:
        return 0
    caps = []
    if ctx.free6 is not None:
        caps.append(ctx.free6 * (E18 * E8 // E6) * lev_bps // (p8 * 10000))
    lim = ctx.g("max_market_order_size_18dec" if not price8 else "max_limit_order_size_18dec") or 0
    if lim:
        caps.append(lim)
    maxn = ctx.g("max_order_notional_6dec") or 0
    if maxn:
        caps.append(maxn * (E18 * E8 // E6) // p8)
    room = lambda cap: cap - ctx.pos18 if buy else cap + ctx.pos18
    mp = ctx.g("max_account_position_18dec") or 0
    if mp:
        caps.append(max(0, room(mp)))
    if agent and ctx.agent_addr:
        ml = next((m for m in (ctx.agent or {}).get("market_limits") or [] if _int(m.get("pair")) == ctx.pair_id and m.get("allowed")), None)
        if not ml:
            return 0
        if _int(ml.get("max_order_18dec")):
            caps.append(_int(ml["max_order_18dec"]))
        if _int(ml.get("max_position_18dec")):
            caps.append(max(0, room(_int(ml["max_position_18dec"]))))
        ex = _int(((ctx.agent or {}).get("policy_v1") or {}).get("max_total_exposure_6dec")) or 0
        if ex:
            caps.append(ex * (E18 * E8 // E6) // p8)
    m = min(caps) if caps else 0
    return max(0, m - m % lot)


def leverage_cap(ctx):
    """The highest leverage an order here may use (the market's, lowered by the agent's limits)."""
    cap = ctx.g("max_leverage_bps_effective") or 0
    if ctx.agent_addr:
        a = ctx.agent or {}
        pol = a.get("policy_v1") or {}
        ml = next((m for m in a.get("market_limits") or [] if _int(m.get("pair")) == ctx.pair_id and m.get("allowed")), None)
        for x in (_int(pol.get("max_leverage_bps")), _int((ml or {}).get("max_leverage_bps"))):
            if x:
                cap = min(cap, x) if cap else x
    return cap


def _fix_list(ctx, order, fails):
    """the changes each failed rule suggests (a full order each, or None for a change outside the order)"""
    size, lev = int(order["size_18dec"]), int(order.get("leverage_bps") or 0)
    market = order.get("order_type") == "market" or int(order.get("price_8dec") or 0) == 0
    lot, maxlev = ctx.g("lot_18dec") or 0, ctx.g("max_leverage_bps_effective") or 0
    codes = {f["code"] for f in fails}
    out, base = [], dict(order)
    cap = leverage_cap(ctx)
    if codes & {"leverage", "agent_refused/max_leverage"} and cap:
        out.append({"label": f"use {cap / 10000:g}× ({'agent limit' if ctx.agent_addr and cap < maxlev else 'market limit'})",
                    "order": {**base, "leverage_bps": cap}})
    use_lev = min(lev, cap) if cap and lev else (cap or lev)
    big = codes & {"order_size_above_max", "market_order_above_max", "max_notional", "position_limit", "insufficient_margin",
                   "agent_refused/max_order", "agent_refused/max_position", "agent_refused/max_total_exposure"}
    if big or (codes & {"leverage", "agent_refused/max_leverage"}):
        ms = max_size(ctx, order["side"], use_lev, 0 if market else int(order.get("price_8dec") or 0))
        if ms > 0 and ms < size:
            out.append({"label": f"max size at {use_lev / 10000:g}×: {units(ms, 18)}", "order": {**base, "size_18dec": str(ms), "leverage_bps": use_lev}})
    if "insufficient_margin" in codes:
        need = next(f for f in fails if f["code"] == "insufficient_margin")
        pval = ctx.mark8 if market else int(order.get("price_8dec") or 0)
        m6 = size * pval * 10000 // (E18 * E8 // E6 * (use_lev or 1)) if pval else 0
        short = max(0, m6 - (ctx.free6 or 0))
        if short:
            out.append({"label": f"deposit {usd(short)} more", "order": None})
    if "lot" in codes and lot:
        out.append({"label": f"size {units(size - size % lot, 18)}", "order": {**base, "size_18dec": str(size - size % lot)}})
    if "tick" in codes:
        tick = price_step(ctx)
        p = int(order.get("price_8dec") or 0)
        out.append({"label": f"price {units(p - p % tick, 8)}", "order": {**base, "price_8dec": str(p - p % tick)}})
    if codes & {"price_band", "agent_refused/price_outside_band"} and not market:
        p = _price_inside(ctx, order["side"])
        if p:
            out.append({"label": f"limit {units(p, 8)} (inside the price band)", "order": {**base, "price_8dec": str(p)}})
    if "min_notional" in codes:
        pval = ctx.mark8 if market else int(order.get("price_8dec") or 0)
        minn = _int(ctx.specs.get("min_order_notional_6dec")) or 0
        if pval:
            need = -(-minn * (E18 * E8 // E6) // pval)
            need += (lot - need % lot) % lot if lot else 0
            out.append({"label": f"size {units(need, 18)} (the smallest order)", "order": {**base, "size_18dec": str(need)}})
    if "reduce_only_nothing_to_reduce" in codes or "reduce_only_would_grow" in codes:
        out.append({"label": "not reduce-only", "order": {**base, "reduce_only": False}})
    if "post_only_would_cross" in codes:
        out.append({"label": "not post-only (it may trade at once)", "order": {**base, "post_only": False}})
    return out


def _fixes(ctx, order, fails):
    """Ready changes from the failed rules, each judged again (passes), and — when one alone is not enough — one order
    with the fixes applied step by step (each step works out its fixes at the values the step before set), when it passes."""
    out = _fix_list(ctx, order, fails)
    for f in out:
        f["passes"] = bool(f["order"]) and check_quiet(ctx, f["order"])
    if not any(f["passes"] for f in out):
        m, labels = dict(order), []
        for _ in range(4):
            r = check(ctx, m, fixes=False)
            if r["can_place"]:
                break
            step = [f for f in _fix_list(ctx, m, r["failed"]) if f["order"]]
            if not step:
                break
            for f in step:
                m.update({k: v for k, v in f["order"].items() if v != m.get(k)})
                labels.append(f["label"])
        if labels and check_quiet(ctx, m):
            out.insert(0, {"label": "together: " + "; ".join(_describe_change(order, m)), "order": m, "passes": True})
    return out


def _describe_change(a, b):
    """what changed between two orders, in words, in the final values"""
    out = []
    if b.get("leverage_bps") != a.get("leverage_bps"):
        out.append(f"{int(b['leverage_bps']) / 10000:g}×")
    if str(b.get("price_8dec")) != str(a.get("price_8dec")):
        out.append(f"limit {units(int(b['price_8dec']), 8)}")
    if str(b.get("size_18dec")) != str(a.get("size_18dec")):
        out.append(f"size {units(int(b['size_18dec']), 18)}")
    for k, w in (("reduce_only", "reduce-only"), ("post_only", "post-only")):
        if bool(b.get(k)) != bool(a.get(k)):
            out.append(("" if b.get(k) else "not ") + w)
    return out


def check_quiet(ctx, order):
    """whether an order passes (no fixes worked out: used to judge the fixes themselves)"""
    return check(ctx, order, fixes=False)["can_place"]


def price_step(ctx):
    """the price step a limit order can be sent on by every route: the tick, and at most 6 decimals"""
    t = ctx.g("tick_8dec") or 1
    return t * 100 // math.gcd(t, 100)


def snap(ctx, order):
    """the order as a signer sends it: the size down onto the lot; a buy's price down and a sell's up onto the price step"""
    o = dict(order)
    lot = ctx.g("lot_18dec") or 1
    o["size_18dec"] = int(o["size_18dec"]) - int(o["size_18dec"]) % lot
    p, st = int(o.get("price_8dec") or 0), price_step(ctx)
    if p and o.get("order_type") != "market":
        o["price_8dec"] = p - p % st if o["side"] == "buy" else p + (st - p % st) % st
    return o


def _price_inside(ctx, side):
    """the furthest limit price the chain's band and the agent's oracle band both allow, on the price step"""
    buy, tick = side == "buy", price_step(ctx)
    edges = []
    band = _int(ctx.specs.get("price_band_bps")) or 0
    if band and ctx.mark8:
        edges.append(ctx.mark8 * (10000 + band) // 10000 if buy else -(-ctx.mark8 * (10000 - band) // 10000))
    ab = _int(((ctx.agent or {}).get("policy_v1") or {}).get("oracle_band_bps")) or 0
    if ctx.agent_addr and ab and ctx.oracle8:
        edges.append(ctx.oracle8 * (10000 + ab) // 10000 if buy else -(-ctx.oracle8 * (10000 - ab) // 10000))
    if not edges:
        return None
    p = min(edges) if buy else max(edges)
    return p - p % tick if buy else p + (tick - p % tick) % tick


def _to8(text):
    whole, _, frac = text.replace(",", "").partition(".")
    return int(whole) * E8 + int((frac + "0" * 8)[:8] or 0)
