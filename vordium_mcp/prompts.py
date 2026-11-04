"""Ready-made prompts and the resources this server offers.

Prompts are plain instructions for the model, naming the tools to call; they carry no chain value. Resources are read
from the network when they are asked for, like every tool result.
"""
import json
import re

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


class PromptError(ValueError):
    """A prompt or resource that does not exist, or an argument it cannot take."""


PROMPTS = [
    {"name": "market_overview", "title": "Market overview",
     "description": "A short read of every market: price, 24 h volume, funding and the network's state.",
     "arguments": [],
     "text": "Give me a market overview of Vordium. Call get_status first, then get_markets and get_oracle. For each "
             "active market give the mark price, the 24 hour volume and the current funding rate (get_funding), in a "
             "short table. End with one sentence on the network's state. Use only figures the tools return; where a "
             "tool cannot read a value, say so."},
    {"name": "check_account", "title": "Check my account",
     "description": "Balances, positions with liquidation prices, open orders and recent trades for one address.",
     "arguments": [{"name": "address", "description": "the account's 0x address", "required": True}],
     "text": "Check the Vordium account {address}. Call get_account for its balances, positions (with entry, mark, "
             "unrealized PnL and liquidation price) and open orders, then get_fills for its recent trades. Summarise "
             "what is at risk first: any position near its liquidation price. Use only figures the tools return."},
    {"name": "season_standings", "title": "Season standings",
     "description": "The current rewards season: its state, the leaderboard top 10, and an address's points if given.",
     "arguments": [{"name": "address", "description": "optional 0x address to place in the standings",
                    "required": False}],
     "text": "Show the current Vordium rewards season. Call get_season, then get_leaderboard with limit 10.{address_part} "
             "Say plainly when the network serves no season or no ranking yet."},
    {"name": "explain_transaction", "title": "Explain this transaction",
     "description": "What one transaction did, in plain words, with its explorer link.",
     "arguments": [{"name": "hash", "description": "the transaction hash (0x…)", "required": True}],
     "text": "Explain the Vordium transaction {hash}. Call get_tx, say in plain words what it did, who sent it, in "
             "which block and whether it succeeded, and give its explorer link."},
    {"name": "compare_markets", "title": "Compare markets",
     "description": "A pair of markets side by side: price, volume, funding, leverage, fees and book depth.",
     "arguments": [{"name": "first", "description": "a market symbol, e.g. BTC", "required": True},
                   {"name": "second", "description": "another market symbol, e.g. ETH", "required": True}],
     "text": "Compare the Vordium markets {first} and {second}. Use get_markets, get_fees, get_funding and "
             "get_orderbook (depth 5) for each, and put price, 24 h volume, funding, maximum leverage, maintenance "
             "margin, maker/taker fees and the spread side by side. Use only figures the tools return."},
]


def list_prompts():
    return [{k: p[k] for k in ("name", "title", "description", "arguments")} for p in PROMPTS]


def _arg(args, name, required):
    v = args.get(name)
    if v is None or str(v).strip() == "":
        if required:
            raise PromptError(f"the prompt needs {name}")
        return None
    v = str(v).strip()
    if len(v) > 80:
        raise PromptError(f"{name} is too long")
    return v


def get_prompt(name, args):
    p = next((p for p in PROMPTS if p["name"] == name), None)
    if p is None:
        raise PromptError(f"no prompt named {name!r}; prompts/list names them")
    if not isinstance(args, dict):
        raise PromptError("arguments must be an object")
    vals = {}
    for a in p["arguments"]:
        vals[a["name"]] = _arg(args, a["name"], a["required"])
    if "address" in vals and vals["address"] is not None and not ADDRESS.match(vals["address"]):
        raise PromptError("address must be a 0x-prefixed 20-byte address")
    if "hash" in vals and not TX_HASH.match(vals["hash"] or ""):
        raise PromptError("hash must be a 0x-prefixed 32-byte transaction hash")
    for k in ("first", "second"):
        if k in vals and not re.match(r"^[A-Za-z0-9\-_/]{1,24}$", vals[k] or ""):
            raise PromptError(f"{k} must be a market symbol such as BTC or ETH-PERP")
    if name == "season_standings":
        a = vals.pop("address", None)
        vals["address_part"] = (f" Then call get_points for {a}: its points by source, rank, whether it qualifies and "
                                f"its VORD estimate." if a else "")
    return {"description": p["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": p["text"].format(**vals)}}]}


# ── resources ────────────────────────────────────────────────────────────────
def list_resources(cfg):
    return [
        {"uri": "vordium://network/spec", "name": "network-spec", "title": "Network spec",
         "description": f"The {cfg.network}'s published spec, read now.", "mimeType": "application/json"},
        {"uri": "vordium://fees", "name": "fees", "title": "Fees table",
         "description": "Maker and taker fees, maximum leverage and margins for every market, read now.",
         "mimeType": "text/markdown"},
        {"uri": "vordium://docs", "name": "docs", "title": "Documentation links",
         "description": "Where to read about Vordium, its markets, fees and this server.", "mimeType": "text/markdown"},
    ]


DOCS = [
    ("Vordium documentation", "https://docs.vordium.com"),
    ("Using Vordium from an AI client (MCP)", "https://docs.vordium.com/build/mcp/"),
    ("Fees", "https://docs.vordium.com/guides/fees/"),
    ("Explorer", "https://vordscan.io"),
]


def read_resource(chain, cfg, uri):
    from .tools import Refusal, call
    if uri == "vordium://network/spec":
        r = chain.spec()
        doc = chain.require(r, "network spec")
        return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(doc, indent=1)}]
    if uri == "vordium://fees":
        try:
            fees = call(chain, cfg, "get_fees", {})
        except Refusal as e:
            raise PromptError(e.payload.get("reason") or "the fees could not be read")
        lines = ["| Market | Maker | Taker | Max leverage | Maintenance margin |", "|---|---|---|---|---|"]
        for m in fees.get("markets") or []:
            def f(v, unit=""):
                return "—" if v is None else f"{v}{unit}"
            lines.append(f"| {m.get('market')} | {f(m.get('maker_fee_pct'), '%')} | {f(m.get('taker_fee_pct'), '%')} | "
                         f"{f(m.get('max_leverage_x'), 'x')} | {f(m.get('maintenance_margin_pct'), '%')} |")
        lines.append("")
        lines.append(f"Read from the {cfg.network} at {fees.get('read_at') or 'call time'}. — means the network "
                     "publishes no value.")
        return [{"uri": uri, "mimeType": "text/markdown", "text": "\n".join(lines)}]
    if uri == "vordium://docs":
        return [{"uri": uri, "mimeType": "text/markdown",
                 "text": "\n".join(f"- [{t}]({u})" for t, u in DOCS)}]
    raise PromptError(f"no resource {uri!r}; resources/list names them")
