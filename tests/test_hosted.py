"""The hosted (public, read-only) server and the read tools it carries, against a fake network: nothing leaves this process."""
import dataclasses, json, os, sys, threading, urllib.error, urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake import AGENT, OWNER, setup  # noqa: E402

from vordium_mcp.config import load  # noqa: E402
from vordium_mcp.networks import NETWORKS, ConfigError  # noqa: E402
from vordium_mcp.server import RateLimit, Stats, handle, make_http_handler  # noqa: E402

N = NETWORKS["testnet"]
R, APP, EXP = N["rest"], N["app"], N["explorer"]
HASH = "0x" + "c7" * 32


def routes():
    return {
        R + "/status": {"chain_id": 101102, "chain_head": 1200, "block_rate_10s": 60.7, "sync_state": "live",
                        "trading_open": True, "trading_active": True, "trading_blockers": [], "inactive_validators": []},
        R + "/trades/1?limit=5": {"pair_id": 1, "trades": [
            {"fill_id": 9, "side": "buy", "price": "2500.5", "size": "0.2", "timestamp": 1_700_000_000, "type": "limit",
             "is_liquidation": False, "taker": OWNER, "maker": AGENT}], "total_count": 1},
        R + "/funding/history/1?limit=3": {"pair_id": 1, "bars": [{"t": 1_700_000_000_000, "rate_bps": "-8446"},
                                                                  {"t": 1_700_003_600_000, "rate_bps": "1000000"}]},
        N["candles"] + "/candles?pair=1&type=mark&res=1h&limit=2": {"bars": [
            {"t": 1_700_000_000, "o": 100, "h": 120, "l": 90, "c": 110, "v": 3, "n": 4},
            {"t": 1_700_003_600, "o": 110, "h": 130, "l": 105, "c": 121, "v": 1, "n": 2}]},
        R + f"/positions/{OWNER}": [{"pair_id": 1, "is_long": False, "size": str(2 * 10 ** 17), "entry_price": 2_400_000_000,
                                     "unrealized_pnl": -20_000_000, "liquidation_price": 3_000_000_000, "margin": 50_000_000}],
        EXP + f"/api/account/{OWNER.lower()}/trades?limit=2": {"items": [
            {"fillId": 9, "pairId": 1, "price8dec": "250050000000", "size18dec": str(2 * 10 ** 17), "role": "taker",
             "takerSide": "buy", "feePaid6dec": "225045", "realizedPnl6dec": "-1500000", "timestampSecs": 1_700_000_000,
             "row": {"height": 77, "valueUsdc": 500.1}}], "next": None},
        R + "/seasons": {"current": 1, "seasons": [{"season_id": 1, "name": "Season 1", "status": "open",
                                                    "start_ms": 1_700_000_000_000, "settings": {"budget_wei": str(10 ** 24)}}]},
        R + "/seasons/1?limit=1": {"participants": 12, "total_points": "100.5", "distribution": {"qualified_count": 4}},
        R + "/seasons/1?limit=2": {"participants": 12, "top": [{"rank": 1, "address": AGENT, "total_points": "40.000000",
                                                                "qualified": True, "vord_wei": str(5 * 10 ** 20)}]},
        R + f"/seasons/1/account/{OWNER}": {"rank": 3, "address": OWNER, "total_points": "12.500000",
                                            "trading_points": "10.000000", "referral_points": "2.500000", "qualified": True,
                                            "vord_wei": str(1234 * 10 ** 18)},
        R + "/agents/limits": {"active": True, "tier_limits": {"SIGNAL": 3, "VECTOR": 5}, "ceiling": 10},
        R + "/referral/tiers": {"tiers": [{"tier": "SIGNAL", "fee_share_bps": 1000, "referee_discount_bps": 500}]},
        R + f"/referral/stats/{OWNER}": {"code": "VORD-1", "tier": "SIGNAL", "referred_volume_usdc": 12000.0,
                                         "total_earned_usdc": 3.5, "taker_only": True, "referee_discount_bps": 500},
        EXP + f"/api/evm/tx/{HASH}": {"found": True, "block": 6262, "from": OWNER, "to": AGENT, "value": "0xde0b6b3a7640000",
                                      "status": 1},
        EXP + "/api/blocks/5": None,
        EXP + "/api/archive/blocks/5": {"block": {"number": 5, "timestamp": 1_700_000_000, "leader": AGENT, "ops_count": 2},
                                        "ops": [{"op_index": 0, "action_type": "order", "actor": OWNER, "summary": "x"}]},
        APP + "/ranks?window=7d&sort=pnl&kind=all&limit=2": {"rows": [{"rank": 1, "account": OWNER, "pnl": 9.5}], "total": 1},
    }


