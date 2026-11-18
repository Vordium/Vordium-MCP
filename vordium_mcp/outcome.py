"""What a write whose answer was lost did, read from the node only.

A write the chain answered is judged by its answer. A write whose answer was lost (a timeout, a dropped connection, a
gateway error, an answer that could not be read) may or may not have reached the chain: it is never "accepted" and never
"refused" until a read shows which. The reads that decide are the node's own: the account's resting orders (committed
state) first, then the node's account and agent records for a refusal or for filled-versus-cancelled. The explorer and the
trades list are never read for an outcome (another order's fill could be taken for this one).

States: ``pending_checking`` (still being checked), ``applied``, ``applied_other`` (applied with another result: filled
before the cancel, some filled first), ``refused`` (a record names the refusal) and ``still_unknown`` (nothing proven
after the limit; it may still apply). An order this server signs carries no deadline, so it is never called
"not applied".

The words come from the shared word file: the SDK's packaged copy when the SDK is installed, else this package's own
copy (the read-only server runs without the SDK). Both are byte copies of the same file, checked for drift.
"""
import importlib.util, json, os, re, time

ACTIONS = ("order", "cancel", "cancel_all")
FINAL = ("applied", "applied_other", "refused")
CHECK_S = 20.0           # one call waits at most this long (a hosted call is cut at 25 s)
RECORDS_LIMIT = 200      # records read per check: a busy account writes many, and they carry no client-nonce filter
NONCE_CODES = ("nonce", "nonce_used", "nonce_window", "duplicate", "duplicate_nonce")   # about a copy, never the order
_NEEDED = {"order": ("pending_checking", "applied", "refused", "still_unknown"),
           "cancel": ("pending_checking", "applied", "applied_other", "refused", "still_unknown"),
           "cancel_all": ("pending_checking", "applied", "applied_other", "refused", "still_unknown")}
_WORDS = None


def _valid(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("limit_s"), int) or not isinstance(doc.get("actions"), dict):
        return False
    for action, states in _NEEDED.items():
        a = doc["actions"].get(action)
        if not isinstance(a, dict):
            return False
        for s in states:
            w = a.get(s)
            if not (isinstance(w, dict) and isinstance(w.get("text"), str) and isinstance(w.get("next"), str)):
                return False
    return isinstance(doc.get("resend_while_pending"), dict)


def _sdk_copy():
    """the SDK's packaged copy, found without importing the SDK (None when it is not installed or has no copy)"""
    try:
        spec = importlib.util.find_spec("vordium")
    except (ImportError, ValueError):
        return None
    for d in (spec.submodule_search_locations or []) if spec else []:
        p = os.path.join(d, "outcome_words.json")
        if os.path.isfile(p):
            return p
    return None


def own_copy():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "outcome_words.json")


def load_words(paths=None):
    """The word file: the first of ``paths`` (default: the SDK's copy, then this package's) that reads and carries every
    sentence this server uses. Raises RuntimeError when none does: a write is then reported without invented words."""
    for p in paths if paths is not None else (_sdk_copy(), own_copy()):
        if not p:
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if _valid(doc):
            return doc
    raise RuntimeError("the outcome word file could not be read")


def words():
    global _WORDS
    if _WORDS is None:
        _WORDS = load_words()
    return _WORDS


_PLACE = re.compile(r"\{([a-z_]+)\}")


def fill(template, values):
    """``template`` with its {placeholders} filled from ``values``. A sentence whose value is missing is left out whole:
    a placeholder is never shown, and no value is ever made up."""
    out = []
    for sentence in re.split(r"(?<=\.)\s+", template or ""):
        names = _PLACE.findall(sentence)
        if any(values.get(n) in (None, "") for n in names):
            continue
        out.append(_PLACE.sub(lambda m: str(values[m.group(1)]), sentence))
    return " ".join(out).strip()


def phrase(_action, _state, **values):
    """(text, next) for an action in a state, from the word file"""
    w = words()["actions"][_action][_state]
    return fill(w["text"], values), fill(w["next"], values)


def limit_s():
    return int(words()["limit_s"])


# ── reads (the node only) ────────────────────────────────────────────────────
def _rows(doc, key="orders"):
    if isinstance(doc, list):
        return doc
    return (doc or {}).get(key) or [] if isinstance(doc, dict) else []


