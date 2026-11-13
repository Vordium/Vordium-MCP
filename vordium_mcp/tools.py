"""The tools, their schemas, and the gate in front of the two write tools.

Rules this file enforces:
  * every figure is read from the network when a tool is called; nothing here is typed;
  * the server checks, before answering, that the network it reads is the one it was started for;
  * a write never succeeds quietly. It returns the chain's own answer unchanged, or a refusal that names the readings
    behind it. While the network reports its agent policy switch off, a write is refused before anything is signed;
  * no key ever arrives through a tool. Arguments that look like key material are refused before they are read,
    logged or forwarded, and the refusal never repeats them.

The limits an agent trades under are enforced by the chain, on every validator. This server only refuses early.
"""
import math, threading
from . import keys, networks, pretrade
import time
import re

from . import reads
from .chain import ChainUnreachable
from .networks import NETWORKS
from .schemas import LOCAL_READ, OUTPUT, READ, WRITE
from .reads import NotServed, short, usd, amount
from .text import DATA_NOTE, clean, count, ident, label, market, state, symbol

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
KEYISH_FIELD = re.compile(r"priv|secret|mnemonic|seed|passphrase|keystore|password|key_?file", re.I)
KEYISH_VALUE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
MNEMONIC_WORD = re.compile(r"[a-z]{3,8}", re.I)   # one word of a recovery phrase, checked word by word (no backtracking)
MNEMONIC_COUNTS = (12, 15, 18, 21, 24)
MAX_TEXT = 4096       # no tool argument is longer; a longer string is refused before any other check

PRICE_6DEC = 10 ** 6       # the order book and the oracle detail route carry prices as 6-decimal integers
SIZE_18DEC = 10 ** 18      # sizes are 18-decimal integers
USDC_6DEC = 10 ** 6        # account figures are 6-decimal USDC integers


class Refusal(Exception):
    def __init__(self, reason, readings=None, remedy=None, note="Nothing was signed or sent."):
        super().__init__(reason)
        self.payload = {"refused": True, "reason": reason, "readings": readings or {}, "remedy": remedy, "note": note}


def reject_key_material(params, hashes=()):
    """Refuse before anything is read, logged or forwarded. The offending value is never echoed back. ``hashes`` names
    the top-level arguments that take a transaction hash, which has the same form as a private key."""
    def walk(node, path="$"):
        if isinstance(node, dict):
            for k, v in node.items():
                if KEYISH_FIELD.search(str(k)):
                    raise Refusal(f"{path}.{k} looks like a key or secret, and this server never takes one through a tool",
                                  remedy="Keys stay on your machine. The trading key is set in the server's own "
                                         "configuration, never passed to a tool. The value was not read or logged.")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            if path in tuple(f"$.{h}" for h in hashes):
                return
            if len(node) > MAX_TEXT:
                raise Refusal(f"{path} is longer than {MAX_TEXT} characters; no tool takes text that long",
                              remedy="Send a shorter value.")
            words = node.split()
            phrase = len(words) in MNEMONIC_COUNTS and all(MNEMONIC_WORD.fullmatch(w) for w in words)
            if phrase or KEYISH_VALUE.match(node.strip()):
                raise Refusal(f"{path} looks like a private key or recovery phrase; this server never takes one "
                              "through a tool",
                              remedy="Keys stay on your machine and are never passed to a tool. The value was not "
                                     "read or logged.")
    walk(params or {})


def source(*reads):
    return [{"url": r["url"], "status": r["status"], "read_at": r["read_at"]} for r in reads if r]


def _address(params, key="address"):
    a = str(params.get(key) or "").strip()
    if not ADDRESS.match(a):
        raise Refusal(f"{key} must be a 0x-prefixed 20-byte account address")
    return a


def _units(v, scale):
    try:
        return int(v) / scale
    except (TypeError, ValueError):
        return None


def _asked(params):
    """The market the caller named, as they gave it: the pair_id when one was given (0 included), else the symbol."""
    return params.get("pair_id") if params.get("pair_id") is not None else params.get("symbol")


def _pair(chain, params, required=True):
    named = params.get("pair_id") is not None or str(params.get("symbol") or "").strip() != ""
    if not named:
        if required:
            raise Refusal("name a market: give its symbol (for example \"BTC\") or its pair_id",
                          remedy="Call get_markets to see every market's symbol and pair_id on this network.")
        return None
    if params.get("pair_id") is not None:
        try:
            p = chain.pair_by_id(int(params.get("pair_id")))
        except (TypeError, ValueError, OverflowError):
            p = None
    else:
        p = chain.pair_by_symbol(params.get("symbol"))
    if p is None and required:
        raise Refusal(f"no market on this network matches {_asked(params)!r}",
                      remedy="Call get_markets: the list is read from the network and changes with it.")
    return p


# ── which network this is ────────────────────────────────────────────────────
IDENTITY_TTL = 60.0


def network_identity(chain, force=False):
    """Check that the network read is the one this server was started for, from three sources: the published spec,
    the network's own answer and the node's status. Any disagreement between them refuses every tool.

    The reports are also held to the network's pin (networks.PINS). A difference does not stop reads: the identity
    carries it as ``pin_problem`` (get_network shows it), and every write is refused (see require_pin)."""
    now = chain.clock()
    cached = getattr(chain, "_identity", None)
    if cached and not force and now - cached["at"] < IDENTITY_TTL:
        return cached["value"]
    sp, tr, st = chain.spec(), chain.truth(cache=True), chain.status()
    spec = chain.require(sp, "network spec")
    status = chain.require(st, "node status")
    truth = tr["json"] if tr.ok and isinstance(tr["json"], dict) else {}
    want_test = chain.network == "testnet"
    problems = []
    if bool(spec.get("testnet") is True) != want_test:
        problems.append(f"the spec at {sp['url']} {'is not' if want_test else 'is'} a test network")
    ids = {"spec": spec.get("chainId"), "status": status.get("chain_id")}
    if truth.get("chain_id") is not None:
        ids["truth"] = truth.get("chain_id")
    if len({v for v in ids.values()}) != 1:
        problems.append("chain ids disagree: " + ", ".join(f"{k} {ident(v)}" for k, v in ids.items()))
    gens = {k: v for k, v in (("spec", spec.get("genesisSha256")), ("truth", truth.get("genesis_sha256")),
                              ("status", status.get("genesis_sha256"))) if v}
    if len({str(v).lower().removeprefix("0x") for v in gens.values()}) > 1:
        problems.append("genesis disagrees: " + ", ".join(f"{k} {ident(v)}" for k, v in gens.items()))
    if truth.get("network") and truth.get("network") != chain.network:
        problems.append(f"the network's own answer names {state(truth.get('network'), 'another network')}")
    if problems:
        raise Refusal(f"this server was started for the {chain.network} but the addresses it reads disagree with that: "
                      + "; ".join(problems),
                      readings={"source": source(sp, tr, st)},
                      remedy="Check --network and any custom address (--rest, --spec, --truth).")
    names = {"spec": "the network spec", "status": "the node's /status", "truth": "the network's own answer"}
    pin_lines = networks.pin_problems(chain.network, {names[k]: v for k, v in ids.items()},
                                      {names[k]: v for k, v in gens.items()})
    if not gens:
        pin_lines.append(f"no source reports the genesis, expected {networks.pin(chain.network)['genesis_sha256']}")
    value = {"network": chain.network, "chain_id": ids["spec"], "genesis_sha256": gens.get("spec") or gens.get("truth"),
             "testnet": want_test, "pin": networks.pin(chain.network),
             "pin_problem": networks.pin_refusal(chain.network, pin_lines) if pin_lines else None}
    chain._identity = {"at": now, "value": value}
    return value


def require_pin(chain, force=False):
    """Refuse (nothing signed) unless what the network reports equals its pin. Returns the pin."""
    ident = network_identity(chain, force=force)
    if ident.get("pin_problem"):
        raise Refusal(ident["pin_problem"], readings={"pin": ident["pin"], "reported": {
                          "chain_id": ident["chain_id"], "genesis_sha256": ident["genesis_sha256"]}},
                      remedy="Check --network and any custom address (--rest). This release signs only for the "
                             "network its pin names; a new genesis needs a new release.")
    return ident["pin"]


def read_switch(chain):
    """The agent policy switch as the network itself vouches for it (its own answer, read now; never remembered)."""
    r = chain.truth(cache=False)
    doc = r["json"] if r.ok and isinstance(r["json"], dict) else None
    if doc is None or doc.get("version") != 1:
        return {"established": False, "why": f"the network's answer could not be read ({r['status']})", "source": source(r)}
    if doc.get("ok") is not True:
        return {"established": False, "why": "the network cannot vouch for its state right now (network_error)",
                "network_error": clean(str(doc.get("error") or "not ok")), "source": source(r)}
    active = doc.get("agent_policy_active")
    if not isinstance(active, bool):
        return {"established": False, "why": "the network's answer carries no value for it", "source": source(r)}
    return {"established": True, "active": active,
            "meaning": ("on: the chain enforces each agent's policy and market limits" if active else
                        "off: agent trading is not switched on; policies are stored, not enforced"),
            "source": source(r)}


