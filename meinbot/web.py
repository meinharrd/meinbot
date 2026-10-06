"""Web search and page fetching, run locally so every model vendor gets them.

Search uses DuckDuckGo via the `ddgs` package (no API key). Pages are fetched
with httpx and reduced to readable text with trafilatura. Requests to private,
loopback and link-local addresses are refused at every redirect hop, so the
model cannot use the bot to reach services on this machine or its network.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
MAX_BYTES = 5_000_000
MAX_REDIRECTS = 5


class WebError(Exception):
    pass


async def search(query: str, limit: int = 8) -> list[dict]:
    """[{title, url, snippet}]"""
    from ddgs import DDGS

    def run():
        return DDGS().text(query, max_results=limit)
    try:
        rows = await asyncio.to_thread(run)
    except Exception as e:
        raise WebError(f"search failed: {e}")
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in rows]


async def _check_public(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise WebError("only http(s) URLs are allowed")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebError(f"cannot resolve {p.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise WebError(f"{p.hostname} resolves to a non-public address")


async def fetch(url: str) -> tuple[str, str, str]:
    """(final url, content type, body text) with redirects checked hop by hop."""
    async with httpx.AsyncClient(timeout=20, http2=True, headers={"User-Agent": USER_AGENT},
                                 follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_public(url)
            async with client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise WebError(f"HTTP {r.status_code}")
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                text = body.decode(r.encoding or "utf-8", errors="replace")
                return str(r.url), ctype, text
        raise WebError("too many redirects")


def readable(html: str, url: str) -> str:
    """Main text of an HTML page as Markdown-ish text."""
    import trafilatura
    text = trafilatura.extract(html, url=url, output_format="markdown", include_links=True,
                               include_tables=True, favor_recall=True)
    return text or ""


async def fetch_text(url: str) -> tuple[str, str]:
    """(final url, readable text) for HTML, plain text or JSON."""
    final, ctype, body = await fetch(url)
    if ctype in ("text/html", "application/xhtml+xml", ""):
        text = await asyncio.to_thread(readable, body, final)
        return final, text or "(no readable text found on the page)"
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/rss+xml"):
        return final, body
    raise WebError(f"unsupported content type {ctype}")
