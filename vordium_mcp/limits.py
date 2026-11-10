"""Limits for the public read-only HTTP server: how many calls run at once, how long one call may take, which client a
request belongs to, and session ids the server can check.

Standard library only. Nothing here stores an address beyond the moment a call runs: the in-flight table holds a
client's bucket only while one of its calls is running.
"""
import hashlib, hmac, ipaddress, re, secrets, threading, time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from .chain import ChainUnreachable
from .networks import ConfigError


@dataclass(frozen=True)
class Limits:
    # connections handled at once (one thread each); past it a connection is answered 503 and closed at once
    max_connections: int = 48
    # calls that read the network, running at once: in all, and per client (an IPv4 address, or an IPv6 /64)
    in_flight: int = 32
    in_flight_per_address: int = 8
    # calls that read the network, per minute across every client (a backstop for the network behind this server)
    global_per_minute: int = 6000
    global_burst: int = 600
    # the most one call may take, every read included (below the proxy's own limit, so the caller always gets an answer)
    call_budget_s: float = 25.0
    # how long one socket read or write may stall (per operation, not a limit on the whole request)
    socket_timeout_s: float = 10.0
    # candles read from another base (the same candle service by a shorter path), with this server's own budget
    candles_base: Optional[str] = None
    candles_per_minute: int = 600
    candles_burst: int = 60


ENV = {"max_connections": ("VORDIUM_MCP_MAX_CONNECTIONS", int, 2),
       "in_flight": ("VORDIUM_MCP_MAX_IN_FLIGHT", int, 1),
       "in_flight_per_address": ("VORDIUM_MCP_MAX_IN_FLIGHT_PER_ADDRESS", int, 1),
       "global_per_minute": ("VORDIUM_MCP_GLOBAL_PER_MINUTE", int, 1),
       "global_burst": ("VORDIUM_MCP_GLOBAL_BURST", int, 1),
       "call_budget_s": ("VORDIUM_MCP_CALL_BUDGET_S", float, 1),
       "socket_timeout_s": ("VORDIUM_MCP_SOCKET_TIMEOUT_S", float, 1),
       "candles_per_minute": ("VORDIUM_MCP_CANDLES_PER_MINUTE", int, 1),
       "candles_burst": ("VORDIUM_MCP_CANDLES_BURST", int, 1)}
LOCAL_BASE = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]):\d{1,5}$")


