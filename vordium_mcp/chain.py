"""The read layer. Every value a tool returns comes from here, and everything here comes from the network.

Nothing about the chain is stored: chain id, genesis, markets, symbols, fees, leverage, prices and balances are read
when a tool is called. If a value cannot be read, the tool says so; it is never filled in from memory.

The transport is injectable (``fetch=``), so the tests drive every branch without a network.
"""
import json, time, urllib.error, urllib.request

from . import __version__

USER_AGENT = f"vordium-mcp/{__version__} (+https://docs.vordium.com/build/mcp/)"


class ChainUnreachable(RuntimeError):
    """The network could not be read. Never swallowed: a tool that cannot read says so."""


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Read(dict):
    """One read: what was asked, what came back, and when. Carried into every tool result as its source."""

    @property
    def ok(self):
        return self.get("status") == 200 and self.get("json") is not None


def _http(method, url, body=None, timeout=12):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                          # DNS, TLS, timeout
        raise ChainUnreachable(f"{url}: {type(e).__name__}: {str(e)[:160]}")


class Chain:
    """Reads one network. ``rest`` is the base of the node's REST routes, ``spec`` the network's published spec and
    ``truth`` the network's own answer (several validators read at once and compared)."""

    def __init__(self, network, rest, spec, truth, fetch=None, ttl=5.0, clock=time.time):
        self.network = network
        self.rest = rest.rstrip("/")
        self.spec_url = spec
        self.truth_url = truth
        self._fetch = fetch or _http
        self.ttl = ttl
        self.clock = clock
        self._cache = {}

    def _read_url(self, url, cache=True):
        now = self.clock()
        hit = self._cache.get(url)
        if cache and hit and now - hit["at"] < self.ttl:
            return hit["read"]
        status, text = self._fetch("GET", url)
        try:
            doc = json.loads(text)
        except Exception:
            doc = None
        r = Read(url=url, status=status, json=doc, text=None if doc is not None else text[:300], read_at=iso(now))
        self._cache[url] = {"at": now, "read": r}
        return r

    def read(self, path, cache=True):
        return self._read_url(self.rest + path, cache)

    def post(self, path, body):
        status, text = self._fetch("POST", self.rest + path, body)
        try:
            doc = json.loads(text)
        except Exception:
            doc = None
        return Read(url=self.rest + path, status=status, json=doc,
                    text=None if doc is not None else text[:300], read_at=iso(self.clock()))

    # ── reads ────────────────────────────────────────────────────────────────
    def spec(self):
        return self._read_url(self.spec_url)

    def truth(self, cache=False):
        return self._read_url(self.truth_url, cache)

    def status(self, cache=True):
        return self.read("/status", cache)

    def markets(self):
        return self.read("/api/pairs/perps")

    def market_specs(self):
        return self.read("/markets/specs")

    def oracle(self):
        return self.read("/oracle/prices")

    def orderbook(self, pair_id):
        return self.read(f"/orderbook/{int(pair_id)}", cache=False)

    def funding(self, pair_id):
        return self.read(f"/funding/{int(pair_id)}")

    def balance(self, address):
        return self.read(f"/balance/{address}", cache=False)

    def equity(self, address):
        return self.read(f"/account/equity/{address}", cache=False)

    def positions(self, address):
        return self.read(f"/positions/{address}", cache=False)

    def orders(self, address):
        return self.read(f"/orders/{address}", cache=False)

    def agents(self, owner):
        return self.read(f"/agents/{owner}", cache=False)

    def agent(self, owner, agent):
        return self.read(f"/agents/{owner}/{agent}", cache=False)

    def session_valid(self, owner, key):
        return self.read(f"/session/valid?owner={owner}&session_key={key}", cache=False)

    # ── helpers over those reads (still nothing typed) ───────────────────────
    def require(self, read, what):
        if not read.ok:
            raise ChainUnreachable(f"{what}: {read['url']} answered {read['status']}"
                                   + (f" {read['text']}" if read.get("text") else ""))
        return read["json"]

    def pairs(self):
        doc = self.require(self.markets(), "market list")
        return list(doc.get("pairs") or [])

    def pair_by_symbol(self, symbol):
        s = str(symbol or "").strip().upper()
        if not s:
            return None
        for p in self.pairs():
            names = {str(p.get("symbol", "")).upper(), str(p.get("name", "")).upper()}
            if s in names or (s.endswith("-PERP") and s[:-5] in names):
                return p
        return None

    def pair_by_id(self, pair_id):
        for p in self.pairs():
            if p.get("pair_id") == pair_id:
                return p
        return None

    def chain_time_ms(self):
        doc = self.require(self.markets(), "market list")
        t = doc.get("chain_time_ms")
        return t if isinstance(t, int) else None