def _get(chain, path):
    """one node read: (json, route) or (None, route) when it failed. A failed read decides nothing."""
    try:
        r = chain.read(path, cache=False)
    except Exception:          # unreachable, or this call's own time is up: not shown yet, never a final
        return None, path
    return (r["json"] if r.ok else None), path


class Reads:
    """The reads of one check, each at most once."""

    def __init__(self, chain, owner, agent=None):
        self.chain, self.owner, self.agent = chain, owner, agent
        self._done = {}

    def _once(self, name, path):
        if name not in self._done:
            self._done[name] = (_get(self.chain, path), int(time.time() * 1000))
        return self._done[name]

    def orders(self):
        (doc, route), at = self._once("orders", f"/orders/{self.owner}")
        ok = isinstance(doc, list) or (isinstance(doc, dict) and isinstance(doc.get("orders"), list))
        return (_rows(doc) if ok else None), route, at

    def records(self):
        (doc, route), at = self._once("records", f"/events/account/{str(self.owner).lower()}?limit={RECORDS_LIMIT}")
        ev = doc.get("events") if isinstance(doc, dict) else None
        return (ev if isinstance(ev, list) else None), route, at

    def agent_records(self):
        if not self.agent:
            return None, None, None
        (doc, route), at = self._once("agent", f"/events/agent/{self.agent}")
        ev = doc.get("events") if isinstance(doc, dict) else None
        return (ev if isinstance(ev, list) else None), route, at

    def height(self):
        (doc, route), at = self._once("status", "/status")
        h = doc.get("chain_head") if isinstance(doc, dict) else None
        return h if isinstance(h, int) and not isinstance(h, bool) else None


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(str(v))
    except ValueError:
        return None


def _side(v):
    s = str(v).strip().lower() if v is not None else ""
    return 0 if s in ("0", "buy", "b", "long") else 1 if s in ("1", "sell", "s", "short") else None


def matches(rec, signed):
    """Whether a row or record carries what was signed (another signer of the account may use the same nonce): the
    market and side always where the row shows them, the size and limit price where it shows them."""
    if not signed:
        return True
    pair = _num(rec.get("pair_id", rec.get("pairId")))
    if pair is not None and signed.get("pair_id") is not None and pair != int(signed["pair_id"]):
        return False
    side, want = _side(rec.get("side")), _side(signed.get("side"))
    if side is not None and want is not None and side != want:
        return False
    size = next((_num(rec.get(k)) for k in ("original_size_18dec", "size_18dec") if _num(rec.get(k)) is not None), None)
    if size is not None and signed.get("size_18dec") is not None and size != int(signed["size_18dec"]):
        return False
    price = _num(rec.get("price_8dec"))
    if price and signed.get("price_8dec") and price != int(signed["price_8dec"]):
        return False
    return True


def _oid(r):
    return str(r.get("order_id", r.get("orderId", r.get("id"))))


def _read(route, height, at_ms):
    return {"source": "node", "route": route, "height": height, "at_ms": at_ms}


def _closed(records, order_id):
    """how a gone order closed, from its records: 'cancelled', 'filled' or None, with the record that says so. A
    cancel record wins over a fill record (a partly filled order that was then cancelled was cancelled); a fill alone
    counts only as the order's close ('order_closed' filled), or as a fill when no close record is listed."""
    cancelled = filled = fill = None
    for e in records or []:
        if not isinstance(e, dict) or _oid(e) != str(order_id):
            continue
        k, st = str(e.get("kind") or "").lower(), str(e.get("status") or "").lower()
        if k == "order_cancelled" or (k == "order_closed" and st == "cancelled"):
            cancelled = cancelled or e
        elif k == "order_filled" or (k == "order_closed" and st == "filled"):
            filled = filled or e
        elif k == "fill":
            fill = fill or e
    if cancelled is not None:
        return "cancelled", cancelled
    if filled is not None or fill is not None:
        return "filled", filled or fill
    return None, None


