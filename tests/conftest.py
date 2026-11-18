"""Every test keeps the SDK's nonce marks in its own folder and reads no block time."""
import pytest


@pytest.fixture(autouse=True)
def _nonce_state(monkeypatch, tmp_path):
    try:
        from vordium import nonces
    except Exception:          # the trade extra is not installed: nothing signs
        yield
        return
    monkeypatch.setenv(nonces.STATE_ENV, str(tmp_path / "nonce-state"))
    monkeypatch.setattr(nonces, "chain_clock_for", lambda rest_url, network=None: None)
    nonces._ALLOCATORS.clear()
    yield
    nonces._ALLOCATORS.clear()


@pytest.fixture(autouse=True)
def _no_read_back_wait(monkeypatch):
    """A lost answer is read back once, not for 20 s (tests that judge the wait set their own clock)."""
    from vordium_mcp import outcome, tools
    monkeypatch.setattr(tools, "SETTLE_S", 0.0)
    monkeypatch.setattr(outcome, "CHECK_S", 0.0)
    try:                                       # an SDK that reads a lost answer back itself: once, here too
        from vordium import dex, outcome as sdk_outcome
        monkeypatch.setattr(sdk_outcome, "SETTLE_S", 0.0)
        monkeypatch.setattr(dex, "SETTLE_DEFAULT", 0.0, raising=False)
    except Exception:
        pass
    yield
