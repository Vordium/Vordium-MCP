"""0.6.0: tool annotations, output schemas, the user's confirmation before an order is signed (MCP elicitation), the
tools that close the circle (get_order, close_position, cancel_all, modify_order, set_tpsl, stop_all), and where the
agent key lives. Against the fake network: no request leaves this process."""
import dataclasses, io, json, os, sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake import AGENT, OWNER, setup  # noqa: E402

from vordium_mcp import keys, schemas, tools  # noqa: E402
from vordium_mcp.config import load  # noqa: E402
from vordium_mcp.networks import NETWORKS, ConfigError  # noqa: E402
from vordium_mcp.server import Session, _complete, handle, serve_stdio  # noqa: E402

REST = NETWORKS["testnet"]["rest"]
POS = [{"id": 501, "pair_id": 1, "is_long": True, "size": "2000000000000000000", "leverage": 3, "entry_price": 240000000000}]
OPEN = [{"id": 77, "pair_id": 1, "side": "Buy", "order_type": "Limit", "price_8dec": 240000000000,
         "size": "1000000000000000000", "remaining_18dec": "1000000000000000000", "leverage_bps": 20000}]
SPECS = {"specs": {"min_order_notional_6dec": "10000000", "price_band_active": True, "mm_release_active": True,
                   "pairs": [{"pair": {"pair_id": 1}, "tick_8dec": "0", "lot_18dec": "0", "maintenance_margin_bps": 200}]}}


def routes(caps=0b11111, tpsl=None, positions=POS, orders=OPEN, specs=SPECS):
    return {REST + f"/positions/{OWNER}": positions, REST + f"/orders/{OWNER}": orders, REST + "/markets/specs": specs,
            REST + f"/tpsl/{OWNER}": tpsl if tpsl is not None else [],
            REST + f"/agents/{OWNER}/{AGENT}": {"found": True, "agent": AGENT, "status": "active", "expires_ms": 9_000_000,
                                                "policy_v1": {"caps_bitmask": caps, "enabled": True}}}


class Stub:
    def __init__(self):
        self.sent = []
        self.ok = {"success": True, "status": "submitted", "status_code": 202}

    def agent_address(self, cfg):
        return AGENT

    def place(self, cfg, rest, **kw):
        self.sent.append(("place", kw))
        return dict(self.ok)

    def cancel(self, cfg, rest, **kw):
        self.sent.append(("cancel", kw))
        return dict(self.ok)

    def cancel_all(self, cfg, rest, pair_id=0):
        self.sent.append(("cancel_all", {"pair_id": pair_id}))
        return {"status_code": 202, "submitted": True, "nonce": 1, "refusal": None}

    def modify(self, cfg, rest, order_id, new_price_8dec, new_size_18dec, pair_id, side, client_nonce):
        self.sent.append(("modify", {"order_id": order_id, "price": new_price_8dec, "size": new_size_18dec, "side": side}))
        return {"status_code": 202, "submitted": True, "nonce": client_nonce, "refusal": None}

    def set_tpsl(self, cfg, rest, pair_id, position_id, take_profit_8dec, stop_loss_8dec):
        self.sent.append(("set_tpsl", {"position_id": position_id, "tp": take_profit_8dec, "sl": stop_loss_8dec}))
        return dict(self.ok)


class Asker:
    """A client session that supports elicitation and answers with ``action`` (and confirm)."""

    def __init__(self, action="accept", confirm=True, supports=True):
        self.asked, self.action, self.confirm, self.supports = [], action, confirm, supports

    def can_elicit(self):
        return self.supports

    def elicit(self, message, schema):
        self.asked.append((message, schema))
        return {"action": self.action, "content": {"confirm": self.confirm}} if self.action == "accept" else {"action": self.action}