# ── judges: one look at the reads; None = not shown yet ──────────────────────
def judge_order(reads, client_nonce, signed=None):
    want = str(int(client_nonce))
    rows, route, at = reads.orders()
    for o in rows or []:
        if isinstance(o, dict) and str(o.get("client_nonce")) == want and matches(o, signed):
            orig, left = _num(o.get("original_size_18dec")), _num(o.get("remaining_18dec"))
            how = "Partly filled" if orig and left is not None and left < orig else "Placed"
            h = _num(o.get("height")) or _num(o.get("placed_height")) or reads.height()
            return {"state": "applied", "values": {"order_words": how, "height": h}, "order_id": _num(o.get("id", o.get("order_id"))),
                    "read": _read(route, h, at)}
    recs, rroute, rat = reads.records()
    mine = [e for e in recs or [] if isinstance(e, dict) and str(e.get("client_nonce")) == want and matches(e, signed)]
    # a record that the order applied wins over a refusal: a resent copy of an order that applied is refused with the
    # nonce code, and that refusal is about the copy, not the order
    placed = next((e for e in mine if e.get("kind") in ("order_placed", "order_closed", "fill", "order_filled")), None)
    refused = next((e for e in mine if e.get("kind") == "order_refused"
                    and str(e.get("code") or "").lower() not in NONCE_CODES), None)
    if placed is None and refused is not None:
        return {"state": "refused", "code": str(refused.get("code") or "refused"), "codes": "order",
                "read": _read(rroute, _num(refused.get("height")), rat)}
    if placed is not None:
        oid = _num(placed.get("order_id"))
        how, rec = _closed(recs, oid) if oid is not None else (None, None)
        h = _num((rec or placed).get("height"))
        return {"state": "applied", "values": {"order_words": "Filled" if how == "filled" else "Placed", "height": h},
                "order_id": oid, "read": _read(rroute, h, rat)}
    arecs, aroute, aat = reads.agent_records()
    a = next((e for e in arecs or [] if isinstance(e, dict) and e.get("kind") == "agent_refused"
              and str(e.get("client_nonce")) == want and str(e.get("reason") or "").lower() not in NONCE_CODES), None)
    if a is not None:
        return {"state": "refused", "code": str(a.get("reason") or "refused"), "codes": "agent",
                "read": _read(aroute, _num(a.get("height")), aat)}
    return None


def judge_cancel(reads, order_id):
    rows, route, at = reads.orders()
    if rows is None:
        return {"state": None, "resting": None, "read": None}            # unreadable: decides nothing
    if any(isinstance(o, dict) and _oid(o) == str(order_id) for o in rows):
        return {"state": None, "resting": True, "read": _read(route, None, at)}
    recs, rroute, rat = reads.records()
    how, rec = _closed(recs, order_id)
    if how is None:                                     # gone, but no record yet says how: keep checking
        return {"state": None, "resting": False, "read": _read(route, None, at)}
    h = _num(rec.get("height"))
    return {"state": "applied" if how == "cancelled" else "applied_other", "values": {"height": h},
            "read": _read(rroute, h, rat), "order_id": int(order_id)}


def own_order_ids(rows, pair_id=None):
    """the account's own resting order ids (in one market, or all when pair_id is None or 0); the network's liquidation
    order is not the account's to cancel"""
    out = []
    for o in rows or []:
        if not isinstance(o, dict) or o.get("liquidation") is True or o.get("is_liquidation") is True:
            continue
        if pair_id and _num(o.get("pair_id")) != int(pair_id):
            continue
        i = _num(o.get("id", o.get("order_id")))
        if i is not None:
            out.append(i)
    return out


def judge_cancel_all(reads, before, pair_id=None):
    """``before``: the order ids listed before the send (None: not read). Applied when none of them is still listed and
    the records say how each one closed (cancelled, or filled first). Without the before-set nothing is final: an empty
    book alone does not prove the cancel-all applied."""
    rows, route, at = reads.orders()
    if rows is None:
        return None
    listed = {str(i) for i in own_order_ids(rows, pair_id)}
    ids = [str(i) for i in before] if before is not None else None
    still = sorted(listed & set(ids), key=int) if ids is not None else sorted(listed, key=int)
    if still or ids is None:
        return {"state": None, "still": still, "read": _read(route, None, at)}
    recs, _, _ = reads.records()
    how = [_closed(recs, i)[0] for i in ids]
    if any(x is None for x in how):          # gone, but a record does not say how yet: filled first is still possible
        return {"state": None, "still": [], "read": _read(route, None, at)}
    m, k = len(ids), how.count("filled")
    h = reads.height()
    return {"state": "applied" if k == 0 else "applied_other", "values": {"n": m - k, "m": m, "k": k, "height": h},
            "read": _read(route, h, at), "still": []}


