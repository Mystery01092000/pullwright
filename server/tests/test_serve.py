from pathlib import Path

from fastapi.testclient import TestClient

from pullwright import serve
from pullwright.store import Store


def test_control_plane_routes(monkeypatch, tmp_path: Path):
    """The assembled app: login at the root, the JSON API under /api (as the web UI calls it), the UI at /."""
    for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "PULLWRIGHT_TEAMS_APP_ID", "GITHUB_APP_ID", "GITHUB_OAUTH_CLIENT_ID", "PULLWRIGHT_REPOS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PULLWRIGHT_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("PULLWRIGHT_WORKDIR", str(tmp_path / "work"))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>pullwright ui</html>")
    monkeypatch.setenv("PULLWRIGHT_WEB_DIST", str(dist))

    root, parts = serve.build(Store())
    assert sorted(parts["intake"].channels) == ["web"] and "dispatcher" not in parts and "slack" not in parts
    c = TestClient(root)
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/runs").status_code == 200
    assert c.get("/auth/github/login", follow_redirects=False).status_code == 503  # OAuth not configured
    assert c.post("/api/teams/messages", json={}).status_code == 404  # Teams not configured
    assert "pullwright ui" in c.get("/").text