# ── read tools ───────────────────────────────────────────────────────────────
def get_network(chain, cfg, params):
    ident = network_identity(chain, force=True)
    r = chain.truth(cache=False)
    doc = r["json"] if r.ok and isinstance(r["json"], dict) else None
    out = {"network": chain.network, "identity": ident, "testnet_tokens_have_no_value": chain.network == "testnet",
           "pinned": {**ident["pin"], "matches": ident["pin_problem"] is None},
           "trading_from_this_server": "off" if cfg.read_only else ("allowed by the operator" if cfg.allow_trading
                                                                     else "not allowed by the operator"),
           "source": source(r)}
    if ident["pin_problem"]:
        out["pin_warning"] = ident["pin_problem"] + " Reads still work; no write tool signs until this matches."
    if doc is None or doc.get("version") != 1:
        out["truth"] = {"established": False, "why": f"the network's answer could not be read ({r['status']})"}
        out["summary"] = f"Connected to the {chain.network}; its own answer could not be read ({r['status']})."
        return _pin_summary(out)
    if doc.get("ok") is not True:
        out["truth"] = {"established": False, "why": "the network cannot vouch for its state right now (network_error)",
                        "network_error": clean(str(doc.get("error") or "not ok")), "stale": doc.get("stale")}
        out["summary"] = f"Connected to the {chain.network}; the network cannot vouch for its state right now."
        return _pin_summary(out)
    committed = doc.get("committed") or {}
    out["truth"] = {
        "established": True,
        "chain_id": doc.get("chain_id"),
        "genesis_sha256": doc.get("genesis_sha256"),
        "finalized_height": doc.get("finalized_height"),
        "validators_seated": (doc.get("validators") or {}).get("seated"),
        "validators_agreeing": committed.get("agreed_by"),
        "agent_policy_active": doc.get("agent_policy_active"),
        "trading_open": doc.get("trading_open"),
        "deposits_open": doc.get("deposits_open"),
        "evm_active": doc.get("evm_active"),
        "age_s": doc.get("age_s"),
        "max_age_s": doc.get("max_age_s"),
    }
    out["note"] = ("These facts come from the network's own answer: several validators are read at once and compared. "
                   "When they cannot agree the answer says so, and this tool reports it as not established.")
    t = out["truth"]
    agreed = (f" agreed by {t['validators_agreeing']} of {t['validators_seated']} validators"
              if all(count(t[k], None) for k in ("validators_agreeing", "validators_seated")) else "")
    out["summary"] = (f"Connected to the {chain.network}{' (chain id ' + count(t['chain_id']) + ')' if count(t['chain_id'], None) else ''}: "
                      f"finalized block {t['finalized_height']:,}{agreed}; trading {({True: 'open', False: 'closed'}).get(t['trading_open'], 'unknown')}, agent trading "
                      f"{({True: 'on', False: 'off'}).get(t['agent_policy_active'], 'unknown')}." if isinstance(t.get("finalized_height"), int)
                      else f"Connected to the {chain.network}.")
    return _pin_summary(out)


def _pin_summary(out):
    if out.get("pin_warning"):
        out["summary"] = ("Warning: the network does not match this release's pin, so nothing will be signed. "
                          + out["summary"])
    return out


def get_markets(chain, cfg, params):
    network_identity(chain)
    m = chain.markets()
    pairs = chain.pairs()
    sp = chain.market_specs()
    specs = sp["json"].get("specs") if sp.ok and isinstance(sp["json"], dict) else None
    by_id = {}
    if isinstance(specs, dict):
        for row in specs.get("pairs") or []:
            pid = (row.get("pair") or {}).get("pair_id")
            if pid is not None:
                by_id[pid] = {k: v for k, v in row.items() if k != "pair"}
    out = []
    for p in pairs:
        row = dict(p)
        if p.get("pair_id") in by_id:
            row["trading_rules"] = by_id[p.get("pair_id")]
        out.append(row)
    venue = None
    if isinstance(specs, dict):
        venue = {k: specs.get(k) for k in ("min_order_notional_6dec", "max_open_orders_per_wallet", "price_band_active",
                                           "price_band_bps", "funding_interval_secs", "withdrawal_fee_6dec")
                 if k in specs}
    active = [market(r) for r in out if r.get("active") is not False and not r.get("paused") and not r.get("delisted")]
    return {"summary": f"{len(out)} markets on the {chain.network}, {len(active)} trading: "
                       + ", ".join(active[:12]) + ("…" if len(active) > 12 else "") + ".",
            "network": chain.network, "count": len(out), "markets": out, "venue_rules": venue,
            "units": "fees in basis points; leverage in basis points (10000 = 1x) and as x; 6dec fields are USDC "
                     "6-decimal integers; 8dec fields 8-decimal prices; 18dec fields 18-decimal sizes. A tick or lot "
                     "of 0 means the network sets none.",
            "source": source(m, sp)}


def get_oracle(chain, cfg, params):
    network_identity(chain)
    o = chain.oracle()
    doc = chain.require(o, "oracle prices")
    prices = (doc.get("prices") if isinstance(doc, dict) else doc) or []
    by_id = {p.get("pair_id"): p for p in chain.pairs()}
    out = []
    for p in prices:
        row = dict(p)
        market = by_id.get(p.get("pair_id"))
        row["market_symbol"] = market.get("symbol") if market else None
        out.append(row)
    if params.get("symbol") or params.get("pair_id") is not None:
        pair = _pair(chain, params)
        out = [r for r in out if r.get("pair_id") == pair.get("pair_id")]
    one = out[0] if len(out) == 1 else None
    return {"summary": (f"{symbol(one.get('market_symbol') or one.get('symbol'), 'market ' + count(one.get('pair_id')))}: mark {usd(one.get('mark_price'))}, oracle "
                        f"{usd(one.get('oracle_price'))}." if one else f"Oracle and mark prices for {len(out)} markets."),
            "network": chain.network, "prices": out, "units": "USD, as decimals",
            "source": source(o, chain.markets())}


def get_orderbook(chain, cfg, params):
    network_identity(chain)
    pair = _pair(chain, params)
    depth = params.get("depth", 20)
    try:
        depth = max(1, min(100, int(depth)))
    except (TypeError, ValueError, OverflowError):
        depth = 20
    ob = chain.orderbook(pair["pair_id"])
    doc = chain.require(ob, "order book")

    def side(levels):
        rows = []
        for lv in (levels or [])[:depth]:
            if isinstance(lv, (list, tuple)) and len(lv) >= 2:
                rows.append({"price": _units(lv[0], PRICE_6DEC), "size": _units(lv[1], SIZE_18DEC)})
        return rows

    bids, asks = side(doc.get("bids")), side(doc.get("asks"))
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    return {"summary": (f"{market(pair)}: best bid {usd(best_bid)}, best ask {usd(best_ask)}, spread "
                        f"{usd(round(best_ask - best_bid, 6))}; {len(bids)} bid and {len(asks)} ask levels shown."
                        if best_bid is not None and best_ask is not None else f"{market(pair)}: the book is "
                        f"{'empty' if not bids and not asks else 'one-sided'}."),
            "network": chain.network, "market": pair.get("symbol"), "pair_id": pair.get("pair_id"),
            "bids": bids, "asks": asks, "best_bid": best_bid, "best_ask": best_ask,
            "spread": round(best_ask - best_bid, 6) if best_bid is not None and best_ask is not None else None,
            "empty": not bids and not asks,
            "units": "price in USD, size in the market's base asset",
            "source": source(ob)}


def get_funding(chain, cfg, params):
    network_identity(chain)
    pair = _pair(chain, params)
    f = chain.funding(pair["pair_id"])
    doc = chain.require(f, "funding")
    rate8 = doc.get("rate_8dec")
    rate = (rate8 / 10 ** 6) if isinstance(rate8, int) else doc.get("rate_pct")
    iv = doc.get("interval_secs")
    out = {"network": chain.network, "market": pair.get("symbol"), "pair_id": pair.get("pair_id"),
           "rate_per_interval_pct": rate, "interval_secs": iv, "next_funding": doc.get("next_funding"),
           "funding_enabled": doc.get("funding_enabled"), "chain_answer": doc,
           "units": "rate_per_interval_pct is a percentage per interval; a positive rate means longs pay shorts; "
                    "history is oldest first",
           "source": source(f)}
    n = params.get("history")
    if n is not None:
        try:
            n = max(1, min(400, int(n)))
        except (TypeError, ValueError, OverflowError):
            raise Refusal("history must be a number of past settlements, 1-400")
        rows, hr = reads.funding_history(chain, pair, n)
        out["history"] = rows
        out["source"] = source(f, hr)
    every = f"{iv // 3600} h" if isinstance(iv, int) and iv % 3600 == 0 else (f"{iv} s" if iv else "interval")
    who = "longs pay shorts" if (rate or 0) > 0 else ("shorts pay longs" if (rate or 0) < 0 else "no one pays")
    out["summary"] = (f"{market(pair)} funding: {rate:+.6f}% per {every} ({who})." if isinstance(rate, (int, float))
                      else f"{market(pair)}: the funding rate is not published.")
    if out.get("history"):
        vals = [r["rate_pct"] for r in out["history"] if r["rate_pct"] is not None]
        if vals:
            out["summary"] += f" Average over the last {len(vals)} settlements: {sum(vals) / len(vals):+.6f}%."
    return out


def get_balance(chain, cfg, params):
    network_identity(chain)
    address = _address(params)
    eq, bal = chain.equity(address), chain.balance(address)
    if not eq.ok and not bal.ok:
        raise Refusal(f"the network did not return balances for this account ({eq['status']}, {bal['status']})",
                      readings={"source": source(eq, bal)})
    e = eq["json"] if eq.ok else {}
    return {"summary": (f"{short(address)}: account value {usd(_units(e.get('total_equity'), USDC_6DEC))}, "
                        f"{usd(_units(e.get('free_balance'), USDC_6DEC))} available to trade." if eq.ok
                        else f"{short(address)}: balances read from the balance route only."),
            "network": chain.network, "address": address,
            "usdc": {"available_to_trade": _units(e.get("free_balance"), USDC_6DEC),
                     "in_positions": _units(e.get("in_positions"), USDC_6DEC),
                     "reserved_by_orders": _units(e.get("reserved_orders"), USDC_6DEC),
                     "unrealized_pnl": _units(e.get("unrealized_pnl"), USDC_6DEC),
                     "account_value": _units(e.get("total_equity"), USDC_6DEC)} if eq.ok else None,
            "chain_answer": {"equity": e if eq.ok else None, "balance": bal["json"] if bal.ok else None},
            "units": "USDC. available_to_trade is what a new order can use as margin; margin is isolated per position.",
            "source": source(eq, bal)}


