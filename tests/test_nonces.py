"""Client nonces come from the SDK's allocator, and a resent copy that already arrived reads as received. Against the
fake network: no request leaves this process."""
import os, sys, time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake import OWNER, setup  # noqa: E402
from test_orders_and_keys import ORDER, routes, run, stub, trading  # noqa: E402,F401

from vordium import nonces  # noqa: E402
from vordium_mcp import tools  # noqa: E402

DAY = 86_400_000


def test_orders_take_rising_nonces_from_the_owners_allocator(stub):
    net, cfg, chain = setup(active=True, **routes())
    c = trading(cfg, stub, orders="auto")
    nonces.allocator(OWNER).seen(int(time.time() * 1000) + 60_000)     # a nonce another process of this machine used
    got = []
    for _ in range(3):
        err, doc = run(chain, c, "place_order", ORDER)
        assert not err
        got.append(doc["client_nonce"])
    assert got == sorted(got) and len(set(got)) == 3 and got[0] > int(time.time() * 1000) + 59_000
    assert [kw["client_nonce"] for k, kw in stub.sent if k == "place"] == got
    assert nonces.allocator(OWNER).last() == got[-1]


def test_a_clock_outside_the_window_refuses_before_signing(stub, monkeypatch):
    net, cfg, chain = setup(active=True, **routes())
    for off in (-3 * DAY, 3 * DAY):
        monkeypatch.setattr(nonces, "chain_clock_for", lambda rest, network=None, off=off: (lambda: int(time.time() * 1000) + off))
        err, doc = run(chain, trading(cfg, stub, orders="auto"), "place_order", ORDER)
        assert err and "clock" in doc["reason"] and "Nothing was signed" in doc["remedy"]
    assert stub.sent == []


def test_an_answer_for_a_copy_that_already_arrived_reads_as_received(stub):
    net, cfg, chain = setup(active=True, **routes())
    stub.ok = {"code": "nonce", "status": "refused", "status_code": 400, "already_received": True, "attempts": 2}
    err, doc = run(chain, trading(cfg, stub, orders="auto"), "place_order", ORDER)
    assert not err and doc["accepted"] is True and doc["already_received"] is True and "not made twice" in doc["summary"]


def _sdk_place(monkeypatch, listed):
    """The real signer path (SDK) with a fake intake: the first answer is lost, the same body is sent again and the
    chain answers it with the nonce code."""
    pytest.importorskip("eth_account")
    import requests
    from eth_account import Account
    from vordium import _http, dex, units
    from vordium.node_domain import NodeIdentity
    from vordium.pins import PINS
    p = PINS["testnet"]
    monkeypatch.setattr(units, "fetch_specs", lambda base_url, timeout=10.0, get=None: units.NO_RULES)
    seen, bodies = set(), []

    class R:
        content, headers = b"{}", {}

        def __init__(self, code, j):
            self.status_code, self._j = code, j

        def json(self):
            return self._j

    def post(url, json=None, timeout=None, **k):
        bodies.append(json)
        first = json["clientNonce"] not in seen
        seen.add(json["clientNonce"])
        if len(bodies) == 1:
            raise requests.Timeout("lost")
        return R(202, {"status": "submitted"}) if first else R(400, {"code": "nonce", "error": "... or already used."})
    monkeypatch.setattr(_http, "post", post)
    vx = dex.Vordex(rpc_url="http://rpc.invalid", identity=lambda: NodeIdentity(p.chain_id, p.genesis_sha256),
                    network="testnet", chain_clock=None)
    monkeypatch.setattr(vx, "order_by_client_nonce", lambda o, n, signed=None: listed(n, signed))
    out = vx.place_order(Account.create().key.hex(), OWNER, 1, dex.BUY, 1, 100, client_nonce=nonces.next_nonce(OWNER))
    assert bodies[0] is bodies[1] and len(seen) == 1
    return out


def test_the_sdk_place_counts_a_resend_as_received_only_with_a_matching_read_back(monkeypatch):
    out = _sdk_place(monkeypatch, lambda n, signed: {"client_nonce": n, **signed, "source": "orders"})
    assert out["already_received"] is True and out["status"] == "received" and out["status_code"] is None


