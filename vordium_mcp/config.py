"""Settings, from the command line first and the environment second. Checked before anything is served."""
import argparse, os, re
from dataclasses import dataclass
from typing import Optional

from .networks import DEFAULT_NETWORK, ConfigError, preset

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    network: str = DEFAULT_NETWORK
    rest: str = ""
    spec: str = ""
    truth: str = ""
    allow_trading: bool = False
    owner: Optional[str] = None
    agent_key_file: Optional[str] = None
    read_only: bool = False
    http: Optional[str] = None
    rate_per_minute: int = 120
    allow_origins: tuple = ()
    trust_proxy: bool = False


def parser():
    p = argparse.ArgumentParser(
        prog="vordium-mcp",
        description="Vordium MCP server. Runs on your machine over stdio; reads the Vordium testnet unless told otherwise.",
        epilog="Each setting can also come from the environment: VORDIUM_MCP_NETWORK, VORDIUM_MCP_REST, "
               "VORDIUM_MCP_ALLOW_TRADING=1, VORDIUM_MCP_OWNER, VORDIUM_MCP_AGENT_KEY_FILE, VORDIUM_MCP_HTTP, "
               "VORDIUM_MCP_RATE_PER_MINUTE, VORDIUM_MCP_ALLOW_ORIGINS (comma-separated). A flag wins over the environment.")
    p.add_argument("--network", choices=("testnet", "mainnet"),
                   help="which network to read (default testnet; mainnet only when named here or in VORDIUM_MCP_NETWORK)")
    p.add_argument("--rest", help="your own node's REST base instead of the network's public one")
    p.add_argument("--spec", help=argparse.SUPPRESS)
    p.add_argument("--truth", help=argparse.SUPPRESS)
    p.add_argument("--allow-trading", action="store_true",
                   help="let place_order and cancel_order sign with the agent key (still refused while the network "
                        "reports agent trading off)")
    p.add_argument("--owner", help="the owner account whose registered agent trades")
    p.add_argument("--agent-key-file", help="file holding the agent's private key (mode 600); read only when signing")
    p.add_argument("--http", metavar="HOST:PORT",
                   help="serve read-only over streamable HTTP at /mcp instead of stdio (no write tools, no keys)")
    p.add_argument("--rate-per-minute", type=int, help="HTTP: requests per client per minute (default 120)")
    p.add_argument("--allow-origin", action="append", default=None,
                   help="HTTP: a browser origin allowed to call /mcp (repeatable; none by default)")
    p.add_argument("--trust-proxy", action="store_true",
                   help="HTTP: take the client address from CF-Connecting-IP / X-Forwarded-For (behind a proxy only)")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    return p


def load(argv=None, env=None):
    env = os.environ if env is None else env
    a = parser().parse_args(argv)
    name = a.network or env.get("VORDIUM_MCP_NETWORK") or DEFAULT_NETWORK
    name, net = preset(name)
    http = a.http or env.get("VORDIUM_MCP_HTTP") or None
    allow = a.allow_trading or str(env.get("VORDIUM_MCP_ALLOW_TRADING", "")).strip().lower() in TRUE
    owner = a.owner or env.get("VORDIUM_MCP_OWNER") or None
    keyfile = a.agent_key_file or env.get("VORDIUM_MCP_AGENT_KEY_FILE") or None
    if owner and not ADDRESS.match(owner):
        raise ConfigError("--owner must be a 0x-prefixed 20-byte address")
    if http:
        if allow or keyfile:
            raise ConfigError("the HTTP server is read-only: it takes no trading flag and no key")
        if not re.match(r"^[\w.\-\[\]:]+:\d{1,5}$", http):
            raise ConfigError("--http takes HOST:PORT, e.g. localhost:8770")
    rate = a.rate_per_minute or int(env.get("VORDIUM_MCP_RATE_PER_MINUTE") or 120)
    if rate < 1:
        raise ConfigError("--rate-per-minute must be at least 1")
    origins = tuple(a.allow_origin or [o for o in str(env.get("VORDIUM_MCP_ALLOW_ORIGINS", "")).split(",") if o.strip()])
    return a, Config(
        network=name,
        rest=(a.rest or env.get("VORDIUM_MCP_REST") or net["rest"]).rstrip("/"),
        spec=a.spec or net["spec"],
        truth=a.truth or net["truth"],
        allow_trading=bool(allow) and not http,
        owner=owner,
        agent_key_file=keyfile,
        read_only=bool(http),
        http=http,
        rate_per_minute=rate,
        allow_origins=tuple(o.strip() for o in origins),
        trust_proxy=bool(a.trust_proxy),
    )