def hosted(**over):
    _, cfg = load(["--hosted", "--http", "127.0.0.1:0"])
    net, _, chain = setup(cfg=cfg, **{**routes(), **over})
    return net, cfg, chain


def call(chain, cfg, name, args=None):
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
               chain, cfg)["result"]
    return r["isError"], r["structuredContent"], json.loads(r["content"][0]["text"])


# ── settings ─────────────────────────────────────────────────────────────────
def test_hosted_settings():
    _, cfg = load(["--hosted", "--http", "127.0.0.1:0"])
    assert cfg.read_only and cfg.hosted and cfg.trust_proxy and not cfg.allow_trading
    assert (cfg.rate_per_minute, cfg.burst, cfg.allow_origins, cfg.short_cache_s) == (60, 20, ("*",), 2.0)
    with pytest.raises(ConfigError):
        load(["--hosted"])
    with pytest.raises(ConfigError):
        load(["--hosted", "--http", "127.0.0.1:1", "--allow-trading"])
    _, local = load([])
    assert not local.hosted and local.short_cache_s == 0.0 and local.burst == local.rate_per_minute


def test_hosted_lists_no_write_tool_and_refuses_one():
    net, cfg, chain = hosted()
    names = [t["name"] for t in handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, chain, cfg)["result"]["tools"]]
    assert "place_order" not in names and "cancel_order" not in names and "get_order" in names and len(names) == 26
    for name in ("place_order", "cancel_order"):
        err, doc, _ = call(chain, cfg, name, {"side": "buy", "size": 1, "symbol": "ETH", "order_id": 1})
        assert err and "no tool named" in doc["reason"]
    assert not any(m != "GET" for m, _ in net.calls)


# ── every new read tool: a summary first, units, sources ─────────────────────
CASES = [("get_status", {}, "is live"), ("get_fees", {"symbol": "ETH"}, "ETHUSDT: taker"),
         ("get_candles", {"symbol": "ETH", "limit": 2}, "+21.00%"), ("get_trades", {"symbol": "ETH", "limit": 5}, "last trade buy"),
         ("get_funding", {"symbol": "ETH", "history": 3}, "Average over the last 2"),
         ("get_account", {"address": OWNER}, "Closest to liquidation"), ("get_fills", {"address": OWNER, "limit": 2}, "realized PnL -$1.50"),
         ("get_agents", {"address": OWNER}, "1 agent"), ("get_tiers", {}, "SIGNAL 3"),
         ("get_season", {}, "budget 1,000,000 VORD"), ("get_points", {"address": OWNER}, "rank 3 of 12"),
         ("get_leaderboard", {"limit": 2}, "40 pts"), ("get_leaderboard", {"board": "trading", "limit": 2}, "$9.50"),
         ("get_referrals", {"address": OWNER}, "VORD-1"), ("get_tx", {"hash": HASH}, "1 VORD, succeeded"),
         ("get_block", {"height": 5}, "2 operations"), ("get_address", {"address": OWNER}, "explorer" and "vordscan")]


@pytest.mark.parametrize("name,args,expect", CASES)
def test_read_tool_answers_with_a_summary(name, args, expect):
    net, cfg, chain = hosted()
    err, doc, text = call(chain, cfg, name, args)
    assert not err, doc
    assert list(text)[0] == "summary" and expect in doc["summary"], doc["summary"]
    assert doc.get("source")


