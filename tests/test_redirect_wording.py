"""Every read answered with a redirect is refused with the same sentence, whichever path reads it."""
import pytest

from vordium_mcp import reads
from vordium_mcp.chain import ChainUnreachable


class _Read(dict):
    @property
    def ok(self):
        return 200 <= self["status"] < 300


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_doc_names_the_redirect(status):
    r = _Read(status=status, url="http://example.invalid/x", json=None, text="")
    with pytest.raises(ChainUnreachable) as e:
        reads._doc(r, "candles")
    assert "a redirect, not followed: use the node's https address" in str(e.value)


def test_doc_plain_error_has_no_redirect_words():
    r = _Read(status=503, url="https://example.invalid/x", json=None, text="")
    with pytest.raises(ChainUnreachable) as e:
        reads._doc(r, "candles")
    assert "redirect" not in str(e.value)


def test_one_helper_for_every_failed_read():
    """chain.answered is the one sentence; the agent view and the order pre-check use it too."""
    import inspect
    from vordium_mcp import chain, tools
    r = _Read(status=302, url="http://example.invalid/status", json=None, text="")
    assert chain.answered(r).endswith("(a redirect, not followed: use the node's https address)")
    src = inspect.getsource(tools)
    assert "answered {st['status']}" not in src and "answered {r['status']}" not in src
    assert src.count("answered(st)") >= 1 and src.count("answered(r)") >= 1
