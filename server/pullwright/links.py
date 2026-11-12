"""Links in a request, fetched before any agent reads it, the way Stripe's Minions run tools over a request's links
before the agent starts.

GitHub issue and pull request links are read through the GitHub client, so private repositories the App can reach
work too. Other https links are fetched as text, with guards because the control plane does the fetching:
- https only, at most 5 links per request, 10 s, 1 MB, 3 redirects;
- every host, including each redirect target, must resolve to public addresses only (no loopback, private,
  link-local or cloud metadata addresses);
- HTML is reduced to text, and each page is cut to its first few thousand characters.

A link that can't be read is listed with the reason; it never fails the request.
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
GITHUB_ITEM = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)")
MAX_LINKS = 5
MAX_BYTES = 1_000_000
PER_LINK_CHARS = 4000


def extract(text: str) -> list[str]:
    seen: list[str] = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?")
        if url not in seen:
            seen.append(url)
    return seen[:MAX_LINKS]


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = "\n".join(line.strip() for line in re.sub(r"[ \t\r\f\v]+", " ", text).splitlines())
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def fetch_page(url: str, http: httpx.Client | None = None) -> str:
    """The text of one public https page. Raises ValueError with a short reason when it can't or may not be read."""
    client = http or httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": "pullwright-link-reader"})
    try:
        for _ in range(4):
            parts = urlparse(url)
            if parts.scheme != "https" or not parts.hostname:
                raise ValueError("only https links are read")
            if not _public(parts.hostname):
                raise ValueError("the host is not a public address")
            with client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ValueError(f"HTTP {r.status_code}")
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                kind = r.headers.get("content-type", "")
                if not any(t in kind for t in ("text/", "json", "xml")) and kind:
                    raise ValueError(f"not a text page ({kind.split(';')[0]})")
                text = body.decode(r.encoding or "utf-8", errors="replace")
                return html_to_text(text) if "html" in kind or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")) else text
        raise ValueError("too many redirects")
    except httpx.HTTPError as e:
        raise ValueError(type(e).__name__) from e
    finally:
        if http is None:
            client.close()


def _github_item(gh: Any, repo: str, number: int) -> str:
    item = gh.get_issue(repo, number)
    kind = "Pull request" if item.get("pull_request") else "Issue"
    return f"{kind} {repo}#{number} ({item.get('state', '')}): {item.get('title', '')}\n\n{item.get('body') or '(no description)'}"


def hydrate(text: str, gh: Any = None, http: httpx.Client | None = None) -> str:
    """A markdown section with what the request's links say, or '' when it has none."""
    urls = extract(text)
    if not urls:
        return ""
    out = ["Linked pages (fetched before planning, cut to the start of each):"]
    for url in urls:
        try:
            m = GITHUB_ITEM.match(url)
            body = _github_item(gh, m.group(1), int(m.group(2))) if (m and gh is not None) else fetch_page(url, http)
            body = body.strip()
            if len(body) > PER_LINK_CHARS:
                body = body[:PER_LINK_CHARS] + "\n[... cut]"
            out.append(f"--- {url}\n{body}")
        except Exception as e:  # a link that can't be read must not stop the request
            reason = str(e) if isinstance(e, ValueError) else type(e).__name__
            out.append(f"--- {url}\n(could not read it: {reason[:200]})")
    return "\n\n".join(out)
