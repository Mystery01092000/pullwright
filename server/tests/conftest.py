import pytest


@pytest.fixture(autouse=True)
def _native_coder(monkeypatch):
    """Tests that run whole jobs use Pullwright's own coder with the scripted fake model, never a real goose or
    Claude Code binary. Tests about coder choice set the variables themselves."""
    monkeypatch.setenv("PULLWRIGHT_CODER", "native")
    monkeypatch.delenv("PULLWRIGHT_CODER_BY_REPO", raising=False)
