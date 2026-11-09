"""The tool list, from the package itself — never written by hand.

  python -m vordium_mcp.toolsdoc lock       the lock (tools.lock.json): every tool's name, title, description, input and
                                             output schema and annotations, as tools/list serves them
  python -m vordium_mcp.toolsdoc markdown   the table the README (and so the PyPI page) and the docs show
  python -m vordium_mcp.toolsdoc check      exit 1 when tools.lock.json differs from the package (CI: an unplanned change
                                             to a tool or a field fails; a planned one updates the lock in the same commit)
"""
import json, os, sys

from . import __version__
from .tools import TOOLS

LOCK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools.lock.json")
KEYS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")


def lock():
    return {"package": "vordium-mcp", "version": __version__,
            "tools": [{k: t[k] for k in KEYS} | {"where": where(t)} for t in TOOLS]}


def where(t):
    if not t.get("write"):
        return "everywhere"                                       # local and the hosted read-only server
    return "local, signs" if t["annotations"].get("destructiveHint") else "local"


def markdown():
    rows = ["| Tool | What it does | Where |", "|---|---|---|"]
    for t in TOOLS:
        what = t["description"].split(" Ask:")[0].split(". Signed on this machine")[0].replace("|", "\\|").rstrip(".") + "."
        rows.append(f"| `{t['name']}` | {what} | {where(t)} |")
    return "\n".join(rows) + "\n"


def _changes(a, b):
    """plain words for what differs between two locks (tool added/removed, field changed)"""
    out, an, bn = [], {t["name"]: t for t in a.get("tools", [])}, {t["name"]: t for t in b.get("tools", [])}
    out += [f"tool added: {n}" for n in bn if n not in an] + [f"tool removed: {n}" for n in an if n not in bn]
    for n in an.keys() & bn.keys():
        out += [f"{n}: {k} changed" for k in KEYS + ("where",) if an[n].get(k) != bn[n].get(k)]
    return out


def main(argv):
    cmd = argv[0] if argv else "check"
    if cmd == "markdown":
        sys.stdout.write(markdown())
        return 0
    if cmd == "lock":
        with open(LOCK, "w", encoding="utf-8") as fh:
            json.dump(lock(), fh, indent=1, sort_keys=False)
            fh.write("\n")
        print(f"wrote {LOCK}")
        return 0
    if cmd == "check":
        try:
            with open(LOCK, encoding="utf-8") as fh:
                held = json.load(fh)
        except OSError:
            print("tools.lock.json is missing: python -m vordium_mcp.toolsdoc lock", file=sys.stderr)
            return 1
        now = lock()
        diff = _changes({**held, "tools": held.get("tools", [])}, now)
        if diff:
            print("the tools differ from tools.lock.json (an unplanned change, or the lock was not updated):\n  "
                  + "\n  ".join(sorted(diff)) + "\nIf planned: python -m vordium_mcp.toolsdoc lock, and note it in CHANGELOG.md.",
                  file=sys.stderr)
            return 1
        print(f"ok: {len(now['tools'])} tools match tools.lock.json")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