def get_positions(chain, cfg, params):
    network_identity(chain)
    address = _address(params)
    pos = chain.positions(address)
    raw = chain.require(pos, "positions")
    items = raw if isinstance(raw, list) else (raw.get("positions") or [])
    by_id = {p.get("pair_id"): p for p in chain.pairs()}
    for it in items:
        m = by_id.get(it.get("pair_id"))
        if m and not it.get("symbol"):
            it["symbol"] = m.get("symbol")
    return {"summary": f"{short(address)} has {len(items)} open position{'s' if len(items) != 1 else ''}"
                       + (": " + ", ".join(f"{symbol(it.get('symbol'), 'market ' + count(it.get('pair_id')))} {'long' if it.get('is_long') else 'short'}" for it in items[:8])
                          if items else "") + ". get_account adds entry, mark, PnL and liquidation prices.",
            "network": chain.network, "address": address, "count": len(items), "positions": items,
            "note": "Fields are the chain's own. Prices are 8-decimal integers where named _8dec, sizes 18-decimal "
                    "where named _18dec.",
            "source": source(pos, chain.markets())}


def get_orders(chain, cfg, params):
    network_identity(chain)
    address = _address(params)
    o = chain.orders(address)
    raw = chain.require(o, "open orders")
    items = raw if isinstance(raw, list) else (raw.get("orders") or [])
    by_id = {p.get("pair_id"): p for p in chain.pairs()}
    for it in items:
        m = by_id.get(it.get("pair_id"))
        if m and not it.get("symbol"):
            it["symbol"] = m.get("symbol")
    return {"summary": f"{short(address)} has {len(items)} resting order{'s' if len(items) != 1 else ''}.",
            "network": chain.network, "address": address, "count": len(items), "orders": items,
            "source": source(o, chain.markets())}


# ── agents: policy and usage, as the chain stores them ───────────────────────
AGENT_RECORDS = 10
POLICY_KEYS = ("status", "mandate", "expires_ms", "policy", "policy_v1", "market_limits", "name", "display_name")
RISK_KEYS = ("accounting", "risk_v1", "lock_active", "expires_ms", "status")


def _agent_view(chain, params, kind, keys, note):
    network_identity(chain)
    owner = _address(params, "address")
    agent = params.get("agent")
    if agent is not None and not ADDRESS.match(str(agent)):
        raise Refusal(f"{agent!r} is not an agent address", remedy="Omit agent to list every agent of this owner.")
    r = chain.agent(owner, str(agent)) if agent is not None else chain.agents(owner)
    if not r.ok:
        return {"summary": f"The agent records of {short(owner)} could not be read ({r['status']}).",
                "available": False, "owner": owner, "reason": f"the agent route answered {r['status']}",
                "note": f"No {kind} is returned from defaults or memory.", "source": source(r)}
    doc = r["json"] or {}
    records = ([] if doc.get("found") is False else [doc]) if agent is not None else list(doc.get("agents") or [])
    if agent is None:
        # the list carries only yes/no flags (has_policy_v1); each agent's own record carries its policy and market
        # limits — read it (the first AGENT_RECORDS agents), and say so when one could not be read
        full = []
        for rec in records[:AGENT_RECORDS]:
            a = rec.get("agent") if isinstance(rec, dict) else None
            one = chain.agent(owner, str(a)) if isinstance(a, str) and ADDRESS.match(a) else None
            full.append(one["json"] if one is not None and one.ok and isinstance(one["json"], dict) and one["json"].get("found") is not False
                        else {**rec, "record_unreadable": True})
        records = full + records[AGENT_RECORDS:]
    sw = read_switch(chain)
    return {"summary": f"{short(owner)}: {len(records)} agent record{'s' if len(records) != 1 else ''}; the chain "
                       + ("enforces agent policies now." if sw.get("active") is True else
                          "does not enforce agent policies yet." if sw.get("established") else
                          "cannot vouch for the agent policy switch right now."),
            "available": True, "network": chain.network, "owner": owner, "agent": agent,
            "count": doc.get("count", len(records)),
            "agents": [{"record": rec, f"{kind}_fields_present": sorted(k for k in rec if k in keys)} for rec in records],
            "policy_switch": sw,
            "enforced_by": "the chain, checked by every validator. This server enforces nothing; it only refuses early.",
            "note": note + " Fields are the chain's own, unchanged. An empty list means this owner has registered no "
                           "agent — not that no limits apply.",
            "source": source(r)}


def get_policy(chain, cfg, params):
    return _agent_view(chain, params, "policy", POLICY_KEYS,
                       "An owner sets each agent's policy and its per-market limits; the chain enforces them while its "
                       "agent policy switch is on (policy_switch, read from the network's own answer).")


def get_risk(chain, cfg, params):
    return _agent_view(chain, params, "risk", RISK_KEYS,
                       "Usage is accounted on-chain against the agent's policy.")


# ── the write gate ───────────────────────────────────────────────────────────
def _refuse_switch_off(chain):
    """The first check, before any other read and before anything is signed."""
    sw = read_switch(chain)
    if not sw["established"]:
        raise Refusal(f"agent trading cannot be confirmed as switched on: {sw['why']}",
                      readings={"policy_switch": sw},
                      remedy="Try again when the network can vouch for its state (get_network shows it).")
    if sw["active"] is not True:
        raise Refusal(f"agent trading is not switched on on the {chain.network} yet (the network's own answer: "
                      "agent_policy_active false)",
                      readings={"policy_switch": sw},
                      remedy="Read-only tools work now. Orders open to agents when the network switches agent policies "
                             "on; get_network shows the current state.")
    return sw


def _agent_ready(chain, cfg, signer):
    """Everything between the configured agent key and the chain, each named with the reading it came from."""
    blockers, readings = [], {}
    if not cfg.allow_trading:
        blockers.append({"blocker": "trading not allowed by the operator",
                         "detail": "start the server with --allow-trading to let it sign orders with the agent key"})
    if not cfg.owner:
        blockers.append({"blocker": "no owner account", "detail": "start the server with --owner (your account address)"})
    if cfg.owner and keys.source(cfg)[0] is None:
        blockers.append({"blocker": "no agent key", "detail": keys.source(cfg)[1]})
    if blockers:
        return blockers, readings, None
    try:
        agent = signer.agent_address(cfg)
    except Exception as e:                                          # the message never carries the key
        blockers.append({"blocker": "agent key unusable", "detail": str(e)[:240]})
        return blockers, readings, None
    if agent.lower() == cfg.owner.lower():
        blockers.append({"blocker": "owner key configured as the agent key",
                         "detail": "this server signs only with an agent key the owner registered; the owner key "
                                   "stays out of it"})
        return blockers, readings, agent
    a = chain.agent(cfg.owner, agent)
    rec = a["json"] if a.ok and isinstance(a["json"], dict) else None
    readings["agent"] = source(a)[0]
    if rec is None or rec.get("found") is False:
        blockers.append({"blocker": "agent not registered", "detail": f"{agent} is not an agent of {cfg.owner}"})
    else:
        if rec.get("revoked") is True or "revok" in str(rec.get("status", "")).lower():
            blockers.append({"blocker": "agent revoked", "detail": f"status {rec.get('status')!r}"})
        exp, now = rec.get("expires_ms"), chain.chain_time_ms()
        if isinstance(exp, int) and (exp == 0 or (now is not None and exp <= now)):
            blockers.append({"blocker": "agent expired", "detail": f"expires_ms {exp} at chain time {now}"})
    if chain.network != "testnet":   # the test network takes an agent's own signature with no session; the main network
        s = chain.session_valid(cfg.owner, agent)        # still asks for one
        sv = s["json"] if s.ok and isinstance(s["json"], dict) else {}
        readings["session"] = {**source(s)[0], "values": sv}
        if not (sv.get("valid") is True and sv.get("confirmed") is True):
            blockers.append({"blocker": "no confirmed session for the agent key",
                             "detail": "the owner must create a session for the agent key before it can sign orders"})
    st = chain.status(cache=False)
    status = st["json"] if st.ok and isinstance(st["json"], dict) else None
    readings["status"] = source(st)[0]
    if status is None:
        blockers.append({"blocker": "node status unreadable", "detail": f"{st['url']} answered {st['status']}"})
    else:
        if status.get("trading_open") is False or status.get("trading_active") is False:
            blockers.append({"blocker": "trading not open", "detail": f"trading_blockers {status.get('trading_blockers')}"})
    return blockers, readings, agent


# Writes that change what the account holds are confirmed by the user (MCP elicitation) unless the user chose
# --orders auto; cancels only take risk off and stop_all must never wait on a dialog.
ASKS = ("place_order", "close_position", "modify_order", "set_tpsl")
# After stop_all, only cancels are signed until `vordium-mcp resume`.
WHILE_STOPPED = ("cancel_order", "cancel_all", "stop_all")
CTX = threading.local()


def _num(x):
    """a figure exactly as entered (no rounding, no exponent), for the user to confirm"""
    from decimal import Decimal
    try:
        d = Decimal(str(x)).normalize()
    except Exception:
        return str(x)
    return format(d, "f")


def _resume_hint(cfg):
    import os
    extra = "" if os.path.expanduser(cfg.state_dir) == os.path.expanduser("~/.vordium-mcp") else f" --state-dir {cfg.state_dir}"
    return f"vordium-mcp resume --owner {cfg.owner} --network {cfg.network}{extra}"


def _session():
    return getattr(CTX, "session", None)


def stop_lock(cfg):
    import os
    return os.path.join(os.path.expanduser(cfg.state_dir), f"stopped-{cfg.network}-{str(cfg.owner or 'none').lower()}.json")


def stopped(cfg):
    import json as _json, os
    p = stop_lock(cfg)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return _json.load(fh)
    except Exception:
        return {"at": "unknown"}


