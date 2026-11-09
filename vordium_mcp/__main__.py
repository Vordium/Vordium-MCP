import sys

from . import __version__
from .config import load
from .networks import ConfigError
from .server import serve_http, serve_stdio


def new_agent_key(path):
    """Make an agent key on this machine: written to PATH (mode 600, never overwritten), only its address printed.
    Register that address in Vordex (Agents → New agent → Use my own key) with its limits; the key never leaves here."""
    import os
    from eth_account import Account
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    acct = Account.create()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"vordium-mcp: {path} already exists; nothing was changed", file=sys.stderr)
        return 2
    with os.fdopen(fd, "w") as f:
        f.write(acct.key.hex())
    print(acct.address)
    return 0


def _sub(argv, prog):
    import argparse, os
    from .config import ADDRESS

    def owner(v):
        if not ADDRESS.match(v):
            raise argparse.ArgumentTypeError("the owner is a 0x-prefixed 20-byte address")
        return v
    p = argparse.ArgumentParser(prog=f"vordium-mcp {prog}")
    p.add_argument("--owner", required=True, type=owner, help="the owner account (0x…)")
    p.add_argument("--network", type=str.lower, choices=("testnet", "mainnet"),
                   default=(os.environ.get("VORDIUM_MCP_NETWORK") or "testnet").strip().lower(),
                   help="the network the server runs on (default: the same setting the server reads, else testnet)")
    return p


def resume(argv):
    """Lift a stop_all on this machine: the server signs orders again (the agent's own state on the chain is untouched)."""
    import os
    p = _sub(argv, "resume")
    p.add_argument("--state-dir", default=os.environ.get("VORDIUM_MCP_STATE_DIR") or "~/.vordium-mcp")
    a = p.parse_args(argv)
    from .config import Config
    from .tools import stop_lock
    lock = stop_lock(Config(network=a.network, owner=a.owner, state_dir=a.state_dir))
    if not os.path.exists(lock):
        print(f"vordium-mcp: not stopped ({lock} does not exist)")
        return 0
    os.remove(lock)
    print(f"vordium-mcp: resumed — the server for {a.owner} on the {a.network} signs again")
    return 0


def key(argv):
    """The agent key in the OS keychain: store (from a file, or typed without echo), status, delete. Never printed."""
    if not argv or argv[0] not in ("store", "status", "delete"):
        print("usage: vordium-mcp key store|status|delete --owner 0x… [--network testnet|mainnet] [--from-file PATH]",
              file=sys.stderr)
        return 2
    import getpass, os
    from . import keys
    p = _sub(argv[1:], f"key {argv[0]}")
    if argv[0] == "store":
        p.add_argument("--from-file", help="read the key from this file (mode 600) or encrypted keystore; delete it afterwards")
        p.add_argument("--passphrase-file", help="the encrypted keystore's passphrase file (mode 600)")
    a = p.parse_args(argv[1:])
    if argv[0] == "status":
        here = keys.keychain_get(a.network, a.owner) is not None
        print(f"vordium-mcp: OS keychain {'available' if keys.keychain_available() else 'NOT available'}; "
              f"agent key for {a.owner} on the {a.network}: {'stored' if here else 'not stored'}")
        return 0 if here else 1
    if argv[0] == "delete":
        ok = keys.keychain_delete(a.network, a.owner)
        print("vordium-mcp: " + ("deleted" if ok else "nothing to delete"))
        return 0 if ok else 1
    if not keys.keychain_available():                 # said before anything is asked for
        print("vordium-mcp: this machine has no secure OS keychain: keep the key in a file (mode 600) or an encrypted "
              "keystore, and name it with --agent-key-file", file=sys.stderr)
        return 2
    try:
        if a.from_file:
            k = keys._from_file(a.from_file, a.passphrase_file)
        else:
            if not sys.stdin.isatty():
                print("vordium-mcp: type the key at a terminal, or give --from-file", file=sys.stderr)
                return 2
            k = getpass.getpass("agent key (hex, not shown): ")
        keys.keychain_put(a.network, a.owner, k)
    except (keys.KeyError_, EOFError, KeyboardInterrupt) as e:
        print(f"vordium-mcp: {e}", file=sys.stderr)
        return 2
    print(f"vordium-mcp: stored in the OS keychain for {a.owner} on the {a.network}."
          + (f" You can now delete {os.path.expanduser(a.from_file)}." if a.from_file else ""))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["resume"]:
        return resume(argv[1:])
    if argv[:1] == ["key"]:
        return key(argv[1:])
    if argv[:1] == ["new-agent-key"]:
        if len(argv) != 2:
            print("usage: vordium-mcp new-agent-key PATH", file=sys.stderr)
            return 2
        return new_agent_key(argv[1])
    try:
        args, cfg = load(argv)
    except ConfigError as e:
        print(f"vordium-mcp: {e}", file=sys.stderr)
        return 2
    if args.version:
        print(__version__)
        return 0
    if cfg.http:
        serve_http(cfg)
    else:
        serve_stdio(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