@pytest.fixture
def stub(monkeypatch, tmp_path):
    s = Stub()
    import vordium_mcp
    monkeypatch.setitem(sys.modules, "vordium_mcp.signer", s)
    monkeypatch.setattr(vordium_mcp, "signer", s, raising=False)
    monkeypatch.setattr(tools.pretrade, "Ctx", lambda *a, **k: object())
    monkeypatch.setattr(tools.pretrade, "snap", lambda ctx, o: o)
    monkeypatch.setattr(tools.pretrade, "check", lambda ctx, o, fixes=True: {"can_place": True, "failed": [], "fixes": [],
                                                                               "figures": {}, "not_checked": []})
    monkeypatch.setattr(tools, "REFUSAL_WAIT_S", 0.0)
    s.state = str(tmp_path / "state")
    return s


def trading(cfg, stub, orders="ask"):
    return dataclasses.replace(cfg, allow_trading=True, owner=OWNER, agent_key_file="/nonexistent", orders=orders,
                               state_dir=stub.state)


def run(chain, cfg, name, args=None, session=None):
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
               chain, cfg, session=session)["result"]
    return r["isError"], r["structuredContent"]


# ── annotations and schemas ──────────────────────────────────────────────────
WRITE_TOOLS = ("place_order", "close_position", "set_tpsl", "modify_order", "cancel_order", "cancel_all", "stop_all")


def test_every_tool_is_annotated_titled_and_has_an_output_schema():
    for t in tools.TOOLS:
        a = t["annotations"]
        assert t["title"] and a["title"] == t["title"] and t["outputSchema"]["required"] == ["summary", "network"], t["name"]
        if t["name"] in WRITE_TOOLS:
            assert a == {**schemas.WRITE, "title": t["title"]}, t["name"]                 # destructive, never idempotent
        else:
            assert a["readOnlyHint"] is True and a["destructiveHint"] is False and a["openWorldHint"] is True, t["name"]
    assert {t["name"] for t in tools.TOOLS} == set(schemas.OUTPUT)


def test_every_input_field_says_what_it_is():
    for t in tools.TOOLS:
        for k, v in t["inputSchema"]["properties"].items():
            assert v.get("description"), f"{t['name']}.{k}"


def _valid(value, schema):
    """the parts of JSON Schema the output schemas use: type (or types), required, properties"""
    kinds = schema.get("type")
    if kinds:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        py = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
        ok = any((k == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
                 or (k in py and isinstance(value, py[k])) for k in kinds)
        if not ok:
            return False
    if isinstance(value, dict):
        if any(r not in value for r in schema.get("required", [])):
            return False
        return all(_valid(value[k], s) for k, s in schema.get("properties", {}).items() if k in value)
    return True


@pytest.mark.parametrize("name,args", [("get_status", {}), ("get_network", {}), ("get_markets", {}), ("get_oracle", {}),
                                       ("get_orderbook", {"symbol": "ETH"}), ("get_funding", {"symbol": "ETH"}),
                                       ("get_balance", {"address": OWNER}), ("get_positions", {"address": OWNER}),
                                       ("get_orders", {"address": OWNER}), ("get_order", {"address": OWNER, "order_id": 77})])
def test_results_match_their_output_schema(name, args):
    net, cfg, chain = setup(**routes())
    err, doc = run(chain, cfg, name, args)
    assert not err, doc
    assert _valid(doc, schemas.OUTPUT[name]), (name, doc)


def test_the_server_completes_summary_and_network():
    net, cfg, chain = setup()
    assert _complete({"x": 1}, cfg, "get_season") == {"x": 1, "network": "testnet", "summary": "Rewards season: read from the testnet."}


def test_tools_list_carries_output_schemas():
    net, cfg, chain = setup()
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, chain, cfg)["result"]["tools"]
    assert all("outputSchema" in t and "handler" not in t for t in listed)


# ── every order asks (MCP elicitation) ───────────────────────────────────────
ORDER = {"symbol": "ETH", "side": "buy", "size": 0.01, "order_type": "limit", "price": 2400}


