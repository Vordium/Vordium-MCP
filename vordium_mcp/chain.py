"""The read layer. Every value a tool returns comes from here, and everything here comes from the network.

Nothing about the chain is stored: chain id, genesis, markets, symbols, fees, leverage, prices and balances are read
when a tool is called. If a value cannot be read, the tool says so; it is never filled in from memory.

The transport is injectable (``fetch=``), so the tests drive every branch without a network.
"""
import json, threading, time, urllib.error, urllib.request

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

    MAX_CACHED = 4000
    MAX_CACHED_BYTES = 6 * 2 ** 20      # bodies kept at once; a public server must not grow without bound
    MAX_ENTRY_BYTES = 2 ** 20           # a larger body is answered but not kept

    def __init__(self, network, rest, spec, truth, fetch=None, ttl=5.0, clock=time.time, short_ttl=0.0, links=None):
        self.network = network
        self.rest = rest.rstrip("/")
        self.spec_url = spec
        self.truth_url = truth
        self.links = dict(links or {})
        self._fetch = fetch or _http
        self.ttl = ttl
        self.short_ttl = short_ttl          # a read asked uncached is still shared this long (a public server: 2 s)
        self.clock = clock
        self._cache = {}
        self._bytes = 0
        self._lock = threading.Lock()
        self._inflight = {}                 # url -> Event: one read of a URL at a time; others wait for its answer
        self.fetches = 0                    # reads that reached the network (not served from the cache)

    def _read_url(self, url, cache=True, ttl=None):
        now = self.clock()
        keep = (self.ttl if ttl is None else ttl) if cache else self.short_ttl
        with self._lock:
            hit = self._cache.get(url)
            if keep > 0 and hit and now - hit["at"] < keep:
                return hit["read"]
            rec = self._inflight.get(url) if keep > 0 else None
            if keep > 0 and rec is None:
                self._inflight[url] = mine = {"ev": threading.Event(), "read": None, "error": None}
        if rec is not None:                     # the same read is already on its way: take its answer, whatever its age
            from .limits import TimeBudgetSpent, spent_here, wait_left
            wait = wait_left(15)                # never past this call's own time limit
            if wait <= 0.05:
                raise spent_here()
            if rec["ev"].wait(wait):
                if rec["read"] is not None:
                    return rec["read"]
                # the other call ran out of ITS time: that is not this call's answer — read it here, within our own limit
                if rec["error"] is not None and not isinstance(rec["error"], TimeBudgetSpent):
                    raise rec["error"]
            if wait_left(15) <= 0.05:
                raise spent_here()
            return self._fetch_now(url, keep)   # it took too long, or ran out of its own time: read it here
        if keep <= 0:
            return self._fetch_now(url, keep)
        try:
            mine["read"] = self._fetch_now(url, keep)
            return mine["read"]
        except ChainUnreachable as e:
            mine["error"] = e
            raise
        finally:
            with self._lock:
                self._inflight.pop(url, None)
            mine["ev"].set()

    def _fetch_now(self, url, keep):
        now = self.clock()
        status, text = self._fetch("GET", url)
        try:
            doc = json.loads(text)
        except Exception:
            doc = None
        r = Read(url=url, status=status, json=doc, text=None if doc is not None else text[:300], read_at=iso(now))
        with self._lock:
            self.fetches += 1
            self._store(url, now, r, len(text or ""), keep)
        return r

    def _store(self, url, now, r, size, keep):
        """Keep a read only as long as it can be served again; drop what has expired on every insert, and the oldest
        when the bodies kept pass MAX_CACHED_BYTES. Called with the lock held."""
        old = self._cache.pop(url, None)
        if old:
            self._bytes -= old["size"]
        horizon = max(self.ttl, self.short_ttl, 30.0)
        for k in [k for k, v in self._cache.items() if now - v["at"] >= horizon]:
            self._bytes -= self._cache.pop(k)["size"]
        if size > self.MAX_ENTRY_BYTES or (keep <= 0 and url not in (self.spec_url, self.truth_url)):
            return
        self._cache[url] = {"at": now, "read": r, "size": size}
        self._bytes += size
        while self._cache and (self._bytes > self.MAX_CACHED_BYTES or len(self._cache) > self.MAX_CACHED):
            k = min(self._cache, key=lambda k: self._cache[k]["at"])
            self._bytes -= self._cache.pop(k)["size"]

    def read_link(self, name, path, cache=True, ttl=None):
        """A read from another public address of this network (the explorer, the app's own services), by name."""
        base = self.links.get(name)
        if not base:
            return None
        return self._read_url(base.rstrip("/") + path, cache, ttl)

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

    def cross(self, address):
        """the account's cross-margin record (cross positions and their account-level liquidation price), where served"""
        return self.read(f"/account/cross/{address}", cache=False)

    def margin_modes(self, address):
        """each pair's margin mode for this account. Returns ('none', None) where the network has no cross margin
        (its read answers 404: every position is isolated); ('unknown', None) when the read fails any other way (a cross
        position's liquidation price must then read unknown, never the per-position one); else ('ok', state) with
        state = {'default': 'cross'|'isolated', 'modes': {pair_id: mode}, 'cross': {position_id: record}}."""
        try:
            r = self.cross(address)
        except Exception:  # noqa: BLE001
            return "unknown", None
        if r.get("status") == 404:
            return "none", None
        j = r.get("json") if r.ok else None
        if not isinstance(j, dict) or j.get("default_mode") not in ("cross", "isolated"):
            return "unknown", None
        modes = {m.get("pair_id"): m.get("mode") for m in (j.get("modes") or []) if isinstance(m, dict)}
        return "ok", {"default": j["default_mode"], "modes": modes,
                      "cross": {c.get("position_id"): c for c in (j.get("cross_positions") or []) if isinstance(c, dict)}}

    def orders(self, address):
        return self.read(f"/orders/{address}", cache=False)

    def agents(self, owner):
        return self.read(f"/agents/{owner}", cache=False)

    def agent(self, owner, agent):
        return self.read(f"/agents/{owner}/{agent}", cache=False)

    def agent_events(self, agent):
        return self.read(f"/events/agent/{agent}", cache=False)

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