def test_funding_history_is_read_as_8_decimal_rates():
    net, cfg, chain = hosted()
    _, doc, _ = call(chain, cfg, "get_funding", {"symbol": "ETH", "history": 3})
    assert [r["rate_pct"] for r in doc["history"]] == [-0.008446, 1.0]


def test_liquidation_distance_uses_the_mark():
    net, cfg, chain = hosted()
    _, doc, _ = call(chain, cfg, "get_account", {"address": OWNER})
    p = doc["positions"][0]
    assert (p["side"], p["entry_price"], p["liquidation_price"], p["mark_price"]) == ("short", 2400.0, 3000.0, 2500.6)
    assert p["distance_to_liquidation_pct"] == round((3000 - 2500.6) / 2500.6 * 100, 2)


def test_a_route_the_network_does_not_serve_is_said_so():
    net, cfg, chain = hosted(**{R + "/seasons": (404, '{"error":"not found"}')})
    for name, args in (("get_season", {}), ("get_points", {"address": OWNER}), ("get_leaderboard", {})):
        err, doc, _ = call(chain, cfg, name, args)
        assert not err and doc["available"] is False and "does not serve" in doc["summary"]


def test_a_transaction_hash_is_taken_but_key_material_elsewhere_is_not():
    net, cfg, chain = hosted()
    assert not call(chain, cfg, "get_tx", {"hash": HASH})[0]
    err, doc, _ = call(chain, cfg, "get_block", {"height": 5, "note": HASH})
    assert err and HASH not in json.dumps(doc)
    err, doc, _ = call(chain, cfg, "get_tx", {"hash": HASH, "private_key": "x"})
    assert err and "looks like a key" in doc["reason"]


def test_bad_arguments_are_refused_in_plain_words():
    net, cfg, chain = hosted()
    for name, args, words in (("get_candles", {"symbol": "ETH", "resolution": "7m"}, "resolution must be"),
                              ("get_block", {"height": "x"}, "height must be"), ("get_tx", {"hash": "0x12"}, "hash must be"),
                              ("get_account", {"address": "nope"}, "address must be"),
                              ("get_leaderboard", {"board": "trading", "window": "1y"}, "window must be")):
        err, doc, _ = call(chain, cfg, name, args)
        assert err and words in doc["reason"] and doc["summary"].startswith("Refused:")


# ── the short shared cache ───────────────────────────────────────────────────
def test_uncached_reads_are_shared_for_two_seconds_when_hosted():
    net, cfg, chain = hosted()
    now = [1000.0]
    chain.clock = lambda: now[0]
    call(chain, cfg, "get_balance", {"address": OWNER})
    call(chain, cfg, "get_balance", {"address": OWNER})
    eq = R + f"/account/equity/{OWNER}"
    assert net.read_urls().count(eq) == 1
    now[0] += 2.5
    call(chain, cfg, "get_balance", {"address": OWNER})
    assert net.read_urls().count(eq) == 2


def test_the_cache_is_bounded():
    net, cfg, chain = hosted()
    chain.MAX_CACHED = 10
    for i in range(50):
        chain._read_url(f"{R}/x/{i}")
    assert len(chain._cache) <= 10


# ── prompts and resources ────────────────────────────────────────────────────
def test_prompts():
    net, cfg, chain = hosted()
    ps = handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}, chain, cfg)["result"]["prompts"]
    assert [p["name"] for p in ps] == ["market_overview", "check_account", "season_standings", "explain_transaction",
                                       "compare_markets"]
    r = handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
                "params": {"name": "check_account", "arguments": {"address": OWNER}}}, chain, cfg)["result"]
    assert OWNER in r["messages"][0]["content"]["text"]
    r = handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                "params": {"name": "check_account", "arguments": {"address": "0xnope"}}}, chain, cfg)
    assert r["error"]["code"] == -32602
    r = handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/get",
                "params": {"name": "season_standings", "arguments": {}}}, chain, cfg)["result"]
    assert "get_points" not in r["messages"][0]["content"]["text"]
    assert net.calls == []                                               # a prompt reads nothing


