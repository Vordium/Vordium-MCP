"""Read tools added for the hosted server: status, fees, candles, trades, accounts, agents, rewards and the explorer.

Same rules as tools.py: every figure is read from the network's public addresses when the tool is called; a value
that cannot be read is reported as not available, never filled in. Each result carries ``summary`` — one plain
sentence a person can read — next to the raw fields and the units they are in.
"""
import re
import time

from .chain import ChainUnreachable

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")

LABEL = {"testnet": "The Vordium testnet", "mainnet": "Vordium mainnet"}
RESOLUTIONS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1D", "3D", "1W", "1M")


# ── formatting ───────────────────────────────────────────────────────────────
def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def scaled(v, scale):
    try:
        t = str(v).strip()
        return int(t, 16 if t.lower().startswith("0x") else 10) / scale
    except (TypeError, ValueError):
        return num(v)


def usd(v, compact=False):
    v = num(v)
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if compact and a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if compact and a >= 1e6:
        return f"{sign}${a / 1e6:.2f}M"
    if compact and a >= 1e4:
        return f"{sign}${a / 1e3:.1f}K"
    if a >= 1000:
        return f"{sign}${a:,.0f}" if a >= 10000 else f"{sign}${a:,.2f}"
    if a >= 1:
        return f"{sign}${a:,.2f}"
    return f"{sign}${a:.6g}"


def amount(v, digits=4):
    v = num(v)
    if v is None:
        return "—"
    t = f"{v:,.{digits}f}"
    return t.rstrip("0").rstrip(".") if "." in t else t


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def pct(v, digits=4):
    v = num(v)
    return "—" if v is None else f"{v:+.{digits}f}%"


def short(a):
    a = str(a or "")
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def source(*reads):
    return [{"url": r["url"], "status": r["status"], "read_at": r["read_at"]} for r in reads if r]


class NotServed(Exception):
    """The network does not serve this read (an answer of 404, or a route it does not know yet)."""


def _doc(read, what):
    if read is None:
        raise NotServed(f"{what} is not published for this network")
    if read["status"] in (404, 400) or (read["status"] == 200 and read["json"] is None):
        raise NotServed(f"the network does not serve {what} ({read['status']})")
    if not read.ok:
        raise ChainUnreachable(f"{what}: {read['url']} answered {read['status']}")
    return read["json"]


def unavailable(chain, what, why, reads=()):
    return {"available": False, "network": chain.network, "summary": f"{LABEL[chain.network]} does not serve {what} "
            "now. Nothing is filled in from memory.", "reason": why, "source": source(*reads)}


def explorer_link(chain, kind, ref):
    base = chain.links.get("explorer")
    return f"{base}/{kind}/{ref}" if base else None


def _identity(chain):
    from .tools import network_identity
    return network_identity(chain)


def _pair(chain, params):
    from .tools import _pair as pair
    return pair(chain, params)


def _address(params, key="address"):
    from .tools import _address as addr
    return addr(params, key)


def _int(params, key, default, lo, hi):
    v = params.get(key, default)
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError, OverflowError):
        return default


# ── network ──────────────────────────────────────────────────────────────────
def get_status(chain, cfg, params):
    _identity(chain)
    st = chain.status()
    s = chain.require(st, "node status")
    tr = chain.truth(cache=True)
    t = tr["json"] if tr.ok and isinstance(tr["json"], dict) and tr["json"].get("ok") is True else {}
    rate = num(s.get("block_rate_10s"))
    live = s.get("sync_state") == "live" and (rate or 0) > 0
    seated = (t.get("validators") or {}).get("seated")
    agreeing = (t.get("committed") or {}).get("agreed_by")
    inactive = s.get("inactive_validators")
    out = {
        "network": chain.network, "live": live, "sync_state": s.get("sync_state"),
        "height": s.get("chain_head"), "finalized_height": t.get("finalized_height"),
        "blocks_per_second": round(rate, 1) if rate is not None else None,
        "validators_seated": seated, "validators_agreeing": agreeing,
        "validators_inactive_count": len(inactive) if isinstance(inactive, list) else None,
        "trading_open": s.get("trading_open"), "trading_blockers": s.get("trading_blockers"),
        "deposits_open": s.get("deposit_door_open"), "oracle_healthy": s.get("oracle_healthy"),
        "agent_trading_on": t.get("agent_policy_active"), "version": s.get("version"),
        "units": "height in blocks; blocks_per_second measured over the last 10 seconds",
        "source": source(st, tr)}
    parts = [f"{LABEL[chain.network]} is {'live' if live else 'not live (' + str(s.get('sync_state')) + ')'}"]
    if isinstance(s.get("chain_head"), int):
        parts.append(f"block {s['chain_head']:,}")
    if rate is not None:
        parts.append(f"{rate:.1f} blocks/s")
    if isinstance(agreeing, int) and isinstance(seated, int):
        parts.append(f"{agreeing} of {seated} validators agreeing")
    tail = {True: "trading open", False: "trading closed"}.get(s.get("trading_open"), "trading state not published")
    out["summary"] = ": ".join([parts[0], ", ".join(parts[1:])]) + f"; {tail}." if len(parts) > 1 else parts[0] + "."
    return out