def _confirm(cfg, kind, summary, details):
    """The user says yes in their AI app before anything is signed, or nothing is signed."""
    if kind not in ASKS or cfg.orders == "auto":
        return {"asked": False, "why": "not an order" if kind not in ASKS else "--orders auto (the user's choice)"}
    s = _session()
    if s is None or not s.can_elicit():
        raise Refusal(f"{kind} refused: this AI app cannot ask you to confirm the order (it does not support MCP "
                      "elicitation), and every order is confirmed by you before it is signed",
                      readings={"order": details},
                      remedy="Use an AI app that supports MCP elicitation, or restart the server with --orders auto to "
                             "let orders within the agent's limits on the chain go through without asking. Nothing was "
                             "signed or sent.")
    answer = s.elicit(summary + " Sign it with your agent key?",
                      {"type": "object", "properties": {"confirm": {"type": "boolean", "title": "Sign this order",
                                                                    "description": summary, "default": False}},
                       "required": ["confirm"]})
    if not (isinstance(answer, dict) and answer.get("action") == "accept"
            and isinstance(answer.get("content"), dict) and answer["content"].get("confirm") is True):
        raise Refusal(f"{kind}: you did not confirm it (answer: {(answer or {}).get('action', 'none')})",
                      remedy="Nothing was signed or sent.")
    return {"asked": True, "confirmed": True}


def _write(chain, cfg, kind, params, do, precheck=None, describe=None):
    network_identity(chain)
    require_pin(chain)
    if cfg.read_only:
        raise Refusal(f"{kind} is not available on a read-only server")
    held = stopped(cfg)
    if held is not None and kind not in WHILE_STOPPED:
        raise Refusal(f"{kind} refused: this server was stopped with stop_all ({held.get('at')}); only cancels are signed",
                      remedy=f"Run `{_resume_hint(cfg)}` on this machine to allow trading again. Nothing was signed or sent.")
    sw = _refuse_switch_off(chain)
    from . import signer
    blockers, readings, agent = _agent_ready(chain, cfg, signer)
    readings["policy_switch"] = sw
    pair = None
    if kind in ("place_order", "close_position", "modify_order", "set_tpsl"):
        pair = _pair(chain, params, required=False)
        if pair is None:
            blockers.append({"blocker": "market not found", "detail": f"{params.get('symbol') or params.get('pair_id')!r}"})
        elif pair.get("active") is False or pair.get("paused") or pair.get("delisted"):
            blockers.append({"blocker": "market not trading", "detail": f"{pair.get('symbol')} "
                             f"lifecycle {pair.get('lifecycle_state')!r} paused {pair.get('paused')}"})
    if blockers:
        raise Refusal(f"{kind} refused: " + "; ".join(b["blocker"] for b in blockers),
                      readings={"blockers": blockers, **readings},
                      remedy="Each blocker names the reading it came from. Nothing was signed or sent.")
    if precheck is not None:
        precheck(agent, pair)                          # every network rule first: an order that would be refused is not signed
    asked = _confirm(cfg, kind, *(describe(pair) if describe else (f"{kind}.", dict(params))))
    held = stopped(cfg)                                # stop_all may have run (here or in another process) while the user decided
    if held is not None and kind not in WHILE_STOPPED:
        raise Refusal(f"{kind} refused: this server was stopped with stop_all ({held.get('at')}) while the order waited",
                      remedy=f"Run `{_resume_hint(cfg)}` to allow trading again. Nothing was signed or sent.")
    require_pin(chain, force=True)                     # read again after the user decided: still the pinned network
    try:
        answer = do(signer, agent, pair)
    except Refusal:
        raise
    except networks.NotSigned as e:                     # refused before any signature was made
        raise Refusal(f"{kind} refused: {e}", remedy="Nothing was signed or sent.")
    except Exception as e:
        if type(e).__name__ == "DeliveryUnknown" and getattr(e, "never_sent", False):   # no copy left this machine
            raise Refusal(f"{kind}: the network could not be reached, so nothing was sent", remedy="Try again when the network answers.")
        if type(e).__name__ == "DeliveryUnknown":       # the SDK sent it and no answer came back: neither accepted nor refused
            answer = {"status": "unknown", "delivery_unknown": True, "detail": str(e)[:300],
                      "nonce": getattr(e, "nonce", None), "attempts": getattr(e, "attempts", None)}
            return _unknown(chain, kind, agent, asked, answer)
        raise Refusal(f"{kind}: it failed while signing or sending ({type(e).__name__}); it may have reached "
                      "the chain", readings={"error": clean(str(e))}, remedy="Read get_orders and get_positions before trying again.",
                      note="It may have been signed and sent: check before trying again.")
    code = answer.get("status_code") if isinstance(answer, dict) else None
    # a copy of the same signed request was sent again after a lost answer, and the chain said its nonce was used: the
    # first copy arrived, and nothing was sent twice
    received = isinstance(answer, dict) and answer.get("already_received") is True
    # the answer was lost, and it is not known whether the chain has it: never "accepted", never "refused"
    unknown = isinstance(answer, dict) and not received and (answer.get("status") == "unknown" or answer.get("unknown") is True
                                                               or answer.get("delivery_unknown") is True)
    if unknown:
        return _unknown(chain, kind, agent, asked, answer)
    if isinstance(answer, dict) and answer.get("status") == "not_sent":
        raise Refusal(f"{kind}: the network could not be reached, so nothing was sent", remedy="Try again when the network answers.")
    if isinstance(answer, dict) and answer.get("status") == "filled":     # a cancel that came after the order filled
        return {"summary": f"{kind}: the order filled before the cancel; check your position.", "network": chain.network,
                "sent": True, "agent": agent, "confirmation": asked, "chain_status": None, "chain_answer": answer,
                "accepted": False, "already_received": False,
                "note": "The order is no longer on the book because it filled: read get_positions."}
    return {"summary": f"{kind}: " + ("received by the chain (an earlier copy of the same signed request arrived; it was "
                                      "not made twice)." if received else
                                      "received by the chain." if isinstance(code, int) and 200 <= code < 300 else
                                      f"the chain answered {code}."),
            "network": chain.network, "sent": True, "agent": agent, "confirmation": asked, "chain_status": code, "chain_answer": answer,
            "accepted": received or (isinstance(code, int) and 200 <= code < 300), "already_received": received,
            "note": "This is the chain's own answer, unchanged. Accepted means received, not filled: read "
                    "get_orders and get_positions to see what the chain applied. The chain enforces the agent's "
                    "policy and market limits; it may still drop an order this server let through."}


def _unknown(chain, kind, agent, asked, answer):
    """A write that was sent and whose answer was lost: it may have reached the chain. Never accepted, never refused."""
    return {"summary": f"{kind}: not known whether the chain received it - it may have reached the chain (the answer "
                       "was lost); it was not sent twice. Check the open orders before placing it again.",
            "network": chain.network, "sent": True, "agent": agent, "confirmation": asked, "chain_status": None,
            "chain_answer": answer, "accepted": None, "already_received": False, "delivery_unknown": True,
            "note": "Read get_orders and get_positions to see what the chain applied before sending it again: a new "
                    "attempt is a new order."}


def client_nonce(chain, cfg, kind):
    """The owner's next client nonce from the SDK's allocator: strictly rising across every process on this machine,
    never back when the clock goes back, and checked against the network's window (its block time when it can be read)
    before anything is signed."""
    try:
        from vordium import nonces
    except Exception:
        raise Refusal(f"{kind} refused: signing needs the trade extra", remedy='pip install "vordium-mcp[trade]". Nothing was signed or sent.')
    if not hasattr(nonces, "next_nonce"):
        raise Refusal(f"{kind} refused: the installed Vordium SDK has no nonce allocator",
                      remedy='pip install -U "vordium-mcp[trade]". Nothing was signed or sent.')
    clock = nonces.chain_clock_for(chain.rest, chain.network)
    try:
        return nonces.next_nonce(cfg.owner, nonces.ORDERS, clock() if clock is not None else None)
    except nonces.ClockOutOfWindow as e:
        raise Refusal(f"{kind} refused: {e}", remedy="Set this computer's clock right, then try again. Nothing was signed or sent.")


SIDES = {"buy": 0, "long": 0, "sell": 1, "short": 1}
TYPES = {"market": 0, "limit": 1}


def place_order(chain, cfg, params):
    return _place(chain, cfg, params, "place_order")