def test_the_sdk_place_without_a_read_back_is_unknown_and_the_tool_says_so(monkeypatch, stub):
    out = _sdk_place(monkeypatch, lambda n, signed: None)
    assert out["status"] == "unknown" and out["already_received"] is False and out["status_code"] is None
    net, cfg, chain = setup(active=True, **routes())
    stub.ok = dict(out)
    err, doc = run(chain, trading(cfg, stub, orders="auto"), "place_order", ORDER)
    assert not err and doc["accepted"] is None and doc["delivery_unknown"] is True and "not known" in doc["summary"]
    assert doc["already_received"] is False


def test_a_delivery_unknown_from_the_sdk_is_reported_as_unknown_never_refused(stub):
    net, cfg, chain = setup(active=True, **routes())

    def lost(cfg, rest, **kw):
        stub.sent.append(("place", kw))
        raise nonces.DeliveryUnknown("It is not known whether the network received this write", kw.get("client_nonce"), 3)
    stub.place = lost
    err, doc = run(chain, trading(cfg, stub, orders="auto"), "place_order", ORDER)
    assert not err and doc["accepted"] is None and doc["delivery_unknown"] is True and "refused" not in doc
    assert "may have reached the chain" in doc["summary"] and "open orders" in doc["summary"]


def test_a_cancel_whose_first_copy_landed_reads_as_accepted_or_unknown_never_refused(stub):
    net, cfg, chain = setup(active=True, **routes())
    stub.ok = {"status": "done", "already_received": True, "order_gone": True, "status_code": None}
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77}, None)
    assert not err and doc["accepted"] is True and "refused" not in doc
    stub.ok = {"status": "unknown", "delivery_unknown": True, "status_code": None}
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77}, None)
    assert not err and doc["accepted"] is None and doc["delivery_unknown"] is True


def test_the_sdk_cancel_reads_the_orders_back_through_the_tool(monkeypatch):
    """The SDK's own cancel: copy 1 lands (its answer lost), copy 2 is answered order_not_found; the order is gone."""
    pytest.importorskip("eth_account")
    import requests
    from eth_account import Account
    from vordium import _http, dex
    from vordium.node_domain import NodeIdentity
    from vordium.pins import PINS
    p = PINS["testnet"]
    n = [0]

    class R:
        content, headers = b"{}", {}

        def __init__(self, code, j):
            self.status_code, self._j = code, j

        def json(self):
            return self._j

    def post(url, json=None, timeout=None, **k):
        n[0] += 1
        if n[0] % 2 == 1:                                     # each cancel's first copy lands, its answer is lost
            raise requests.ReadTimeout("lost")
        return R(400, {"code": "order_not_found", "error": "order not open"})
    monkeypatch.setattr(_http, "post", post)
    vx = dex.Vordex(rpc_url="http://rpc.invalid", identity=lambda: NodeIdentity(p.chain_id, p.genesis_sha256),
                    network="testnet", chain_clock=None)
    monkeypatch.setattr(vx, "_get", lambda path: {"events": [{"kind": "order_cancelled", "order_id": 77}]}
                        if path.startswith("/events/") else [])
    out = vx.cancel_order(Account.create().key.hex(), OWNER, 77)
    assert out["status"] == "done" and out["already_received"] is True
    monkeypatch.setattr(vx, "_get", lambda path: {"events": [{"kind": "order_closed", "order_id": 77, "status": "filled"}]}
                        if path.startswith("/events/") else [])
    assert vx.cancel_order(Account.create().key.hex(), OWNER, 77)["status"] == "filled"



def test_a_cancel_after_a_fill_is_not_accepted_and_a_never_sent_write_is_refused(stub):
    net, cfg, chain = setup(active=True, **routes())
    stub.ok = {"status": "filled", "already_received": False, "order_gone": True, "status_code": None}
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77}, None)
    assert not err and doc["accepted"] is False and "filled before the cancel" in doc["summary"]

    def unreachable(cfg, rest, **kw):
        raise nonces.DeliveryUnknown("The network could not be reached after 3 tries; nothing was sent.", None, 3, never_sent=True)
    stub.cancel = unreachable
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77}, None)
    assert err and "nothing was sent" in doc["reason"]
