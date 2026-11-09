"""MCP over JSON-RPC 2.0: stdio (the default, for a client on your own machine) and a read-only streamable HTTP mode.

Standard library only. A tool's result is JSON text; a refusal is an MCP tool error (isError) carrying the refusal, so
a model sees it as a failure and never as an empty success.
"""
import hashlib, json, math, re, secrets, sys, threading, time, traceback
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .chain import Chain
from .networks import NETWORKS
from .prompts import PromptError, get_prompt, list_prompts, list_resources, read_resource
from . import tools as _tools
from .tools import Refusal, call, listed_tools

NAME = "vordium-mcp"
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")     # newest first: offered to a client that asks for another
MAX_BODY = 64 * 1024


def instructions(cfg):
    lines = [f"You are connected to Vordium ({cfg.network}). Every value is read from the network when a tool is called."]
    if cfg.network == "testnet":
        lines.append("This is the test network: its tokens have no value.")
    if cfg.read_only:
        lines.append("This server is read-only: it has no trading tools. To trade as a policy-limited agent, run "
                     "vordium-mcp on your own machine (uvx vordium-mcp).")
    else:
        lines.append("place_order, close_position, modify_order, set_tpsl, cancel_order, cancel_all and stop_all sign on "
                     "the user's machine with a registered agent key and are refused while the network reports agent "
                     "trading off; the chain enforces the agent's limits. Every order is shown to the user to confirm "
                     "before it is signed, unless the user chose auto. Call get_network first to see the current state.")
    lines.append("Never ask the user for a private key or recovery phrase: no tool takes one.")
    return " ".join(lines)


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _content(payload, is_error=False):
    """The result as structured content and, for clients that read text, the same JSON serialized. Its first field is
    ``summary``: one plain sentence a person can read."""
    if isinstance(payload, dict):
        summary = payload.get("summary")
        if not summary and payload.get("refused"):
            summary = "Refused: " + str(payload.get("reason")) + (f" {payload['remedy']}" if payload.get("remedy") else "")
        if summary:
            payload = {"summary": summary, **{k: v for k, v in payload.items() if k != "summary"}}
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1, default=str)}],
            "structuredContent": payload, "isError": is_error}


# How long a public server shares one tool's answer for the same arguments (seconds). Live books and account reads are
# shared briefly; market rules, candles and the season change slowly. A server on your own machine shares nothing.
RESULT_TTL = {"get_orderbook": 1.0, "get_status": 2.0, "get_network": 2.0, "get_oracle": 2.0, "get_trades": 2.0,
              "get_account": 2.0, "get_balance": 2.0, "get_positions": 2.0, "get_orders": 2.0, "get_fills": 2.0,
              "get_agents": 2.0, "get_policy": 2.0, "get_risk": 2.0, "get_address": 2.0, "get_points": 5.0,
              "get_referrals": 5.0, "get_markets": 5.0, "get_fees": 5.0, "get_funding": 5.0, "get_candles": 5.0,
              "get_tiers": 5.0, "get_season": 5.0, "get_leaderboard": 5.0, "get_tx": 5.0, "get_block": 5.0}


class ResultCache:
    """A tool's serialized answer per (tool, arguments), kept RESULT_TTL seconds, bounded in bytes. Only for a public
    server: many clients asking the same thing get one read of the network and one serialization."""

    def __init__(self, max_bytes=8 * 2 ** 20, clock=time.monotonic):
        self.max, self.clock, self.lock = max_bytes, clock, threading.Lock()
        self.items, self.bytes = {}, 0

    def get(self, key):
        with self.lock:
            hit = self.items.get(key)
            if hit and self.clock() < hit[0]:
                return hit[1]
        return None

    def put(self, key, ttl, text):
        now = self.clock()
        with self.lock:
            # each key once: the expired ones and this key's old answer (a key may be both)
            for k in {k for k, v in self.items.items() if v[0] <= now} | ({key} if key in self.items else set()):
                self.bytes -= len(self.items.pop(k)[1])
            if len(text) > self.max // 8:
                return
            self.items[key] = (now + ttl, text)
            self.bytes += len(text)
            while self.bytes > self.max and self.items:
                k = min(self.items, key=lambda k: self.items[k][0])
                self.bytes -= len(self.items.pop(k)[1])