def _place(chain, cfg, params, kind):
    side = SIDES.get(str(params.get("side", "")).strip().lower())
    otype = TYPES.get(str(params.get("order_type", "limit")).strip().lower())
    if side is None:
        raise Refusal("side must be buy or sell")
    if otype is None:
        raise Refusal("order_type must be market or limit (stop orders are not available on Vordium)")
    try:
        size = float(params.get("size"))
        leverage = float(params.get("leverage", 1))
        price = float(params.get("price")) if otype == 1 else 0.0
    except (TypeError, ValueError):
        raise Refusal("size, price and leverage must be numbers")
    if not all(math.isfinite(x) for x in (size, price, leverage)):
        raise Refusal("size, price and leverage must be finite numbers")
    if size <= 0 or leverage < 1 or (otype == 1 and price <= 0):
        raise Refusal("size must be above 0, leverage at least 1, and a limit order needs a price above 0")

    nonce = []                                       # taken when the order is signed; sent explicitly, so the chain's refusal record can be matched to it

    def precheck(agent, pair):
        try:
            ctx = pretrade.Ctx(chain, cfg.owner, pair["pair_id"], agent)
        except pretrade.Unreadable as e:             # an order is never signed unchecked
            raise Refusal(f"{kind} refused: the order could not be checked against the network ({e})",
                          remedy="Nothing was signed or sent. Try again when the network answers.")
        o = pretrade.snap(ctx, _chain_order(pair["pair_id"], params, side, otype, size, price, leverage))   # as the signer sends it
        r = pretrade.check(ctx, o)
        if not r["can_place"]:
            raise Refusal(f"{kind} refused before signing — the network would refuse it: "
                          + "; ".join(f["words"] for f in r["failed"]),
                          readings={"failed": r["failed"], "figures": r["figures"], "not_checked": r["not_checked"]},
                          remedy="Fixes that pass: " + ("; ".join(f["label"] for f in r["fixes"] if f.get("passes")) or "none worked out")
                                 + ". Nothing was signed or sent.")

    def do(signer, agent, pair):
        nonce.append(client_nonce(chain, cfg, kind))
        return signer.place(cfg, chain.rest, pair_id=pair["pair_id"], side=side, size=size, price=price,
                            leverage=leverage, order_type=otype, reduce_only=bool(params.get("reduce_only", False)),
                            post_only=bool(params.get("post_only", False)), client_nonce=nonce[0])
    def describe(pair):
        what = (f"{'Buy' if side == 0 else 'Sell'} {_num(params.get('size'))} {label(pair)} "
                + (f"at {_num(params.get('price'))} (limit)" if otype == 1 else "at the market price")
                + f", {_num(params.get('leverage', 1))}x"
                + (", reduce only" if params.get("reduce_only") else "") + f", on the {chain.network}.")
        return what, {"market": label(pair), "side": "buy" if side == 0 else "sell", "size": size,
                      "order_type": "limit" if otype == 1 else "market", "price": price if otype == 1 else None,
                      "leverage": leverage, "reduce_only": bool(params.get("reduce_only", False))}
    out = _write(chain, cfg, kind, params, do, precheck, describe)
    if out.get("accepted"):
        try:
            why = refusal_of(chain, out.get("agent"), nonce[0])
        except Exception:
            why, out["refusal_check"] = None, "the chain's refusal record could not be read; get_orders shows what it applied"
        if why is not None:
            out.update(accepted=False, refused=True, refusal_reason=why,
                       note="The chain received the order and then refused it (its agent_refused record names the reason). "
                            "Nothing reached the book.")
            _add_meaning(chain, out)
    else:
        _intake_refusal(chain, out)
    out["client_nonce"] = nonce[0] if nonce else None
    return out


def _chain_order(pair_id, params, side, otype, size, price, leverage):
    """the order in the chain's units, as the pre-trade check reads it"""
    from decimal import Decimal
    return {"pair_id": pair_id, "side": "buy" if side == 0 else "sell", "size_18dec": int(Decimal(str(size)) * 10 ** 18),
            "price_8dec": int(Decimal(str(price)) * 10 ** 8) if otype == 1 else 0, "order_type": "limit" if otype == 1 else "market",
            "reduce_only": bool(params.get("reduce_only", False)), "post_only": bool(params.get("post_only", False)),
            "leverage_bps": int(Decimal(str(leverage)) * 10000)}


def check_order(chain, cfg, params):
    """the pre-trade check for the configured owner (and its agent, when a key is set), nothing signed or sent"""
    from . import signer
    if not cfg.owner:
        raise Refusal("check_order needs the owner account: start the server with --owner")
    pair = _pair(chain, params, required=True)
    side = SIDES.get(str(params.get("side", "")).strip().lower())
    otype = TYPES.get(str(params.get("order_type", "limit")).strip().lower())
    if side is None or otype is None:
        raise Refusal("side is buy or sell; order_type is market or limit")
    agent = signer.agent_address(cfg) if keys.source(cfg)[0] else None
    ctx = pretrade.Ctx(chain, cfg.owner, pair["pair_id"], agent)
    lev = params.get("leverage")
    leverage = float(lev) if lev not in (None, "") else (pretrade.leverage_cap(ctx) or 10000) / 10000
    r = pretrade.check(ctx, _chain_order(pair["pair_id"], params, side, otype, float(params.get("size")),
                                        float(params.get("price") or 0), leverage))
    r["summary"] = ("The network would take it." if r["can_place"] else "The network would refuse it: "
                    + "; ".join(f["words"] for f in r["failed"])) + " Nothing was signed or sent."
    return r


def my_buying_power(chain, cfg, params):
    """per market: the largest long and short at each leverage the limits allow (free margin and the agent's limits)"""
    from . import signer
    if not cfg.owner:
        raise Refusal("my_buying_power needs the owner account: start the server with --owner")
    agent = signer.agent_address(cfg) if keys.source(cfg)[0] else None
    pairs = [_pair(chain, params, required=True)] if params.get("symbol") or params.get("pair_id") else chain.pairs()[:5]
    out = []
    for p in pairs:
        ctx = pretrade.Ctx(chain, cfg.owner, p["pair_id"], agent)
        cap = pretrade.leverage_cap(ctx)
        levs = [l for l in (10000, 20000, 30000, 50000, 100000, 200000, 500000) if l <= cap] or ([cap] if cap else [])
        out.append({"market": label(p), "max_leverage": cap / 10000,
                    "by_leverage": [{"leverage": l / 10000, "max_long": pretrade.units(pretrade.max_size(ctx, "buy", l), 18),
                                     "max_short": pretrade.units(pretrade.max_size(ctx, "sell", l), 18)} for l in levs]})
    return {"summary": "Sizes at the mark, from free margin" + (" and the agent's limits" if agent else "") + ".", "markets": out}


def refusal_meanings(chain):
    """The network's agent refusal codes: {reason key: plain meaning} from the spec's agents.refusalCodes.reasons, or {}
    when the network publishes none (a reason is then reported as the chain wrote it)."""
    r = chain.spec()
    doc = r["json"] if r.ok and isinstance(r["json"], dict) else {}
    rc = (doc.get("agents") or {}).get("refusalCodes") if isinstance(doc.get("agents"), dict) else None
    out = {}
    for x in (rc or {}).get("reasons") or [] if isinstance(rc, dict) else []:
        if isinstance(x, dict) and isinstance(x.get("key"), str) and isinstance(x.get("meaning"), str):
            out[x["key"]] = x["meaning"]
    return out


def _add_meaning(chain, out):
    """the network's own words for a refusal; read after the order was sent, so a failed read never turns into an error"""
    try:
        meaning = refusal_meanings(chain).get(out.get("refusal_reason"))
    except Exception:
        return
    if meaning:
        out["refusal_meaning"] = meaning


def _intake_refusal(chain, out):
    """A node that checks agent orders at intake answers 403 with code agent_refused, the reason key and its meaning."""
    ans = out.get("chain_answer")
    if isinstance(ans, dict) and ans.get("code") == "agent_refused":
        out.update(refused=True, refusal_reason=str(ans.get("reason") or "refused"),
                   note="The chain refused the agent's order when it received it. Nothing reached the book.")
        if isinstance(ans.get("meaning"), str) and ans["meaning"]:
            out["refusal_meaning"] = ans["meaning"]
        else:
            _add_meaning(chain, out)


REFUSAL_WAIT_S = 4.0


def refusal_of(chain, agent, nonce, wait_s=None, step=0.5):
    """The reason the chain gives for refusing this order (GET /events/agent/{agent}, kind agent_refused, matched by
    client_nonce), or None when it records none within wait_s. A 2xx answer is only receipt: the refusal comes after."""
    if not agent:
        return None
    end = time.monotonic() + (REFUSAL_WAIT_S if wait_s is None else wait_s)
    while True:
        r = chain.agent_events(agent)
        ev = r["json"].get("events") if r.ok and isinstance(r["json"], dict) else None
        for e in ev or []:
            if isinstance(e, dict) and e.get("kind") == "agent_refused" and str(e.get("client_nonce")) == str(nonce):
                return str(e.get("reason") or "refused")
        if time.monotonic() >= end:
            return None
        time.sleep(step)


def cancel_order(chain, cfg, params):
    try:
        order_id = int(params.get("order_id"))
    except (TypeError, ValueError, OverflowError):
        raise Refusal("order_id must be the chain's order id (get_orders lists them)")

    def do(signer, agent, pair):
        return signer.cancel(cfg, chain.rest, order_id=order_id)
    out = _write(chain, cfg, "cancel_order", params, do)
    if not out.get("accepted"):
        _intake_refusal(chain, out)
    return out



# ── closing the circle: one order, one position, everything at once ──
CAP_BITS = {"place": 1, "cancel": 2, "modify": 4, "tpsl": 8, "close": 16}


def _owner(cfg, params, what):
    a = params.get("address") or cfg.owner
    if not a:
        raise Refusal(f"{what} needs an account: give address, or start the server with --owner")
    return _address({"address": a})


def _rows(raw, key):
    return raw if isinstance(raw, list) else ((raw or {}).get(key) or [])


def _open_order(chain, owner, order_id):
    o = chain.orders(owner)
    rows = _rows(chain.require(o, "open orders"), "orders")
    hit = next((r for r in rows if str(r.get("id", r.get("order_id"))) == str(order_id)), None)
    return hit, o


def _position(chain, owner, pair_id):
    pos = chain.positions(owner)
    rows = _rows(chain.require(pos, "positions"), "positions")
    hit = next((p for p in rows if p.get("pair_id") == pair_id and int(str(p.get("size", p.get("size_18dec", 0))) or 0) != 0), None)
    return hit, pos


def _caps_refusal(chain, cfg, agent, cap, kind):
    """The agent's own permission for this kind of write, as the chain stores it (None = allowed or not published)."""
    a = chain.agent(cfg.owner, agent)
    rec = a["json"] if a.ok and isinstance(a["json"], dict) else {}
    pol = rec.get("policy_v1") if isinstance(rec.get("policy_v1"), dict) else None
    bits = pol.get("caps_bitmask") if pol else None
    if isinstance(bits, int) and bits and not bits & CAP_BITS[cap]:
        return Refusal(f"{kind} refused: this agent is not allowed to {cap} (its permissions on the chain: caps {bits})",
                       readings={"agent": source(a)[0], "caps_bitmask": bits},
                       remedy=f"The owner can allow '{cap}' in the agent's permissions (Vordex → Agents → Edit). Nothing "
                              "was signed or sent.")
    return None