def _fee_rates(spec, specs, pairs_doc, pid):
    """Maker and taker fees in bps for one market, in the order the app resolves them: a market's own non-zero rate in
    the chain's market specs wins; else the chain's default perpetuals rate; else the spec's effective rate."""
    s = (specs or {}).get("specs") or {}
    d = None
    if isinstance(s.get("perps_taker_fee_cbps"), int) and isinstance(s.get("perps_maker_fee_cbps"), int):
        d = (s["perps_taker_fee_cbps"] / 100, s["perps_maker_fee_cbps"] / 100)
    entry = next((x for x in s.get("pairs") or [] if (x.get("pair") or {}).get("pair_id") == pid), None)
    sp = (entry or {}).get("pair") or {}
    taker = maker = None
    markets = spec.get("markets") if isinstance(spec, dict) else None
    if isinstance(markets, list) and markets:
        m = next((x for x in markets if x.get("id") == pid), None) or {}
        eff = m.get("effective") or {}
        taker = m.get("takerBps") if num(m.get("takerBps")) is not None else (d[0] if d else eff.get("takerBps"))
        maker = m.get("makerBps") if num(m.get("makerBps")) is not None else (d[1] if d else eff.get("makerBps"))
    else:
        p = next((x for x in (pairs_doc or {}).get("pairs") or [] if x.get("pair_id") == pid), {})
        taker = d[0] if d else p.get("taker_fee_bps")
        maker = d[1] if d else None
    if num(sp.get("maker_fee_bps")) not in (None, 0):
        maker = sp["maker_fee_bps"]
    if (num(sp.get("taker_fee_bps")) or 0) > 0:
        taker = sp["taker_fee_bps"]
    return num(taker), num(maker), entry


def get_fees(chain, cfg, params):
    _identity(chain)
    spr, msr, pr = chain.spec(), chain.market_specs(), chain.markets()
    spec = spr["json"] if spr.ok and isinstance(spr["json"], dict) else {}
    specs = msr["json"] if msr.ok and isinstance(msr["json"], dict) else {}
    pairs_doc = chain.require(pr, "market list")
    want = None
    if params.get("symbol") or params.get("pair_id") is not None:
        want = _pair(chain, params).get("pair_id")
    mspec = {m.get("id"): m for m in spec.get("markets") or []} if isinstance(spec.get("markets"), list) else {}
    rows = []
    for p in pairs_doc.get("pairs") or []:
        pid = p.get("pair_id")
        if want is not None and pid != want:
            continue
        taker, maker, entry = _fee_rates(spec, specs, pairs_doc, pid)
        e = entry or {}
        lev_bps = num(e.get("max_leverage_bps_effective"))
        lev = (lev_bps / 10000 if lev_bps else None) or num(((mspec.get(pid) or {}).get("effective") or {}).get("maxLeverageX")) \
            or num(p.get("max_leverage_x"))
        mm = num(e.get("maintenance_margin_bps") if e else p.get("maintenance_margin_bps"))
        lf = num(e.get("liquidation_fee_bps"))
        rows.append({"market": p.get("symbol"), "pair_id": pid, "state": p.get("lifecycle_state"),
                     "taker_fee_pct": round(taker / 100, 4) if taker is not None else None,
                     "maker_fee_pct": round(maker / 100, 4) if maker is not None else None,
                     "max_leverage_x": round(lev, 2) if lev else None,
                     "initial_margin_pct": round(100 / lev, 2) if lev else None,
                     "maintenance_margin_pct": round(mm / 100, 2) if mm is not None else None,
                     "liquidation_fee_pct": round(lf / 100, 2) if lf is not None else None,
                     "min_order_usdc": scaled((specs.get("specs") or {}).get("min_order_notional_6dec"), 10 ** 6),
                     "tick": scaled(e.get("tick_8dec"), 10 ** 8) or None, "lot": scaled(e.get("lot_18dec"), 10 ** 18) or None})
    if want is not None and not rows:
        raise NotServed("no fees for that market")
    out = {"network": chain.network, "count": len(rows), "markets": rows,
           "withdrawal_fee_usdc": scaled(((specs.get("specs") or {}).get("withdrawal_fee_6dec")), 10 ** 6),
           "funding_interval_secs": (specs.get("specs") or {}).get("funding_interval_secs"),
           "units": "fees, margins and the liquidation fee in percent of notional; a negative maker fee is a rebate; "
                    "initial margin is 1 / maximum leverage; — or null means the network publishes no value",
           "read_at": msr.get("read_at"), "source": source(spr, msr, pr)}
    if len(rows) == 1:
        r = rows[0]
        out["summary"] = (f"{r['market']}: taker {r['taker_fee_pct']}%, maker {r['maker_fee_pct']}%, up to "
                          f"{r['max_leverage_x']}x, maintenance margin {r['maintenance_margin_pct']}%, liquidation fee "
                          f"{r['liquidation_fee_pct']}%.")
    else:
        takers = sorted({r["taker_fee_pct"] for r in rows if r["taker_fee_pct"] is not None})
        levs = [r["max_leverage_x"] for r in rows if r["max_leverage_x"]]
        out["summary"] = (f"{len(rows)} markets on the {chain.network}: taker fee "
                          f"{'/'.join(str(t) for t in takers) or '—'}%, leverage up to {max(levs) if levs else '—'}x. "
                          "Each market's row has its maker fee, margins and liquidation fee.")
    return out