def test_resources():
    net, cfg, chain = hosted()
    rs = handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, chain, cfg)["result"]["resources"]
    assert [r["uri"] for r in rs] == ["vordium://network/spec", "vordium://fees", "vordium://docs"]
    for uri, needle in (("vordium://network/spec", "chainId"), ("vordium://fees", "| ETHUSDT |"), ("vordium://docs", "docs.vordium.com")):
        c = handle({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": uri}}, chain, cfg)["result"]["contents"]
        assert needle in c[0]["text"]
    assert handle({"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "file:///etc/passwd"}},
                  chain, cfg)["error"]["code"] == -32002


def test_initialize_offers_tools_prompts_and_resources():
    net, cfg, chain = hosted()
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, chain, cfg)["result"]
    assert r["protocolVersion"] == "2025-03-26" and set(r["capabilities"]) == {"tools", "prompts", "resources"}
    assert "read-only" in r["instructions"] and "uvx vordium-mcp" in r["instructions"]


# ── over HTTP: rate limit, CORS, health ──────────────────────────────────────
@pytest.fixture
def server():
    from http.server import ThreadingHTTPServer
    net, cfg, chain = hosted()
    clock = [0.0]
    limiter = RateLimit(cfg.rate_per_minute, burst=cfg.burst, clock=lambda: clock[0])
    outer = RateLimit(cfg.rate_per_minute * 20, burst=cfg.burst * 20, clock=lambda: clock[0])
    stats = Stats()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(cfg, chain, limiter, stats, outer))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", clock, net
    srv.shutdown()
    srv.server_close()


def req(url, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    r = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def open_session(url, ip):
    """A session id as the server issues it, at initialize."""
    s, h, _ = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                  {"CF-Connecting-IP": ip})
    assert s == 200
    return h["Mcp-Session-Id"]


def test_burst_then_slow_down_per_client(server):
    url, clock, net = server
    ping = {"jsonrpc": "2.0", "id": 41, "method": "prompts/get", "params": {"name": "-"}}
    for _ in range(20):
        assert req(url + "/mcp", ping, {"CF-Connecting-IP": "203.0.113.5"})[0] == 200
    s, h, b = req(url + "/mcp", ping, {"CF-Connecting-IP": "203.0.113.5"})
    doc = json.loads(b)
    assert s == 429 and doc["id"] == 41 and doc["error"]["message"].startswith("Slow down") and int(h["Retry-After"]) >= 1
    assert req(url + "/mcp", ping, {"CF-Connecting-IP": "203.0.113.6"})[0] == 200      # another visitor is not held
    clock[0] += 1.0                                                                       # 60/min refills one a second
    assert req(url + "/mcp", ping, {"CF-Connecting-IP": "203.0.113.5"})[0] == 200
    s, _, b = req(url + "/health")
    c = json.loads(b)["counters"]
    assert c["rate_limited"] == 1 and "203.0.113" not in b


def test_cors_is_open_for_mcp_clients(server):
    url, _, _ = server
    s, h, _ = req(url + "/mcp", headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST"},
                  method="OPTIONS")
    assert s == 204 and h["Access-Control-Allow-Origin"] == "*" and "Mcp-Session-Id" in h["Access-Control-Allow-Headers"]
    s, h, b = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"Origin": "https://chatgpt.com"})
    assert s == 200 and h["Access-Control-Allow-Origin"] == "*"


def test_health_counts_calls_per_tool_and_nothing_else(server):
    url, _, _ = server
    for _ in range(3):
        req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "get_balance", "arguments": {"address": OWNER}}})
    req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
    s, _, b = req(url + "/health")
    d = json.loads(b)
    assert s == 200 and d["read_only"] and d["limits"] == {"per_minute": 60, "burst": 20, "per_address_per_minute": 1200,
                                                           "in_flight": 32, "in_flight_per_address": 8,
                                                           "all_clients_per_minute": 6000, "call_time_limit_s": 25.0}
    assert d["counters"]["tool_calls"] == {"get_balance": 3, "unknown": 1}
    assert OWNER.lower() not in b.lower() and "nope" not in b
    s, _, b = req(url + "/health?deep=1")
    assert s == 200 and json.loads(b)["network_read"]["ok"] is True