def get_order(chain, cfg, params):
    network_identity(chain)
    owner = _owner(cfg, params, "get_order")
    try:
        order_id = int(params.get("order_id"))
    except (TypeError, ValueError, OverflowError):
        raise Refusal("order_id must be the chain's order id (get_orders lists them)")
    hit, o = _open_order(chain, owner, order_id)
    if hit is None:
        return {"summary": f"Order {order_id} is not resting for {short(owner)} now: it filled, was cancelled, or is "
                           "another account's. get_fills shows the account's trades.",
                "network": chain.network, "address": owner, "order_id": order_id, "found": False, "order": None,
                "source": source(o)}
    m = chain.pair_by_id(hit.get("pair_id")) or {}
    hit.setdefault("symbol", m.get("symbol"))
    return {"summary": f"Order {order_id}: {state(hit.get('side'), '')} "
                       f"{symbol(hit.get('symbol'), 'market ' + count(hit.get('pair_id')))} "
                       f"{state(hit.get('order_type'), '')}, {amount(hit.get('remaining_18dec'), 18)} left of {amount(hit.get('size'), 18)}.",
            "network": chain.network, "address": owner, "order_id": order_id, "found": True, "order": hit,
            "source": source(o)}


def close_position(chain, cfg, params):
    """A reduce-only order against the owner's position in one market: all of it, or a fraction."""
    network_identity(chain)
    if not cfg.owner:
        raise Refusal("close_position needs the owner account: start the server with --owner")
    pair = _pair(chain, params, required=True)
    pos, _ = _position(chain, cfg.owner, pair["pair_id"])
    if pos is None:
        raise Refusal(f"close_position: {cfg.owner} has no open position in {label(pair)}",
                      remedy="get_positions lists the account's positions. Nothing was signed or sent.")
    try:
        frac = float(params.get("fraction", 1))
    except (TypeError, ValueError):
        raise Refusal("fraction is a number above 0, at most 1 (default 1: the whole position)")
    if not (0 < frac <= 1):
        raise Refusal("fraction is above 0 and at most 1 (default 1: the whole position)")
    from decimal import Decimal
    size = Decimal(abs(int(str(pos.get("size", pos.get("size_18dec")))))) * Decimal(str(frac)) / Decimal(10 ** 18)
    otype = str(params.get("order_type", "market")).strip().lower()
    order = {"pair_id": pair["pair_id"], "side": "sell" if pos.get("is_long") else "buy", "size": format(size, "f"),
             "order_type": otype, "leverage": float(pos.get("leverage") or 1), "reduce_only": True}
    if otype == "limit":
        order["price"] = params.get("price")
    return _place(chain, cfg, order, "close_position")


def cancel_all(chain, cfg, params):
    """Every open order of the owner (one market, or all of them), as the agent."""
    pair = _pair(chain, params, required=False) if (params.get("symbol") or params.get("pair_id")) else None
    if (params.get("symbol") or params.get("pair_id")) and pair is None:
        raise Refusal(f"cancel_all: no market on this network matches {_asked(params)!r}",
                      remedy="Call get_markets for the symbols and pair_ids, or leave both out to cancel in every market.")
    ms = chain.market_specs()
    one_shot = ms.ok and isinstance(ms["json"], dict) and (ms["json"].get("specs") or {}).get("mm_release_active") is True

    def do(signer, agent, _pair):
        if one_shot:
            return signer.cancel_all(cfg, chain.rest, pair_id=(pair or {}).get("pair_id", 0))
        # this network cancels one order at a time
        listed = _rows(chain.require(chain.orders(cfg.owner), "open orders"), "orders")
        mine = [r for r in listed if pair is None or r.get("pair_id") == pair["pair_id"]]
        answers = []
        for r in mine:
            oid = int(r.get("id", r.get("order_id")))
            try:
                answers.append({"order_id": oid, **signer.cancel(cfg, chain.rest, order_id=oid)})
            except Exception as e:                      # the rest are still tried; each answer says what happened
                answers.append({"order_id": oid, "status_code": None, "error": f"{type(e).__name__}: {str(e)[:120]}"})
        ok = all(isinstance(a.get("status_code"), int) and 200 <= a["status_code"] < 300 for a in answers)
        return {"status_code": 202 if ok else 400, "one_by_one": len(answers), "answers": answers}
    out = _write(chain, cfg, "cancel_all", params, do)
    out["scope"] = label(pair) if pair else "every market"
    return out


def modify_order(chain, cfg, params):
    """A resting order to a new price and/or size: a signed cancel-and-replace (a new order id, no time priority)."""
    from decimal import Decimal
    network_identity(chain)
    if not cfg.owner:
        raise Refusal("modify_order needs the owner account: start the server with --owner")
    try:
        order_id = int(params.get("order_id"))
    except (TypeError, ValueError, OverflowError):
        raise Refusal("order_id must be the chain's order id (get_orders lists them)")
    hit, _ = _open_order(chain, cfg.owner, order_id)
    if hit is None:
        raise Refusal(f"modify_order: order {order_id} is not resting for {cfg.owner}",
                      remedy="get_orders lists the open orders. Nothing was signed or sent.")
    specs = chain.market_specs()
    if not (specs.ok and isinstance(specs["json"], dict) and (specs["json"].get("specs") or {}).get("mm_release_active") is True):
        raise Refusal("modify_order: this network does not change orders in place",
                      remedy="Cancel the order (cancel_order) and place a new one (place_order). Nothing was signed or sent.")
    try:
        new_price = int(Decimal(str(params["price"])) * 10 ** 8) if params.get("price") not in (None, "") else int(hit.get("price_8dec") or 0)
        new_size = int(Decimal(str(params["size"])) * 10 ** 18) if params.get("size") not in (None, "") else int(str(hit.get("remaining_18dec") or hit.get("size")))
    except Exception:
        raise Refusal("price and size must be numbers")
    if new_price <= 0 or new_size <= 0:
        raise Refusal("modify_order needs a price and a size above 0")
    if new_price == int(hit.get("price_8dec") or 0) and new_size == int(str(hit.get("remaining_18dec") or hit.get("size") or 0)):
        raise Refusal("modify_order: that is the order as it rests now; give a new price or size")
    side = "buy" if str(hit.get("side", "")).lower().startswith("b") else "sell"
    nonce = []                                       # taken when the new order is signed
    p2 = {**params, "pair_id": hit.get("pair_id")}

    def precheck(agent, pair):
        r = _caps_refusal(chain, cfg, agent, "modify", "modify_order")
        if r is not None:
            raise r
        try:
            ctx = pretrade.Ctx(chain, cfg.owner, pair["pair_id"], agent)
        except pretrade.Unreadable as e:
            raise Refusal(f"modify_order refused: the new order could not be checked against the network ({e})",
                          remedy="Nothing was signed or sent. Try again when the network answers.")
        o = {"pair_id": pair["pair_id"], "side": side, "size_18dec": new_size, "price_8dec": new_price, "order_type": "limit",
             "reduce_only": bool(hit.get("reduce_only")), "post_only": bool(hit.get("post_only")),
             "leverage_bps": int(hit.get("leverage_bps") or 10000)}
        r = pretrade.check(ctx, pretrade.snap(ctx, o))
        # the order it replaces leaves the book first, so its own margin and size are free again: those rules are the
        # chain's to judge here
        failed = [f for f in r["failed"] if f.get("rule") not in ("margin", "position")]
        if failed:
            raise Refusal("modify_order refused before signing — the network would refuse the new order: "
                          + "; ".join(f["words"] for f in failed),
                          readings={"failed": failed, "figures": r["figures"]}, remedy="Nothing was signed or sent.")

    def describe(pair):
        from decimal import Decimal
        what = (f"Change order {order_id} ({side} {label(pair)}) to "
                f"{_num(Decimal(new_size) / Decimal(10 ** 18))} at {_num(Decimal(new_price) / Decimal(10 ** 8))}, on the "
                f"{chain.network}: it leaves the book and a new order rests in its place.")
        return what, {"order_id": order_id, "new_price": new_price / 1e8, "new_size": new_size / 1e18}

    def do(signer, agent, pair):
        nonce.append(client_nonce(chain, cfg, "modify_order"))
        return signer.modify(cfg, chain.rest, order_id, new_price, new_size, pair["pair_id"], side, nonce[0])
    out = _write(chain, cfg, "modify_order", p2, do, precheck, describe)
    out.update(replaced_order_id=order_id, client_nonce=nonce[0] if nonce else None,
               note="Accepted means received: the new order has a new id (get_orders lists it).")
    return out


