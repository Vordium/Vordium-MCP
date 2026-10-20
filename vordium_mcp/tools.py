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
import time
import re

from .chain import ChainUnreachable

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
KEYISH_FIELD = re.compile(r"priv|secret|mnemonic|seed|passphrase|keystore|password|key_?file", re.I)
KEYISH_VALUE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
MNEMONIC = re.compile(r"^(\s*[a-z]{3,8}){12,24}\s*$", re.I)

PRICE_6DEC = 10 ** 6       # the order book and the oracle detail route carry prices as 6-decimal integers
SIZE_18DEC = 10 ** 18      # sizes are 18-decimal integers
USDC_6DEC = 10 ** 6        # account figures are 6-decimal USDC integers


class Refusal(Exception):
    def __init__(self, reason, readings=None, remedy=None):
        super().__init__(reason)
        self.payload = {"refused": True, "reason": reason, "readings": readings or {}, "remedy": remedy,
                        "note": "Nothing was signed or sent."}


def reject_key_material(params):
    """Refuse before anything is read, logged or forwarded. The offending value is never echoed back."""
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
            words = node.split()
            if (MNEMONIC.match(node) and len(words) in (12, 15, 18, 21, 24)) or KEYISH_VALUE.match(node.strip()):
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


def _pair(chain, params, required=True):
    if params.get("pair_id") is not None:
        try:
            p = chain.pair_by_id(int(params.get("pair_id")))
        except (TypeError, ValueError):
            p = None
    else:
        p = chain.pair_by_symbol(params.get("symbol"))
    if p is None and required:
        raise Refusal(f"no market on this network matches {params.get('symbol') or params.get('pair_id')!r}",
                      remedy="Call get_markets: the list is read from the network and changes with it.")
    return p


# ── which network this is ────────────────────────────────────────────────────
IDENTITY_TTL = 60.0


def network_identity(chain, force=False):
    """Check that the network read is the one this server was started for, from three sources: the published spec,
    the network's own answer and the node's status. Any disagreement refuses every tool."""
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
        problems.append(f"chain ids disagree: {ids}")
    gens = {k: v for k, v in (("spec", spec.get("genesisSha256")), ("truth", truth.get("genesis_sha256"))) if v}
    if len(set(gens.values())) > 1:
        problems.append(f"genesis disagrees: {gens}")
    if truth.get("network") and truth.get("network") != chain.network:
        problems.append(f"the network's own answer names {truth.get('network')!r}")
    if problems:
        raise Refusal(f"this server was started for the {chain.network} but the addresses it reads disagree with that: "
                      + "; ".join(problems),
                      readings={"source": source(sp, tr, st)},
                      remedy="Check --network and any custom address (--rest, --spec, --truth).")
    value = {"network": chain.network, "chain_id": ids["spec"], "genesis_sha256": gens.get("spec") or gens.get("truth"),
             "testnet": want_test}
    chain._identity = {"at": now, "value": value}
    return value