# ── markets: candles, trades, funding history ────────────────────────────────
def get_candles(chain, cfg, params):
    _identity(chain)
    pair = _pair(chain, params)
    res = str(params.get("resolution") or "1h")
    if res not in RESOLUTIONS:
        from .tools import Refusal
        raise Refusal(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    kind = str(params.get("price") or "mark").lower()
    if kind not in ("mark", "last"):
        from .tools import Refusal
        raise Refusal("price must be mark or last")
    limit = _int(params, "limit", 24, 1, 500)
    r = chain.read_link("candles", f"/candles?pair={pair['pair_id']}&type={kind}&res={res}&limit={limit}", ttl=5.0)
    try:
        doc = _doc(r, "candles")
    except NotServed as e:
        return unavailable(chain, "candles", str(e), [r])
    bars = [{"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(b.get("t", 0))), "open": b.get("o"),
             "high": b.get("h"), "low": b.get("l"), "close": b.get("c"), "volume": b.get("v"), "trades": b.get("n")}
            for b in doc.get("bars") or []][-limit:]
    out = {"network": chain.network, "market": pair.get("symbol"), "pair_id": pair.get("pair_id"), "price": kind,
           "resolution": res, "count": len(bars), "candles": bars,
           "units": "time is each candle's start (UTC); prices in USD; volume in the market's base asset; oldest first, "
                    "the last candle still open", "source": source(r)}
    if bars:
        o, c = num(bars[0]["open"]), num(bars[-1]["close"])
        hi = max((num(b["high"]) or 0) for b in bars)
        lo = min((num(b["low"]) for b in bars if num(b["low"]) is not None), default=None)
        ch = ((c - o) / o * 100) if o and c is not None else None
        out["summary"] = (f"{pair.get('symbol')} {kind} price over {len(bars)} × {res}: {usd(o)} → {usd(c)} "
                          f"({pct(ch, 2)}), high {usd(hi)}, low {usd(lo)}.")
    else:
        out["summary"] = f"{pair.get('symbol')}: the network has no {res} candles for this market yet."
    return out


def get_trades(chain, cfg, params):
    _identity(chain)
    pair = _pair(chain, params)
    limit = _int(params, "limit", 20, 1, 100)
    r = chain.read(f"/trades/{int(pair['pair_id'])}?limit={limit}", cache=False)
    doc = chain.require(r, "recent trades")
    rows = []
    for t in (doc.get("trades") or [])[:limit]:
        rows.append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t.get("timestamp") or 0))),
                     "side": t.get("side"), "price": num(t.get("price")), "size": num(t.get("size")),
                     "value_usd": round(num(t.get("price")) * num(t.get("size")), 2)
                     if num(t.get("price")) is not None and num(t.get("size")) is not None else None,
                     "type": t.get("type"), "liquidation": t.get("is_liquidation"), "fill_id": t.get("fill_id"),
                     "taker": t.get("taker"), "maker": t.get("maker")})
    out = {"network": chain.network, "market": pair.get("symbol"), "pair_id": pair.get("pair_id"), "count": len(rows),
           "total_recorded": doc.get("total_count"), "trades": rows,
           "units": "price in USD, size in the market's base asset; side is the taker's; newest first",
           "source": source(r)}
    if rows:
        vol = sum(x["value_usd"] or 0 for x in rows)
        out["summary"] = (f"{pair.get('symbol')}: last trade {rows[0]['side']} {amount(rows[0]['size'])} at "
                          f"{usd(rows[0]['price'])} ({rows[0]['time']}); the {len(rows)} most recent trades total "
                          f"{usd(vol, True)}.")
    else:
        out["summary"] = f"{pair.get('symbol')}: no trades recorded on the {chain.network} yet."
    return out


def funding_history(chain, pair, n):
    """The last n funding settlements. The route's rate_bps field carries the rate × 10^8 (the same unit as the
    current read's rate_8dec), so it is converted as such and named for what it is."""
    r = chain.read(f"/funding/history/{int(pair['pair_id'])}?limit={n}")
    doc = chain.require(r, "funding history")
    rows = []
    for b in (doc.get("bars") or [])[-n:]:
        rate8 = scaled(b.get("rate_bps"), 1)
        rows.append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(b.get("t") or 0) / 1000)),
                     "rate_pct": round(rate8 / 10 ** 6, 6) if rate8 is not None else None})
    return rows, r