def limits_from_env(env):
    """The limits, each from its VORDIUM_MCP_* variable when set. Checked before anything is served."""
    over = {}
    for field, (name, kind, least) in ENV.items():
        raw = str(env.get(name) or "").strip()
        if not raw:
            continue
        try:
            v = kind(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a number")
        if not v >= least or v != v or v == float("inf"):
            raise ConfigError(f"{name} must be at least {least}")
        over[field] = v
    base = str(env.get("VORDIUM_MCP_CANDLES_BASE") or "").strip().rstrip("/")
    if base:
        if not LOCAL_BASE.match(base):
            raise ConfigError("VORDIUM_MCP_CANDLES_BASE takes a local http://127.0.0.1:PORT")
        over["candles_base"] = base
    lim = Limits(**over)
    if lim.in_flight_per_address > lim.in_flight:
        raise ConfigError("VORDIUM_MCP_MAX_IN_FLIGHT_PER_ADDRESS cannot be above VORDIUM_MCP_MAX_IN_FLIGHT")
    if lim.in_flight >= lim.max_connections:
        raise ConfigError("VORDIUM_MCP_MAX_IN_FLIGHT must be below VORDIUM_MCP_MAX_CONNECTIONS (lists and refusals need room)")
    return lim


# ── which client a request belongs to ────────────────────────────────────────
def client_key(address):
    """The bucket a client address is counted in: an IPv4 address as itself (/32), an IPv6 address by its /64 (one
    household or one server holds a whole /64, so a new address inside it is the same client). Anything else is kept as
    given, cut to 64 characters."""
    s = str(address or "").strip()
    try:
        ip = ipaddress.ip_address(s.split("%", 1)[0])
    except ValueError:
        return "?" + s[:64]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


# ── session ids the server can check ─────────────────────────────────────────
SESSION = re.compile(r"^[0-9a-f]{32}$")


class SessionIds:
    """Mcp-Session-Id values this server issued: 16 hex of chance and 16 hex of an HMAC over them, keyed by a secret
    drawn when the server starts and kept only in memory. Nothing is stored per session; an id the server did not issue
    (or issued before it started again) does not check out."""

    def __init__(self, key=None):
        self._key = key or secrets.token_bytes(32)

    def _tag(self, nonce):
        return hmac.new(self._key, nonce.encode(), hashlib.sha256).hexdigest()[:16]

    def issue(self):
        nonce = secrets.token_hex(8)
        return nonce + self._tag(nonce)

    def valid(self, sid):
        return (isinstance(sid, str) and SESSION.match(sid) is not None
                and hmac.compare_digest(sid[16:], self._tag(sid[:16])))


# ── calls running at once ────────────────────────────────────────────────────
class InFlight:
    """Calls running now, in all and per client bucket. ``enter`` never waits: it admits a call or says which cap is full."""

    def __init__(self, total, per_key):
        self.total, self.per_key = total, per_key
        self.lock, self.n, self.by_key = threading.Lock(), 0, {}

    def enter(self, key):
        """None when admitted (the caller must ``leave`` with the same key), else "address" or "total"."""
        with self.lock:
            mine = self.by_key.get(key, 0)
            if mine >= self.per_key:
                return "address"
            if self.n >= self.total:
                return "total"
            self.by_key[key] = mine + 1
            self.n += 1
            return None

    def leave(self, key):
        with self.lock:
            mine = self.by_key.get(key, 0)
            if mine <= 1:
                self.by_key.pop(key, None)
            else:
                self.by_key[key] = mine - 1
            self.n = max(0, self.n - 1)

    def running(self):
        with self.lock:
            return self.n


# ── the time one call may take ───────────────────────────────────────────────
_local = threading.local()


class TimeBudgetSpent(ChainUnreachable):
    """A call reached its time limit before every read it needed was answered."""


@contextmanager
def deadline(seconds):
    """Every network read made by this thread inside the block shares ``seconds``: each read waits at most what is left,
    and a read asked for after the time is up is not made."""
    prev = getattr(_local, "end", None), getattr(_local, "budget", None), getattr(_local, "spent", False)
    _local.end, _local.budget, _local.spent = time.monotonic() + seconds, seconds, False
    try:
        yield
    finally:
        _local.end, _local.budget, _local.spent = prev


def budget_spent():
    """True when a read inside the current ``deadline`` block was cut short or not made."""
    return bool(getattr(_local, "spent", False))


def _spend():
    _local.spent = True
    return TimeBudgetSpent(f"this call reached its time limit of {_local.budget:g} s before every read it needed was "
                           "answered. Nothing is filled in from memory; try again, or ask for less.")


def wait_left(default):
    """How long this thread may wait for another call's read: ``default``, or less when its own deadline is nearer."""
    end = getattr(_local, "end", None)
    return default if end is None else min(default, end - time.monotonic())


def spent_here():
    """This call's own time-limit refusal (marks the call as cut short)."""
    return _spend()


def budget_fetch(inner, candles=None):
    """``inner`` (method, url, body=None, timeout=12) with the calling thread's deadline applied, and candle reads sent
    by ``candles`` (a CandleRoute) when given. Without a deadline and a candle route it behaves exactly as ``inner``."""

    def fetch(method, url, body=None, timeout=12):
        end = getattr(_local, "end", None)
        kw = {}
        if end is not None:
            left = end - time.monotonic()
            if left <= 0.05:
                raise _spend()
            kw["timeout"] = min(timeout, left)
        elif timeout != 12:
            kw["timeout"] = timeout
        try:
            if candles is not None and candles.matches(url):
                return candles.fetch(inner, method, url, body, kw)
            return inner(method, url, body, **kw)
        except TimeBudgetSpent:
            raise
        except ChainUnreachable as e:
            if end is not None and time.monotonic() >= end - 0.05:
                raise _spend() from e
            raise
    return fetch


class CandleRoute:
    """Candle reads sent to ``local`` (the same candle service, reached without leaving this machine) instead of
    ``public``, within this server's own budget. What a caller sees — the source of every answer, every error — names the
    public address only."""

    def __init__(self, public, local, limiter):
        self.public, self.local, self.limiter = public.rstrip("/"), local.rstrip("/"), limiter

    def matches(self, url):
        return url.startswith(self.public + "/candles")

    def fetch(self, inner, method, url, body, kw):
        ok, wait = self.limiter.take("candles")
        if not ok:
            raise ChainUnreachable(f"candles: this server's share of the candle service is in use; try again in {wait} s")
        try:
            return inner(method, self.local + url[len(self.public):], body, **kw)
        except ChainUnreachable as e:
            raise ChainUnreachable(str(e).replace(self.local, self.public)) from None