def test_an_order_is_signed_only_after_the_user_confirms(stub):
    net, cfg, chain = setup(active=True, **routes())
    asker = Asker("accept", True)
    err, doc = run(chain, trading(cfg, stub), "place_order", ORDER, asker)
    assert not err and doc["confirmation"] == {"asked": True, "confirmed": True} and stub.sent[0][0] == "place"
    msg, schema = asker.asked[0]
    assert "Buy 0.01 ETH at 2400 (limit)" in msg and schema["required"] == ["confirm"]


@pytest.mark.parametrize("answer", [Asker("decline"), Asker("cancel"), Asker("accept", False)])
def test_a_declined_order_is_not_signed(stub, answer):
    net, cfg, chain = setup(active=True, **routes())
    err, doc = run(chain, trading(cfg, stub), "place_order", ORDER, answer)
    assert err and "did not confirm" in doc["reason"] and stub.sent == []


def test_an_app_that_cannot_ask_gets_a_refusal_not_a_silent_order(stub):
    net, cfg, chain = setup(active=True, **routes())
    for session in (None, Asker(supports=False)):
        err, doc = run(chain, trading(cfg, stub), "place_order", ORDER, session)
        assert err and "cannot ask you to confirm" in doc["reason"] and "--orders auto" in doc["remedy"]
    assert stub.sent == []


def test_auto_within_limits_is_the_users_choice_and_never_asks(stub):
    net, cfg, chain = setup(active=True, **routes())
    asker = Asker()
    err, doc = run(chain, trading(cfg, stub, orders="auto"), "place_order", ORDER, asker)
    assert not err and asker.asked == [] and doc["confirmation"]["asked"] is False and stub.sent[0][0] == "place"


def test_cancels_take_risk_off_and_do_not_ask(stub):
    net, cfg, chain = setup(active=True, **routes())
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77}, None)
    assert not err and stub.sent == [("cancel", {"order_id": 77})]


def test_stdio_asks_the_client_and_waits_for_its_answer(stub):
    """the server writes elicitation/create, reads the client's answer from stdin, then signs and answers the call"""
    net, cfg, chain = setup(active=True, **routes())
    cfg = trading(cfg, stub)
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}}}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "place_order", "arguments": ORDER}},
             {"jsonrpc": "2.0", "id": "vordium-elicit-1", "result": {"action": "accept", "content": {"confirm": True}}}]
    out = io.StringIO()
    serve_stdio(cfg, stdin=io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"), stdout=out, chain=chain)
    sent = [json.loads(x) for x in out.getvalue().splitlines()]
    assert sent[1]["method"] == "elicitation/create" and sent[1]["id"] == "vordium-elicit-1"
    assert sent[2]["id"] == 2 and sent[2]["result"]["isError"] is False and stub.sent[0][0] == "place"


def test_a_session_without_an_answer_cancels():
    s = Session(write=lambda m: None, read=lambda wait: None)
    s.capabilities = {"elicitation": {}}
    assert s.can_elicit() and s.elicit("x", {})["action"] == "cancel"


# ── closing the circle ───────────────────────────────────────────────────────
def test_get_order_finds_a_resting_order_or_says_it_is_gone():
    net, cfg, chain = setup(**routes())
    err, doc = run(chain, cfg, "get_order", {"address": OWNER, "order_id": 77})
    assert not err and doc["found"] is True and doc["order"]["id"] == 77
    err, doc = run(chain, cfg, "get_order", {"address": OWNER, "order_id": 78})
    assert not err and doc["found"] is False and "not resting" in doc["summary"]


def test_close_position_sells_the_long_reduce_only(stub):
    net, cfg, chain = setup(active=True, **routes())
    err, doc = run(chain, trading(cfg, stub, "auto"), "close_position", {"symbol": "ETH", "fraction": 0.5})
    kind, kw = stub.sent[0]
    assert not err and kind == "place" and kw["side"] == 1 and kw["reduce_only"] is True and kw["size"] == 1.0 and kw["order_type"] == 0


def test_close_position_without_a_position_signs_nothing(stub):
    net, cfg, chain = setup(active=True, **routes(positions=[]))
    err, doc = run(chain, trading(cfg, stub, "auto"), "close_position", {"symbol": "ETH"})
    assert err and "no open position" in doc["reason"] and stub.sent == []


def test_cancel_all_is_one_request_where_the_network_offers_it(stub):
    net, cfg, chain = setup(active=True, **routes())
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {})
    assert not err and stub.sent == [("cancel_all", {"pair_id": 0})] and doc["scope"] == "every market"


