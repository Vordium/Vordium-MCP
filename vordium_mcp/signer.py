"""Local signing with the agent key — the only place this server touches a key.

The agent key is a key the owner registered on-chain as an agent and gave a session. Its file path is set in the
server's own configuration (``--agent-key-file``); a tool never receives it. The file is read when an order is signed,
the key is kept only for that call, and it is never logged, returned or sent: only the signature leaves this machine.

Signing uses the published Vordium SDK (``pip install "vordium-mcp[trade]"``), the same signer the SDK's own clients
use. The SDK reads the node's chain id and genesis and refuses to sign for a node that serves another network.
"""
import os, re, stat

KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class SignerUnavailable(RuntimeError):
    pass


def _sdk():
    try:
        from eth_account import Account                        # noqa: F401
        from vordium import dex, network                       # noqa: F401
    except Exception:
        raise SignerUnavailable('signing needs the trade extra: pip install "vordium-mcp[trade]"')
    return dex, network


def _read_key(path):
    """The agent key from its file. Errors name the problem, never the content."""
    p = os.path.expanduser(str(path))
    try:
        st = os.stat(p)
    except OSError:
        raise SignerUnavailable(f"the agent key file {p} cannot be read")
    if not stat.S_ISREG(st.st_mode):
        raise SignerUnavailable(f"the agent key file {p} is not a regular file")
    if os.name == "posix" and st.st_mode & 0o077:
        raise SignerUnavailable(f"the agent key file {p} can be read by other users; run: chmod 600 {p}")
    with open(p, "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    if not KEY.match(raw):
        raise SignerUnavailable(f"the agent key file {p} does not hold one 32-byte hex key")
    return raw if raw.startswith("0x") else "0x" + raw


def agent_address(path):
    _sdk()
    from eth_account import Account
    return Account.from_key(_read_key(path)).address


def _client(cfg, rest):
    dex, network = _sdk()
    net = network.resolve_network(network=cfg.network)
    return dex, dex.Vordex(rpc_url=rest, network=net)


def place(cfg, rest, pair_id, side, size, price, leverage, order_type, reduce_only, post_only, client_nonce=None):
    dex, client = _client(cfg, rest)
    return client.place_order(session_key=_read_key(cfg.agent_key_file), owner=cfg.owner, pair_id=int(pair_id),
                              side=int(side), size=float(size), price=float(price), leverage=float(leverage),
                              order_type=int(order_type), reduce_only=bool(reduce_only), post_only=bool(post_only),
                              client_nonce=client_nonce)


def cancel(cfg, rest, order_id):
    dex, client = _client(cfg, rest)
    return client.cancel_order(session_key=_read_key(cfg.agent_key_file), owner=cfg.owner, order_id=int(order_id))