# ── accounts ─────────────────────────────────────────────────────────────────
def get_account(chain, cfg, params):
    _identity(chain)
    a = _address(params)
    eq, pos, ords = chain.equity(a), chain.positions(a), chain.orders(a)
    e = chain.require(eq, "account balances")
    plist = chain.require(pos, "positions")
    olist = chain.require(ords, "open orders")
    plist = plist if isinstance(plist, list) else (plist.get("positions") or [])
    olist = olist if isinstance(olist, list) else (olist.get("orders") or [])
    pairs = {p.get("pair_id"): p for p in chain.pairs()}
    o = chain.oracle()
    marks = {}
    if o.ok:
        od = o["json"]
        for row in (od.get("prices") if isinstance(od, dict) else od) or []:
            marks[row.get("pair_id")] = num(row.get("mark_price"))
    positions = []
    for p in plist:
        size = scaled(p.get("size"), 10 ** 18)
        entry = scaled(p.get("entry_price"), 10 ** 6)
        upnl = scaled(p.get("unrealized_pnl"), 10 ** 6)
        liq = scaled(p.get("liquidation_price"), 10 ** 6)
        mark = marks.get(p.get("pair_id"))
        dist = abs(mark - liq) / mark * 100 if mark and liq else None
        positions.append({"market": (pairs.get(p.get("pair_id")) or {}).get("symbol"), "pair_id": p.get("pair_id"),
                          "side": "long" if p.get("is_long") else "short", "size": size, "entry_price": entry,
                          "mark_price": mark, "unrealized_pnl_usd": upnl, "margin_usd": scaled(p.get("margin"), 10 ** 6),
                          "leverage": p.get("leverage"), "liquidation_price": liq or None,
                          "distance_to_liquidation_pct": round(dist, 2) if dist is not None else None})
    orders = [{"order_id": x.get("id"), "market": (pairs.get(x.get("pair_id")) or {}).get("symbol"),
               "side": x.get("side"), "type": x.get("order_type"), "price": scaled(x.get("price"), 10 ** 6),
               "size": scaled(x.get("remaining_18dec") or x.get("size"), 10 ** 18), "reduce_only": x.get("reduce_only"),
               "post_only": x.get("post_only"), "status": x.get("status")} for x in olist]
    bal = {"account_value": scaled(e.get("total_equity"), 10 ** 6), "available_to_trade": scaled(e.get("free_balance"), 10 ** 6),
           "in_positions": scaled(e.get("in_positions"), 10 ** 6), "reserved_by_orders": scaled(e.get("reserved_orders"), 10 ** 6),
           "unrealized_pnl": scaled(e.get("unrealized_pnl"), 10 ** 6)}
    out = {"network": chain.network, "address": a, "usdc": bal, "positions": positions, "orders": orders,
           "explorer": explorer_link(chain, "address", a),
           "units": "USD / USDC; sizes in each market's base asset; distance_to_liquidation_pct is how far the mark "
                    "price is from the liquidation price; realized PnL per trade is in get_fills",
           "source": source(eq, pos, ords, o)}
    risk = min((p for p in positions if p["distance_to_liquidation_pct"] is not None),
               key=lambda p: p["distance_to_liquidation_pct"], default=None)
    s = (f"{short(a)}: account value {usd(bal['account_value'])}, {usd(bal['available_to_trade'])} available, "
         f"{len(positions)} position{'s' if len(positions) != 1 else ''}, {len(orders)} open order"
         f"{'s' if len(orders) != 1 else ''}, unrealized PnL {usd(bal['unrealized_pnl'])}.")
    if risk:
        s += (f" Closest to liquidation: {risk['market']} {risk['side']} at {usd(risk['liquidation_price'])}, "
              f"{risk['distance_to_liquidation_pct']}% from the mark.")
    out["summary"] = s
    return out


def get_fills(chain, cfg, params):
    _identity(chain)
    a = _address(params)
    limit = _int(params, "limit", 20, 1, 50)
    r = chain.read_link("explorer", f"/api/account/{a.lower()}/trades?limit={limit}", cache=False)
    try:
        doc = _doc(r, "account trade history")
    except NotServed as e:
        return unavailable(chain, "account trade history", str(e), [r])
    pairs = {p.get("pair_id"): p for p in chain.pairs()}
    rows = []
    for t in (doc.get("items") or [])[:limit]:
        row = t.get("row") or {}
        rows.append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t.get("timestampSecs") or 0))),
                     "market": (pairs.get(t.get("pairId")) or {}).get("symbol"), "role": t.get("role"),
                     "taker_side": t.get("takerSide"), "price": scaled(t.get("price8dec"), 10 ** 8),
                     "size": scaled(t.get("size18dec"), 10 ** 18), "value_usd": num(row.get("valueUsdc")),
                     "fee_usd": scaled(t.get("feePaid6dec"), 10 ** 6),
                     "realized_pnl_usd": scaled(t.get("realizedPnl6dec"), 10 ** 6), "closing": t.get("isClose"),
                     "block": row.get("height"), "fill_id": t.get("fillId")})
    known = [x["realized_pnl_usd"] for x in rows if x["realized_pnl_usd"] is not None]
    realized = sum(known) if known else None
    fees = sum(x["fee_usd"] or 0 for x in rows)
    out = {"network": chain.network, "address": a, "count": len(rows), "trades": rows, "more": bool(doc.get("next")),
           "realized_pnl_usd_in_these": round(realized, 6) if realized is not None else None,
           "trades_without_recorded_pnl": len(rows) - len(known), "fees_usd_in_these": round(fees, 6),
           "records": ({k: doc["records"].get(k) for k in ("fromHeight", "throughHeight")}
                       | {"gaps": len(doc["records"]["gaps"]) if isinstance(doc["records"].get("gaps"), list) else None})
           if isinstance(doc.get("records"), dict) else None,
           "explorer": explorer_link(chain, "address", a),
           "units": "price in USD, size in the market's base asset, fees and PnL in USDC; newest first; the network "
                    "records realized PnL on taker trades only (null on a maker trade)", "source": source(r)}
    s = f"{short(a)}: {len(rows)} recent trade{'s' if len(rows) != 1 else ''}"
    if not rows:
        s += " recorded by the explorer."
    else:
        s += f", fees {usd(fees)}"
        s += (f", realized PnL {usd(realized)} on the {len(known)} taker trade{'s' if len(known) != 1 else ''}"
              if known else ", no realized PnL recorded (maker trades carry none)")
        s += (f"; {len(rows) - len(known)} maker trade{'s' if len(rows) - len(known) != 1 else ''} carry none."
              if known and len(known) < len(rows) else ".")
    out["summary"] = s
    return out


