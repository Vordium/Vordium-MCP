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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
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