class Stats:
    """Counters only: calls per tool, prompt and method, refusals and rate-limited requests. No address, no argument."""

    def __init__(self, clock=time.time):
        self.started, self.clock, self.lock = clock(), clock, threading.Lock()
        self.calls, self.methods = {}, {}
        self.refused = self.rate_limited = self.errors = self.cache_errors = 0

    def method(self, name):
        with self.lock:
            key = name if isinstance(name, str) and len(name) < 40 else "other"
            self.methods[key] = self.methods.get(key, 0) + 1

    def tool(self, name, refused):
        with self.lock:
            self.calls[name] = self.calls.get(name, 0) + 1
            self.refused += 1 if refused else 0

    def bump(self, field):
        with self.lock:
            setattr(self, field, getattr(self, field) + 1)

    def snapshot(self):
        with self.lock:
            return {"uptime_s": int(self.clock() - self.started), "tool_calls": dict(sorted(self.calls.items())),
                    "methods": dict(sorted(self.methods.items())), "refused": self.refused,
                    "rate_limited": self.rate_limited, "errors": self.errors,
                    "cache_errors": self.cache_errors}


class Session:
    """One client connection over stdio: what the client said it can do (at initialize), and a way to ask the user a
    question in the middle of a tool call (MCP elicitation) — the server's own request, written out and answered before
    the call goes on. Messages that arrive while it waits are kept and handled afterwards, in order."""

    def __init__(self, write=None, read=None, wait_s=600.0):
        self.capabilities = {}
        self._write, self._read, self._n, self.queued, self.wait_s = write, read, 0, [], wait_s

    current = None                                         # the id of the tools/call being run (its cancel aborts a question)

    def can_elicit(self):
        """A client that declared elicitation in form mode ({} or with "form"): a URL-only client cannot show the order."""
        e = self.capabilities.get("elicitation")
        return (self._write is not None and self._read is not None and isinstance(e, dict)
                and (not e or isinstance(e.get("form"), dict)))

    def elicit(self, message, schema):
        self._n += 1
        rid = f"vordium-elicit-{self._n}"
        self._write({"jsonrpc": "2.0", "id": rid, "method": "elicitation/create",
                     "params": {"message": message, "requestedSchema": schema}})
        end = time.monotonic() + self.wait_s
        while time.monotonic() < end:
            m = self._read(max(0.0, end - time.monotonic()))
            if m is None:                                   # the client went away, or no answer in time
                return {"action": "cancel"}
            if isinstance(m, dict) and m.get("id") == rid and "method" not in m:
                return m.get("result") if isinstance(m.get("result"), dict) else {"action": "decline"}
            if (isinstance(m, dict) and m.get("method") == "notifications/cancelled" and self.current is not None
                    and (m.get("params") or {}).get("requestId") == self.current):
                return {"action": "cancel"}                # the client gave up on this call: never sign behind its back
            self.queued.append(m)
        return {"action": "cancel"}


CHEAP = ("initialize", "ping", "tools/list", "prompts/list", "resources/list", "resources/templates/list")


def _complete(payload, cfg, name):
    """Every successful result carries summary and network (outputSchema requires both)."""
    if isinstance(payload, dict):
        if "network" not in payload:
            payload = {**payload, "network": cfg.network}
        if not payload.get("summary"):
            t = next((t for t in listed_tools(cfg) if t["name"] == name), {})
            payload = {**payload, "summary": f"{t.get('title', name)}: read from the {payload['network']}."}
    return payload


