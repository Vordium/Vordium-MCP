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
