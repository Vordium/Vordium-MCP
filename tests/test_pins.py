"""Each network is pinned to its chain id and genesis: a network that reports anything else is read (with a warning) but
never signed for. Against the fake network; the last test reads the public networks with VORDIUM_MCP_LIVE=1."""
import dataclasses, json, os, sys, urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake import setup, truth  # noqa: E402
from test_orders_and_keys import ORDER, routes, run, stub, trading  # noqa: E402,F401  (stub is a fixture)

from vordium_mcp import networks  # noqa: E402
from vordium_mcp.config import load  # noqa: E402
from vordium_mcp.networks import NETWORKS, PINS  # noqa: E402

T = NETWORKS["testnet"]
OTHER = "e" * 64


def reports(chain_id=101102, genesis=PINS["testnet"]["genesis_sha256"], status_genesis=None):
    """The fake test network reporting ``chain_id`` / ``genesis`` from its spec, its own answer and the node."""
    status = {"chain_id": chain_id, "trading_open": True, "trading_active": True, "trading_blockers": []}
    if status_genesis is not None:
        status["genesis_sha256"] = status_genesis
    return {T["spec"]: {"testnet": True, "chainId": chain_id, "genesisSha256": genesis},
            T["truth"]: truth(active=True, chain_id=chain_id, genesis=genesis),
            T["rest"] + "/status": status}


def test_the_networks_carry_their_pins():
    assert PINS == {"testnet": {"chain_id": 101102, "genesis_sha256": "8b5d414c5cd5d18bc468b64f87ac8e2c2b5ff494c769d5e09dcf50ce75a59792"},
                    "mainnet": {"chain_id": 101101, "genesis_sha256": "2c1c0679fa6ab8358f1d3e8d294a8d1c73ac2e2caf9079f1216abedc3d9fdf4f"}}
    assert set(PINS) == set(NETWORKS)


def test_a_matching_network_signs(stub):
    net, cfg, chain = setup(active=True, **routes(), **reports(status_genesis=PINS["testnet"]["genesis_sha256"]))
    err, doc = run(chain, cfg, "get_network")
    assert not err and doc["pinned"]["matches"] is True and "pin_warning" not in doc
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77})
    assert not err and stub.sent == [("cancel", {"order_id": 77})]


@pytest.mark.parametrize("over,needle", [
    (reports(chain_id=101101), "reports chain id 101101, expected 101102"),                   # wrong chain id everywhere
    (reports(genesis=OTHER), f"reports genesis {OTHER}"),                                     # wrong genesis everywhere
])
def test_a_network_off_its_pin_reads_with_a_warning_and_never_signs(stub, over, needle):
    net, cfg, chain = setup(active=True, **routes(), **over)
    err, doc = run(chain, cfg, "get_network")
    assert not err and doc["pinned"]["matches"] is False and needle in doc["pin_warning"]
    assert doc["summary"].startswith("Warning:")
    err, doc = run(chain, cfg, "get_markets")
    assert not err                                                                            # reads go on
    for tool, args in (("cancel_order", {"order_id": 77}), ("place_order", ORDER), ("cancel_all", {})):
        err, doc = run(chain, trading(cfg, stub, "auto"), tool, args)
        assert err and "Refusing to sign for the testnet" in doc["reason"] and needle in doc["reason"], tool
        assert "Nothing was signed" in doc["reason"]
    assert stub.sent == []


def test_spec_and_node_disagree_refuses(stub):
    net, cfg, chain = setup(active=True, **routes(), **reports(status_genesis=OTHER))
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77})
    assert err and "genesis disagrees" in doc["reason"] and stub.sent == []


def test_an_address_of_your_own_is_held_to_the_named_networks_pin(stub):
    own = "https://node.example/chain"
    _, cfg = load(["--rest", own], env={})
    assert cfg.network == "testnet" and cfg.rest == own                       # an address always comes with a network
    over = {own + k[len(T["rest"]):]: v for k, v in routes().items()}
    over.update({own + "/status": {"chain_id": 101101, "trading_open": True}, own + "/api/pairs/perps": {"pairs": []}})
    net, _, chain = setup(active=True, cfg=cfg, **over)
    err, doc = run(chain, trading(cfg, stub), "cancel_order", {"order_id": 77})
    assert err and "disagree" in doc["reason"] and stub.sent == []
    with pytest.raises(SystemExit):
        load(["--network", "devnet"], env={})                                 # no network without a pin


def test_signer_refuses_when_the_sdk_pin_differs(monkeypatch):
    pytest.importorskip("eth_account")
    vordium = pytest.importorskip("vordium")
    if not hasattr(vordium.network, "signing_pin"):
        pytest.skip("needs a Vordium SDK that pins its networks")
    from vordium import network as sdknet
    from vordium.pins import PINS as SDK_PINS
    from vordium_mcp import signer
    pin = SDK_PINS["testnet"]
    n = sdknet.Network(chain_id=pin.chain_id, chain_id_hex=hex(pin.chain_id), chain_name="t", rpc_url="https://rpc.invalid",
                       ws_url=None, explorer_url=None, bridge_address=None, bridge_chain=None, bridge_chain_id=None,
                       native_symbol="VORD", native_decimals=18, verifying_contract=sdknet.VERIFYING_CONTRACT, source="test",
                       genesis_sha256=pin.genesis_sha256, name="testnet")
    monkeypatch.setattr(sdknet, "resolve_network", lambda **kw: n)
    cfg = dataclasses.replace(load([], env={})[1], allow_trading=True)
    signer._net(cfg)                                                          # the same pin: accepted
    monkeypatch.setitem(networks.PINS, "testnet", {"chain_id": 101102, "genesis_sha256": OTHER})
    with pytest.raises(signer.NotSigned, match="installed SDK"):
        signer._net(cfg)
    monkeypatch.setattr(sdknet, "resolve_network", lambda **kw: dataclasses.replace(n, genesis_sha256=OTHER))
    with pytest.raises(signer.NotSigned, match="Refusing to sign for testnet"):
        signer._net(cfg)


def test_signer_refuses_an_sdk_without_pins(monkeypatch):
    pytest.importorskip("eth_account")
    vordium = pytest.importorskip("vordium")
    from vordium_mcp import signer
    monkeypatch.delattr(vordium.network, "signing_pin", raising=False)
    with pytest.raises(signer.SignerUnavailable, match="pins its networks"):
        signer._sdk()


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vordium-mcp-tests", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


@pytest.mark.skipif(os.environ.get("VORDIUM_MCP_LIVE") != "1", reason="set VORDIUM_MCP_LIVE=1 to read the public networks")
@pytest.mark.parametrize("name", sorted(NETWORKS))
def test_the_pins_equal_the_live_networks(name):
    n, pin = NETWORKS[name], PINS[name]
    spec, status = _get(n["spec"]), _get(n["rest"] + "/status")
    assert spec["chainId"] == status["chain_id"] == pin["chain_id"]
    assert spec["genesisSha256"].lower() == status["genesis_sha256"].lower() == pin["genesis_sha256"]