def test_http_refuses_writes_and_streams(server):
    url, _, _ = server
    s, _, b = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "place_order", "arguments": {"side": "buy", "size": 1}}})
    assert s == 200 and json.loads(b)["result"]["isError"] is True
    assert req(url + "/mcp")[0] == 405
    assert req(url + "/.well-known/oauth-protected-resource")[0] == 404


# ── review fixes ─────────────────────────────────────────────────────────────
def test_an_account_whose_positions_cannot_be_read_is_not_reported_flat():
    net, cfg, chain = hosted(**{R + f"/positions/{OWNER}": (500, '{"error":"busy"}')})
    err, doc, _ = call(chain, cfg, "get_account", {"address": OWNER})
    assert err and "could not be read" in doc["reason"]


def test_maker_trades_carry_no_realized_pnl_and_are_not_counted_as_zero():
    maker = {"items": [{"fillId": 1, "pairId": 1, "price8dec": "100", "size18dec": "1", "role": "maker", "isClose": True,
                        "feePaid6dec": "0", "realizedPnl6dec": None, "timestampSecs": 1, "row": {}}], "next": None}
    net, cfg, chain = hosted(**{EXP + f"/api/account/{OWNER.lower()}/trades?limit=2": maker})
    _, doc, _ = call(chain, cfg, "get_fills", {"address": OWNER, "limit": 2})
    assert doc["realized_pnl_usd_in_these"] is None and doc["trades_without_recorded_pnl"] == 1
    assert "no realized PnL recorded" in doc["summary"]


def test_trading_roi_is_a_percentage():
    net, cfg, chain = hosted(**{APP + "/ranks?window=7d&sort=pnl&kind=all&limit=2":
                                {"rows": [{"rank": 1, "account": OWNER, "pnl": 68.76, "roi": 0.0191}], "total": 1}})
    _, doc, _ = call(chain, cfg, "get_leaderboard", {"board": "trading", "limit": 2})
    assert doc["rows"][0]["roi_pct"] == 1.91


def test_an_unpublished_trading_state_is_not_called_closed():
    net, cfg, chain = hosted(**{R + "/status": {"chain_id": 101102, "chain_head": 5, "block_rate_10s": 1.0, "sync_state": "live"}})
    _, doc, _ = call(chain, cfg, "get_status")
    assert "closed" not in doc["summary"] and "not published" in doc["summary"]


def test_health_counts_reads_that_reached_the_network(server):
    url, _, net = server
    for _ in range(3):
        req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "get_balance", "arguments": {"address": OWNER}}})
    d = json.loads(req(url + "/health")[2])
    assert d["counters"]["network_reads"] == len(net.calls)


def test_a_refused_post_closes_its_connection(server):
    import http.client
    url, _, _ = server
    host, port = url.split("//")[1].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=5)
    c.request("POST", "/mcp", body=b'{"jsonrpc":"2.0","id":1,"method":"ping"}', headers={"Content-Type": "text/plain"})
    r = c.getresponse()
    assert r.status == 415 and r.getheader("Connection") == "close"
    r.read()
    c.close()


def test_the_cache_drops_expired_reads_and_stays_under_its_byte_cap():
    net, cfg, chain = hosted()
    now = [0.0]
    chain.clock = lambda: now[0]
    chain.MAX_CACHED_BYTES = 2000
    big = "x" * 300
    chain._fetch = lambda m, u, body=None, timeout=12: (200, '{"v":"%s"}' % big)
    for i in range(40):
        chain._read_url(f"{R}/a/{i}")
    assert chain._bytes <= 2000 and len(chain._cache) <= 7
    now[0] += 120
    chain._read_url(f"{R}/b")
    assert set(chain._cache) == {f"{R}/b"}
    chain.short_ttl = 0
    chain._read_url(f"{R}/c", cache=False)                 # a read that can never be served again is not kept
    assert f"{R}/c" not in chain._cache


