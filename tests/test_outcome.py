"""A write whose answer was lost: read back from the node (never the explorer), in the shared outcome words, per action;
and check_outcome, the read that follows it up. Against the fake network: no request leaves this process."""
import dataclasses, importlib.util, json, os, sys, time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake import AGENT, OWNER, setup  # noqa: E402
from test_orders_and_keys import ORDER, SPECS, routes, run, stub, trading  # noqa: E402,F401

from vordium_mcp import outcome, tools  # noqa: E402
from vordium_mcp.networks import NETWORKS  # noqa: E402

REST = NETWORKS["testnet"]["rest"]
EVENTS = REST + f"/events/account/{OWNER.lower()}?limit={outcome.RECORDS_LIMIT}"
AGENT_EVENTS = REST + f"/events/agent/{AGENT}"
CODES = {"order_refusal_codes": [{"code": "min_notional", "meaning": "The order is worth less than the minimum.",
                                  "action": "Raise the order's size."}],
         "agent_refusal_keys": [{"key": "policy_expired", "meaning": "The agent's policy has expired.",
                                 "action": "The owner sets a new policy."}]}
WORDS = json.load(open(outcome.own_copy(), encoding="utf-8"))
# placing an order takes its client nonce from the SDK's allocator (the trade extra)
needs_sdk = pytest.mark.skipif(importlib.util.find_spec("vordium") is None, reason="placing an order needs the trade extra")


def lost(stub, kind="place"):
    """the SDK's answer when the answer was lost and its own read did not show the write"""
    def send(cfg, rest, **kw):
        stub.sent.append((kind, kw))
        return {"status": "unknown", "delivery_unknown": True, "status_code": None}
    setattr(stub, kind, send)


def net_for(orders, events=None, agent_events=None, **extra):
    r = routes(orders=orders)
    r.update({EVENTS: {"events": events or []}, AGENT_EVENTS: {"events": agent_events or []}, REST + "/orders/codes": CODES,
              REST + "/status": {"chain_id": 101102, "trading_open": True, "trading_active": True, "trading_blockers": [],
                                 "chain_head": 4242}})
    r.update(extra)
    return setup(active=True, **r)


def closed(*ids, status="cancelled"):
    return [{"kind": "order_closed", "order_id": i, "status": status} for i in ids]


def w(action, state):
    return WORDS["actions"][action][state]


# ── order ────────────────────────────────────────────────────────────────────
@needs_sdk
def test_a_lost_order_that_the_node_lists_reads_applied_with_the_block(stub, monkeypatch):
    lost(stub)
    rows = []
    net, cfg, chain = net_for(rows)
    real = stub.place

    def place(cfg_, rest, **kw):                      # the order reached the book; its answer was lost
        rows.append({"id": 9001, "pair_id": 1, "side": "buy", "client_nonce": kw["client_nonce"]})
        net.routes[REST + f"/orders/{OWNER}"] = rows
        return real(cfg_, rest, **kw)
    stub.place = place
    err, doc = run(chain, trading(cfg, stub, "auto"), "place_order", ORDER)
    assert not err and doc["state"] == "applied" and doc["accepted"] is True and doc["already_received"] is True
    assert doc["text"] == "Placed. Found on the network at block 4242." and doc["order_id"] == 9001
    assert doc["read"]["source"] == "node" and doc["read"]["route"] == f"/orders/{OWNER}" and "follow_up" not in doc
    assert len(stub.sent) == 1                        # never sent again


@needs_sdk
def test_a_lost_order_refused_by_its_record_reads_refused_with_the_chains_reason(stub):
    lost(stub)
    net, cfg, chain = net_for([])
    real = stub.place

    def place(cfg_, rest, **kw):
        net.routes[EVENTS] = {"events": [{"kind": "order_refused", "client_nonce": kw["client_nonce"], "code": "min_notional",
                                          "pair_id": 1, "side": "buy", "height": 77}]}
        return real(cfg_, rest, **kw)
    stub.place = place
    err, doc = run(chain, trading(cfg, stub, "auto"), "place_order", ORDER)
    assert not err and doc["state"] == "refused" and doc["accepted"] is False and doc["refused"] is True
    assert doc["refusal_reason"] == "min_notional" and doc["refusal_meaning"] == "The order is worth less than the minimum."
    assert doc["text"] == "Not placed: The order is worth less than the minimum." and doc["next"] == "Raise the order's size."
    # the network's text stays in its own fields: the server's own sentence names the code only
    assert doc["summary"] == "place_order: Not placed: min_notional." and doc["read"]["height"] == 77
    assert doc["resend"]["allowed"] is True and doc["resend"]["same_bytes"] is False