def handle(message, chain, cfg, stats=None, session=None):
    """One JSON-RPC message in, one response out (or None for a notification)."""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    method, id_ = message.get("method"), message.get("id")
    if method is None and ("result" in message or "error" in message):
        return None                                        # a client's answer to a question that is no longer open
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(id_, -32602, "params must be an object")
    if stats:
        stats.method(method)
    if method == "initialize":
        if session is not None and isinstance(params.get("capabilities"), dict):
            session.capabilities = params["capabilities"]
        asked = params.get("protocolVersion")
        return _result(id_, {
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False},
                             "resources": {"listChanged": False, "subscribe": False}},
            "serverInfo": {"name": NAME, "title": "Vordium", "version": __version__},
            "instructions": instructions(cfg),
        })
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [{k: v for k, v in t.items() if k in ("name", "title", "description", "inputSchema", "outputSchema", "annotations")}
                                       for t in listed_tools(cfg)]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        known = any(t["name"] == name for t in listed_tools(cfg))
        if not isinstance(args, dict):
            return _result(id_, _content({"refused": True, "reason": "arguments must be an object", "network": cfg.network}, True))
        _tools.CTX.session = session
        if session is not None:
            session.current = id_
        try:
            out = _result(id_, _content(_complete(call(chain, cfg, name, args), cfg, name)))
            if stats:
                stats.tool(name if known else "unknown", False)
            return out
        except Refusal as r:
            if stats:
                stats.tool(name if known else "unknown", True)
            return _result(id_, _content({**r.payload, "network": cfg.network}, is_error=True))
        except Exception as e:                                    # never a fabricated success
            if stats:
                stats.tool(name if known else "unknown", True)
                stats.bump("errors")
            signs = any(t["name"] == name and t.get("annotations", {}).get("destructiveHint") for t in listed_tools(cfg))
            return _result(id_, _content({"refused": True, "reason": f"{type(e).__name__}: {str(e)[:300]}", "network": cfg.network,
                                          "note": ("This failed while signing or sending: read get_orders and get_positions "
                                                   "before trying again." if signs else "Nothing was signed or sent.")},
                                         is_error=True))
        finally:
            _tools.CTX.session = None
            if session is not None:
                session.current = None
    if method == "prompts/list":
        return _result(id_, {"prompts": list_prompts()})
    if method == "prompts/get":
        try:
            return _result(id_, get_prompt(params.get("name"), params.get("arguments") or {}))
        except PromptError as e:
            return _error(id_, -32602, str(e))
    if method == "resources/list":
        return _result(id_, {"resources": list_resources(cfg)})
    if method == "resources/templates/list":
        return _result(id_, {"resourceTemplates": []})
    if method == "resources/read":
        try:
            return _result(id_, {"contents": read_resource(chain, cfg, params.get("uri"))})
        except PromptError as e:
            return _error(id_, -32002, str(e))
    if id_ is None:
        return None
    return _error(id_, -32601, f"method not found: {method}")


def make_chain(cfg, fetch=None):
    links = {k: v for k, v in NETWORKS.get(cfg.network, {}).items() if k in ("app", "explorer", "candles")}
    return Chain(cfg.network, cfg.rest, cfg.spec, cfg.truth, fetch=fetch, short_ttl=cfg.short_cache_s, links=links)