def test_fills_carry_a_gap_count_not_the_gap_list():
    page = {"items": [], "next": None, "records": {"fromHeight": 1, "throughHeight": 9, "gaps": [[2, 3]] * 1749}}
    net, cfg, chain = hosted(**{EXP + f"/api/account/{OWNER.lower()}/trades?limit=2": page})
    _, doc, text = call(chain, cfg, "get_fills", {"address": OWNER, "limit": 2})
    assert doc["records"] == {"fromHeight": 1, "throughHeight": 9, "gaps": 1749} and len(json.dumps(text)) < 4000


def test_referrals_read_the_friends_discount_from_its_own_field():
    net, cfg, chain = hosted(**{R + f"/referral/stats/{OWNER}": {"code": "VORD-1", "referee_discount_bps": 0,
                                                                  "referees_get_discount_bps": 500}})
    _, doc, _ = call(chain, cfg, "get_referrals", {"address": OWNER})
    assert doc["friends_discount_pct"] == 5.0 and doc["own_discount_as_referee_pct"] == 0.0


@pytest.mark.parametrize("name,args", [("get_block", {"height": float("inf")}), ("get_trades", {"symbol": "ETH", "limit": float("inf")}),
                                       ("get_funding", {"symbol": "ETH", "history": float("inf")}),
                                       ("get_orderbook", {"pair_id": float("inf")}), ("get_orderbook", {"symbol": "ETH", "depth": float("inf")}), ("get_season", {"season_id": float("inf")}), ("get_candles", {"pair_id": float("inf")})])
def test_an_infinite_number_is_refused_or_defaulted_not_an_internal_error(name, args):
    net, cfg, chain = hosted()
    err, doc, _ = call(chain, cfg, name, args)
    assert "OverflowError" not in json.dumps(doc)


# ── per connection: hosted AI clients call from a few addresses for all their users ──
def test_initialize_gives_a_session_and_each_session_has_its_own_budget(server):
    url, clock, net = server
    ip = {"CF-Connecting-IP": "192.0.2.7"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    sids = []
    for _ in range(3):
        s, h, _ = req(url + "/mcp", init, ip)
        assert s == 200 and len(h["Mcp-Session-Id"]) == 32
        sids.append(h["Mcp-Session-Id"])
    assert len(set(sids)) == 3
    ping = {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "-"}}
    for sid in sids:                                   # 3 connections from one address: 20 at once each
        for _ in range(20):
            assert req(url + "/mcp", ping, {**ip, "Mcp-Session-Id": sid})[0] == 200
        s, _, b = req(url + "/mcp", ping, {**ip, "Mcp-Session-Id": sid})
        assert s == 429 and "per connection" in json.loads(b)["error"]["message"]
    s, _, b = req(url + "/health")
    assert sids[0] not in b and "192.0.2.7" not in b


def test_an_address_has_a_looser_cap_across_its_connections():
    from http.server import ThreadingHTTPServer
    net, cfg, chain = hosted()
    clock = [0.0]
    limiter = RateLimit(60, burst=2, clock=lambda: clock[0])
    outer = RateLimit(600, burst=5, clock=lambda: clock[0])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(cfg, chain, limiter, Stats(), outer))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    ping = {"jsonrpc": "2.0", "id": 9, "method": "ping"}
    sids = [open_session(url, f"192.0.2.{100 + i}") for i in range(7)]   # opened elsewhere: this address spends nothing
    codes = [req(url + "/mcp", ping, {"CF-Connecting-IP": "192.0.2.9", "Mcp-Session-Id": sid})[0] for sid in sids]
    srv.shutdown(); srv.server_close()
    assert codes == [200] * 5 + [429] * 2


def test_a_public_server_shares_one_answer_for_the_same_question(server):
    url, clock, net = server
    body = lambda i, sym: {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                           "params": {"name": "get_fees", "arguments": {"symbol": sym}}}
    s1, _, b1 = req(url + "/mcp", body(1, "ETH"))
    n = len(net.calls)
    s2, _, b2 = req(url + "/mcp", body("two", "ETH"))
    assert s1 == s2 == 200 and len(net.calls) == n                       # the second answer read nothing
    d1, d2 = json.loads(b1), json.loads(b2)
    assert d2["id"] == "two" and d1["result"] == d2["result"]
    d3 = json.loads(req(url + "/mcp", body(3, "BTC"))[2])
    assert d3["result"] != d1["result"] and "BTCUSDT" in d3["result"]["structuredContent"]["summary"]   # their own answer
    c = json.loads(req(url + "/health")[2])["counters"]["tool_calls"]
    assert c["get_fees"] == 3