def read_switch(chain):
    """The agent policy switch as the network itself vouches for it (its own answer, read now; never remembered)."""
    r = chain.truth(cache=False)
    doc = r["json"] if r.ok and isinstance(r["json"], dict) else None
    if doc is None or doc.get("version") != 1:
        return {"established": False, "why": f"the network's answer could not be read ({r['status']})", "source": source(r)}
    if doc.get("ok") is not True:
        return {"established": False, "why": f"the network cannot vouch right now: {doc.get('error') or 'not ok'}",
                "source": source(r)}
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
           "trading_from_this_server": "off" if cfg.read_only else ("allowed by the operator" if cfg.allow_trading
                                                                     else "not allowed by the operator"),
           "source": source(r)}
    if doc is None or doc.get("version") != 1:
        out["truth"] = {"established": False, "why": f"the network's answer could not be read ({r['status']})"}
        return out
    if doc.get("ok") is not True:
        out["truth"] = {"established": False, "why": str(doc.get("error") or "not ok"), "stale": doc.get("stale")}
        return out
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
    return {"network": chain.network, "count": len(out), "markets": out, "venue_rules": venue,
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
    return {"network": chain.network, "prices": out, "units": "USD, as decimals",
            "source": source(o, chain.markets())}


def get_orderbook(chain, cfg, params):
    network_identity(chain)
    pair = _pair(chain, params)
    depth = params.get("depth", 20)
    try:
        depth = max(1, min(100, int(depth)))
    except (TypeError, ValueError):
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
    return {"network": chain.network, "market": pair.get("symbol"), "pair_id": pair.get("pair_id"),
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
    return {"network": chain.network, "market": pair.get("symbol"), "pair_id": pair.get("pair_id"),
            "rate_per_interval_pct": (rate8 / 10 ** 6) if isinstance(rate8, int) else doc.get("rate_pct"),
            "interval_secs": doc.get("interval_secs"), "next_funding": doc.get("next_funding"),
            "funding_enabled": doc.get("funding_enabled"), "chain_answer": doc,
            "units": "rate_per_interval_pct is a percentage per interval; a positive rate means longs pay shorts",
            "source": source(f)}


def get_balance(chain, cfg, params):
    network_identity(chain)
    address = _address(params)
    eq, bal = chain.equity(address), chain.balance(address)
    if not eq.ok and not bal.ok:
        raise Refusal(f"the network did not return balances for this account ({eq['status']}, {bal['status']})",
                      readings={"source": source(eq, bal)})
    e = eq["json"] if eq.ok else {}
    return {"network": chain.network, "address": address,
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
    return {"network": chain.network, "address": address, "count": len(items), "positions": items,
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
    return {"network": chain.network, "address": address, "count": len(items), "orders": items,
            "source": source(o, chain.markets())}


# ── agents: policy and usage, as the chain stores them ───────────────────────
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
        return {"available": False, "owner": owner, "reason": f"the agent route answered {r['status']}",
                "note": f"No {kind} is returned from defaults or memory.", "source": source(r)}
    doc = r["json"] or {}
    records = ([] if doc.get("found") is False else [doc]) if agent is not None else list(doc.get("agents") or [])
    return {"available": True, "network": chain.network, "owner": owner, "agent": agent,
            "count": doc.get("count", len(records)),
            "agents": [{"record": rec, f"{kind}_fields_present": sorted(k for k in rec if k in keys)} for rec in records],
            "policy_switch": read_switch(chain),
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
                         "detail": "start the server with --allow-trading (or VORDIUM_MCP_ALLOW_TRADING=1) to let it "
                                   "sign orders with the agent key"})
    if not cfg.owner:
        blockers.append({"blocker": "no owner account", "detail": "set --owner (VORDIUM_MCP_OWNER)"})
    if not cfg.agent_key_file:
        blockers.append({"blocker": "no agent key", "detail": "set --agent-key-file (VORDIUM_MCP_AGENT_KEY_FILE)"})
    if blockers:
        return blockers, readings, None
    try:
        agent = signer.agent_address(cfg.agent_key_file)
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
    s = chain.session_valid(cfg.owner, agent)
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


def _write(chain, cfg, kind, params, do):
    network_identity(chain)
    if cfg.read_only:
        raise Refusal(f"{kind} is not available on a read-only server")
    sw = _refuse_switch_off(chain)
    from . import signer
    blockers, readings, agent = _agent_ready(chain, cfg, signer)
    readings["policy_switch"] = sw
    pair = None
    if kind == "place_order":
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
    answer = do(signer, agent, pair)
    code = answer.get("status_code") if isinstance(answer, dict) else None
    return {"network": chain.network, "sent": True, "agent": agent, "chain_status": code, "chain_answer": answer,
            "accepted": isinstance(code, int) and 200 <= code < 300,
            "note": "This is the chain's own answer, unchanged. Accepted means received, not filled: read "
                    "get_orders and get_positions to see what the chain applied. The chain enforces the agent's "
                    "policy and market limits; it may still drop an order this server let through."}


SIDES = {"buy": 0, "long": 0, "sell": 1, "short": 1}
TYPES = {"market": 0, "limit": 1}


def place_order(chain, cfg, params):
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
    if size <= 0 or leverage < 1 or (otype == 1 and price <= 0):
        raise Refusal("size must be above 0, leverage at least 1, and a limit order needs a price above 0")

    nonce = int(time.time() * 1000)                  # sent explicitly, so the chain's refusal record can be matched to it

    def do(signer, agent, pair):
        return signer.place(cfg, chain.rest, pair_id=pair["pair_id"], side=side, size=size, price=price,
                            leverage=leverage, order_type=otype, reduce_only=bool(params.get("reduce_only", False)),
                            post_only=bool(params.get("post_only", False)), client_nonce=nonce)
    out = _write(chain, cfg, "place_order", params, do)
    if out.get("accepted"):
        why = refusal_of(chain, out.get("agent"), nonce)
        if why is not None:
            out.update(accepted=False, refused=True, refusal_reason=why,
                       note="The chain received the order and then refused it (its agent_refused record names the reason). "
                            "Nothing reached the book.")
            _add_meaning(chain, out)
    else:
        _intake_refusal(chain, out)
    out["client_nonce"] = nonce
    return out


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
    meaning = refusal_meanings(chain).get(out.get("refusal_reason"))
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
    except (TypeError, ValueError):
        raise Refusal("order_id must be the chain's order id (get_orders lists them)")

    def do(signer, agent, pair):
        return signer.cancel(cfg, chain.rest, order_id=order_id)
    out = _write(chain, cfg, "cancel_order", params, do)
    if not out.get("accepted"):
        _intake_refusal(chain, out)
    return out


# ── schemas ──────────────────────────────────────────────────────────────────
ADDR = {"type": "string", "description": "0x-prefixed 20-byte account address"}
MARKET = {"symbol": {"type": "string", "description": "market symbol as get_markets lists it, e.g. ETHUSDT or ETH"},
          "pair_id": {"type": "integer", "description": "or the market's pair id"}}
RO = {"readOnlyHint": True, "openWorldHint": True}

TOOLS = [
    {"name": "get_network",
     "description": "Which Vordium network this server reads, checked against the network itself, and the facts the "
                    "network vouches for right now (chain id, genesis, validators, whether trading, deposits and agent "
                    "trading are on). Call this first.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": RO, "handler": get_network},
    {"name": "get_markets",
     "description": "Every market the network lists, with its state, leverage, fees and trading rules (tick, lot, "
                    "minimum size), as published now.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": RO, "handler": get_markets},
    {"name": "get_oracle", "description": "Oracle and mark prices, for every market or one.",
     "inputSchema": {"type": "object", "properties": dict(MARKET)}, "annotations": RO, "handler": get_oracle},
    {"name": "get_orderbook", "description": "Resting bids and asks for one market, best first.",
     "inputSchema": {"type": "object", "properties": {**MARKET, "depth": {"type": "integer", "description": "levels per side, 1-100 (default 20)"}}},
     "annotations": RO, "handler": get_orderbook},
    {"name": "get_funding", "description": "The funding rate, interval and next settlement for one market.",
     "inputSchema": {"type": "object", "properties": dict(MARKET)}, "annotations": RO, "handler": get_funding},
    {"name": "get_balance", "description": "An account's USDC: available to trade, in positions, reserved by orders, unrealized PnL.",
     "inputSchema": {"type": "object", "properties": {"address": ADDR}, "required": ["address"]},
     "annotations": RO, "handler": get_balance},
    {"name": "get_positions", "description": "An account's open positions, with market symbols.",
     "inputSchema": {"type": "object", "properties": {"address": ADDR}, "required": ["address"]},
     "annotations": RO, "handler": get_positions},
    {"name": "get_orders", "description": "An account's resting orders, with their order ids.",
     "inputSchema": {"type": "object", "properties": {"address": ADDR}, "required": ["address"]},
     "annotations": RO, "handler": get_orders},
    {"name": "get_policy",
     "description": "Each agent's policy and market limits as the chain stores them, and whether the network enforces "
                    "them now (its agent policy switch).",
     "inputSchema": {"type": "object", "properties": {"address": {**ADDR, "description": "the owner account"},
                                                      "agent": {"type": "string", "description": "optional agent address"}},
                     "required": ["address"]},
     "annotations": RO, "handler": get_policy},
    {"name": "get_risk", "description": "Each agent's usage against its policy, as the chain accounts it.",
     "inputSchema": {"type": "object", "properties": {"address": {**ADDR, "description": "the owner account"},
                                                      "agent": {"type": "string", "description": "optional agent address"}},
                     "required": ["address"]},
     "annotations": RO, "handler": get_risk},
    {"name": "place_order",
     "description": "Place a market or limit order as the configured agent. Signed on this machine with the agent key "
                    "set in the server's configuration; refused before anything is signed while the network reports "
                    "agent trading off. The chain enforces the agent's policy and limits.",
     "inputSchema": {"type": "object", "properties": {
         **MARKET,
         "side": {"type": "string", "enum": ["buy", "sell"]},
         "size": {"type": "number", "description": "in the market's base asset, e.g. 0.01 ETH"},
         "order_type": {"type": "string", "enum": ["market", "limit"], "description": "default limit"},
         "price": {"type": "number", "description": "limit price in USD (limit orders only)"},
         "leverage": {"type": "number", "description": "e.g. 2 for 2x (default 1)"},
         "reduce_only": {"type": "boolean"}, "post_only": {"type": "boolean"}},
         "required": ["side", "size"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
     "handler": place_order, "write": True},
    {"name": "cancel_order", "description": "Cancel one of the configured owner's resting orders, as the agent. Same "
                                            "gate as place_order.",
     "inputSchema": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
     "handler": cancel_order, "write": True},
]


def listed_tools(cfg):
    """A read-only server does not list the write tools at all."""
    return [t for t in TOOLS if not (t.get("write") and cfg.read_only)]


def call(chain, cfg, name, params):
    tool = next((t for t in listed_tools(cfg) if t["name"] == name), None)
    if tool is None:
        raise Refusal(f"no tool named {name!r} on this server", remedy="Call tools/list.")
    reject_key_material(params)
    try:
        return tool["handler"](chain, cfg, params or {})
    except ChainUnreachable as e:
        raise Refusal(f"the network could not be read: {e}",
                      remedy="No value is returned from memory. Try again when the network answers.")
