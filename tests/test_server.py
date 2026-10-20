"""The server's contract, against a fake network: no request leaves this process."""
import dataclasses, io, json, os, stat, sys, tempfile, threading, urllib.error, urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake import AGENT, OWNER, FakeNet, setup  # noqa: E402

from vordium_mcp import tools  # noqa: E402
from vordium_mcp.config import Config, load  # noqa: E402
from vordium_mcp.networks import NETWORKS, ConfigError  # noqa: E402
from vordium_mcp.server import RateLimit, handle, make_chain, make_http_handler, serve_stdio  # noqa: E402

READS = ["get_network", "get_markets", "get_oracle", "get_orderbook", "get_funding", "get_balance", "get_positions",
         "get_orders", "get_policy", "get_risk"]
WRITES = ["place_order", "cancel_order"]


def call(chain, cfg, name, args=None):
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
               chain, cfg)["result"]
    return r["isError"], json.loads(r["content"][0]["text"])


# ── protocol ─────────────────────────────────────────────────────────────────
def test_initialize_and_list():
    net, cfg, chain = setup()
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, chain, cfg)
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert "testnet" in r["result"]["instructions"] and "private key" in r["result"]["instructions"]
    names = [t["name"] for t in handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, chain, cfg)["result"]["tools"]]
    assert names == READS + WRITES
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, chain, cfg) is None
    assert handle({"jsonrpc": "2.0", "id": 3, "method": "nope"}, chain, cfg)["error"]["code"] == -32601
    assert net.calls == []                                           # nothing read just to start


def test_unknown_protocol_version_gets_the_newest():
    net, cfg, chain = setup()
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}, chain, cfg)
    assert r["result"]["protocolVersion"] == "2025-06-18"