# ── agents ───────────────────────────────────────────────────────────────────
def get_agents(chain, cfg, params):
    _identity(chain)
    owner = _address(params)
    r = chain.agents(owner)
    doc = chain.require(r, "agents")
    agents = list(doc.get("agents") or [])
    reads, rows = [r], []
    for ag in agents[:10]:
        key = ag.get("agent")
        row = {"agent": key, "name": ag.get("display_name") or ag.get("name"), "status": ag.get("status"),
               "mandate": ag.get("mandate"), "expires_ms": ag.get("expires_ms")}
        if key and ADDRESS.match(str(key)) and len(rows) < 5:
            d = chain.agent(owner, key)
            reads.append(d)
            if d.ok and isinstance(d["json"], dict):
                dd = d["json"]
                row["limits"] = {"policy": dd.get("policy_v1"), "market_limits": dd.get("market_limits")}
                row["usage"] = dd.get("accounting")
            ev = chain.read(f"/events/agent/{key}?limit=5", cache=False)
            reads.append(ev)
            if ev.ok and isinstance(ev["json"], dict):
                row["recent_actions"] = [{k: x.get(k) for k in ("kind", "at_ms", "height", "pair_id", "reason",
                                                                "client_nonce") if k in x}
                                         for x in (ev["json"].get("events") or [])[:5]]
        rows.append(row)
    lim = doc.get("agent_limit") if isinstance(doc.get("agent_limit"), dict) else None
    from .tools import read_switch
    out = {"network": chain.network, "owner": owner, "count": doc.get("count", len(agents)), "agents": rows,
           "agent_limit": lim, "agent_trading": read_switch(chain),
           "units": "limits and usage are the chain's own fields: _6dec = USDC 6-decimal integers, _bps = basis points, "
                    "_18dec = base-asset 18-decimal sizes, _ms = milliseconds since 1970",
           "source": source(*reads)}
    s = f"{short(owner)} has {len(agents)} agent{'s' if len(agents) != 1 else ''}"
    if lim and lim.get("agent_limit") is not None:
        s += f" (limit {lim.get('agent_limit')}{', tier ' + str(lim.get('tier')) if lim.get('tier') else ''})"
    if rows:
        s += ": " + "; ".join(f"{x['name'] or short(x['agent'])} {x['status']}" for x in rows[:5])
    out["summary"] = s + "."
    return out


def get_tiers(chain, cfg, params):
    _identity(chain)
    al, rt = chain.read("/agents/limits"), chain.read("/referral/tiers")
    out = {"network": chain.network, "source": source(al, rt)}
    parts = []
    try:
        a = _doc(al, "agent limits by tier")
        if not isinstance(a.get("tier_limits"), dict):
            raise NotServed("the network does not serve agent limits by tier")
        out["agent_limits"] = {"active": a.get("active"), "per_tier": a.get("tier_limits"), "ceiling": a.get("ceiling")}
        parts.append("agents per tier " + ", ".join(f"{k} {v}" for k, v in a["tier_limits"].items()))
    except NotServed as e:
        out["agent_limits"] = {"available": False, "reason": str(e)}
    try:
        t = _doc(rt, "referral tiers")
        tiers = [{"tier": x.get("tier"), "fee_share_pct": (num(x.get("fee_share_bps")) or 0) / 100
                  if x.get("fee_share_bps") is not None else None,
                  "friends_discount_pct": (num(x.get("referee_discount_bps")) or 0) / 100
                  if x.get("referee_discount_bps") is not None else None,
                  "max_agents": x.get("agents_max"),
                  "referred_volume_to_reach_usdc": scaled(x.get("threshold_referred_volume_usdc_6dec"), 10 ** 6),
                  "how": x.get("how")} for x in t.get("tiers") or []]
        out["referral_tiers"] = {"tiers": tiers, "discount_applies_to": t.get("discount_applies_to")}
        parts.append(f"{len(tiers)} referral tiers")
    except NotServed as e:
        out["referral_tiers"] = {"available": False, "reason": str(e)}
    out["units"] = "percent; referred volume in USDC"
    out["summary"] = (f"Tiers on the {chain.network}: " + "; ".join(parts) + "." if parts else
                      f"{LABEL[chain.network]} serves no tier tables now.")
    return out