def set_tpsl(chain, cfg, params):
    """Set, move or remove the take-profit and stop-loss of the owner's position in one market."""
    from decimal import Decimal
    network_identity(chain)
    if not cfg.owner:
        raise Refusal("set_tpsl needs the owner account: start the server with --owner")
    pair = _pair(chain, params, required=True)
    pos, _ = _position(chain, cfg.owner, pair["pair_id"])
    if pos is None:
        raise Refusal(f"set_tpsl: {cfg.owner} has no open position in {label(pair)}",
                      remedy="A take-profit or stop-loss belongs to an open position. Nothing was signed or sent.")
    pid = pos.get("id", pos.get("position_id"))
    t = chain.read(f"/tpsl/{cfg.owner}", cache=False)
    if not (t.ok and isinstance(t["json"], (list, dict))):
        raise Refusal("set_tpsl: the position's take-profit and stop-loss could not be read, so the leg you did not name "
                      "cannot be kept", readings={"tpsl": source(t)[0]},
                      remedy="Nothing was signed or sent. Try again when the network answers.")
    rows = _rows(t["json"], "orders")
    cur = next((r for r in rows if str(r.get("position_id")) == str(pid)), {}) if rows else {}
    now_tp, now_sl = int(cur.get("take_profit") or 0), int(cur.get("stop_loss") or 0)
    remove = str(params.get("remove", "")).strip().lower()
    if remove not in ("", "take_profit", "stop_loss", "both"):
        raise Refusal("remove is take_profit, stop_loss or both")

    def leg(name, now, gone):
        if gone:
            return 0
        v = params.get(name)
        if v in (None, ""):
            return now
        try:
            x = int(Decimal(str(v)) * 10 ** 8)
        except Exception:
            raise Refusal(f"{name} must be a price")
        if x <= 0:
            raise Refusal(f"{name} must be above 0 (use remove to take it off)")
        return x
    tp = leg("take_profit", now_tp, remove in ("take_profit", "both"))
    sl = leg("stop_loss", now_sl, remove in ("stop_loss", "both"))
    if (tp, sl) == (now_tp, now_sl):
        raise Refusal("set_tpsl: nothing would change (give take_profit, stop_loss, or remove)")
    mark = next((p.get("mark_price") for p in (chain.oracle()["json"] or {}).get("prices", []) if p.get("pair_id") == pair["pair_id"]), None) \
        if chain.oracle().ok and isinstance(chain.oracle()["json"], dict) else None
    long_ = bool(pos.get("is_long"))
    if isinstance(mark, (int, float)) and mark > 0:
        m8 = int(mark * 10 ** 8)
        if tp and tp != now_tp and (tp <= m8 if long_ else tp >= m8):
            raise Refusal(f"set_tpsl: a take-profit for a {'long' if long_ else 'short'} sits {'above' if long_ else 'below'} "
                          f"the mark ({mark:g})", remedy="Nothing was signed or sent.")
        if sl and sl != now_sl and (sl >= m8 if long_ else sl <= m8):
            raise Refusal(f"set_tpsl: a stop-loss for a {'long' if long_ else 'short'} sits {'below' if long_ else 'above'} "
                          f"the mark ({mark:g})", remedy="Nothing was signed or sent.")

    def precheck(agent, _pair):
        r = _caps_refusal(chain, cfg, agent, "tpsl", "set_tpsl")
        if r is not None:
            raise r

    def describe(_pair):
        from decimal import Decimal
        f = lambda v: "none" if not v else _num(Decimal(v) / Decimal(10 ** 8))
        return (f"{label(pair)} {'long' if long_ else 'short'} position: take-profit {f(tp)}, "
                f"stop-loss {f(sl)} (now {f(now_tp)} / {f(now_sl)}), on the {chain.network}."), {"take_profit": tp / 1e8 or None,
                                                                                               "stop_loss": sl / 1e8 or None}

    def do(signer, agent, _pair):
        return signer.set_tpsl(cfg, chain.rest, pair["pair_id"], pid, tp, sl)
    out = _write(chain, cfg, "set_tpsl", {**params, "pair_id": pair["pair_id"]}, do, precheck, describe)
    out.update(position_id=pid, take_profit=tp / 1e8 or None, stop_loss=sl / 1e8 or None)
    return out


def stop_all(chain, cfg, params):
    """The kill switch: this server stops signing (locked until `vordium-mcp resume`), then every open order is cancelled."""
    import json as _json, os
    network_identity(chain)
    if not cfg.owner:
        raise Refusal("stop_all needs the owner account: start the server with --owner")
    p = stop_lock(cfg)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(p, "w", encoding="utf-8") as fh:
        _json.dump({"at": at, "network": chain.network, "owner": cfg.owner}, fh)
    try:
        cancelled = cancel_all(chain, cfg, {})
        cancel_note = cancelled.get("summary")
    except Refusal as r:
        cancelled, cancel_note = None, f"open orders were NOT cancelled: {r.payload.get('reason')}"
    except Exception as e:
        cancelled, cancel_note = None, (f"open orders may NOT be cancelled ({type(e).__name__}): read get_orders and cancel "
                                        "them with cancel_all")
    app = (NETWORKS.get(chain.network) or {}).get("app", "")
    return {"summary": f"Stopped at {at}: this server signs nothing new (only cancels) until `{_resume_hint(cfg)}`. "
                       f"{cancel_note} To end the agent itself on the chain, revoke it in Vordex → Agents (your wallet signs).",
            "network": chain.network, "stopped": True, "at": at, "lock": p, "cancel_all": cancelled,
            "revoke_on_chain": f"{app}/agents" if app else None,
            "note": "An agent key cannot revoke itself on the chain: only the owner can (Agents → Revoke)."}


# ── schemas ──────────────────────────────────────────────────────────────────
ADDR = {"type": "string", "description": "0x-prefixed 20-byte account address (0x followed by 40 hex characters)",
        "pattern": "^0x[0-9a-fA-F]{40}$"}
MARKET = {"symbol": {"type": "string", "description": "market symbol as get_markets lists it, e.g. BTC, ETH or ETHUSDT"},
          "pair_id": {"type": "integer", "description": "or the market's pair id, e.g. 1"}}
SEASON = {"season_id": {"type": "integer", "description": "a season number (default: the current season)"}}


def _t(name, title, description, properties=None, required=None, handler=None):
    schema = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return {"name": name, "title": title, "description": description, "inputSchema": schema,
            "outputSchema": OUTPUT[name], "annotations": {**READ, "title": title}, "handler": handler}


TOOLS = [
    _t("get_status", "Network status",
       "Is the network live: block height, blocks per second, validators agreeing, and whether trading and deposits "
       "are open. Ask: \"Is Vordium up right now?\"", handler=reads.get_status),
    _t("get_network", "Network identity",
       "Which Vordium network this server reads, checked against the network itself, and the facts it vouches for "
       "(chain id, genesis, finalized block, agent trading on or off), and whether it matches the chain id and genesis this "
       "release is pinned to (nothing is signed otherwise). Ask: \"Which network am I connected to?\"",
       handler=get_network),
    _t("get_markets", "Markets",
       "Every market the network lists, with its state, leverage, fees and trading rules (tick, lot, minimum size). "
       "Ask: \"Which markets can I trade?\"", handler=get_markets),
    _t("get_fees", "Fees and margins",
       "Maker and taker fees, maximum leverage, initial and maintenance margin, and the liquidation terms as the network "
       "publishes them (the fee, or none), for every market or one. Ask: \"What does it cost to trade BTC with 10x?\"", dict(MARKET), handler=reads.get_fees),
    _t("get_oracle", "Prices",
       "Oracle and mark prices, for every market or one. Ask: \"What is the ETH price?\"", dict(MARKET),
       handler=get_oracle),
    _t("get_orderbook", "Order book",
       "Resting bids and asks for one market, best first, with the spread. Ask: \"How deep is the BTC book?\"",
       {**MARKET, "depth": {"type": "integer", "description": "levels per side, 1-100 (default 20)", "minimum": 1, "maximum": 100}},
       handler=get_orderbook),
    _t("get_candles", "Candles",
       "Price candles (open, high, low, close, volume) for one market. Ask: \"How did ETH move over the last day?\"",
       {**MARKET, "resolution": {"type": "string", "enum": ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1D", "3D", "1W", "1M"],
                                 "description": "candle length (default 1h)"},
        "price": {"type": "string", "enum": ["mark", "last"], "description": "mark price (default) or last traded price"},
        "limit": {"type": "integer", "description": "number of candles, 1-500 (default 24)", "minimum": 1, "maximum": 500}},
       handler=reads.get_candles),
    _t("get_trades", "Recent trades",
       "The latest trades in one market: time, side, price, size and value. Ask: \"Show the last 10 BTC trades.\"",
       {**MARKET, "limit": {"type": "integer", "description": "1-100 (default 20)", "minimum": 1, "maximum": 100}},
       handler=reads.get_trades),
    _t("get_funding", "Funding",
       "The funding rate, interval and next settlement for one market, and optionally its past settlements. Ask: "
       "\"What has ETH funding been over the last day?\"",
       {**MARKET, "history": {"type": "integer", "description": "also return this many past settlements, 1-400",
                              "minimum": 1, "maximum": 400}}, handler=get_funding),
    _t("get_account", "Account",
       "One account at a glance: USDC balances, positions with entry, mark, unrealized PnL and liquidation price, and "
       "open orders. Ask: \"Check my account 0x…\"", {"address": ADDR}, ["address"], reads.get_account),
    _t("get_fills", "Trade history",
       "An account's recent trades with price, size, fee and realized PnL, as the explorer records them. Ask: "
       "\"What did 0x… trade today?\"",
       {"address": ADDR, "limit": {"type": "integer", "description": "1-50 (default 20)", "minimum": 1, "maximum": 50}},
       ["address"], reads.get_fills),
    _t("get_balance", "Balance",
       "An account's USDC: available to trade, in positions, reserved by orders, unrealized PnL. Ask: \"How much "
       "USDC does 0x… have?\"", {"address": ADDR}, ["address"], get_balance),
    _t("get_positions", "Positions",
       "An account's open positions as the chain stores them. Ask: \"What positions does 0x… hold?\"",
       {"address": ADDR}, ["address"], get_positions),
    _t("get_orders", "Open orders",
       "An account's resting orders, with their order ids. Ask: \"Which orders does 0x… have open?\"",
       {"address": ADDR}, ["address"], get_orders),
    _t("get_agents", "Agents",
       "An owner's AI agents: status, limits and policy, usage, and each agent's latest actions. Ask: \"What are my "
       "agents doing?\"", {"address": {**ADDR, "description": "the owner account"}}, ["address"],
       reads.get_agents),
    _t("get_policy", "Agent policy",
       "Each agent's policy and market limits as the chain stores them, and whether the network enforces them now. "
       "Ask: \"What limits does my agent trade under?\"",
       {"address": {**ADDR, "description": "the owner account"},
        "agent": {"type": "string", "description": "optional agent address"}}, ["address"], get_policy),
    _t("get_risk", "Agent usage",
       "Each agent's usage against its policy, as the chain accounts it. Ask: \"How much of its daily loss limit has "
       "my agent used?\"", {"address": {**ADDR, "description": "the owner account"},
                              "agent": {"type": "string", "description": "optional agent address"}}, ["address"], get_risk),
    _t("get_tiers", "Tiers",
       "Agent limits by tier and the referral tier table, where the network serves them. Ask: \"How many agents can "
       "each tier run?\"", handler=reads.get_tiers),
    _t("get_season", "Rewards season",
       "The current rewards season: status, VORD budget, rules, participants and how many qualify. Ask: \"What is "
       "the current season?\"", dict(SEASON), handler=reads.get_season),
    _t("get_points", "Points",
       "An address's season points by source, its rank, whether it qualifies and its VORD estimate. Ask: \"What's my "
       "rank in the season?\"", {"address": ADDR, **SEASON}, ["address"], reads.get_points),
    _t("get_leaderboard", "Leaderboard",
       "The season's points leaderboard, or the trading leaderboard by PnL, ROI or volume. Ask: \"Who leads the "
       "season?\"",
       {"board": {"type": "string", "enum": ["points", "trading"], "description": "points (default) or trading"},
        "limit": {"type": "integer", "description": "1-100 (default 10)", "minimum": 1, "maximum": 100},
        "window": {"type": "string", "enum": ["24h", "7d", "30d", "all"], "description": "trading board only (default 7d)"},
        "sort": {"type": "string", "enum": ["pnl", "roi", "volume"], "description": "trading board only (default pnl)"},
        **SEASON}, handler=reads.get_leaderboard),
    _t("get_referrals", "Referrals",
       "An address's referral code, tier, referred volume and earnings. Ask: \"How much have my referrals earned?\"",
       {"address": ADDR}, ["address"], reads.get_referrals),
    _t("get_tx", "Transaction",
       "One EVM transaction by hash: block, sender, receiver, value and result, with its explorer link. The hash is "
       "sent to the explorer to look it up, so give only a transaction hash, never a key. Ask: \"Explain transaction "
       "0x…\"",
       {"hash": {"type": "string", "description": "0x-prefixed 32-byte transaction hash", "pattern": "^0x[0-9a-fA-F]{64}$"}},
       ["hash"], reads.get_tx) | {"hash_args": ("hash",)},
    _t("get_block", "Block",
       "One block by height: time, proposer, operations, with its explorer link. Ask: \"What happened in block "
       "152,000,000?\"", {"height": {"type": "integer", "description": "block number", "minimum": 0}}, ["height"],
       reads.get_block),
    _t("get_address", "Address",
       "An address summary: account value, open positions, agents and EVM transactions, with its explorer link. Ask: "
       "\"Look up 0x…\"", {"address": ADDR}, ["address"], reads.get_address),
    _t("get_order", "One order",
       "One resting order by its id: market, side, price, size left. An order that is not resting now (it filled or "
       "was cancelled) is reported as gone. Ask: \"What is the state of order 8840974?\"",
       {"order_id": {"type": "integer", "description": "the chain's order id, as get_orders lists it"},
        "address": {**ADDR, "description": "the account (default: the server's configured owner)"}}, ["order_id"], get_order),
]