def test_stdio_loop():
    net, cfg, chain = setup()
    out = io.StringIO()
    serve_stdio(cfg, io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n'), out, chain)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["result"] == {} and lines[1]["error"]["code"] == -32700


# ── reads ────────────────────────────────────────────────────────────────────
def test_every_read_tool_answers():
    net, cfg, chain = setup()
    args = {"get_orderbook": {"symbol": "ETH"}, "get_funding": {"symbol": "ETHUSDT"}, "get_balance": {"address": OWNER},
            "get_positions": {"address": OWNER}, "get_orders": {"address": OWNER},
            "get_policy": {"address": OWNER}, "get_risk": {"address": OWNER, "agent": AGENT}}
    for name in READS:
        err, doc = call(chain, cfg, name, args.get(name))
        assert not err, (name, doc)
        assert doc.get("network", doc.get("identity", {}).get("network")) == "testnet" or name in ("get_policy", "get_risk")


def test_network_reports_the_networks_own_answer():
    net, cfg, chain = setup()
    err, doc = call(chain, cfg, "get_network")
    assert doc["truth"]["agent_policy_active"] is False and doc["truth"]["validators_agreeing"] == 7
    assert doc["testnet_tokens_have_no_value"] is True and doc["trading_from_this_server"] == "not allowed by the operator"


def test_network_not_established_when_validators_disagree():
    n = NETWORKS["testnet"]
    net, cfg, chain = setup(**{n["truth"]: {"version": 1, "ok": False, "error": "disagree"}})
    err, doc = call(chain, cfg, "get_network")
    assert not err and doc["truth"] == {"established": False, "why": "disagree", "stale": None}


def test_units_are_decoded():
    net, cfg, chain = setup()
    _, ob = call(chain, cfg, "get_orderbook", {"symbol": "ETH"})
    assert ob["bids"][0] == {"price": 2500.0, "size": 0.1} and ob["asks"][0]["price"] == 2501.5 and ob["spread"] == 1.5
    _, bal = call(chain, cfg, "get_balance", {"address": OWNER})
    assert bal["usdc"]["available_to_trade"] == 12.5
    _, f = call(chain, cfg, "get_funding", {"symbol": "ETH"})
    assert f["rate_per_interval_pct"] == pytest.approx(-0.000105)


def test_markets_carry_trading_rules():
    net, cfg, chain = setup()
    _, m = call(chain, cfg, "get_markets")
    assert m["count"] == 2 and m["markets"][0]["trading_rules"]["maintenance_margin_bps"] == 200
    assert m["venue_rules"]["min_order_notional_6dec"] == "10000000"


def test_unknown_market_and_bad_address_refused():
    net, cfg, chain = setup()
    assert call(chain, cfg, "get_orderbook", {"symbol": "DOGE"})[0]
    assert call(chain, cfg, "get_balance", {"address": "0x12"})[0]


def test_unreadable_network_is_said_never_filled_in():
    n = NETWORKS["testnet"]
    net, cfg, chain = setup(**{n["rest"] + "/oracle/prices": (502, "bad gateway")})
    err, doc = call(chain, cfg, "get_oracle")
    assert err and "could not be read" in doc["reason"]


# ── which network ────────────────────────────────────────────────────────────
def test_wrong_network_refuses_every_tool():
    n = NETWORKS["testnet"]
    net, cfg, chain = setup(**{n["spec"]: {"chainId": 101101, "genesisSha256": "ab" * 32}})
    for name in ("get_network", "get_markets", "get_oracle"):
        err, doc = call(chain, cfg, name)
        assert err and "disagree" in doc["reason"]


def test_a_main_network_spec_is_not_the_testnet():
    n = NETWORKS["testnet"]
    net, cfg, chain = setup(**{n["spec"]: {"chainId": 101102, "genesisSha256": "ab" * 32}})
    err, doc = call(chain, cfg, "get_markets")
    assert err and "is not a test network" in doc["reason"]


def test_chain_ids_must_agree():
    n = NETWORKS["testnet"]
    net, cfg, chain = setup(**{n["rest"] + "/status": {"chain_id": 1}})
    err, doc = call(chain, cfg, "get_markets")
    assert err and "chain ids disagree" in doc["reason"]


def test_mainnet_reads_mainnet():
    net, cfg, chain = setup("mainnet")
    err, doc = call(chain, cfg, "get_network")
    assert not err and doc["network"] == "mainnet" and doc["testnet_tokens_have_no_value"] is False


# ── configuration ────────────────────────────────────────────────────────────
def test_testnet_is_the_default_and_mainnet_must_be_named():
    _, cfg = load([], env={})
    assert cfg.network == "testnet" and cfg.rest == NETWORKS["testnet"]["rest"]
    _, cfg = load(["--network", "mainnet"], env={})
    assert cfg.network == "mainnet" and cfg.rest == NETWORKS["mainnet"]["rest"]
    _, cfg = load([], env={"VORDIUM_MCP_NETWORK": "mainnet"})
    assert cfg.network == "mainnet"
    with pytest.raises(ConfigError):
        load([], env={"VORDIUM_MCP_NETWORK": "devnet"})


def test_http_mode_is_read_only_and_takes_no_key():
    _, cfg = load(["--http", "localhost:0"], env={})
    assert cfg.read_only and not cfg.allow_trading
    for bad in (["--http", "localhost:0", "--allow-trading"], ["--http", "localhost:0", "--agent-key-file", "/x"]):
        with pytest.raises(ConfigError):
            load(bad, env={})
    with pytest.raises(ConfigError):
        load(["--owner", "0x12"], env={})


# ── keys never come through a tool ───────────────────────────────────────────
@pytest.mark.parametrize("args", [
    {"address": OWNER, "private_key": "x"},
    {"address": OWNER, "note": "0x" + "1f" * 32},
    {"address": OWNER, "memo": "abandon " * 11 + "about"},
    {"address": OWNER, "nested": [{"mnemonic": "y"}]},
])
def test_key_material_refused_and_never_echoed(args):
    net, cfg, chain = setup()
    err, doc = call(chain, cfg, "get_balance", args)
    assert err and "never" in doc["reason"]
    text = json.dumps(doc)
    assert "1f" * 32 not in text and "abandon" not in text
    assert net.calls == []                                           # refused before anything was read


# ── writes ───────────────────────────────────────────────────────────────────
ORDER = {"symbol": "ETH", "side": "buy", "size": 0.01, "order_type": "limit", "price": 2400}


def trading_cfg(cfg, keyfile="/nonexistent"):
    return dataclasses.replace(cfg, allow_trading=True, owner=OWNER, agent_key_file=keyfile)


@pytest.mark.parametrize("name,args", [("place_order", ORDER), ("cancel_order", {"order_id": 7})])
def test_switch_off_refuses_first_and_reads_nothing_more(stub, name, args):
    net, cfg, chain = setup(active=False)
    err, doc = call(chain, trading_cfg(cfg), name, args)
    assert err and "agent trading is not switched on" in doc["reason"] and doc["note"] == "Nothing was signed or sent."
    urls = net.read_urls()
    assert not any("/agents/" in u or "/session/" in u for u in urls)  # refused before the agent was even looked at
    assert all(m == "GET" for m, _ in net.calls) and stub.sent == []


def test_switch_not_established_refuses():
    n = NETWORKS["testnet"]
    net, cfg, chain = setup(**{n["truth"]: {"version": 1, "ok": False, "error": "stale"}})
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert err and "cannot be confirmed" in doc["reason"] and "stale" in doc["reason"]


def test_switch_unreachable_refuses():
    n = NETWORKS["testnet"]
    net, cfg, chain = setup(**{n["truth"]: (503, "down")})
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert err and "cannot be confirmed" in doc["reason"]


class StubSigner:
    def __init__(self, agent=AGENT, answer=None):
        self.agent, self.sent = agent, []
        self.answer = answer or {"success": True, "status": "submitted", "status_code": 202}

    def agent_address(self, path):
        return self.agent

    def place(self, cfg, rest, **kw):
        self.sent.append(("place", kw))
        return dict(self.answer)

    def cancel(self, cfg, rest, **kw):
        self.sent.append(("cancel", kw))
        return dict(self.answer)


@pytest.fixture
def stub(monkeypatch):
    s = StubSigner()
    import vordium_mcp
    monkeypatch.setitem(sys.modules, "vordium_mcp.signer", s)
    monkeypatch.setattr(vordium_mcp, "signer", s, raising=False)
    return s


def test_switch_on_but_operator_did_not_allow(stub):
    net, cfg, chain = setup(active=True)
    err, doc = call(chain, cfg, "place_order", ORDER)
    assert err and "trading not allowed by the operator" in doc["reason"] and stub.sent == []


def test_switch_on_all_clear_signs_and_relays(stub):
    net, cfg, chain = setup(active=True)
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert not err and doc["accepted"] is True and doc["chain_answer"]["status"] == "submitted"
    kind, kw = stub.sent[0]
    assert kind == "place" and kw["pair_id"] == 1 and kw["side"] == 0 and kw["order_type"] == 1 and kw["price"] == 2400
    err, doc = call(chain, trading_cfg(cfg), "cancel_order", {"order_id": 7})
    assert not err and stub.sent[1] == ("cancel", {"order_id": 7})


def test_market_order_carries_no_price(stub):
    net, cfg, chain = setup(active=True)
    err, doc = call(chain, trading_cfg(cfg), "place_order",
                    {"symbol": "ETH", "side": "sell", "size": 1, "order_type": "market", "price": 2500})
    assert not err and stub.sent[0][1]["price"] == 0.0 and stub.sent[0][1]["side"] == 1


@pytest.fixture(autouse=True)
def _short_refusal_wait(monkeypatch):
    import vordium_mcp.tools as T
    monkeypatch.setattr(T, "REFUSAL_WAIT_S", 0.2)


def test_a_refusal_the_chain_records_after_receipt_is_reported(stub, monkeypatch):
    """The intake answers 202, then the chain refuses the agent's order: the tool must say refused, with the reason."""
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 1_792_000_000.0)
    rest = NETWORKS["testnet"]["rest"]
    ev = {"events": [{"kind": "agent_refused", "op_kind": "PlaceOrder", "client_nonce": 1_792_000_000_000, "reason": "price_outside_band"}]}
    net, cfg, chain = setup(active=True, **{rest + f"/events/agent/{AGENT}": ev})
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert not err and doc["accepted"] is False and doc["refused"] is True and doc["refusal_reason"] == "price_outside_band"
    assert stub.sent[0][1]["client_nonce"] == 1_792_000_000_000
    # a refusal of ANOTHER order is not this order's
    ev2 = {"events": [{"kind": "agent_refused", "client_nonce": 5, "reason": "price_outside_band"}]}
    net, cfg, chain = setup(active=True, **{rest + f"/events/agent/{AGENT}": ev2})
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert not err and doc["accepted"] is True and "refused" not in doc