@needs_sdk
def test_a_lost_agent_order_refused_by_the_agents_record(stub):
    lost(stub)
    net, cfg, chain = net_for([])
    real = stub.place

    def place(cfg_, rest, **kw):
        net.routes[AGENT_EVENTS] = {"events": [{"kind": "agent_refused", "client_nonce": kw["client_nonce"],
                                                "reason": "policy_expired"}]}
        return real(cfg_, rest, **kw)
    stub.place = place
    err, doc = run(chain, trading(cfg, stub, "auto"), "place_order", ORDER)
    assert doc["state"] == "refused" and doc["refusal_reason"] == "policy_expired"
    assert doc["text"] == "Not placed: The agent's policy has expired." and doc["next"] == "The owner sets a new policy."


@needs_sdk
def test_a_lost_order_not_shown_is_pending_never_not_applied_and_names_its_follow_up(stub):
    lost(stub)
    net, cfg, chain = net_for([{"id": 5, "pair_id": 1, "client_nonce": 1}])        # someone else's order
    err, doc = run(chain, trading(cfg, stub, "auto"), "place_order", ORDER)
    assert not err and doc["state"] == "pending_checking" and doc["accepted"] is None
    assert (doc["text"], doc["next"]) == (w("order", "pending_checking")["text"], w("order", "pending_checking")["next"])
    f = doc["follow_up"]
    assert f["tool"] == "check_outcome" and f["arguments"]["kind"] == "order"
    assert f["arguments"]["client_nonce"] == doc["client_nonce"] and f["arguments"]["address"] == OWNER
    assert f["arguments"]["agent"] == AGENT and isinstance(f["arguments"]["since_ms"], int)
    assert doc["resend"] == {"allowed": False, "same_bytes": False, "label": None}


@needs_sdk
def test_a_matching_nonce_on_another_market_is_not_this_order(stub):
    lost(stub)
    net, cfg, chain = net_for([])
    real = stub.place

    def place(cfg_, rest, **kw):
        net.routes[REST + f"/orders/{OWNER}"] = [{"id": 1, "pair_id": 2, "side": "buy", "client_nonce": kw["client_nonce"]}]
        return real(cfg_, rest, **kw)
    stub.place = place
    err, doc = run(chain, trading(cfg, stub, "auto"), "place_order", ORDER)
    assert doc["state"] == "pending_checking"


# ── cancel ───────────────────────────────────────────────────────────────────
def test_a_lost_cancel_has_cancel_words_never_order_words(stub):
    lost(stub, "cancel")
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}])                        # still resting
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77})
    assert not err and doc["state"] == "pending_checking"
    assert doc["text"] == w("cancel", "pending_checking")["text"] and "place it again" not in doc["summary"].lower()
    assert doc["resend"] == {"allowed": True, "same_bytes": True, "label": "Send the same cancel again"}
    assert doc["follow_up"]["arguments"] == {"kind": "cancel", "address": OWNER, "order_id": 77,
                                             "since_ms": doc["follow_up"]["arguments"]["since_ms"]}


@pytest.mark.parametrize("record,state,text", [
    ({"kind": "order_closed", "order_id": 77, "status": "cancelled", "height": 9}, "applied", "Cancelled. The order is off the book."),
    ({"kind": "order_closed", "order_id": 77, "status": "filled", "height": 9}, "applied_other", "Filled before the cancel.")])
def test_a_lost_cancel_whose_order_is_gone_reads_its_record(stub, record, state, text):
    lost(stub, "cancel")
    net, cfg, chain = net_for([], events=[record])
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77})
    assert doc["state"] == state and doc["text"] == text and doc["accepted"] is (state == "applied")
    assert doc["read"]["route"] == EVENTS[len(REST):] and doc["read"]["height"] == 9


def test_a_cancel_gone_without_a_record_keeps_checking_and_never_says_still_on_the_book(stub):
    v = outcome.judge_cancel(_reads(orders=[], events=[]), 77)
    assert v["state"] is None and v["resting"] is False
    o = outcome.result(None, "cancel", v, since_ms=0, now_ms=10 ** 9)
    assert o["state"] == "still_unknown" and o["text"] == "Still not known after 2 minutes." and "book" not in o["text"]
    assert o["resend"]["allowed"] is False
    o = outcome.result(None, "cancel", {"state": None, "resting": True}, since_ms=0, now_ms=10 ** 9)
    assert o["text"] == w("cancel", "still_unknown")["text"] and o["resend"]["allowed"] is True