# what an order says, for every tool that places one
ORDER_PROPS = {
    **MARKET,
    "side": {"type": "string", "enum": ["buy", "sell"], "description": "buy opens or adds to a long, sell to a short"},
    "size": {"type": "number", "description": "in the market's base asset, e.g. 0.01 for 0.01 ETH"},
    "order_type": {"type": "string", "enum": ["market", "limit"], "description": "limit (default) rests at its price; market fills now"},
    "price": {"type": "number", "description": "limit price in USD (limit orders only)"},
    "leverage": {"type": "number", "description": "e.g. 2 for 2x (default 1)"},
    "reduce_only": {"type": "boolean", "description": "only reduce an open position, never open or add"},
    "post_only": {"type": "boolean", "description": "rest on the book or be refused; never take liquidity"},
}
SIGNED = ("Signed on this machine with the agent key (OS keychain or key file); the chain enforces the agent's policy and "
          "limits. Refused before anything is signed while the network reports agent trading off, or after stop_all.")
ASKED = " Every order is first confirmed by you in your AI app, unless you started the server with --orders auto."


def _w(name, title, description, properties, required=None, handler=None):
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": name, "title": title, "description": description, "inputSchema": schema,
            "outputSchema": OUTPUT[name], "annotations": {**WRITE, "title": title}, "handler": handler, "write": True}


def _l(name, title, description, properties, required=None, handler=None):
    """local only: reads the configured owner and agent; a read-only server lists none of these"""
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": name, "title": title, "description": description, "inputSchema": schema,
            "outputSchema": OUTPUT[name], "annotations": {**LOCAL_READ, "title": title}, "handler": handler, "write": True}


TOOLS += [
    _l("check_order", "Check an order",
       "Check an order against every rule the network runs (market, size, price band, margin, the agent's limits) for "
       "the configured owner and agent, without signing or sending anything: whether it would be taken, each rule it "
       "breaks with the real numbers, and fixes that pass. Ask: \"Can I buy 0.1 ETH at 20x?\"",
       {**ORDER_PROPS, "leverage": {"type": "number", "description": "default: the highest the limits allow"}},
       ["side", "size"], check_order),
    _l("my_buying_power", "My buying power",
       "Per market, the largest long and short the configured owner can open at each allowed leverage, from free margin "
       "and the agent's limits together. Ask: \"How much ETH can I buy?\"", {**MARKET}, None, my_buying_power),
    _w("place_order", "Place order",
       "Place a market or limit order as the configured agent, after checking it against every network rule (an order "
       "the network would refuse is not signed). " + SIGNED + ASKED, ORDER_PROPS, ["side", "size"], place_order),
    _w("close_position", "Close position",
       "Close the owner's position in one market — all of it, or a fraction — with a reduce-only order (market by "
       "default), with the full pre-trade check. " + SIGNED + ASKED,
       {**MARKET, "fraction": {"type": "number", "description": "how much of the position, above 0 and at most 1 (default 1: all)",
                               "exclusiveMinimum": 0, "maximum": 1},
        "order_type": {"type": "string", "enum": ["market", "limit"], "description": "market (default) or limit"},
        "price": {"type": "number", "description": "limit price in USD (limit only)"}}, None, close_position),
    _w("set_tpsl", "Set take-profit / stop-loss",
       "Set, move or remove the take-profit and stop-loss of the owner's position in one market. A leg you do not name "
       "stays as it is; remove takes a leg off. A take-profit sits on the profit side of the mark, a stop-loss on the "
       "loss side. " + SIGNED + ASKED,
       {**MARKET, "take_profit": {"type": "number", "description": "new take-profit price in USD"},
        "stop_loss": {"type": "number", "description": "new stop-loss price in USD"},
        "remove": {"type": "string", "enum": ["take_profit", "stop_loss", "both"], "description": "take a leg off"}},
       None, set_tpsl),
    _w("modify_order", "Modify order",
       "Move one resting order to a new price and/or size: a signed cancel-and-replace (the new order gets a new id and "
       "no time priority); the new order is checked against the network's rules (its margin is the chain's to judge). " + SIGNED + ASKED,
       {"order_id": {"type": "integer", "description": "the chain's order id, as get_orders lists it"},
        "price": {"type": "number", "description": "new limit price in USD (default: unchanged)"},
        "size": {"type": "number", "description": "new size in the base asset (default: what is left)"}},
       ["order_id"], modify_order),
    _w("cancel_order", "Cancel order",
       "Cancel one of the configured owner's resting orders, as the agent. " + SIGNED,
       {"order_id": {"type": "integer", "description": "the chain's order id, as get_orders lists it"}}, ["order_id"],
       cancel_order),
    _w("cancel_all", "Cancel all orders",
       "Cancel every resting order of the configured owner — in one market, or in all of them — as the agent. " + SIGNED,
       {**MARKET}, None, cancel_all),
    _w("stop_all", "Stop all (kill switch)",
       "Stop this server from signing anything new (only cancels, until you run `vordium-mcp resume` on this machine), "
       "then cancel every resting order. Revoking the agent itself on the chain is the owner's step in Vordex → Agents.",
       {}, None, stop_all),
]


def listed_tools(cfg):
    """A read-only server does not list the write tools at all."""
    return [t for t in TOOLS if not (t.get("write") and cfg.read_only)]


# results that carry names or other text the network was given (market, agent and season names, mandates, codes,
# refusal meanings): each says that this text is data
NETWORK_TEXT = ("get_status", "get_network", "get_markets", "get_fees", "get_oracle", "get_orderbook", "get_positions",
                "get_orders", "get_order", "get_account", "get_fills", "get_agents", "get_policy", "get_risk",
                "get_season", "get_points", "get_leaderboard", "get_referrals", "get_tiers", "get_tx", "get_block",
                "check_order", "my_buying_power", "place_order", "close_position", "modify_order", "set_tpsl",
                "cancel_order", "cancel_all")


def call(chain, cfg, name, params):
    tool = next((t for t in listed_tools(cfg) if t["name"] == name), None)
    if tool is None:
        raise Refusal(f"no tool named {name!r} on this server", remedy="Call tools/list.")
    reject_key_material(params, tool.get("hash_args", ()))
    try:
        out = tool["handler"](chain, cfg, params or {})
        if name in NETWORK_TEXT and isinstance(out, dict):
            out.setdefault("data_note", DATA_NOTE)
        return out
    except NotServed as e:
        raise Refusal(f"{e}", remedy="Nothing is filled in from memory. get_status shows what the network serves now.")
    except ChainUnreachable as e:
        raise Refusal(f"the network could not be read: {e}",
                      readings={"network_answer": e.answer} if getattr(e, "answer", None) else None,
                      remedy="No value is returned from memory. Try again when the network answers.")