def test_cancel_all_one_by_one_elsewhere(stub):
    net, cfg, chain = setup(active=True, **routes(specs={"specs": {**SPECS["specs"], "mm_release_active": False}}))
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {"symbol": "ETH"})
    assert not err and stub.sent == [("cancel", {"order_id": 77})] and doc["chain_answer"]["one_by_one"] == 1


def test_modify_order_replaces_at_the_new_price(stub):
    net, cfg, chain = setup(active=True, **routes())
    asker = Asker()
    err, doc = run(chain, trading(cfg, stub), "modify_order", {"order_id": 77, "price": 2450}, asker)
    assert not err and stub.sent == [("modify", {"order_id": 77, "price": 245000000000, "size": 10 ** 18, "side": "buy"})]
    assert "leaves the book" in asker.asked[0][0] and doc["replaced_order_id"] == 77


def test_modify_order_refused_when_the_agent_may_not_modify(stub):
    net, cfg, chain = setup(active=True, **routes(caps=0b10011))                 # place, cancel, close: an app-made AI agent
    err, doc = run(chain, trading(cfg, stub, "auto"), "modify_order", {"order_id": 77, "price": 2450})
    assert err and "not allowed to modify" in doc["reason"] and "Agents" in doc["remedy"] and stub.sent == []


def test_set_tpsl_keeps_the_leg_not_named_and_removes_on_request(stub):
    tp = [{"position_id": 501, "take_profit": 260000000000, "stop_loss": 230000000000}]
    net, cfg, chain = setup(active=True, **routes(tpsl=tp))
    err, doc = run(chain, trading(cfg, stub, "auto"), "set_tpsl", {"symbol": "ETH", "stop_loss": 2400})
    assert not err and stub.sent[-1] == ("set_tpsl", {"position_id": 501, "tp": 260000000000, "sl": 240000000000})
    err, doc = run(chain, trading(cfg, stub, "auto"), "set_tpsl", {"symbol": "ETH", "remove": "take_profit"})
    assert not err and stub.sent[-1] == ("set_tpsl", {"position_id": 501, "tp": 0, "sl": 230000000000})


def test_set_tpsl_on_the_wrong_side_of_the_mark_is_refused(stub):
    net, cfg, chain = setup(active=True, **routes())
    err, doc = run(chain, trading(cfg, stub, "auto"), "set_tpsl", {"symbol": "ETH", "take_profit": 2000})   # mark 2500.6, a long
    assert err and "take-profit for a long sits above" in doc["reason"] and stub.sent == []


def test_stop_all_locks_this_server_then_cancels(stub):
    net, cfg, chain = setup(active=True, **routes())
    c = trading(cfg, stub, "auto")
    err, doc = run(chain, c, "stop_all", {})
    assert not err and doc["stopped"] is True and os.path.exists(doc["lock"]) and stub.sent == [("cancel_all", {"pair_id": 0})]
    assert doc["revoke_on_chain"] == "https://testnet.vordex.xyz/agents"
    err, doc = run(chain, c, "place_order", ORDER)
    assert err and "stopped with stop_all" in doc["reason"] and "vordium-mcp resume" in doc["remedy"]
    err, doc = run(chain, c, "cancel_order", {"order_id": 77})                  # cancels still work
    assert not err