# ── rewards ──────────────────────────────────────────────────────────────────
def _season(chain, params):
    r = chain.read("/seasons")
    doc = _doc(r, "rewards seasons")
    seasons = doc.get("seasons") or []
    want = params.get("season_id")
    if want is not None:
        try:
            want = int(want)
        except (TypeError, ValueError, OverflowError):
            from .tools import Refusal
            raise Refusal("season_id must be a number (get_season lists them)")
    sid = want if want is not None else doc.get("current")
    s = next((x for x in seasons if x.get("season_id") == sid), None)
    if s is None:
        raise NotServed(f"no season {sid!r} on this network")
    return s, r, seasons


def _vord(wei):
    return scaled(wei, 10 ** 18)


def _points(v):
    return num(v)


def get_season(chain, cfg, params):
    _identity(chain)
    try:
        s, r, seasons = _season(chain, params)
    except NotServed as e:
        return unavailable(chain, "a rewards season", str(e), [chain.read("/seasons")])
    d = chain.read(f"/seasons/{int(s['season_id'])}?limit=1")
    det = d["json"] if d.ok and isinstance(d["json"], dict) else {}
    st = s.get("settings") or {}
    dist = det.get("distribution") or {}
    out = {"network": chain.network, "season_id": s.get("season_id"), "name": s.get("name"), "status": s.get("status"),
           "starts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((s.get("start_ms") or 0) / 1000)) if s.get("start_ms") else None,
           "ends": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s["end_ms"] / 1000)) if s.get("end_ms") else None,
           "budget_vord": _vord(st.get("budget_wei")), "cap_per_wallet_vord": _vord(st.get("cap_wei")),
           "qualify_fees_usdc": scaled(st.get("qualify_fee_usdc_6dec"), 10 ** 6),
           "referral_points_share_pct": {k: v / 100 for k, v in (st.get("referral_share_bps") or {}).items()},
           "referee_bonus_pct": (num(st.get("referee_bonus_bps")) or 0) / 100 if st.get("referee_bonus_bps") is not None else None,
           "participants": det.get("participants"), "total_points": _points(det.get("total_points")),
           "qualified_count": dist.get("qualified_count", s.get("qualified_count")),
           "estimate": dist.get("estimate"), "settings": st,
           "all_seasons": [{"season_id": x.get("season_id"), "name": x.get("name"), "status": x.get("status")} for x in seasons],
           "units": "VORD in whole tokens; points as the season's rules (settings) count them; percent",
           "source": source(r, d)}
    out["summary"] = (f"{s.get('name') or 'Season ' + str(s.get('season_id'))} is {s.get('status')}: budget "
                      f"{amount(out['budget_vord'], 0)} VORD, {out['participants'] if out['participants'] is not None else '—'} "
                      f"participants, {out['qualified_count'] if out['qualified_count'] is not None else '—'} qualified.")
    return out


def _row(x):
    return {"rank": x.get("rank"), "address": x.get("address"), "total_points": _points(x.get("total_points")),
            "trading_points": _points(x.get("trading_points")), "referral_points": _points(x.get("referral_points")),
            "referee_bonus_points": _points(x.get("referee_bonus_points")), "social_points": _points(x.get("social_points")),
            "pool_points": _points(x.get("pool_points")), "fees_paid_usdc": scaled(x.get("fees_paid_usdc_6dec"), 10 ** 6),
            "qualified": x.get("qualified"), "vord_estimate": _vord(x.get("vord_wei")), "at_cap": x.get("at_cap"),
            "estimate": x.get("estimate")}


def get_points(chain, cfg, params):
    _identity(chain)
    a = _address(params)
    try:
        s, r, _ = _season(chain, params)
    except NotServed as e:
        return unavailable(chain, "rewards points", str(e), [chain.read("/seasons")])
    ar = chain.read(f"/seasons/{int(s['season_id'])}/account/{a}", cache=False)
    try:
        doc = _doc(ar, "an account's season points")
    except NotServed as e:
        return unavailable(chain, "an account's season points", str(e), [r, ar])
    row = _row(doc)
    det = chain.read(f"/seasons/{int(s['season_id'])}?limit=1")
    total = det["json"].get("participants") if det.ok and isinstance(det["json"], dict) else None
    out = {"network": chain.network, "season_id": s.get("season_id"), "season": s.get("name"), "address": a, **row,
           "excluded": doc.get("excluded"), "participants": total,
           "units": "points as the season counts them (its rules are in get_season); VORD in whole tokens, an estimate "
                    "until the season closes", "source": source(r, ar, det)}
    src = [f"{k.replace('_points', '')} {amount(row[k], 2)}" for k in
           ("trading_points", "referral_points", "referee_bonus_points", "social_points", "pool_points") if row[k]]
    out["summary"] = (f"{short(a)} in {s.get('name') or 'season ' + str(s.get('season_id'))}: "
                      f"{amount(row['total_points'], 2)} points" + (f" ({', '.join(src)})" if src else "")
                      + (f", rank {row['rank']}" + (f" of {total}" if total else "") if row["rank"] else ", not ranked yet")
                      + f", {'qualified' if row['qualified'] else 'not qualified yet'}"
                      + (f", about {amount(row['vord_estimate'], 2)} VORD" if row["vord_estimate"] else "") + ".")
    return out