def test_answers_are_never_shared_on_a_server_on_your_own_machine():
    from vordium_mcp.server import ResultCache
    net, cfg, chain = setup()
    assert cfg.short_cache_s == 0
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(dataclasses.replace(cfg, read_only=True, http="x:1"),
                                                                   chain, RateLimit(600)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_balance", "arguments": {"address": OWNER}}}
    req(url + "/mcp", body); n = len(net.calls); req(url + "/mcp", body)
    srv.shutdown(); srv.server_close()
    assert len(net.calls) > n


def test_the_answer_cache_is_bounded_and_expires():
    from vordium_mcp.server import ResultCache
    now = [0.0]
    rc = ResultCache(max_bytes=800, clock=lambda: now[0])
    for i in range(20):
        rc.put(f"k{i}", 5, "x" * 90)
    assert rc.bytes <= 800
    now[0] += 6
    assert rc.get("k19") is None
    rc.put("big", 5, "y" * 200)                                        # larger than an eighth of the cap: not kept
    assert rc.get("big") is None and rc.bytes == 0


def test_the_rate_table_has_a_hard_bound():
    rl = RateLimit(60, burst=20, max_clients=100)
    for i in range(5000):
        rl.take(f"s:{i:032x}")
    assert len(rl.buckets) <= 100


def test_a_refused_connection_does_not_spend_its_address_budget():
    from http.server import ThreadingHTTPServer
    net, cfg, chain = hosted()
    clock = [0.0]
    limiter = RateLimit(60, burst=2, clock=lambda: clock[0])
    outer = RateLimit(600, burst=4, clock=lambda: clock[0])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(cfg, chain, limiter, Stats(), outer))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    ping = {"jsonrpc": "2.0", "id": 9, "method": "prompts/get", "params": {"name": "-"}}
    a, b = open_session(url, "192.0.2.40"), open_session(url, "192.0.2.40")
    loud = [req(url + "/mcp", ping, {"CF-Connecting-IP": "192.0.2.4", "Mcp-Session-Id": a})[0] for _ in range(10)]
    quiet = req(url + "/mcp", ping, {"CF-Connecting-IP": "192.0.2.4", "Mcp-Session-Id": b})[0]
    srv.shutdown(); srv.server_close()
    assert loud == [200, 200] + [429] * 8 and quiet == 200


def test_a_refusal_is_never_shared_and_arguments_are_not_kept(server):
    url, clock, net = server
    import vordium_mcp.server as srvmod
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_account", "arguments": {"address": OWNER, "private_key": "x"}}}
    s, _, b = req(url + "/mcp", body)
    assert s == 200 and json.loads(b)["result"]["isError"] is True
    s, _, b = req(url + "/mcp", body)
    assert json.loads(b)["result"]["isError"] is True
    d = json.loads(req(url + "/health")[2])["counters"]
    assert d["refused"] == 2


def test_requests_without_a_session_count_against_the_address_cap_too():
    from http.server import ThreadingHTTPServer
    net, cfg, chain = hosted()
    clock = [0.0]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(cfg, chain, RateLimit(60, burst=50, clock=lambda: clock[0]),
                                                                   Stats(), RateLimit(600, burst=3, clock=lambda: clock[0])))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    codes = [req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"CF-Connecting-IP": "192.0.2.8"})[0] for _ in range(5)]
    srv.shutdown(); srv.server_close()
    assert codes == [200, 200, 200, 429, 429]


def test_new_connections_count_against_their_address_only():
    from http.server import ThreadingHTTPServer
    net, cfg, chain = hosted()
    clock = [0.0]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(cfg, chain, RateLimit(60, burst=2, clock=lambda: clock[0]),
                                                                   Stats(), RateLimit(600, burst=50, clock=lambda: clock[0])))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    codes = [req(url + "/mcp", init, {"CF-Connecting-IP": "192.0.2.10"})[0] for _ in range(6)]
    srv.shutdown(); srv.server_close()
    assert codes == [200] * 6            # many users of one hosted client open connections from one address