def test_resume_lifts_the_stop(stub, tmp_path):
    from vordium_mcp.__main__ import main
    net, cfg, chain = setup(active=True, **routes())
    c = trading(cfg, stub, "auto")
    run(chain, c, "stop_all", {})
    assert tools.stopped(c) is not None
    assert main(["resume", "--owner", OWNER, "--state-dir", stub.state]) == 0 and tools.stopped(c) is None


# ── the agent key ────────────────────────────────────────────────────────────
class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_keyring(self):
        return self

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user))


def test_the_keychain_is_used_when_no_key_file_is_named():
    kr = FakeKeyring()
    cfg = load(["--owner", OWNER], env={})[1]
    assert keys.source(cfg, kr)[0] is None
    keys.keychain_put("testnet", OWNER, "4c" * 32, kr)
    assert keys.source(cfg, kr) == ("keychain", None) and keys.read(cfg, kr) == "0x" + "4c" * 32
    assert ("vordium-mcp", f"testnet:{OWNER.lower()}") in kr.store


def test_a_key_file_others_can_read_stops_the_server_at_start(tmp_path):
    p = tmp_path / "agent.key"
    p.write_text("4c" * 32)
    os.chmod(p, 0o644)
    with pytest.raises(ConfigError) as e:
        load(["--allow-trading", "--owner", OWNER, "--agent-key-file", str(p)], env={})
    assert "refusing to start" in str(e.value) and "chmod 600" in str(e.value)
    os.chmod(p, 0o600)
    assert load(["--allow-trading", "--owner", OWNER, "--agent-key-file", str(p)], env={})[1].allow_trading is True


def test_an_encrypted_keystore_opens_with_its_passphrase_file(tmp_path):
    pytest.importorskip("eth_account")
    from eth_account import Account
    key = "0x" + "4c" * 32
    ks, pw = tmp_path / "agent.json", tmp_path / "pass"
    ks.write_text(json.dumps(Account.encrypt(key, "correct horse", kdf="pbkdf2", iterations=2)))
    pw.write_text("correct horse\n")
    os.chmod(ks, 0o600)
    os.chmod(pw, 0o600)
    cfg = load(["--owner", OWNER, "--agent-key-file", str(ks), "--agent-key-passphrase-file", str(pw)], env={})[1]
    assert keys.read(cfg) == key
    pw.write_text("wrong")
    with pytest.raises(keys.KeyError_) as e:
        keys.read(cfg)
    assert "could not be opened" in str(e.value) and "wrong" not in str(e.value)


def test_orders_mode_is_ask_unless_chosen():
    assert load([], env={})[1].orders == "ask"
    assert load([], env={"VORDIUM_MCP_ORDERS": "auto"})[1].orders == "auto"
    with pytest.raises(ConfigError):
        load([], env={"VORDIUM_MCP_ORDERS": "always"})


# ── the tool list is locked: an unplanned change fails ───────────────────────
def test_tools_match_the_lock(monkeypatch):
    from vordium_mcp import toolsdoc
    # the lock next to this test tree (the repo, or a clean install's copy) — not beside the installed package
    monkeypatch.setattr(toolsdoc, "LOCK", os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools.lock.json"))
    assert toolsdoc.main(["check"]) == 0


def test_the_lock_names_a_change_in_plain_words():
    from vordium_mcp import toolsdoc
    held = toolsdoc.lock()
    now = json.loads(json.dumps(held))
    now["tools"] = [t for t in now["tools"] if t["name"] != "stop_all"]
    now["tools"][0]["inputSchema"] = {"type": "object", "properties": {"x": {}}}
    assert set(toolsdoc._changes(held, now)) == {"tool removed: stop_all", "get_status: inputSchema changed"}


def test_the_readme_tool_table_is_the_generated_one():
    from vordium_mcp import toolsdoc
    readme = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "README.md"), encoding="utf-8").read()
    assert toolsdoc.markdown() in readme, "README tool table is not the generated one: python -m vordium_mcp.toolsdoc markdown"