def interval(elapsed):
    """the poll plan: every 1 s for the first 10 s, every 2 s to 30 s, every 5 s after"""
    return 1.0 if elapsed < 10 else 2.0 if elapsed < 30 else 5.0


def check(judge, until, sleep=None, clock=None, left=None):
    """Run ``judge(Reads)`` until it gives a final or ``until`` (a monotonic time) passes; at least one look. ``left``
    (seconds this call may still use) stops it early. Returns the last verdict (possibly {'state': None, ...}) or None."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    start, v = clock(), None
    while True:
        v = judge()
        if v is not None and v.get("state") in FINAL:
            return v
        wait = interval(clock() - start)
        room = until - clock()
        if left is not None:
            room = min(room, left() - 2.0)      # keep time to answer
        if room < wait:
            return v
        sleep(wait)


# ── the result fields ────────────────────────────────────────────────────────
def meanings(chain, codes):
    """the chain's own meaning and action for a refusal code ({code: (meaning, action)}), read from /orders/codes"""
    doc, _ = _get(chain, "/orders/codes")
    key, name = ("agent_refusal_keys", "key") if codes == "agent" else ("order_refusal_codes", "code")
    out = {}
    for x in (doc or {}).get(key) or [] if isinstance(doc, dict) else []:
        if isinstance(x, dict) and isinstance(x.get(name), str):
            out[x[name]] = (x.get("meaning"), x.get("action"))
    return out


def _bare(s):
    return s.rstrip(" .") if isinstance(s, str) else None


def result(chain, action, verdict, follow=None, since_ms=None, now_ms=None):
    """state, text, next, resend, read and (while still checking) the follow-up call, for ``action``"""
    from .text import clean
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    state = (verdict or {}).get("state")
    values = dict((verdict or {}).get("values") or {})
    meaning = None
    if state == "refused":
        mean = ((verdict.get("meaning"), verdict.get("action")) if verdict.get("meaning")
                else meanings(chain, verdict.get("codes")).get(verdict.get("code"), (None, None)))
        meaning = clean(mean[0]) or None
        values.update(meaning=_bare(meaning) or verdict.get("code"), action=clean(mean[1]) or "")
    if state not in FINAL:
        aged = since_ms is not None and now_ms - int(since_ms) >= limit_s() * 1000
        state = "still_unknown" if aged else "pending_checking"
        still = (verdict or {}).get("still")
        values.setdefault("n", len(still) if still is not None else None)
    text, nxt = phrase(action, state, **values)
    if action == "cancel" and state == "still_unknown" and (verdict or {}).get("resting") is not True:
        # "still on the book" holds only when the last read showed it there: else only the first sentence is true
        text, nxt = re.split(r"(?<=\.)\s+", text)[0], ""
    label = words()["resend_while_pending"].get(action)
    if state in FINAL:
        resend = {"allowed": state == "refused" and action == "order", "same_bytes": False, "label": None}
    elif action == "cancel":                     # a cancel carries no nonce: signing it again gives the same request
        resting = (verdict or {}).get("resting") is not False
        resend = {"allowed": resting, "same_bytes": True, "label": label if resting else None}
    else:                                        # an order never again while it may apply; cancel all: not until the
        resend = {"allowed": False, "same_bytes": False, "label": label}   # chain says it applies once
    own = " ".join(x for x in (text, nxt) if x)
    if state == "refused":                       # the server's own sentence names the code only: the meaning is the
        code = verdict.get("code")               # network's text and stays in its own fields (text, refusal_meaning)
        safe = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,48}", code) else "refused"
        own = phrase(action, state, meaning=safe)[0]
    out = {"state": state, "text": text, "next": nxt, "resend": resend,
           "read": (verdict or {}).get("read"), "own_sentence": own}
    if state in ("pending_checking", "still_unknown") and follow is not None:
        out["follow_up"] = {"tool": "check_outcome", "arguments": follow}
    if verdict and verdict.get("order_id") is not None and action == "order":
        out["order_id"] = verdict["order_id"]
    if verdict and verdict.get("still"):
        out["still_open"] = [int(i) for i in verdict["still"] if str(i).isdigit()]
    if state == "refused":
        out["refusal_reason"] = verdict.get("code")
        if meaning:
            out["refusal_meaning"] = meaning
    return out
