"""Local signing with the agent key — the only place this server touches a key.

The agent key is a key the owner registered on-chain as an agent. It lives in the OS keychain (``vordium-mcp key store``)
or in a file named in the server's own configuration (``--agent-key-file``, mode 600, or an encrypted keystore); a tool
never receives it. It is read when a write is signed, kept only for that call, and never logged, returned or sent: only
the signature leaves this machine (see keys.py).

Signing uses the published Vordium SDK (``pip install "vordium-mcp[trade]"``), the same signer the SDK's own clients
use. Every signature is made under the network's pin (networks.PINS): the SDK in use must carry the same pin, and the
SDK refuses to sign when eth_chainId, the spec or the node's /status reports anything else (NotSigned here).
"""
from . import keys, networks
from .networks import NotSigned


class SignerUnavailable(RuntimeError):
    pass


def _sdk():
    try:
        from eth_account import Account                        # noqa: F401
        from vordium import dex, network                       # noqa: F401
    except Exception:
        raise SignerUnavailable('signing needs the trade extra: pip install "vordium-mcp[trade]"')
    if not hasattr(network, "signing_pin"):
        raise SignerUnavailable('signing needs a Vordium SDK that pins its networks: pip install -U "vordium-mcp[trade]"')
    return dex, network


def _key(cfg):
    try:
        return keys.read(cfg)
    except keys.KeyError_ as e:
        raise SignerUnavailable(str(e))


def agent_address(cfg):
    """The agent's address, from the configured key source (never the key itself)."""
    _sdk()
    from eth_account import Account
    return Account.from_key(_key(cfg)).address


def _net(cfg):
    dex, network = _sdk()
    try:
        net = network.resolve_network(network=cfg.network)
        sdk_pin = network.signing_pin(net)       # eth_chainId, the spec and the node's /status equal the SDK's pin
    except network.NetworkResolutionError as e:
        raise NotSigned(str(e))
    mine = networks.pin(cfg.network)
    if (sdk_pin.chain_id, sdk_pin.genesis_sha256) != (mine["chain_id"], mine["genesis_sha256"]):
        raise NotSigned(f"this server is pinned to chain {mine['chain_id']} genesis {mine['genesis_sha256']} for the "
                        f"{cfg.network}, but the installed SDK to chain {sdk_pin.chain_id} genesis "
                        f"{sdk_pin.genesis_sha256}. Install matching releases. Nothing was signed.")
    network.set_network(net)                     # the Operator and MarketMaker sign under the process network's domain
    return dex, network, net


def _signed(fn):
    """Run one signing call; a refusal the SDK raises before signing (another network) becomes NotSigned."""
    from vordium import network
    try:
        return fn()
    except network.NetworkResolutionError as e:  # NetworkMismatch, PinMismatch, UnpinnedNetwork: nothing was signed
        raise NotSigned(str(e))


def _client(cfg, rest):
    dex, network, net = _net(cfg)
    return dex, dex.Vordex(rpc_url=rest, network=net)


def place(cfg, rest, pair_id, side, size, price, leverage, order_type, reduce_only, post_only, client_nonce=None):
    dex, client = _client(cfg, rest)
    return _signed(lambda: client.place_order(
        session_key=_key(cfg), owner=cfg.owner, pair_id=int(pair_id), side=int(side), size=float(size),
        price=float(price), leverage=float(leverage), order_type=int(order_type), reduce_only=bool(reduce_only),
        post_only=bool(post_only), client_nonce=client_nonce))


def cancel(cfg, rest, order_id):
    dex, client = _client(cfg, rest)
    return _signed(lambda: client.cancel_order(session_key=_key(cfg), owner=cfg.owner, order_id=int(order_id)))


def _submitted(s):
    """The SDK's Submitted as plain data (the refusal named, never a key)."""
    r = getattr(s, "refusal", None)
    return {"status_code": getattr(s, "status_code", None), "submitted": bool(getattr(s, "ok", False)),
            "nonce": getattr(s, "nonce", None), "already_received": bool(getattr(s, "already_received", False)),
            "unknown": bool(getattr(s, "unknown", False)),
            "refusal": None if r is None else {"code": getattr(r, "code", None), "message": getattr(r, "message", None),
                                               "detail": getattr(r, "detail", None)}}


def _mm(cfg, rest):
    _net(cfg)
    from vordium.mm import MarketMaker
    return MarketMaker(rpc_url=rest, signer_key=_key(cfg), owner=cfg.owner)


def cancel_all(cfg, rest, pair_id=0):
    """Every open order of the owner on ``pair_id`` (0 = every market), in one signed request."""
    m = _mm(cfg, rest)
    return _submitted(_signed(lambda: m.cancel_all(pair_id=int(pair_id))))


def modify(cfg, rest, order_id, new_price_8dec, new_size_18dec, pair_id, side, client_nonce):
    """A signed cancel-and-replace: the order leaves the book and a new one (new id) rests at the new price and size."""
    m = _mm(cfg, rest)
    return _submitted(_signed(lambda: m.modify(int(order_id), int(new_price_8dec), int(new_size_18dec),
                                               client_nonce=int(client_nonce), pair_id=int(pair_id), side=side)))


def set_tpsl(cfg, rest, pair_id, position_id, take_profit_8dec, stop_loss_8dec):
    """Both legs of a position's take-profit / stop-loss as they should stand (0 = no such leg)."""
    _net(cfg)
    from vordium.operator import AgentBox, Operator, SessionSigner
    op = Operator(owner=cfg.owner, signer=SessionSigner(private_key=_key(cfg)), box=AgentBox(rest_url=rest), submit_url=rest)
    answer = _signed(lambda: op.set_tpsl(int(pair_id), int(position_id), int(take_profit_8dec), int(stop_loss_8dec)))
    # this route answers {"success": true, "status": "submitted"} or an error body, without a status code of its own
    ok = isinstance(answer, dict) and (answer.get("success") is True or answer.get("status") == "submitted") and not answer.get("error")
    return {"status_code": 202 if ok else 400, **(answer if isinstance(answer, dict) else {"answer": answer})}