# ── cancel all ───────────────────────────────────────────────────────────────
ONE_SHOT = {"specs": {**SPECS["specs"], "mm_release_active": True}}


def test_a_lost_cancel_all_is_judged_by_the_orders_listed_before_it(stub):
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}, {"id": 78, "pair_id": 1}])

    def cancel_all(cfg_, rest, pair_id=0):
        stub.sent.append(("cancel_all", {"pair_id": pair_id}))
        net.routes[REST + f"/orders/{OWNER}"] = [{"id": 90, "pair_id": 1}]     # both gone; a newer order rests
        net.routes[EVENTS] = {"events": closed(77, 78)}
        return {"status_code": None, "submitted": False, "unknown": True, "refusal": {"code": "delivery_unknown"}}
    stub.cancel_all = cancel_all
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {})
    assert not err and doc["state"] == "applied" and doc["text"] == "All 2 orders cancelled." and doc["accepted"] is True
    assert stub.sent == [("cancel_all", {"pair_id": 0})]                         # never sent again


def test_a_lost_cancel_all_still_listing_orders_names_them_and_offers_no_resend(stub):
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}, {"id": 78, "pair_id": 1}])
    stub.cancel_all = lambda cfg_, rest, pair_id=0: {"status_code": None, "unknown": True}
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {})
    assert doc["state"] == "pending_checking" and doc["still_open"] == [77, 78] and doc["accepted"] is None
    assert doc["text"] == w("cancel_all", "pending_checking")["text"] and doc["resend"]["allowed"] is False
    assert doc["follow_up"]["arguments"]["order_ids"] == [77, 78]


def test_some_filled_first_is_applied_other(stub):
    v = outcome.judge_cancel_all(_reads(orders=[], events=closed(77) + closed(78, status="filled")), [77, 78])
    o = outcome.result(None, "cancel_all", v)
    assert o["state"] == "applied_other" and o["text"] == "1 of 2 cancelled; 1 filled first."


def test_an_answered_cancel_all_is_still_judged_by_the_read(stub):
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}])

    def cancel_all(cfg_, rest, pair_id=0):
        net.routes[REST + f"/orders/{OWNER}"] = []
        net.routes[EVENTS] = {"events": closed(77)}
        return {"status_code": 202, "submitted": True, "nonce": 1, "refusal": None}
    stub.cancel_all = cancel_all
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {})
    assert doc["state"] == "applied" and doc["summary"] == "cancel_all: All 1 orders cancelled."


def test_one_by_one_is_judged_by_the_read_not_the_status_codes(stub, monkeypatch):
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}, {"id": 78, "pair_id": 1}],
                              **{REST + "/markets/specs": {"specs": {**SPECS["specs"], "mm_release_active": False}}})
    monkeypatch.setattr(stub, "cancel", lambda cfg_, rest, order_id, **k: {"status_code": 202})   # 2xx, but 78 still rests
    net.routes[REST + f"/orders/{OWNER}"] = [{"id": 77, "pair_id": 1}, {"id": 78, "pair_id": 1}]
    real_read = chain.read
    seen = []

    def read(path, cache=True):
        seen.append(path)
        if path == f"/orders/{OWNER}" and len([p for p in seen if p == path]) > 1:
            net.routes[REST + path] = [{"id": 78, "pair_id": 1}]
        return real_read(path, cache)
    monkeypatch.setattr(chain, "read", read)
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {})
    assert doc["accepted"] is False and doc["still_open"] == [78] and "state" not in doc   # answered: no "answer lost" words


def test_stop_all_reports_its_cancel_part_state(stub):
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}])
    stub.cancel_all = lambda cfg_, rest, pair_id=0: {"status_code": None, "unknown": True}
    err, doc = run(chain, trading(cfg, stub, "auto"), "stop_all", {})
    assert not err and doc["stopped"] is True and doc["cancel_state"] == "pending_checking"
    assert "Cancel all was sent but the answer was lost" in doc["summary"]