def test_refusal_reads_in_the_networks_own_words(stub, monkeypatch):
    """With the network's refusal codes published, a refused order carries the reason key AND its meaning."""
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 1_792_000_000.0)
    n, rest = NETWORKS["testnet"], NETWORKS["testnet"]["rest"]
    ev = {"events": [{"kind": "agent_refused", "client_nonce": 1_792_000_000_000, "reason": "price_outside_band"}]}
    codes = {"reasons": [{"key": "price_outside_band", "meaning": "The price is outside the policy's band around the oracle price."}]}
    net, cfg, chain = setup(active=True, **{rest + f"/events/agent/{AGENT}": ev})
    spec = dict(chain.spec()["json"]); spec["agents"] = {"refusalCodes": codes}
    net, cfg, chain = setup(active=True, **{rest + f"/events/agent/{AGENT}": ev, n["spec"]: spec})
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert not err and doc["refused"] is True and doc["refusal_reason"] == "price_outside_band"
    assert doc["refusal_meaning"] == "The price is outside the policy's band around the oracle price."


def test_an_intake_refusal_is_reported_with_its_reason(stub):
    """A node that checks agent orders at intake answers 403 agent_refused: refused, with the reason and meaning."""
    stub.answer = {"code": "agent_refused", "reason": "registration_not_live", "meaning": "The agent's registration is revoked or expired.", "status_code": 403}
    net, cfg, chain = setup(active=True)
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert not err and doc["accepted"] is False and doc["refused"] is True and doc["refusal_reason"] == "registration_not_live"
    assert doc["refusal_meaning"] == "The agent's registration is revoked or expired."


