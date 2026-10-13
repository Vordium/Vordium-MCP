import sys

from . import __version__
from .config import load
from .networks import ConfigError
from .server import serve_http, serve_stdio


def main(argv=None):
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