def test_concurrent_identical_reads_reach_the_network_once():
    import time as _t
    net, cfg, chain = hosted()
    calls = []

    def slow(method, url, body=None, timeout=12):
        calls.append(url)
        _t.sleep(0.3)
        return 200, '{"ok": true}'
    chain._fetch = slow
    ts = [threading.Thread(target=chain._read_url, args=(f"{R}/x",)) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls == [f"{R}/x"]


def test_a_failed_shared_read_is_reported_to_its_waiters_not_retried():
    import time as _t
    from vordium_mcp.chain import ChainUnreachable
    net, cfg, chain = hosted()
    n = [0]

    def down(method, url, body=None, timeout=12):
        n[0] += 1
        _t.sleep(0.2)
        raise ChainUnreachable("down")
    chain._fetch = down
    out = []

    def go():
        try:
            chain._read_url(f"{R}/y")
            out.append("ok")
        except ChainUnreachable:
            out.append("down")
    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join(timeout=10) for t in ts]
    assert out == ["down"] * 5 and n[0] == 1 and not chain._inflight


def test_a_slow_or_unkept_shared_read_still_reaches_the_network_once():
    import time as _t
    net, cfg, chain = hosted()
    calls = []

    def slow_big(method, url, body=None, timeout=12):
        calls.append(url)
        _t.sleep(0.3)
        return 200, '{"v": "' + "x" * (2 ** 20 + 10) + '"}'          # over the size a cache keeps
    chain._fetch = slow_big
    chain.short_ttl = 0.1                                               # older than its keep by the time it lands
    ts = [threading.Thread(target=chain._read_url, args=(f"{R}/z", False)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls == [f"{R}/z"]


def test_listing_what_the_server_offers_costs_no_connection_budget(server):
    """a scanner or client that lists tools, prompts and resources many times is never told to slow down for it
    (only tools/call and the other real requests spend a connection's budget; the address budget still applies)."""
    url, clock, net = server
    ip = {"CF-Connecting-IP": "198.51.100.7"}
    s, h, b = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, ip)
    sid = {**ip, "Mcp-Session-Id": h["Mcp-Session-Id"]}
    for i in range(15):                                   # 60 listing calls on one connection: its own bucket holds 20
        for m in ("tools/list", "prompts/list", "resources/list", "ping"):
            assert req(url + "/mcp", {"jsonrpc": "2.0", "id": i, "method": m}, sid)[0] == 200, (i, m)


def test_result_cache_reputs_an_expired_key():
    """An expired answer asked for again is stored again, and the byte
    count matches what is held."""
    from vordium_mcp.server import ResultCache
    now = [0.0]
    c = ResultCache(max_bytes=1000, clock=lambda: now[0])
    c.put("k", 1, "aaaa")
    c.put("other", 1, "bb")
    now[0] = 5.0                       # both expired
    c.put("k", 1, "cccccc")            # the expired key is stored again
    assert c.get("k") == "cccccc" and c.get("other") is None
    assert c.bytes == sum(len(v[1]) for v in c.items.values()) == 6
    c.put("k", 1, "dd")                # live key replaced
    assert c.get("k") == "dd" and c.bytes == 2


def test_a_failing_answer_cache_still_answers(server, monkeypatch):
    """The cache only saves work: if storing an answer fails, the caller still gets it, and the failure is counted."""
    import vordium_mcp.server as srvmod
    url, clock, net = server

    def broken(self, key, ttl, text):
        raise KeyError("cache")
    monkeypatch.setattr(srvmod.ResultCache, "put", broken)
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}}
    s, _, b = req(url + "/mcp", body)
    doc = json.loads(b)
    assert s == 200 and doc["id"] == 7 and doc["result"]["isError"] is False
    assert json.loads(req(url + "/health")[2])["counters"]["cache_errors"] == 1