def test_the_registry_entry_names_this_release():
    from vordium_mcp import __version__
    doc = json.load(open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "server.json"), encoding="utf-8"))
    readme = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "README.md"), encoding="utf-8").read()
    assert doc["version"] == __version__ and doc["packages"][0]["version"] == __version__
    assert f"mcp-name: {doc['name']}" in readme                      # the registry checks the PyPI page names this server


# ── review round: each fix held by a test ────────────────────────────────────
def test_a_refusal_carries_network_so_schema_checking_clients_show_it(stub):
    net, cfg, chain = setup(active=True, **routes())
    err, doc = run(chain, trading(cfg, stub), "place_order", ORDER, None)
    assert err and doc["network"] == "testnet" and doc["summary"].startswith("Refused")


def test_set_tpsl_refuses_when_the_current_legs_cannot_be_read(stub):
    over = routes()
    over[REST + f"/tpsl/{OWNER}"] = (503, json.dumps({"error": "busy"}))
    net, cfg, chain = setup(active=True, **over)
    err, doc = run(chain, trading(cfg, stub, "auto"), "set_tpsl", {"symbol": "ETH", "take_profit": 2600})
    assert err and "could not be read" in doc["reason"] and stub.sent == []


def test_a_stop_during_the_question_wins(stub):
    net, cfg, chain = setup(active=True, **routes())
    c = trading(cfg, stub)

    class StopThenAccept(Asker):
        def elicit(self, message, schema):
            os.makedirs(os.path.dirname(tools.stop_lock(c)), exist_ok=True)
            open(tools.stop_lock(c), "w").write('{"at": "now"}')          # another process ran stop_all meanwhile
            return super().elicit(message, schema)
    err, doc = run(chain, c, "place_order", ORDER, StopThenAccept())
    assert err and "while the order waited" in doc["reason"] and stub.sent == []