def serve_stdio(cfg, stdin=None, stdout=None, chain=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    chain = chain or make_chain(cfg)

    def write(obj):
        stdout.write(json.dumps(obj, default=str) + "\n")
        stdout.flush()

    import queue
    lines, closed = queue.Queue(), threading.Event()

    def reader():                                  # every line the client sends, in order, as soon as it arrives
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.put(json.loads(line))
                except Exception:
                    lines.put("!parse")
        except Exception:
            pass                                   # an unreadable stdin ends the session like a closed one
        finally:
            closed.set()
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def next_line(wait_s=None):
        """One message from the client: a parsed dict, "!parse" for a bad line, or None at the end (or no answer in time)."""
        if closed.is_set() and lines.empty():
            return None                            # the client went away (even if a question consumed the end marker)
        try:
            return lines.get(timeout=wait_s)
        except queue.Empty:
            return None

    session = Session(write=write, read=lambda wait: next_line(wait))
    while True:
        message = session.queued.pop(0) if session.queued else next_line()
        if message is None:
            break
        if message == "!parse":
            write(_error(None, -32700, "parse error"))
            continue
        try:
            response = handle(message, chain, cfg, session=session)
        except Exception:
            print(traceback.format_exc()[:800], file=sys.stderr)
            response = _error(message.get("id") if isinstance(message, dict) else None, -32603, "internal error")
        if response is not None:
            write(response)


# ── read-only streamable HTTP ────────────────────────────────────────────────
class RateLimit:
    """A token bucket per client address: ``per_minute`` requests, refilled continuously, at most ``burst`` at once.
    Only the bucket is kept, in memory, and a bucket idle for two minutes is dropped: no address is stored beyond that."""

    def __init__(self, per_minute, clock=time.monotonic, max_clients=10000, burst=None):
        self.rate, self.clock, self.max = per_minute / 60.0, clock, max_clients
        self.cap = float(burst or per_minute)
        self.per_minute = per_minute
        self.buckets, self.lock = OrderedDict(), threading.Lock()     # least recently used first

    def allow(self, key):
        return self.take(key)[0]

    def take(self, key):
        """(allowed, seconds until the next request would be allowed)."""
        now = self.clock()
        with self.lock:
            while self.buckets:                                # idle two minutes: dropped (oldest first, so this stops early)
                k, (_, at) = next(iter(self.buckets.items()))
                if now - at < 120:
                    break
                del self.buckets[k]
            tokens, at = self.buckets.pop(key, (self.cap, now))
            tokens = min(self.cap, tokens + (now - at) * self.rate)
            while len(self.buckets) >= self.max:               # a hard bound: the least recently seen go first
                self.buckets.popitem(last=False)
            if tokens < 1:
                self.buckets[key] = (tokens, now)
                return False, max(1, math.ceil((1 - tokens) / self.rate))
            self.buckets[key] = (tokens - 1, now)
            return True, 0


CORS_HEADERS = "Content-Type, Accept, Authorization, MCP-Protocol-Version, Mcp-Session-Id, Last-Event-ID"
SESSION = re.compile(r"^[0-9a-f]{32}$")
ADDRESS_FACTOR = 20     # one address may carry many connections (a hosted AI client calls from a few addresses for all its users)


def make_http_handler(cfg, chain, limiter, stats=None, address_limiter=None, results=None):
    """``limiter`` holds each connection (its Mcp-Session-Id, given at initialize; an address when a client sends none);
    ``address_limiter`` holds each address across all its connections, ADDRESS_FACTOR times looser."""
    stats = stats or Stats()
    any_origin = "*" in cfg.allow_origins
    if address_limiter is None:     # a public server: connections share an address; elsewhere every request is the address's
        factor = ADDRESS_FACTOR if cfg.hosted else 1
        address_limiter = RateLimit(limiter.per_minute * factor, burst=int(limiter.cap) * factor)
    results = results if results is not None else (ResultCache() if cfg.short_cache_s > 0 else None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "vordium-mcp"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):                      # no request log: addresses are not recorded
            return

        def _client(self):
            if cfg.trust_proxy:
                h = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]
                if h.strip():
                    return h.strip()
            return self.client_address[0]

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return {}
            if any_origin:
                return {"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "Mcp-Session-Id, Retry-After"}
            if origin in cfg.allow_origins:
                return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                        "Access-Control-Expose-Headers": "Mcp-Session-Id, Retry-After"}
            return {}

        def _refuse(self, code, body, headers=None):
            self.close_connection = True                       # the body was not read: never reuse this connection
            return self._send(code, body, {**(headers or {}), "Connection": "close"})

        def _send(self, code, body=None, headers=None, raw=None):
            data = raw if raw is not None else (b"" if body is None else json.dumps(body, default=str).encode())
            self.send_response(code)
            for k, v in {**self._cors(), **(headers or {})}.items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if body is not None or raw is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            return origin is None or any_origin or origin in cfg.allow_origins

        def _path(self):
            return self.path.split("?")[0]

        def do_OPTIONS(self):
            if not self._origin_ok():
                return self._send(403, {"error": "origin not allowed"})
            return self._send(204, None, {"Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
                                          "Access-Control-Allow-Headers": CORS_HEADERS,
                                          "Access-Control-Max-Age": "600"})

        def do_GET(self):
            path = self._path()
            if path == "/health":
                body = {"ok": True, "name": NAME, "version": __version__, "network": cfg.network,
                        "read_only": cfg.read_only,
                        "limits": {"per_minute": limiter.per_minute, "burst": int(limiter.cap),
                                   "per_address_per_minute": address_limiter.per_minute},
                        "counters": {**stats.snapshot(), "network_reads": getattr(chain, "fetches", None)}}
                if "deep=1" in (self.path.split("?", 1)[1] if "?" in self.path else ""):
                    t0 = time.monotonic()
                    try:
                        from .tools import network_identity
                        network_identity(chain)
                        body["network_read"] = {"ok": True, "ms": int((time.monotonic() - t0) * 1000)}
                    except Exception as e:
                        body["ok"] = False
                        body["network_read"] = {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                                                "why": type(e).__name__}
                return self._send(200 if body["ok"] else 503, body)
            if path == "/mcp":
                return self._send(405, {"error": "this server opens no event stream; POST JSON-RPC to /mcp"},
                                  {"Allow": "POST, OPTIONS"})
            return self._send(404, {"error": "not found"})

        def do_DELETE(self):
            return self._send(405, {"error": "no sessions to end"}, {"Allow": "POST, OPTIONS"})

        def do_POST(self):
            if self._path() != "/mcp":
                return self._refuse(404, {"error": "not found"})
            if not self._origin_ok():
                return self._refuse(403, {"error": "origin not allowed"})
            # MCP-Protocol-Version is never refused: the version is agreed at initialize, and every version listed above
            # reads the same messages here; a client's newer or unknown header must not cut off a working connection.
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._refuse(415, {"error": "Content-Type must be application/json"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._refuse(413, {"error": "body too large"})
            try:
                message = json.loads(self.rfile.read(n) or b"null")
            except Exception:
                return self._send(400, _error(None, -32700, "parse error"))
            if isinstance(message, list):
                return self._send(400, _error(None, -32600, "batches are not accepted"))
            client = self._client()
            sid = (self.headers.get("Mcp-Session-Id") or "").strip().lower()
            sid = sid if cfg.hosted and SESSION.match(sid) else None     # connections are told apart on a public server only
            opening = isinstance(message, dict) and message.get("method") == "initialize"
            # listing what the server offers costs nothing per connection (only the address's budget below)
            cheap = isinstance(message, dict) and (message.get("method") in CHEAP
                                                   or str(message.get("method", "")).startswith("notifications/"))
            ok, wait, per, n, cap = True, 0, None, 0, 0
            if (sid or not opening) and not cheap:                       # its own budget first: a refused request costs its address nothing
                ok, wait = limiter.take(("s:" + sid) if sid else ("a:" + client))
                per, n, cap = ("connection" if sid else "address"), limiter.per_minute, int(limiter.cap)
            if ok:                                       # then the address's budget across its connections
                ok, wait = address_limiter.take("a:" + client)
                per, n, cap = "address", address_limiter.per_minute, int(address_limiter.cap)
            if not ok:
                stats.bump("rate_limited")
                id_ = message.get("id") if isinstance(message, dict) else None
                return self._send(429, _error(id_, -32029,
                                              f"Slow down: this public server answers {n} requests a minute per {per} "
                                              f"(up to {cap} at once). Try again in {wait} s, or run vordium-mcp on "
                                              "your own machine for unlimited reads."),
                                  {"Retry-After": str(wait)})
            shared = self._shared(message)
            if shared is not None:
                return self._send(200, raw=shared)
            try:
                response = handle(message, chain, cfg, stats)
            except Exception:
                stats.bump("errors")
                response = _error(message.get("id") if isinstance(message, dict) else None, -32603, "internal error")
            if response is None:
                return self._send(202)
            headers = {}
            if cfg.hosted and opening and "result" in response:
                headers["Mcp-Session-Id"] = sid or secrets.token_hex(16)     # stateless: only its rate bucket is kept
            key = self._result_key(message)
            if key and "result" in response and response["result"].get("isError") is False:   # a refusal is never shared
                text = json.dumps(response["result"], default=str)
                try:
                    results.put(key[1], RESULT_TTL[key[0]], text)
                except Exception:          # the cache only saves work: its failure never costs the caller the answer
                    stats.bump("cache_errors")
                return self._send(200, headers=headers, raw=self._wrap(message.get("id"), text))
            return self._send(200, response, headers)

        def _result_key(self, message):
            if results is None or not isinstance(message, dict) or message.get("method") != "tools/call":
                return None
            p = message.get("params") or {}
            name, args = (p.get("name"), p.get("arguments") or {}) if isinstance(p, dict) else (None, None)
            if name not in RESULT_TTL or not isinstance(args, dict):
                return None
            try:
                key = name + "\0" + json.dumps(args, sort_keys=True, allow_nan=False)
            except (TypeError, ValueError):
                return None
            return name, hashlib.sha256(key.encode()).hexdigest()      # fixed size; the arguments are not kept

        @staticmethod
        def _wrap(id_, text):
            return ('{"jsonrpc": "2.0", "id": ' + json.dumps(id_) + ', "result": ' + text + "}").encode()

        def _shared(self, message):
            key = self._result_key(message)
            if not key:
                return None
            text = results.get(key[1])
            if text is None:
                return None
            stats.tool(key[0], False)                                   # only answers that were not refusals are shared
            stats.method("tools/call")
            return self._wrap(message.get("id"), text)
    return Handler


class _Server(ThreadingHTTPServer):
    request_queue_size = 256            # bursts of new connections wait to be accepted instead of being refused
    daemon_threads = True


def serve_http(cfg, chain=None, ready=None):
    if not cfg.read_only:
        raise RuntimeError("the HTTP server runs read-only only")
    host, port = cfg.http.rsplit(":", 1)
    host = host.strip("[]")
    chain = chain or make_chain(cfg)
    limiter = RateLimit(cfg.rate_per_minute, burst=cfg.burst or cfg.rate_per_minute)
    httpd = _Server((host, int(port)), make_http_handler(cfg, chain, limiter))
    if ready:
        ready(httpd)
    print(f"vordium-mcp {__version__}: read-only{', hosted' if cfg.hosted else ''}, {cfg.network}, "
          f"http://{host}:{httpd.server_address[1]}/mcp", file=sys.stderr)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
