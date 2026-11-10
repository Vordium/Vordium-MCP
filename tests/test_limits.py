"""The public server's limits: calls at once (per address and in all), every client together, the time one call may take,
session ids the server can check, and addresses grouped as the network groups them. Against a fake network only."""
import json, os, socket, sys, threading, time, urllib.error, urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake import OWNER, FakeNet, setup  # noqa: E402

from vordium_mcp.chain import ChainUnreachable  # noqa: E402
from vordium_mcp.config import load  # noqa: E402
from vordium_mcp.limits import (InFlight, Limits, SessionIds, TimeBudgetSpent, budget_fetch, client_key,  # noqa: E402
                                deadline, limits_from_env)
from vordium_mcp.networks import NETWORKS, ConfigError  # noqa: E402
from vordium_mcp.server import RateLimit, Stats, _Server, handle, make_chain, make_http_handler  # noqa: E402

N = NETWORKS["testnet"]


def account(i):
    return "0x" + f"{i:040x}"


def balance_call(i, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": "get_balance", "arguments": {"address": account(i)}}}


class Gate:
    """A fake network whose balance reads wait until the test opens the gate (or a delay passes)."""

    def __init__(self, delay=None):
        self.net, self.open, self.delay, self.waiting, self.lock = FakeNet(), threading.Event(), delay, 0, threading.Lock()
        self.timeouts = []

    def __call__(self, method, url, body=None, timeout=12):
        self.timeouts.append(timeout)
        if "/balance/" in url or "/account/equity/" in url:
            with self.lock:
                self.waiting += 1
            if self.delay is not None:
                if self.delay > timeout:                 # as a socket would: the read gives up at its timeout
                    time.sleep(timeout)
                    raise ChainUnreachable(f"{url}: TimeoutError: timed out")
                time.sleep(self.delay)
            else:
                self.open.wait(10)
            return 200, json.dumps({"total": 1, "free_balance": 1, "total_equity": 1})
        return self.net(method, url, body, timeout)


def hosted_server(fetch, limits=None, limiter=None, outer=None, global_limiter=None, max_connections=48, stats=None):
    _, cfg = load(["--hosted", "--http", "127.0.0.1:0"])
    chain = make_chain(cfg, fetch=fetch)
    stats = stats or Stats()
    limiter = limiter or RateLimit(cfg.rate_per_minute, burst=cfg.burst)
    handler = make_http_handler(cfg, chain, limiter, stats, outer, limits=limits, global_limiter=global_limiter)
    srv = _Server(("127.0.0.1", 0), handler, max_connections=max_connections, stats=stats)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", stats


def stop(srv):
    srv.shutdown()
    srv.server_close()


def req(url, body=None, headers=None, method=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    r = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def ip(a):
    return {"CF-Connecting-IP": a}


def open_session(url, address="198.51.100.1"):
    s, h, _ = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18"}}, ip(address))
    assert s == 200
    return h["Mcp-Session-Id"]


def until(cond, limit=5.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ── addresses: IPv4 by address, IPv6 by /64 ──────────────────────────────────
def test_ipv4_addresses_are_counted_one_by_one():
    assert client_key("203.0.113.5") == "203.0.113.5"
    assert client_key("203.0.113.5") != client_key("203.0.113.6")
    assert client_key("::ffff:203.0.113.5") == "203.0.113.5"          # an IPv4 address written as IPv6 is itself


def test_ipv6_addresses_are_counted_by_their_64():
    k = client_key("2001:db8:1:2::1")
    assert k == "2001:db8:1:2::/64"
    for other in ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:0db8:0001:0002:0000:0000:0000:0042", "2001:DB8:1:2:a:b:c:d"):
        assert client_key(other) == k
    assert client_key("2001:db8:1:3::1") != k
    assert client_key("fe80::1%eth0") == "fe80::/64"
    assert client_key("not an address").startswith("?") and len(client_key("x" * 500)) == 65


def test_ipv6_neighbours_in_one_64_share_the_address_budget():
    srv, url, _ = hosted_server(Gate(delay=0), outer=RateLimit(600, burst=3))
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    codes = [req(url + "/mcp", ping, ip(f"2001:db8:aa:bb::{i:x}"))[0] for i in range(1, 6)]
    other = req(url + "/mcp", ping, ip("2001:db8:aa:bc::1"))[0]
    v4 = [req(url + "/mcp", ping, ip(f"192.0.2.{i}"))[0] for i in range(1, 6)]
    stop(srv)
    assert codes == [200, 200, 200, 429, 429] and other == 200 and v4 == [200] * 5


# ── session ids ──────────────────────────────────────────────────────────────
def test_session_ids_check_out_only_when_this_server_issued_them():
    ids = SessionIds()
    sid = ids.issue()
    assert len(sid) == 32 and ids.valid(sid) and len({ids.issue() for _ in range(50)}) == 50
    flipped = sid[:-1] + ("0" if sid[-1] != "0" else "1")
    assert not ids.valid(flipped) and not ids.valid(sid[:16] + "0" * 16)
    assert not SessionIds().valid(sid)                      # a server started again does not know it
    assert not ids.valid("a" * 32) and not ids.valid(sid.upper()) and not ids.valid(sid + "0") and not ids.valid(None)


def test_a_made_up_session_id_is_refused_and_costs_its_address():
    stats = Stats()
    srv, url, _ = hosted_server(Gate(delay=0), outer=RateLimit(600, burst=4), stats=stats)
    sid = open_session(url, "198.51.100.9")
    call = {"jsonrpc": "2.0", "id": 7, "method": "prompts/get", "params": {"name": "-"}}
    s, _, b = req(url + "/mcp", call, {**ip("192.0.2.50"), "Mcp-Session-Id": "ab" * 16})
    doc = json.loads(b)
    assert s == 404 and doc["id"] == 7 and doc["error"]["code"] == -32001 and "initialize" in doc["error"]["message"]
    # a listing with a made-up id is refused too; each refusal spends the address, then the address is slowed down
    codes = [req(url + "/mcp", {"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
                 {**ip("192.0.2.50"), "Mcp-Session-Id": f"{i:032x}"})[0] for i in range(5)]
    assert codes == [404, 404, 404, 429, 429]
    assert req(url + "/mcp", call, {**ip("198.51.100.9"), "Mcp-Session-Id": sid})[0] == 200      # an issued id works
    health = req(url + "/health")[2]
    stop(srv)
    assert json.loads(health)["counters"]["unknown_session"] == 4 and "ab" * 16 not in health


def test_minting_session_ids_gives_no_fresh_budget():
    """With a budget of 2 per connection, a client inventing a new id for every request gets no answer at all."""
    srv, url, _ = hosted_server(Gate(delay=0), limiter=RateLimit(60, burst=2), outer=RateLimit(6000, burst=100))
    call = {"jsonrpc": "2.0", "id": 1, "method": "prompts/get", "params": {"name": "-"}}
    minted = [req(url + "/mcp", call, {**ip("192.0.2.60"), "Mcp-Session-Id": os.urandom(16).hex()})[0] for _ in range(10)]
    sid = open_session(url, "192.0.2.60")
    issued = [req(url + "/mcp", call, {**ip("192.0.2.60"), "Mcp-Session-Id": sid})[0] for _ in range(4)]
    stop(srv)
    assert minted == [404] * 10 and issued == [200, 200, 429, 429]


def test_initialize_never_takes_the_clients_own_id():
    srv, url, _ = hosted_server(Gate(delay=0))
    mine = "cd" * 16
    s, h, _ = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                  {**ip("192.0.2.61"), "Mcp-Session-Id": mine})
    call = {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "-"}}
    given = req(url + "/mcp", call, {**ip("192.0.2.61"), "Mcp-Session-Id": h["Mcp-Session-Id"]})[0]
    kept = req(url + "/mcp", call, {**ip("192.0.2.61"), "Mcp-Session-Id": mine})[0]
    stop(srv)
    assert s == 200 and h["Mcp-Session-Id"] != mine and given == 200 and kept == 404


def test_a_server_on_your_own_machine_ignores_session_ids():
    net, cfg, chain = setup()
    import dataclasses
    srv = _Server(("127.0.0.1", 0), make_http_handler(dataclasses.replace(cfg, read_only=True, http="x:1"), chain,
                                                       RateLimit(600)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    s, h, _ = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Mcp-Session-Id": "ab" * 16})
    stop(srv)
    assert s == 200 and "Mcp-Session-Id" not in h


# ── calls at once ────────────────────────────────────────────────────────────
def test_in_flight_counts_per_key_and_in_all():
    f = InFlight(total=3, per_key=2)
    assert [f.enter("a"), f.enter("a"), f.enter("a")] == [None, None, "address"]
    assert f.enter("b") is None and f.enter("c") == "total" and f.running() == 3
    f.leave("a")
    assert f.enter("c") is None and f.enter("c") == "total"
    for k in ("a", "b", "c"):
        f.leave(k)
    assert f.running() == 0 and f.by_key == {}


def test_calls_at_once_are_capped_per_address_and_in_all_and_listing_still_answers():
    gate = Gate()
    stats = Stats()
    srv, url, _ = hosted_server(gate, limits=Limits(in_flight=3, in_flight_per_address=2, max_connections=10), stats=stats)
    out = {}

    def run(name, i, address):
        out[name] = req(url + "/mcp", balance_call(i), ip(address))

    held = [threading.Thread(target=run, args=(f"a{i}", i, "192.0.2.70")) for i in range(2)]
    held.append(threading.Thread(target=run, args=("b0", 10, "192.0.2.71")))
    for t in held:
        t.start()
    assert until(lambda: gate.waiting >= 3)                                    # three calls are reading the network
    t0 = time.monotonic()
    s_a, h_a, b_a = req(url + "/mcp", balance_call(3, "third"), ip("192.0.2.70"))    # the same address, a third call
    s_c, h_c, b_c = req(url + "/mcp", balance_call(4), ip("192.0.2.72"))              # a new address, all places taken
    fast = time.monotonic() - t0
    listing = req(url + "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, ip("192.0.2.70"))[0]
    opened = req(url + "/mcp", {"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {}}, ip("192.0.2.70"))[0]
    health = json.loads(req(url + "/health")[2])
    gate.open.set()
    for t in held:
        t.join(10)
    after = req(url + "/mcp", balance_call(5), ip("192.0.2.72"))[0]
    stop(srv)
    assert fast < 1.0                                                          # refused at once, never queued
    assert s_a == 429 and json.loads(b_a)["id"] == "third" and "at once per address" in json.loads(b_a)["error"]["message"]
    assert s_c == 503 and json.loads(b_c)["error"]["code"] == -32030 and h_c["Retry-After"] == "1"
    assert listing == 200 and opened == 200                                    # what costs nothing still answers
    assert health["counters"]["in_flight"] == 3 and health["counters"]["busy"] == 2
    assert [out[k][0] for k in ("a0", "a1", "b0")] == [200, 200, 200] and after == 200


def test_every_client_together_has_a_cap_and_shared_answers_do_not_spend_it():
    stats = Stats()
    clock = [0.0]
    srv, url, _ = hosted_server(Gate(delay=0), global_limiter=RateLimit(60, burst=2, max_clients=1, clock=lambda: clock[0]),
                                stats=stats)
    codes = [req(url + "/mcp", balance_call(i), ip(f"192.0.2.{80 + i}")) for i in range(3)]
    again = req(url + "/mcp", balance_call(0, 9), ip("192.0.2.90"))[0]             # the same question: a shared answer
    listing = req(url + "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, ip("192.0.2.91"))[0]
    clock[0] += 1.0
    later = req(url + "/mcp", balance_call(7), ip("192.0.2.92"))[0]
    health = json.loads(req(url + "/health")[2])["counters"]
    stop(srv)
    assert [c[0] for c in codes] == [200, 200, 503] and int(codes[2][1]["Retry-After"]) >= 1
    assert "Busy" in json.loads(codes[2][2])["error"]["message"]
    assert again == 200 and listing == 200 and later == 200 and health["in_flight"] == 0


def test_connections_past_the_cap_get_a_fast_503_and_places_come_back():
    srv, url, stats = hosted_server(Gate(delay=0), limits=Limits(socket_timeout_s=5, in_flight=1, max_connections=2),
                                    max_connections=2)
    port = srv.server_address[1]
    holders = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]    # two idle connections hold both places
    time.sleep(0.2)
    extra = socket.create_connection(("127.0.0.1", port))
    extra.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    extra.settimeout(2)
    t0 = time.monotonic()
    reply = b""
    while True:
        chunk = extra.recv(4096)
        if not chunk:
            break
        reply += chunk
    fast = time.monotonic() - t0
    extra.close()
    for h in holders:
        h.close()
    time.sleep(0.2)
    after = req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, ip("192.0.2.95"))[0]
    stop(srv)
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 503") and b"Retry-After: 1" in head and fast < 1.0
    assert json.loads(body)["error"]["code"] == -32030 and after == 200 and stats.busy == 1


# ── the time one call may take ───────────────────────────────────────────────
def test_reads_share_the_calls_time_limit_and_a_late_read_is_not_made():
    seen = []

    def slow(method, url, body=None, timeout=12):
        seen.append(timeout)
        time.sleep(0.3)
        return 200, "{}"

    fetch = budget_fetch(slow)
    with deadline(0.5):
        fetch("GET", "u1")                                  # 0.3 s of 0.5
        fetch("GET", "u2")                                  # waits at most what is left
        with pytest.raises(TimeBudgetSpent) as e:
            fetch("GET", "u3")
    assert seen[0] <= 0.5 and seen[1] <= 0.21 and len(seen) == 2 and "time limit of 0.5 s" in str(e.value)
    assert isinstance(e.value, ChainUnreachable)


def test_without_a_time_limit_reads_are_made_exactly_as_before():
    seen = []

    def rec(method, url, body=None, **kw):
        seen.append(kw)
        return 200, "{}"

    fetch = budget_fetch(rec)
    fetch("GET", "a")
    fetch("POST", "b", {"x": 1})
    fetch("GET", "c", timeout=3)
    assert seen == [{}, {}, {"timeout": 3}]


def test_a_slow_tool_is_cut_off_cleanly_and_its_answer_is_not_shared():
    gate = Gate(delay=0.8)
    stats = Stats()
    srv, url, _ = hosted_server(gate, limits=Limits(call_budget_s=1.0), stats=stats)
    req(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_network", "arguments": {}}})
    t0 = time.monotonic()
    s, _, b = req(url + "/mcp", balance_call(1, "slow"), ip("192.0.2.100"))
    took = time.monotonic() - t0
    n = gate.waiting
    s2, _, b2 = req(url + "/mcp", balance_call(1, "again"), ip("192.0.2.100"))
    health = json.loads(req(url + "/health")[2])["counters"]
    stop(srv)
    doc = json.loads(b)
    assert s == 200 and doc["id"] == "slow" and doc["result"]["isError"] is True and took < 1.6
    text = json.dumps(doc["result"]["structuredContent"])
    assert "time limit of 1 s" in text and "Nothing was signed or sent." in text
    # asked again: read again (the first read is still shared, so this one finishes in time), never the cut-off answer
    assert s2 == 200 and gate.waiting > n and json.loads(b2)["result"]["isError"] is False
    assert health["time_budget"] == 1 and health["in_flight"] == 0 and max(gate.timeouts) <= 1.0


def test_the_time_limit_stays_below_the_proxys():
    assert Limits().call_budget_s + 12 < 60                  # a read already waiting on another call's read: at most 12 s more


# ── settings ─────────────────────────────────────────────────────────────────
def test_limits_from_the_environment():
    assert limits_from_env({}) == Limits()
    lim = limits_from_env({"VORDIUM_MCP_MAX_IN_FLIGHT": "20", "VORDIUM_MCP_MAX_IN_FLIGHT_PER_ADDRESS": "4",
                           "VORDIUM_MCP_CALL_BUDGET_S": "20.5", "VORDIUM_MCP_CANDLES_BASE": "http://127.0.0.1:4182/"})
    assert (lim.in_flight, lim.in_flight_per_address, lim.call_budget_s, lim.candles_base) == (20, 4, 20.5,
                                                                                                 "http://127.0.0.1:4182")
    for bad in ({"VORDIUM_MCP_MAX_IN_FLIGHT": "x"}, {"VORDIUM_MCP_MAX_IN_FLIGHT": "0"},
                {"VORDIUM_MCP_CALL_BUDGET_S": "nan"}, {"VORDIUM_MCP_CALL_BUDGET_S": "inf"},
                {"VORDIUM_MCP_MAX_IN_FLIGHT": "4", "VORDIUM_MCP_MAX_IN_FLIGHT_PER_ADDRESS": "5"},
                {"VORDIUM_MCP_MAX_IN_FLIGHT": "48"},
                {"VORDIUM_MCP_CANDLES_BASE": "https://example.com"}):
        with pytest.raises(ConfigError):
            limits_from_env(bad)


# ── candles by the shorter path ──────────────────────────────────────────────
def candle_chain(limits):
    _, cfg = load(["--hosted", "--http", "127.0.0.1:0"])
    local = limits.candles_base + "/candles?pair=1&type=mark&res=1h&limit=2"
    net = FakeNet(**{local: {"bars": [{"t": 1_700_000_000, "o": 1, "h": 2, "l": 1, "c": 2, "v": 0, "n": 0}]}})
    return net, cfg, make_chain(cfg, fetch=net, limits=limits)


def test_candles_can_be_read_by_a_shorter_path_and_answers_name_the_public_one():
    lim = Limits(candles_base="http://127.0.0.1:4999")
    net, cfg, chain = candle_chain(lim)
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_candles", "arguments": {"symbol": "ETH", "resolution": "1h", "limit": 2}}},
               chain, cfg)["result"]
    text = r["content"][0]["text"]
    assert r["isError"] is False and r["structuredContent"]["count"] == 1
    assert "127.0.0.1" not in text and N["candles"] + "/candles?" in text
    assert any(u.startswith("http://127.0.0.1:4999/candles?") for u in net.read_urls())
    assert not any(u.startswith(N["candles"] + "/candles") for u in net.read_urls())


def test_candles_by_the_shorter_path_have_their_own_budget_and_errors_name_the_public_address():
    lim = Limits(candles_base="http://127.0.0.1:4999", candles_per_minute=1, candles_burst=1)
    net, cfg, chain = candle_chain(lim)
    ask = lambda n: handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",                 # noqa: E731
                            "params": {"name": "get_candles", "arguments": {"symbol": "ETH", "limit": n}}},
                           chain, cfg)["result"]
    assert ask(2)["isError"] is False
    r = ask(3)
    text = r["content"][0]["text"]
    assert r["isError"] is True and "share of the candle service" in text and "127.0.0.1" not in text

    def down(method, url, body=None, timeout=12):
        raise ChainUnreachable(f"{url}: TimeoutError: timed out")

    _, cfg2 = load(["--hosted", "--http", "127.0.0.1:0"])
    fetch = budget_fetch(down, __import__("vordium_mcp.limits", fromlist=["CandleRoute"]).CandleRoute(
        N["candles"], "http://127.0.0.1:4999", RateLimit(60)))
    with pytest.raises(ChainUnreachable) as e:
        fetch("GET", N["candles"] + "/candles?pair=1")
    assert "127.0.0.1" not in str(e.value) and N["candles"] in str(e.value)


def test_other_reads_never_take_the_shorter_path():
    lim = Limits(candles_base="http://127.0.0.1:4999")
    net, cfg, chain = candle_chain(lim)
    handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_balance", "arguments": {"address": OWNER}}},
           chain, cfg)
    assert net.read_urls() and not any("127.0.0.1" in u for u in net.read_urls())


def test_a_call_waiting_on_a_shared_read_keeps_its_own_time_limit():
    """A call that waits on the same URL another call is reading never waits past its own deadline, and another call's
    time-limit failure is not handed to it as its own answer."""
    import threading, time
    from vordium_mcp import limits
    from vordium_mcp.chain import Chain

    started = threading.Event()

    def slow(method, url, body=None, timeout=12):
        started.set()
        time.sleep(min(timeout, 3))
        return 200, b'{"ok": true}'

    chain = Chain("testnet", "http://stub", "http://stub/spec", "http://stub/truth", fetch=limits.budget_fetch(slow), ttl=5, short_ttl=2)
    first = threading.Thread(target=lambda: chain._read_url("http://stub/x"))
    first.start()
    started.wait(2)
    t0 = time.monotonic()
    with limits.deadline(1.0):
        try:
            chain._read_url("http://stub/x")
        except limits.TimeBudgetSpent:
            assert limits.budget_spent()
    assert time.monotonic() - t0 < 1.6
    first.join(5)
