"""The trade extra never allows an SDK older than the first release with the signing and reading rules the server
relies on (no redirect following, ASCII-only figures)."""
import os
import re

FLOOR = (1, 6, 1)


def test_the_trade_extra_requires_the_safe_sdk():
    text = open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), encoding="utf-8").read()
    m = re.search(r'trade\s*=\s*\[[^\]]*"vordium>=(\d+)\.(\d+)\.(\d+)"', text)
    assert m, "the trade extra names no vordium floor"
    assert tuple(int(x) for x in m.groups()) >= FLOOR
