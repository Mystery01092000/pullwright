import httpx
import pytest

from pullwright import links


def test_extract_dedupes_trims_and_caps():
    text = ("See https://github.com/acme/app/issues/12, and https://docs.example.com/api#limits. "
            "Again https://github.com/acme/app/issues/12 " + " ".join(f"https://e.com/{i}" for i in range(10)))
    got = links.extract(text)
    assert got[:2] == ["https://github.com/acme/app/issues/12", "https://docs.example.com/api#limits"]
    assert len(got) == links.MAX_LINKS


def test_html_to_text_drops_scripts_and_tags():
    raw = "<html><head><title>x</title></head><body><script>alert(1)</script><h1>Rate limits</h1><p>5 per minute&nbsp;max</p></body></html>"
    assert links.html_to_text(raw) == "Rate limits\n5 per minute\xa0max"


def test_only_public_https_is_fetched(monkeypatch):
    with pytest.raises(ValueError, match="https"):
        links.fetch_page("http://example.com/")
    with pytest.raises(ValueError, match="public"):
        links.fetch_page("https://localhost/admin")
    with pytest.raises(ValueError, match="public"):
        links.fetch_page("https://169.254.169.254/latest/meta-data")  # cloud metadata


def test_redirects_are_checked_again(monkeypatch):
    monkeypatch.setattr(links, "_public", lambda host: host != "internal.example")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "docs.example.com":
            return httpx.Response(302, headers={"location": "https://internal.example/secret"})
        return httpx.Response(200, text="secret")

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(ValueError, match="public"):
        links.fetch_page("https://docs.example.com/page", http)


def test_hydrate_reads_github_items_and_pages_and_reports_failures(monkeypatch):
    monkeypatch.setattr(links, "_public", lambda host: True)

    class GH:
        def get_issue(self, repo, number):
            return {"title": "Login is slow", "state": "open", "body": "p95 is 4s", "pull_request": None}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/missing":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>Use a token bucket.</p>" + "x" * 9000)

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    out = links.hydrate("Fix https://github.com/acme/app/issues/7 like https://blog.example.com/rl (also https://blog.example.com/missing)", GH(), http)
    assert "Issue acme/app#7 (open): Login is slow\n\np95 is 4s" in out
    assert "Use a token bucket." in out and "[... cut]" in out
    assert "--- https://blog.example.com/missing\n(could not read it: HTTP 404)" in out
    assert links.hydrate("no links here", GH(), http) == ""