def test_a_cancelled_call_ends_its_question_unsigned():
    msgs = [{"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}]
    s = Session(write=lambda m: None, read=lambda wait: msgs.pop(0) if msgs else None)
    s.capabilities, s.current = {"elicitation": {}}, 2
    assert s.elicit("x", {}) == {"action": "cancel"} and s.queued == []


def test_a_late_answer_from_the_client_is_not_answered():
    net, cfg, chain = setup()
    assert handle({"jsonrpc": "2.0", "id": "vordium-elicit-1", "result": {"action": "accept"}}, chain, cfg) is None


def test_a_url_only_client_cannot_be_asked_about_an_order():
    s = Session(write=lambda m: None, read=lambda wait: None)
    for caps, can in (({"elicitation": {}}, True), ({"elicitation": {"form": {}}}, True), ({"elicitation": {"url": {}}}, False), ({}, False)):
        s.capabilities = caps
        assert s.can_elicit() is can, caps


def test_cancel_all_one_by_one_reports_each_answer(stub, monkeypatch):
    two = OPEN + [{**OPEN[0], "id": 78}]
    net, cfg, chain = setup(active=True, **routes(orders=two, specs={"specs": {**SPECS["specs"], "mm_release_active": False}}))
    calls = []

    def cancel(cfg, rest, order_id):
        calls.append(order_id)
        if order_id == 78:
            raise ConnectionError("dropped")
        return {"status_code": 202}
    monkeypatch.setattr(stub, "cancel", cancel)
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {})
    answers = doc["chain_answer"]["answers"]
    assert calls == [77, 78] and answers[0]["status_code"] == 202 and "dropped" in answers[1]["error"] and doc["accepted"] is False


def test_a_failure_while_sending_never_says_nothing_was_sent(stub, monkeypatch):
    net, cfg, chain = setup(active=True, **routes())

    def boom(*a, **k):
        raise TimeoutError("no answer")
    monkeypatch.setattr(stub, "place", boom)
    err, doc = run(chain, trading(cfg, stub, "auto"), "place_order", ORDER)
    assert err and "may have reached the chain" in doc["reason"] and "get_orders" in doc["remedy"]


def test_the_question_shows_the_figures_exactly(stub):
    net, cfg, chain = setup(active=True, **routes())
    asker = Asker("decline")
    run(chain, trading(cfg, stub), "place_order", {**ORDER, "price": 101234.5, "size": "0.0012345678"}, asker)
    assert "Buy 0.0012345678 ETH at 101234.5 (limit)" in asker.asked[0][0]


class PlaintextKeyring(FakeKeyring):
    pass


def test_a_plaintext_keyring_is_never_used():
    kr = PlaintextKeyring()
    kr.store[("vordium-mcp", f"testnet:{OWNER.lower()}")] = "0x" + "4c" * 32
    with pytest.raises(keys.KeyError_) as e:
        keys.keychain_put("testnet", OWNER, "4c" * 32, kr)
    assert "no secure OS keychain" in str(e.value) and keys.keychain_get("testnet", OWNER, kr) is None


def test_a_named_key_file_is_checked_at_start_even_without_trading(tmp_path):
    p = tmp_path / "agent.key"
    p.write_text("4c" * 32)
    os.chmod(p, 0o644)
    with pytest.raises(ConfigError):
        load(["--owner", OWNER, "--agent-key-file", str(p)], env={})


def test_resume_follows_the_servers_network_and_checks_the_owner(stub, monkeypatch, capsys):
    from vordium_mcp.__main__ import main
    monkeypatch.setenv("VORDIUM_MCP_NETWORK", "mainnet")
    c = dataclasses.replace(load([], env={})[1], network="mainnet", owner=OWNER, state_dir=stub.state)
    os.makedirs(stub.state, exist_ok=True)
    open(tools.stop_lock(c), "w").write("{}")
    assert main(["resume", "--owner", OWNER, "--state-dir", stub.state]) == 0 and tools.stopped(c) is None
    with pytest.raises(SystemExit):
        main(["resume", "--owner", "nonsense"])


def test_the_lock_check_fails_on_a_changed_lock(tmp_path, monkeypatch):
    from vordium_mcp import toolsdoc
    held = toolsdoc.lock()
    held["tools"] = held["tools"][:-1]
    p = tmp_path / "tools.lock.json"
    p.write_text(json.dumps(held))
    monkeypatch.setattr(toolsdoc, "LOCK", str(p))
    assert toolsdoc.main(["check"]) == 1


def test_a_failure_while_sending_does_not_also_say_nothing_was_sent(stub, monkeypatch):
    net, cfg, chain = setup(active=True, **routes())
    monkeypatch.setattr(stub, "cancel", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("late")))
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77})
    assert err and "may have been signed and sent" in doc["note"] and "Nothing was signed" not in doc["note"]


def test_arguments_that_are_not_an_object_are_refused_with_the_network():
    net, cfg, chain = setup()
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_status", "arguments": [1]}}, chain, cfg)
    assert r["result"]["isError"] is True and r["result"]["structuredContent"]["network"] == "testnet"


def test_a_stopped_server_does_not_even_ask(stub):
    net, cfg, chain = setup(active=True, **routes())
    c = trading(cfg, stub)
    os.makedirs(os.path.dirname(tools.stop_lock(c)), exist_ok=True)
    open(tools.stop_lock(c), "w").write('{"at": "earlier"}')
    asker = Asker()
    err, doc = run(chain, c, "place_order", ORDER, asker)
    assert err and "stopped with stop_all (earlier)" in doc["reason"] and asker.asked == [] and stub.sent == []
