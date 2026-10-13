"""Where this server asks, per network, and how it chooses one.

These are addresses, not chain values. Chain id, genesis, markets, prices, fees and limits are read from the network
when a tool is called; nothing about the chain is stored in this package.

The test network is the default. The main network is used only when it is named explicitly
(``--network mainnet`` or ``VORDIUM_MCP_NETWORK=mainnet``).
"""

DEFAULT_NETWORK = "testnet"

NETWORKS = {
    "testnet": {
        "label": "Vordium testnet",
        "rest": "https://testnet.vordex.xyz/chain",
        "spec": "https://testnet.vordex.xyz/network-spec.json",
        "truth": "https://rpc-testnet.vordium.com/truth/v1",
        "app": "https://testnet.vordex.xyz",
        "testnet": True,
    },
    "mainnet": {
        "label": "Vordium mainnet",
        "rest": "https://rpc.vordium.com",
        "spec": "https://rpc.vordium.com/network-spec.json",
        "truth": "https://rpc.vordium.com/truth/v1",
        "app": "https://app.vordex.xyz",
        "testnet": False,
    },
}


class ConfigError(ValueError):
    """The server was started with settings it will not run with. Raised before anything is read or served."""


def preset(name):
    key = str(name or "").strip().lower()
    if key not in NETWORKS:
        raise ConfigError(f"unknown network {name!r}; choose one of: {', '.join(sorted(NETWORKS))}")
    return key, dict(NETWORKS[key])
