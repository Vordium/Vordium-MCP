"""Load check for the public server's limits (run by hand; not collected by pytest).

Starts the hosted server in a child process on a free 127.0.0.1 port, reading a stub network that answers every account
read after DELAY seconds (no request leaves this machine), then floods it from one client while a second client asks
one question at a time. Prints one JSON line per scenario: what the second client got, and what the flood got.

    options: [--seconds 8] [--workers 64] [--delay 0.25] [--tasks-max 64] [--root TREE] [--only TEXT]

--tasks-max makes the server's process refuse to start a thread past that many, as a task limit on its service does.
--root runs the server from another tree with this stub network and this load (two trees, one load).
"""
import argparse, collections, json, os, subprocess, sys, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CHILD = r"""
import json, os, sys, threading, time
sys.path.insert(0, %(root)r); sys.path.insert(0, %(here)r)
TASKS = int(os.environ.get("LOAD_TASKS_MAX") or 0)
if TASKS:
    _start = threading.Thread.start
    def start(self):
        if threading.active_count() >= TASKS:
            raise RuntimeError("can't start new thread")
        return _start(self)
    threading.Thread.start = start
from fake import FakeNet
from vordium_mcp.chain import ChainUnreachable
from vordium_mcp.config import load
from vordium_mcp.server import make_chain, serve_http
DELAY = float(os.environ["LOAD_DELAY"])
net = FakeNet()
def stub(method, url, body=None, timeout=12):
    if "/balance/" in url or "/account/equity/" in url:
        if DELAY > timeout:
            time.sleep(timeout); raise ChainUnreachable(url + ": TimeoutError")
        time.sleep(DELAY)
        return 200, json.dumps({"total": 1, "free_balance": 1, "total_equity": 1})
    return net(method, url, body, timeout)
_, cfg = load(["--hosted", "--http", "127.0.0.1:0"] + sys.argv[1:])
try:
    from vordium_mcp.limits import limits_from_env
    chain = make_chain(cfg, fetch=stub, limits=limits_from_env(os.environ))
except ImportError:
    chain = make_chain(cfg, fetch=stub)
serve_http(cfg, chain=chain, ready=lambda h: (print(h.server_address[1], flush=True)))
"""


def post(url, body, headers, timeout=30):
    data = json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method="POST",
                               headers={"Content-Type": "application/json", **headers})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), time.monotonic() - t0
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, {}, b"", time.monotonic() - t0


def init(url, address):
    s, h, _, _ = post(url, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}, {"CF-Connecting-IP": address})
    return h.get("Mcp-Session-Id") if s == 200 else None


class Counter:
    def __init__(self):
        self.n, self.lock = 0, threading.Lock()

    def next(self):
        with self.lock:
            self.n += 1
            return self.n


def call(i):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": "get_balance", "arguments": {"address": "0x" + f"{i:040x}"}}}


def run(url, pid, seconds, workers, flood, accounts):
    stop = time.monotonic() + seconds
    flood_codes = collections.Counter()
    quiet = []
    lock = threading.Lock()
    threads_max = [0]

    def flooder(w):
        address, sid = flood(w)
        h = {"CF-Connecting-IP": address}
        while time.monotonic() < stop:
            hh = dict(h)
            s = sid() if callable(sid) else sid
            if s:
                hh["Mcp-Session-Id"] = s
            code = post(url, call(accounts.next()), hh)[0]
            with lock:
                flood_codes[code] += 1

    def quiet_client():
        sid = init(url, "198.51.100.20")
        while time.monotonic() < stop:
            s, _, b, took = post(url, call(10 ** 9 + accounts.next()), {"CF-Connecting-IP": "198.51.100.20",
                                                                          "Mcp-Session-Id": sid or ""})
            ok = s == 200 and json.loads(b).get("result", {}).get("isError") is False
            quiet.append((ok, s, took))
            time.sleep(0.1)

    def watch():
        while time.monotonic() < stop:
            try:
                with open(f"/proc/{pid}/status") as f:
                    n = int(next(l for l in f if l.startswith("Threads:")).split()[1])
                threads_max[0] = max(threads_max[0], n)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05)

    ts = [threading.Thread(target=flooder, args=(w,)) for w in range(workers if flood else 0)]
    ts += [threading.Thread(target=quiet_client), threading.Thread(target=watch)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(seconds + 40)
    lat = sorted(t for _, _, t in quiet)
    pick = lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))] * 1000) if lat else None   # noqa: E731
    return {"second_client": {"calls": len(quiet), "ok": sum(1 for ok, _, _ in quiet if ok),
                              "codes": dict(collections.Counter(str(s) for _, s, _ in quiet)),
                              "p50_ms": pick(0.5), "p95_ms": pick(0.95), "max_ms": round(lat[-1] * 1000) if lat else None},
            "flood": {str(k): v for k, v in sorted(flood_codes.items(), key=lambda kv: str(kv[0]))},
            "server_threads_max": threads_max[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--delay", type=float, default=0.25)
    ap.add_argument("--tasks-max", type=int, default=0)
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    accounts = Counter()
    scenarios = [
        ("alone", [], None),
        ("one address, own sessions", [], lambda w: ("192.0.2.10", init_cache("192.0.2.10", w))),
        ("one IPv6 /64, a new address each", [], lambda w: (f"2001:db8:dead:beef::{w + 1:x}", init_cache(f"2001:db8:dead:beef::{w + 1:x}", w))),
        ("one address, made-up session ids", [], lambda w: ("192.0.2.11", lambda: os.urandom(16).hex())),
        ("one address, own sessions, rate limits lifted", ["--rate-per-minute", "1000000", "--burst", "1000000"],
         lambda w: ("192.0.2.12", init_cache("192.0.2.12", w))),
    ]
    global URL
    for name, flags, flood in scenarios:
        if a.only and a.only not in name:
            continue
        env = {**os.environ, "LOAD_DELAY": str(a.delay), "LOAD_TASKS_MAX": str(a.tasks_max)}
        p = subprocess.Popen([sys.executable, "-c", CHILD % {"root": os.path.abspath(a.root), "here": HERE}] + flags,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env, text=True)
        try:
            port = int(p.stdout.readline())
            URL = f"http://127.0.0.1:{port}/mcp"
            SESS.clear()
            out = run(URL, p.pid, a.seconds, a.workers, flood, accounts)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as r:
                c = json.loads(r.read())["counters"]
            out["server_counters"] = {k: c.get(k) for k in ("rate_limited", "busy", "unknown_session", "time_budget", "errors")}
            print(json.dumps({"scenario": name, "tree": os.path.basename(os.path.abspath(a.root)),
                              "tasks_max": a.tasks_max, "workers": a.workers if flood else 0, "delay_s": a.delay,
                              "seconds": a.seconds, **out}), flush=True)
        finally:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


URL = None
SESS = {}


def init_cache(address, w):
    """One session per flooding worker, opened before the flood starts."""
    if (address, w) not in SESS:
        SESS[(address, w)] = init(URL, address)
    return SESS[(address, w)]


if __name__ == "__main__":
    main()