# ── check_outcome ──────────────────────────────────────────────────────
def test_check_outcome_reaches_the_final_for_each_kind():
    net, cfg, chain = net_for([{"id": 5, "pair_id": 1, "side": "sell", "client_nonce": 123, "height": 50}],
                              events=[{"kind": "order_closed", "order_id": 77, "status": "cancelled", "height": 51}]
                              + closed(78))
    ro = dataclasses.replace(cfg, read_only=True)
    err, doc = run(chain, ro, "check_outcome", {"kind": "order", "address": OWNER, "client_nonce": 123})
    assert not err and doc["state"] == "applied" and doc["text"] == "Placed. Found on the network at block 50."
    assert doc["summary"].startswith("The order with client nonce 123: Placed.") and "follow_up" not in doc
    err, doc = run(chain, ro, "check_outcome", {"kind": "cancel", "address": OWNER, "order_id": 77})
    assert doc["state"] == "applied" and doc["text"] == "Cancelled. The order is off the book."
    err, doc = run(chain, ro, "check_outcome", {"kind": "cancel_all", "address": OWNER, "order_ids": [77, 78]})
    assert doc["state"] == "applied" and doc["text"] == "All 2 orders cancelled."
    err, doc = run(chain, ro, "check_outcome", {"kind": "cancel_all", "address": OWNER, "order_ids": [5]})
    assert doc["state"] == "pending_checking" and doc["still_open"] == [5]


def test_check_outcome_after_the_limit_is_still_unknown_never_refused():
    net, cfg, chain = net_for([])
    old = int(time.time() * 1000) - (outcome.limit_s() + 1) * 1000
    err, doc = run(chain, cfg, "check_outcome", {"kind": "order", "address": OWNER, "client_nonce": 9, "since_ms": old})
    assert not err and doc["state"] == "still_unknown" and doc["text"] == w("order", "still_unknown")["text"]
    assert doc["follow_up"]["arguments"]["since_ms"] == old and "refused" not in doc
    err, doc = run(chain, cfg, "check_outcome", {"kind": "order", "address": OWNER, "client_nonce": 9,
                                                       "since_ms": int(time.time() * 1000) - 1000})
    assert doc["state"] == "pending_checking"


@pytest.mark.parametrize("args,why", [({"kind": "revoke", "address": OWNER}, "kind is order"),
                                      ({"kind": "order", "address": OWNER}, "needs client_nonce"),
                                      ({"kind": "cancel", "address": OWNER}, "needs order_id"),
                                      ({"kind": "order", "address": OWNER, "client_nonce": "x"}, "whole number"),
                                      ({"kind": "order", "address": OWNER, "client_nonce": 1, "since_ms": 10 ** 15}, "future"),
                                      ({"kind": "cancel_all", "address": OWNER, "order_ids": ["a"]}, "whole order ids"),
                                      ({"kind": "order", "client_nonce": 1}, "needs an account")])
def test_check_outcome_refuses_what_it_cannot_judge(args, why):
    net, cfg, chain = net_for([])
    err, doc = run(chain, cfg, "check_outcome", args)
    assert err and why in doc["reason"]


def test_check_outcome_is_bounded_and_polls_on_the_plan(monkeypatch):
    net, cfg, chain = net_for([])
    t = [0.0]
    sleeps = []
    monkeypatch.setattr(outcome, "CHECK_S", 20.0)
    monkeypatch.setattr(tools.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(outcome.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(outcome.time, "sleep", lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)))
    err, doc = run(chain, cfg, "check_outcome", {"kind": "order", "address": OWNER, "client_nonce": 9})
    assert doc["state"] == "pending_checking" and sum(sleeps) <= 20.0
    assert sleeps[:10] == [1.0] * 10 and set(sleeps[10:]) == {2.0}


def test_the_poll_plan():
    assert [outcome.interval(x) for x in (0, 9.9, 10, 29.9, 30, 100)] == [1, 1, 2, 2, 5, 5]
    looks = []
    v = outcome.check(lambda: looks.append(1) or None, until=0.0, sleep=lambda s: None, clock=lambda: 0.0)
    assert v is None and looks == [1]                                    # always one look, even with no time
    v = outcome.check(lambda: {"state": "applied"}, until=100.0, sleep=lambda s: 1 / 0, clock=lambda: 0.0)
    assert v["state"] == "applied"                                       # a final ends it at once