def get_leaderboard(chain, cfg, params):
    _identity(chain)
    board = str(params.get("board") or "points").lower()
    limit = _int(params, "limit", 10, 1, 100)
    if board == "trading":
        window = str(params.get("window") or "7d")
        if window not in ("24h", "7d", "30d", "all"):
            from .tools import Refusal
            raise Refusal("window must be 24h, 7d, 30d or all")
        sort = str(params.get("sort") or "pnl")
        if sort not in ("pnl", "roi", "volume"):
            from .tools import Refusal
            raise Refusal("sort must be pnl, roi or volume")
        r = chain.read_link("app", f"/ranks?window={window}&sort={sort}&kind=all&limit={limit}")
        try:
            doc = _doc(r, "the trading leaderboard")
        except NotServed as e:
            return unavailable(chain, "the trading leaderboard", str(e), [r])
        rows = [{"rank": x.get("rank"), "account": x.get("account"), "agent": x.get("agent"), "kind": x.get("kind"),
                 "name": x.get("name"), "pnl_usd": x.get("pnl"),
                 "roi_pct": round(num(x.get("roi")) * 100, 4) if num(x.get("roi")) is not None else None,
                 "volume_usd": x.get("volume"),
                 "trades": x.get("trades"), "win_rate": x.get("winRate")} for x in (doc.get("rows") or [])[:limit]]
        out = {"network": chain.network, "board": "trading", "window": window, "sort": sort, "total": doc.get("total"),
               "rows": rows, "computed_at": doc.get("computedAt"),
               "units": "USD; ranked from the network's own fills", "source": source(r)}
        out["summary"] = (f"Trading leaderboard ({window}, by {sort}): " +
                          ("; ".join(f"#{x['rank']} {x['name'] or short(x['account'])} {usd(x['pnl_usd'])}" for x in rows[:3])
                           if rows else "no ranked traders yet") + ".")
        return out
    try:
        s, r, _ = _season(chain, params)
    except NotServed as e:
        return unavailable(chain, "a season leaderboard", str(e), [chain.read("/seasons")])
    d = chain.read(f"/seasons/{int(s['season_id'])}?limit={limit}")
    doc = chain.require(d, "season leaderboard")
    rows = [_row(x) for x in (doc.get("top") or [])[:limit]]
    out = {"network": chain.network, "board": "points", "season_id": s.get("season_id"), "season": s.get("name"),
           "participants": doc.get("participants"), "rows": rows,
           "units": "points as the season counts them (its rules are in get_season); VORD in whole tokens, an estimate "
                    "until the season closes", "source": source(r, d)}
    out["summary"] = (f"{s.get('name') or 'Season ' + str(s.get('season_id'))} top {len(rows)}: " +
                      ("; ".join(f"#{x['rank']} {short(x['address'])} {amount(x['total_points'], 2)} pts" for x in rows[:3])
                       if rows else "no one ranked yet") + ".")
    return out


def get_referrals(chain, cfg, params):
    _identity(chain)
    a = _address(params)
    r = chain.read(f"/referral/stats/{a}", cache=False)
    try:
        d = _doc(r, "referral stats")
    except NotServed as e:
        return unavailable(chain, "referral stats", str(e), [r])
    d = d.get("data") if isinstance(d.get("data"), dict) else d
    out = {"network": chain.network, "address": a, "code": d.get("code"), "tier": d.get("tier"),
           "referred_by": d.get("referrer"), "referred_volume_usd": num(d.get("referred_volume_usdc")),
           "pending_earnings_usd": num(d.get("pending_earnings_usdc")), "total_earned_usd": num(d.get("total_earned_usdc")),
           "progress_to_next_tier": d.get("progress_to_next_tier"),
           "friends_discount_pct": (num(d.get("referees_get_discount_bps")) or 0) / 100
           if d.get("referees_get_discount_bps") is not None else None,
           "own_discount_as_referee_pct": (num(d.get("referee_discount_bps")) or 0) / 100
           if d.get("referee_discount_bps") is not None else None,
           "discount_on": "taker fees" if d.get("taker_only") is True else ("trading fees" if d.get("taker_only") is False else None),
           "units": "USD; percent", "source": source(r)}
    out["summary"] = (f"{short(a)}: " + (f"referral code {d.get('code')}, tier {d.get('tier')}, referred volume "
                                         f"{usd(out['referred_volume_usd'], True)}, earned {usd(out['total_earned_usd'])}."
                                         if d.get("code") else "no referral code yet."))
    return out


