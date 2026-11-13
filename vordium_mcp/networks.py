"""Where this server asks, per network, how it chooses one, and the identity each network is pinned to.

Each network carries its pin: its chain id and genesis sha256. Before anything is signed, the pin is compared with what
the network reports (the spec, the node's /status and the network's own answer here; eth_chainId and the node again in
the SDK); when any differs, nothing is signed. A new genesis for a network needs a new release of this package.
Markets, prices, fees and limits are read from the network when a tool is called; they are not stored here.

The test network is the default. The main network is used only when it is named explicitly
(``--network mainnet`` or ``VORDIUM_MCP_NETWORK=mainnet``).
"""
from .text import ident

DEFAULT_NETWORK = "testnet"

NETWORKS = {
    "testnet": {
        "label": "Vordium testnet",
        "rest": "https://testnet.vordex.xyz/chain",
        "spec": "https://testnet.vordex.xyz/network-spec.json",
        "truth": "https://rpc-testnet.vordium.com/truth/v1",
        "app": "https://testnet.vordex.xyz",
        "candles": "https://testnet.vordex.xyz",
        "explorer": "https://testnet.vordscan.io",
        "testnet": True,
    },
    "mainnet": {
        "label": "Vordium mainnet",
        "rest": "https://rpc.vordium.com",
        "spec": "https://rpc.vordium.com/network-spec.json",
        "truth": "https://rpc.vordium.com/truth/v1",
        "app": "https://app.vordex.xyz",
        "candles": "https://app.vordex.xyz",
        "explorer": "https://vordscan.io",
        "testnet": False,
    },
}


PINS = {
    "testnet": {"chain_id": 101102, "genesis_sha256": "8b5d414c5cd5d18bc468b64f87ac8e2c2b5ff494c769d5e09dcf50ce75a59792"},
    "mainnet": {"chain_id": 101101, "genesis_sha256": "2c1c0679fa6ab8358f1d3e8d294a8d1c73ac2e2caf9079f1216abedc3d9fdf4f"},
}


def pin(name):
    """The pin of a network: ``{"chain_id": int, "genesis_sha256": 64 lower-case hex}``."""
    key, _ = preset(name)
    return dict(PINS[key])


def _genesis(v):
    if not isinstance(v, str):
        return None
    g = v[2:] if v[:2] in ("0x", "0X") else v
    return g.lower()


def pin_problems(name, chain_ids, geneses):
    """One plain line per reported value that differs from the network's pin. ``chain_ids`` / ``geneses`` map a source
    to what it reported; a source that reported nothing (None) is skipped."""
    p = pin(name)
    out = []
    for src, cid in chain_ids.items():
        if cid is not None and cid != p["chain_id"]:
            out.append(f"{src} reports chain id {ident(cid)}, expected {p['chain_id']}")
    for src, g in geneses.items():
        if g is not None and _genesis(g) != p["genesis_sha256"]:
            out.append(f"{src} reports genesis {ident(_genesis(g) or g)}, expected {p['genesis_sha256']}")
    return out


def pin_refusal(name, problems):
    return (f"Refusing to sign for the {name}: " + "; ".join(problems)
            + ". Nothing was signed or sent. Check that the addresses serve this network.")


class NotSigned(RuntimeError):
    """Refused before any signature was made: the network is not the one this release is pinned to."""


class ConfigError(ValueError):
    """The server was started with settings it will not run with. Raised before anything is read or served."""


def preset(name):
    key = str(name or "").strip().lower()
    if key not in NETWORKS:
        raise ConfigError(f"unknown network {name!r}; choose one of: {', '.join(sorted(NETWORKS))}")
    return key, dict(NETWORKS[key])