def test_the_read_only_server_judges_without_the_sdk(monkeypatch):
    monkeypatch.setattr(outcome, "_WORDS", None)
    monkeypatch.setattr(outcome, "_sdk_copy", lambda: None)
    net, cfg, chain = net_for([], events=[{"kind": "order_closed", "order_id": 1, "status": "cancelled"}])
    ro = dataclasses.replace(cfg, read_only=True)
    assert "check_outcome" in [t["name"] for t in tools.listed_tools(ro)]
    err, doc = run(chain, ro, "check_outcome", {"kind": "cancel", "address": OWNER, "order_id": 1})
    assert not err and doc["state"] == "applied"


# ── no outcome path reads the explorer or the trades ─────────────────────────
@needs_sdk
def test_no_outcome_path_fetches_an_explorer_host_or_the_trades(stub):
    explorers = {NETWORKS[n]["explorer"].split("//")[1] for n in NETWORKS}
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}])
    lost(stub)
    lost(stub, "cancel")
    stub.cancel_all = lambda cfg_, rest, pair_id=0: {"status_code": None, "unknown": True}
    c = trading(cfg, stub, "auto")
    run(chain, c, "place_order", ORDER)
    run(chain, c, "cancel_order", {"order_id": 77})
    run(chain, c, "cancel_all", {})
    for args in ({"kind": "order", "client_nonce": 1, "agent": AGENT}, {"kind": "cancel", "order_id": 77},
                 {"kind": "cancel_all", "order_ids": [77]}):
        run(chain, c, "check_outcome", {"address": OWNER, **args})
    urls = net.read_urls()
    assert urls and not [u for u in urls if any(h in u for h in explorers)]
    assert not [u for u in urls if "/trades" in u or "/fills" in u or "/api/" in u and "/api/pairs/perps" not in u]
    assert any(u.startswith(EVENTS) for u in urls) and any(u == REST + f"/orders/{OWNER}" for u in urls)


# ── the words ────────────────────────────────────────────────────────────────
def test_the_package_carries_the_word_file_and_every_sentence_this_server_uses():
    doc = outcome.load_words([outcome.own_copy()])
    assert doc["limit_s"] == 120 and doc == WORDS
    for action, states in outcome._NEEDED.items():
        for st in states:
            assert doc["actions"][action][st]["text"], (action, st)


def test_the_sdk_copy_is_read_first_and_a_broken_one_is_skipped(tmp_path):
    good = tmp_path / "sdk.json"
    good.write_text(json.dumps({**WORDS, "limit_s": 99}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"limit_s": 1, "actions": {}}))
    assert outcome.load_words([str(good), outcome.own_copy()])["limit_s"] == 99
    assert outcome.load_words([str(bad), outcome.own_copy()])["limit_s"] == 120
    with pytest.raises(RuntimeError):
        outcome.load_words([str(bad)])


def test_a_missing_value_leaves_its_sentence_out():
    assert outcome.fill("{order_words}. Found on the network at block {height}.", {"order_words": "Placed"}) == "Placed."
    assert outcome.fill("Not placed: {meaning}.", {"meaning": "x"}) == "Not placed: x."
    assert outcome.fill("{action}", {}) == ""


def _reads(orders, events, agent_events=None):
    class R:
        owner, agent = OWNER, None

        def orders(self):
            return orders, f"/orders/{OWNER}", 1

        def records(self):
            return events, "/events/account", 1

        def agent_records(self):
            return agent_events, "/events/agent", 1

        def height(self):
            return 7
    return R()


# ── review round: each fix held by a test ────────────────────────────────────
@needs_sdk
def test_an_sdk_final_decides_over_a_matching_row_of_another_signer(stub):
    net, cfg, chain = net_for([])

    def place(cfg_, rest, **kw):          # the SDK proved a refusal; another signer's order with this nonce rests
        net.routes[REST + f"/orders/{OWNER}"] = [{"id": 3, "pair_id": 1, "side": "buy", "client_nonce": kw["client_nonce"]}]
        return {"status": "refused", "status_code": None, "delivery_unknown": False,
                "outcome": {"state": "refused", "applied": False, "final": True, "read": {"source": "node", "height": 12},
                            "refusal": {"code": "min_notional", "meaning": "Too small.", "action": "Raise it."}}}
    stub.place = place
    err, doc = run(chain, trading(cfg, stub, "auto"), "place_order", ORDER)
    assert doc["state"] == "refused" and doc["refusal_reason"] == "min_notional" and doc["text"] == "Not placed: Too small."
    assert json.dumps(doc)                                      # plain data only