# ── explorer ─────────────────────────────────────────────────────────────────
def get_block(chain, cfg, params):
    _identity(chain)
    try:
        n = int(params.get("height"))
        if n < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        from .tools import Refusal
        raise Refusal("height must be a block number, e.g. 152561063")
    r = chain.read_link("explorer", f"/api/blocks/{n}", ttl=30.0)
    reads = [r]
    doc = r["json"] if r and r.ok and isinstance(r["json"], dict) else None
    ops = None
    if doc is None:
        a = chain.read_link("explorer", f"/api/archive/blocks/{n}", ttl=30.0)
        reads.append(a)
        if a and a.ok and isinstance(a["json"], dict):
            doc = a["json"].get("block") or {}
            ops = [{k: o.get(k) for k in ("op_index", "action_type", "actor", "summary")} for o in (a["json"].get("ops") or [])[:50]]
    if not doc:
        return {"available": False, "network": chain.network, "height": n, "explorer": explorer_link(chain, "block", n),
                "summary": f"Block {n:,} is not in the explorer's index (it may be ahead of the chain or not yet indexed).",
                "source": source(*reads)}
    t = doc.get("timestamp")
    out = {"network": chain.network, "height": doc.get("number", n), "hash": doc.get("hash"),
           "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t))) if t else None,
           "leader": doc.get("leader"), "operations": doc.get("ops_count"), "evm_transactions": doc.get("tx_count"),
           "ops": ops, "explorer": explorer_link(chain, "block", n), "source": source(*reads)}
    out["summary"] = (f"Block {n:,}" + (f" at {out['time']}" if out["time"] else "") +
                      f": {plural(doc.get('ops_count'), 'operation') if isinstance(doc.get('ops_count'), int) else '— operations'}, "
                      f"{plural(doc.get('tx_count'), 'EVM transaction') if isinstance(doc.get('tx_count'), int) else '— EVM transactions'}, proposed by "
                      f"{short(doc.get('leader'))}. {out['explorer']}")
    return out


def get_tx(chain, cfg, params):
    _identity(chain)
    h = str(params.get("hash") or "").strip()
    if not TX_HASH.match(h):
        from .tools import Refusal
        raise Refusal("hash must be a 0x-prefixed 32-byte transaction hash")
    r = chain.read_link("explorer", f"/api/evm/tx/{h.lower()}", ttl=10.0)
    doc = r["json"] if r and r.ok and isinstance(r["json"], dict) else None
    link = explorer_link(chain, "tx", h)
    if not doc or doc.get("found") is False:
        return {"available": False, "network": chain.network, "hash": h, "explorer": link,
                "summary": f"No EVM transaction {short(h)} on the {chain.network}. Trading operations (orders, deposits, "
                           "withdrawals) have no hash: look them up by account with get_fills, or by chain height with "
                           "get_block.",
                "source": source(r)}
    ok = doc.get("status")
    out = {"network": chain.network, "hash": h, "evm_block": doc.get("block"), "from": doc.get("from"), "to": doc.get("to"),
           "value_vord": _vord(doc.get("value")), "gas_used": doc.get("gas_used"),
           "succeeded": (ok in (1, "1", "0x1", True, "success")) if ok is not None else None,
           "contract_created": doc.get("contract_address") if doc.get("is_create") else None,
           "explorer": link, "units": "value in VORD (whole tokens); evm_block counts EVM blocks, not chain heights "
                                      "(get_block takes a chain height)", "chain_answer": doc, "source": source(r)}
    out["summary"] = (f"Transaction {short(h)} in EVM block {doc.get('block')}: {short(doc.get('from'))} → "
                      f"{short(doc.get('to') or doc.get('contract_address'))}, {amount(out['value_vord'])} VORD, "
                      f"{'succeeded' if out['succeeded'] else 'failed' if out['succeeded'] is False else 'status unknown'}. {link}")
    return out


def get_address(chain, cfg, params):
    _identity(chain)
    a = _address(params)
    x = chain.read_link("explorer", f"/api/address/{a.lower()}", cache=False)
    eq, pos, ag = chain.equity(a), chain.positions(a), chain.agents(a)
    e = eq["json"] if eq.ok and isinstance(eq["json"], dict) else {}
    out = {"network": chain.network, "address": a,
           "evm_transactions": (x["json"] or {}).get("tx_count") if x and x.ok and isinstance(x["json"], dict) else None,
           "account_value_usdc": scaled(e.get("total_equity"), 10 ** 6) if e else None,
           "open_positions": len(pos["json"]) if pos.ok and isinstance(pos["json"], list) else None,
           "agents": (ag["json"] or {}).get("count") if ag.ok and isinstance(ag["json"], dict) else None,
           "explorer": explorer_link(chain, "address", a),
           "units": "USDC", "source": source(x, eq, pos, ag)}
    out["summary"] = (f"{short(a)}: account value {usd(out['account_value_usdc'])}, {out['open_positions'] if out['open_positions'] is not None else '—'} "
                      f"open positions, {out['agents'] if out['agents'] is not None else '—'} agents, "
                      f"{out['evm_transactions'] if out['evm_transactions'] is not None else '—'} EVM transactions. {out['explorer']}")
    return out
