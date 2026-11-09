"""Local signing with the agent key — the only place this server touches a key.

The agent key is a key the owner registered on-chain as an agent. It lives in the OS keychain (``vordium-mcp key store``)
or in a file named in the server's own configuration (``--agent-key-file``, mode 600, or an encrypted keystore); a tool
never receives it. It is read when a write is signed, kept only for that call, and never logged, returned or sent: only
the signature leaves this machine (see keys.py).

Signing uses the published Vordium SDK (``pip install "vordium-mcp[trade]"``), the same signer the SDK's own clients
use. The SDK reads the node's chain id and genesis and refuses to sign for a node that serves another network.
"""
from . import keys


class SignerUnavailable(RuntimeError):
    pass


def _sdk():
    try:
        from eth_account import Account                        # noqa: F401
        from vordium import dex, network                       # noqa: F401
    except Exception:
        raise SignerUnavailable('signing needs the trade extra: pip install "vordium-mcp[trade]"')
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
    net = network.resolve_network(network=cfg.network)
    network.set_network(net)                     # the Operator and MarketMaker sign under the process network's domain
    return dex, network, net


def _client(cfg, rest):
    dex, network, net = _net(cfg)
    return dex, dex.Vordex(rpc_url=rest, network=net)


def place(cfg, rest, pair_id, side, size, price, leverage, order_type, reduce_only, post_only, client_nonce=None):
    dex, client = _client(cfg, rest)
    return client.place_order(session_key=_key(cfg), owner=cfg.owner, pair_id=int(pair_id),
                              side=int(side), size=float(size), price=float(price), leverage=float(leverage),
                              order_type=int(order_type), reduce_only=bool(reduce_only), post_only=bool(post_only),
                              client_nonce=client_nonce)


def cancel(cfg, rest, order_id):
    dex, client = _client(cfg, rest)
    return client.cancel_order(session_key=_key(cfg), owner=cfg.owner, order_id=int(order_id))


def _submitted(s):
    """The SDK's Submitted as plain data (the refusal named, never a key)."""
    r = getattr(s, "refusal", None)
    return {"status_code": getattr(s, "status_code", None), "submitted": bool(getattr(s, "ok", False)),
            "nonce": getattr(s, "nonce", None),
            "refusal": None if r is None else {"code": getattr(r, "code", None), "message": getattr(r, "message", None),
                                               "detail": getattr(r, "detail", None)}}


def _mm(cfg, rest):
    _net(cfg)
    from vordium.mm import MarketMaker
    return MarketMaker(rpc_url=rest, signer_key=_key(cfg), owner=cfg.owner)


def cancel_all(cfg, rest, pair_id=0):
    """Every open order of the owner on ``pair_id`` (0 = every market), in one signed request."""
    return _submitted(_mm(cfg, rest).cancel_all(pair_id=int(pair_id)))


def modify(cfg, rest, order_id, new_price_8dec, new_size_18dec, pair_id, side, client_nonce):
    """A signed cancel-and-replace: the order leaves the book and a new one (new id) rests at the new price and size."""
    return _submitted(_mm(cfg, rest).modify(int(order_id), int(new_price_8dec), int(new_size_18dec),
                                            client_nonce=int(client_nonce), pair_id=int(pair_id), side=side))


def set_tpsl(cfg, rest, pair_id, position_id, take_profit_8dec, stop_loss_8dec):
    """Both legs of a position's take-profit / stop-loss as they should stand (0 = no such leg)."""
    _net(cfg)
    from vordium.operator import AgentBox, Operator, SessionSigner
    op = Operator(owner=cfg.owner, signer=SessionSigner(private_key=_key(cfg)), box=AgentBox(rest_url=rest), submit_url=rest)
    answer = op.set_tpsl(int(pair_id), int(position_id), int(take_profit_8dec), int(stop_loss_8dec))
    # this route answers {"success": true, "status": "submitted"} or an error body, without a status code of its own
    ok = isinstance(answer, dict) and (answer.get("success") is True or answer.get("status") == "submitted") and not answer.get("error")
    return {"status_code": 202 if ok else 400, **(answer if isinstance(answer, dict) else {"answer": answer})}
