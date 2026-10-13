"""Local signing with the published SDK (runs when the trade extra is installed). Nothing is sent: the POST is caught."""
import os

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("vordium")

from eth_account import Account  # noqa: E402
from eth_utils import keccak  # noqa: E402

from vordium import _http, caps, network, node_domain  # noqa: E402

from vordium_mcp import signer  # noqa: E402
from vordium_mcp.config import Config  # noqa: E402

KEY = "0x" + "4c" * 32
OWNER = "0x00000000000000000000000000000000000000aa"
GENESIS = "ab" * 32
CHAIN = 101102


class Caught:
    def __init__(self):
        self.calls = []

    def __call__(self, url, json=None, timeout=None, **kw):
        self.calls.append((url, json))

        class R:
            status_code = 202
            content = b'{"success": true, "status": "submitted"}'

            @staticmethod
            def json():
                return {"success": True, "status": "submitted"}
        return R()


@pytest.fixture
def env(tmp_path, monkeypatch):
    p = tmp_path / "agent.key"
    p.write_text(KEY)
    os.chmod(p, 0o600)
    net = network.Network(chain_id=CHAIN, chain_id_hex=hex(CHAIN), chain_name="t", rpc_url="https://rpc.invalid",
                          ws_url=None, explorer_url=None, bridge_address=None, bridge_chain=None, bridge_chain_id=None,
                          native_symbol="VORD", native_decimals=18, verifying_contract=network.VERIFYING_CONTRACT,
                          source="test", genesis_sha256=GENESIS, name="testnet", api_url="https://rest.invalid")
    monkeypatch.setattr(network, "resolve_network", lambda **kw: net)
    monkeypatch.setattr(node_domain, "node_identity", lambda url, **kw: node_domain.NodeIdentity(CHAIN, GENESIS))
    caught = Caught()
    monkeypatch.setattr(_http, "post", caught)
    cfg = Config(network="testnet", allow_trading=True, owner=OWNER, agent_key_file=str(p))
    return cfg, caught


def recover(body_fields, sig):
    sep = caps.vordcore_domain_separator(CHAIN, network.VERIFYING_CONTRACT, GENESIS)
    digest = keccak(b"\x19\x01" + sep + body_fields)
    rs = bytes.fromhex(sig[2:])
    assert len(rs) == 64                                             # order signatures are r||s; the chain tries both v
    return {Account._recover_hash(digest, signature=rs + bytes([v])) for v in (27, 28)}


def test_place_is_signed_by_the_agent_key_only(env):
    cfg, caught = env
    out = signer.place(cfg, "https://rest.invalid", pair_id=1, side=0, size=0.01, price=2400.0, leverage=2.0,
                       order_type=1, reduce_only=False, post_only=True)
    assert out["status"] == "submitted"
    url, body = caught.calls[0]
    agent = Account.from_key(KEY).address
    assert url == "https://rest.invalid/order/place" and body["owner"] == OWNER and body["sessionKey"] == agent
    assert body["orderType"] == "Limit" and body["side"] == "buy" and body["price"] == 2400.0 and body["post_only"] is True
    struct = caps.place_order_struct_hash(OWNER, 1, 0, int(body["size_18dec"]), 240000000000, 20000, 1, False,
                                          body["clientNonce"], True, 0, 0)
    assert agent in recover(struct, body["signature"])
    assert KEY[2:] not in repr(body) and KEY[2:] not in repr(out)


def test_cancel_is_signed_by_the_agent_key(env):
    cfg, caught = env
    signer.cancel(cfg, "https://rest.invalid", order_id=7)
    url, body = caught.calls[0]
    assert url.endswith("/order/cancel") and body["order_id"] == 7
    assert Account.from_key(KEY).address in recover(caps.cancel_order_struct_hash(OWNER, 7), body["signature"])


def test_wrong_node_is_refused_before_signing(env, monkeypatch):
    cfg, caught = env
    monkeypatch.setattr(node_domain, "node_identity", lambda url, **kw: node_domain.NodeIdentity(101101, GENESIS))
    with pytest.raises(network.NetworkMismatch):
        signer.place(cfg, "https://rest.invalid", pair_id=1, side=0, size=0.01, price=2400.0, leverage=1.0,
                     order_type=1, reduce_only=False, post_only=False)
    assert caught.calls == []