def test_chain_refusal_is_relayed_not_success(stub):
    stub.answer = {"error": "insufficient margin", "status_code": 400}
    net, cfg, chain = setup(active=True)
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert not err and doc["accepted"] is False and doc["chain_answer"]["error"] == "insufficient margin"


@pytest.mark.parametrize("over,blocker", [
    ({"/session/valid?owner=%s&session_key=%s" % (OWNER, AGENT): {"valid": False, "confirmed": False}}, "no confirmed session"),
    ({"/agents/%s/%s" % (OWNER, AGENT): {"found": False}}, "agent not registered"),
    ({"/agents/%s/%s" % (OWNER, AGENT): {"found": True, "status": "revoked", "expires_ms": 9_000_000}}, "agent revoked"),
    ({"/agents/%s/%s" % (OWNER, AGENT): {"found": True, "status": "active", "expires_ms": 0}}, "agent expired"),
    ({"/agents/%s/%s" % (OWNER, AGENT): {"found": True, "status": "active", "expires_ms": 4_000}}, "agent expired"),
    ({"/status": {"chain_id": 101102, "trading_open": False, "trading_blockers": ["x"]}}, "trading not open"),
])
def test_each_blocker_refuses_before_signing(stub, over, blocker):
    rest = NETWORKS["testnet"]["rest"]
    net, cfg, chain = setup(active=True, **{rest + k: v for k, v in over.items()})
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert err and blocker in doc["reason"] and stub.sent == []


def test_paused_market_refused(stub):
    net, cfg, chain = setup(active=True)
    err, doc = call(chain, trading_cfg(cfg), "place_order", {**ORDER, "symbol": "BTC"})
    assert err and "market not trading" in doc["reason"] and stub.sent == []