def test_an_applied_record_wins_over_a_nonce_refusal_of_a_resent_copy():
    recs = [{"kind": "order_refused", "client_nonce": 5, "code": "nonce_used", "pair_id": 1, "side": "buy"},
            {"kind": "order_closed", "client_nonce": 5, "order_id": 8, "status": "filled", "pair_id": 1, "side": "buy"}]
    v = outcome.judge_order(_reads(orders=[], events=recs), 5, {"pair_id": 1, "side": 0})
    assert v["state"] == "applied" and v["values"]["order_words"] == "Filled"
    v = outcome.judge_order(_reads(orders=[], events=recs[:1]), 5, {"pair_id": 1, "side": 0})
    assert v is None                                            # a nonce code alone is about a copy: keep checking


def test_a_partly_filled_order_that_was_cancelled_reads_cancelled():
    recs = [{"kind": "fill", "order_id": 77}, {"kind": "order_closed", "order_id": 77, "status": "cancelled"}]
    assert outcome.judge_cancel(_reads(orders=[], events=recs), 77)["state"] == "applied"


def test_cancel_all_skips_the_networks_liquidation_order_and_needs_the_before_set():
    rows = [{"id": 1, "pair_id": 1, "liquidation": True}, {"id": 2, "pair_id": 2}]
    assert outcome.own_order_ids(rows) == [2] and outcome.own_order_ids(rows, 1) == [] and outcome.own_order_ids(rows, 0) == [2]
    v = outcome.judge_cancel_all(_reads(orders=[{"id": 1, "liquidation": True}], events=closed(2)), [2])
    assert v["state"] == "applied"
    v = outcome.judge_cancel_all(_reads(orders=[], events=[]), None)
    assert v["state"] is None                                  # an empty book without the before-set proves nothing
    v = outcome.judge_cancel_all(_reads(orders=[], events=[]), [2])
    assert v["state"] is None                                  # gone without a record: filled first is still possible


def test_a_cancel_all_the_chain_refused_is_not_read_back(stub):
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}])
    stub.cancel_all = lambda cfg_, rest, pair_id=0: {"status_code": 400, "submitted": False,
                                                     "refusal": {"code": "bad", "message": "no"}}
    err, doc = run(chain, trading(cfg, stub), "cancel_all", {})
    assert doc["accepted"] is False and "state" not in doc and "follow_up" not in doc
    err, doc = run(chain, trading(cfg, stub, "auto"), "stop_all", {})
    assert doc["cancel_state"] == "refused"


def test_check_outcome_refuses_odd_digits_and_takes_pair_0_as_every_market():
    net, cfg, chain = net_for([{"id": 5, "pair_id": 2}], events=closed(5))
    err, doc = run(chain, cfg, "check_outcome", {"kind": "cancel_all", "address": OWNER, "order_ids": ["²"]})
    assert err and "whole order ids" in doc["reason"]
    err, doc = run(chain, cfg, "check_outcome", {"kind": "cancel_all", "address": OWNER, "order_ids": [5], "pair_id": 0})
    assert doc["state"] == "pending_checking" and doc["still_open"] == [5]


def test_a_public_server_checks_for_a_shorter_time(monkeypatch):
    net, cfg, chain = net_for([])
    t, sleeps = [0.0], []
    monkeypatch.setattr(outcome, "CHECK_S", 20.0)
    monkeypatch.setattr(tools.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(outcome.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(outcome.time, "sleep", lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)))
    hosted = dataclasses.replace(cfg, read_only=True, hosted=True)
    run(chain, hosted, "check_outcome", {"kind": "order", "address": OWNER, "client_nonce": 9})
    assert 0 < sum(sleeps) <= tools.HOSTED_CHECK_S


def test_stop_all_never_calls_a_cancel_still_being_read_refused(stub, monkeypatch):
    net, cfg, chain = net_for([{"id": 77, "pair_id": 1}, {"id": 78, "pair_id": 1}],
                              **{REST + "/markets/specs": {"specs": {**SPECS["specs"], "mm_release_active": False}}})
    monkeypatch.setattr(stub, "cancel", lambda cfg_, rest, order_id, **k: {"status_code": 202})   # taken; still listed
    err, doc = run(chain, trading(cfg, stub, "auto"), "stop_all", {})
    assert doc["cancel_all"]["accepted"] is False and doc["cancel_all"]["follow_up"]
    assert doc["cancel_state"] == "pending_checking"
