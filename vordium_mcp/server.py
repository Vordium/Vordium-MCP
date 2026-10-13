"""MCP over JSON-RPC 2.0: stdio (the default, for a client on your own machine) and a read-only streamable HTTP mode.

Standard library only. A tool's result is JSON text; a refusal is an MCP tool error (isError) carrying the refusal, so
a model sees it as a failure and never as an empty success.
"""
import json, sys, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .chain import Chain
from .tools import Refusal, call, listed_tools

NAME = "vordium-mcp"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024


def instructions(cfg):
    lines = [f"You are connected to Vordium ({cfg.network}). Every value is read from the network when a tool is called."]
    if cfg.network == "testnet":
        lines.append("This is the test network: its tokens have no value.")
    if cfg.read_only:
        lines.append("This server is read-only: it has no trading tools.")
    else:
        lines.append("place_order and cancel_order sign on the user's machine with a registered agent key and are refused "
                     "while the network reports agent trading off; the chain enforces the agent's limits. Call "
                     "get_network first to see the current state.")
    lines.append("Never ask the user for a private key or recovery phrase: no tool takes one.")
    return " ".join(lines)


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _content(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1, default=str)}],
            "structuredContent": payload, "isError": is_error}


def handle(message, chain, cfg):
    """One JSON-RPC message in, one response out (or None for a notification)."""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    method, id_ = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(id_, {
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "title": "Vordium", "version": __version__},
            "instructions": instructions(cfg),
        })
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [{k: v for k, v in t.items() if k in ("name", "description", "inputSchema", "annotations")}
                                       for t in listed_tools(cfg)]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _result(id_, _content({"refused": True, "reason": "arguments must be an object"}, True))
        try:
            return _result(id_, _content(call(chain, cfg, name, args)))
        except Refusal as r:
            return _result(id_, _content(r.payload, is_error=True))
        except Exception as e:                                    # never a fabricated success
            return _result(id_, _content({"refused": True, "reason": f"{type(e).__name__}: {str(e)[:300]}",
                                          "note": "Nothing was signed or sent."}, is_error=True))
    if id_ is None:
        return None
    return _error(id_, -32601, f"method not found: {method}")


def make_chain(cfg, fetch=None):
    return Chain(cfg.network, cfg.rest, cfg.spec, cfg.truth, fetch=fetch)


def serve_stdio(cfg, stdin=None, stdout=None, chain=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    chain = chain or make_chain(cfg)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        try:
            response = handle(message, chain, cfg)
        except Exception:
            print(traceback.format_exc()[:800], file=sys.stderr)
            response = _error(message.get("id") if isinstance(message, dict) else None, -32603, "internal error")
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


# ── read-only streamable HTTP ────────────────────────────────────────────────
class RateLimit:
    """A token bucket per client address: ``per_minute`` requests, refilled continuously."""

    def __init__(self, per_minute, clock=time.monotonic, max_clients=10000):
        self.rate, self.cap, self.clock, self.max = per_minute / 60.0, float(per_minute), clock, max_clients
        self.buckets, self.lock = {}, threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self.lock:
            tokens, at = self.buckets.get(key, (self.cap, now))
            tokens = min(self.cap, tokens + (now - at) * self.rate)
            if len(self.buckets) >= self.max and key not in self.buckets:
                self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 120}
            if tokens < 1:
                self.buckets[key] = (tokens, now)
                return False
            self.buckets[key] = (tokens - 1, now)
            return True


def make_http_handler(cfg, chain, limiter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vordium-mcp"
        sys_version = ""

        def log_message(self, fmt, *args):                      # no request log: addresses are not recorded
            return

        def _client(self):
            if cfg.trust_proxy:
                h = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]
                if h.strip():
                    return h.strip()
            return self.client_address[0]

        def _send(self, code, body=None, headers=None):
            data = b"" if body is None else json.dumps(body, default=str).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            return origin is None or origin in cfg.allow_origins

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                return self._send(200, {"ok": True, "name": NAME, "version": __version__, "network": cfg.network})
            if self.path.split("?")[0] == "/mcp":
                return self._send(405, {"error": "this server opens no event stream; POST JSON-RPC to /mcp"},
                                  {"Allow": "POST"})
            return self._send(404, {"error": "not found"})

        def do_DELETE(self):
            return self._send(405, {"error": "no sessions to end"}, {"Allow": "POST"})

        def do_POST(self):
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            if not self._origin_ok():
                return self._send(403, {"error": "origin not allowed"})
            if not limiter.allow(self._client()):
                return self._send(429, {"error": "too many requests"}, {"Retry-After": "10"})
            pv = self.headers.get("MCP-Protocol-Version")
            if pv and pv not in SUPPORTED_PROTOCOLS:
                return self._send(400, {"error": f"unsupported MCP-Protocol-Version {pv}"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, {"error": "Content-Type must be application/json"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            try:
                message = json.loads(self.rfile.read(n) or b"null")
            except Exception:
                return self._send(400, _error(None, -32700, "parse error"))
            if isinstance(message, list):
                return self._send(400, _error(None, -32600, "batches are not accepted"))
            try:
                response = handle(message, chain, cfg)
            except Exception:
                response = _error(message.get("id") if isinstance(message, dict) else None, -32603, "internal error")
            if response is None:
                return self._send(202)
            return self._send(200, response)
    return Handler


def serve_http(cfg, chain=None, ready=None):
    if not cfg.read_only:
        raise RuntimeError("the HTTP server runs read-only only")
    host, port = cfg.http.rsplit(":", 1)
    host = host.strip("[]")
    chain = chain or make_chain(cfg)
    httpd = ThreadingHTTPServer((host, int(port)), make_http_handler(cfg, chain, RateLimit(cfg.rate_per_minute)))
    httpd.daemon_threads = True
    if ready:
        ready(httpd)
    print(f"vordium-mcp {__version__}: read-only, {cfg.network}, http://{host}:{httpd.server_address[1]}/mcp",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