def test_owner_key_is_not_an_agent_key(stub):
    stub.agent = OWNER
    net, cfg, chain = setup(active=True)
    err, doc = call(chain, trading_cfg(cfg), "place_order", ORDER)
    assert err and "owner key configured as the agent key" in doc["reason"] and stub.sent == []


def test_bad_order_shapes_refused(stub):
    net, cfg, chain = setup(active=True)
    for args in ({**ORDER, "side": "up"}, {**ORDER, "order_type": "stop"}, {**ORDER, "size": 0},
                 {**ORDER, "price": None}, {**ORDER, "leverage": 0.5}):
        assert call(chain, trading_cfg(cfg), "place_order", args)[0]
    assert stub.sent == []


def test_read_only_server_has_no_write_tools():
    net, cfg, chain = setup(active=True)
    ro = dataclasses.replace(cfg, read_only=True)
    names = [t["name"] for t in handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, chain, ro)["result"]["tools"]]
    assert names == READS
    err, doc = call(chain, ro, "place_order", ORDER)
    assert err and "no tool named" in doc["reason"]


# ── the agent key file ───────────────────────────────────────────────────────
def test_key_file_must_be_private_and_is_never_echoed(tmp_path):
    from vordium_mcp import signer
    key = "0x" + "4c" * 32
    p = tmp_path / "agent.key"
    p.write_text(key + "\n")
    os.chmod(p, 0o644)
    with pytest.raises(signer.SignerUnavailable) as e:
        signer._read_key(str(p))
    assert "chmod 600" in str(e.value) and key not in str(e.value)
    os.chmod(p, 0o600)
    assert signer._read_key(str(p)) == key
    p.write_text("not a key")
    with pytest.raises(signer.SignerUnavailable) as e:
        signer._read_key(str(p))
    assert "not a key" not in str(e.value)


def test_agent_address_from_key_file(tmp_path):
    pytest.importorskip("eth_account")
    pytest.importorskip("vordium")
    from eth_account import Account
    from vordium_mcp import signer
    key = "0x" + "4c" * 32
    p = tmp_path / "agent.key"
    p.write_text(key)
    os.chmod(p, 0o600)
    assert signer.agent_address(str(p)) == Account.from_key(key).address


# ── read-only HTTP ───────────────────────────────────────────────────────────
@pytest.fixture
def http_server():
    from http.server import ThreadingHTTPServer
    net, cfg, chain = setup(active=True)
    cfg = dataclasses.replace(cfg, read_only=True, http="localhost:0", rate_per_minute=6, allow_origins=("https://ok.example",))
    srv = ThreadingHTTPServer(("localhost", 0), make_http_handler(cfg, chain, RateLimit(cfg.rate_per_minute)))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://localhost:{srv.server_address[1]}"
    srv.shutdown()


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_serves_reads_and_no_writes(http_server):
    url = http_server + "/mcp"
    s, b = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert s == 200 and "read-only" in json.loads(b)["result"]["instructions"]
    s, b = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in json.loads(b)["result"]["tools"]] == READS
    s, b = post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert s == 202 and b == b""
    s, b = post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "place_order", "arguments": ORDER}})
    assert json.loads(b)["result"]["isError"] is True


def test_http_refusals(http_server):
    url = http_server + "/mcp"
    assert post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Origin": "https://evil.example"})[0] == 403
    assert post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Origin": "https://ok.example"})[0] == 200
    assert post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"MCP-Protocol-Version": "1999-01-01"})[0] == 400
    assert post(url, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])[0] == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url, timeout=5)
    assert e.value.code == 405
    codes = [post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] for _ in range(8)]
    assert 429 in codes                                              # 6 a minute per client


def test_rate_limit_refills():
    t = [0.0]
    rl = RateLimit(60, clock=lambda: t[0])
    assert all(rl.allow("a") for _ in range(60)) and not rl.allow("a") and rl.allow("b")
    t[0] += 1.0
    assert rl.allow("a") and not rl.allow("a")
