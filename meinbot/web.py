"""Web search and page fetching, run locally so every model vendor gets them.

Search uses DuckDuckGo via the `ddgs` package (no API key). Pages are fetched
with httpx and reduced to readable text with trafilatura. When that yields
next to nothing (JavaScript apps) or the site refuses plain HTTP clients, the
page is rendered in headless Chromium (Playwright) instead.

Requests to private, loopback and link-local addresses are refused at every
redirect hop, and in the browser for every request the page makes, so the
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
        if "no results" in str(e).lower():
            return []
        raise WebError(f"search failed: {e}")
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in rows]


_public_cache: dict[str, bool] = {}


async def _is_public_host(host: str, port: int) -> bool:
    if host not in _public_cache:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            _public_cache[host] = bool(infos) and all(
                ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)
        except (socket.gaierror, ValueError):
            _public_cache[host] = False
    return _public_cache[host]


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


# ---- headless browser -------------------------------------------------------

_browser = None
_browser_lock = asyncio.Lock()
RENDER_MIN_CHARS = 400        # less readable text than this from plain HTTP -> render


async def _get_browser():
    global _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(headless=True)
    return _browser


async def render(url: str) -> tuple[str, str]:
    """(final url, readable text) of a page after running its JavaScript."""
    await _check_public(url)
    browser = await _get_browser()
    ctx = await browser.new_context(user_agent=USER_AGENT, java_script_enabled=True,
                                    accept_downloads=False, service_workers="block")
    blocked: list[str] = []

    async def guard(route):
        req = route.request
        p = urlparse(req.url)
        if p.scheme in ("data", "blob"):
            return await route.continue_()
        if p.scheme not in ("http", "https") or not await _is_public_host(
                p.hostname or "", p.port or (443 if p.scheme == "https" else 80)):
            blocked.append(req.url)
            return await route.abort()
        if req.resource_type in ("image", "media", "font"):
            return await route.abort()
        await route.continue_()

    def watch(req):            # redirect hops bypass route(); refuse the result if any went private
        p = urlparse(req.url)
        if p.scheme in ("http", "https") and not _public_cache.get(p.hostname or "", True):
            blocked.append(req.url)

    try:
        await ctx.route("**/*", guard)
        page = await ctx.new_page()
        page.on("request", watch)
        try:
            await page.goto(url, wait_until="networkidle", timeout=25000)
        except Exception:
            await page.wait_for_timeout(2000)      # slow or chatty page: take what has rendered
        final = page.url
        if not await _is_public_host(urlparse(final).hostname or "", 443):
            raise WebError("page redirected to a non-public address")
        html = await page.content()
        text = await asyncio.to_thread(readable, html, final)
        visible = await page.inner_text("body")
        # Article extraction can drop most of an app-like page (tabs, cards);
        # fall back to all visible text when it kept much less than that.
        if len(text) < RENDER_MIN_CHARS or len(visible) > 2 * len(text):
            text = visible
        if any(urlparse(b).hostname == urlparse(final).hostname for b in blocked):
            raise WebError("page tried to reach a non-public address")
        # App pages often keep their content behind links; list them so the model can follow.
        links = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => [e.innerText.trim().replace(/\\s+/g, ' ').slice(0, 60), e.href])")
        seen, lines = set(), []
        for label, href in links:
            if href.startswith("http") and href not in seen and href.rstrip("/") != final.rstrip("/"):
                seen.add(href)
                lines.append(f"- {label or '(no text)'}: {href}")
        if lines:
            text += "\n\nLinks on this page:\n" + "\n".join(lines[:80])
        return final, text
    finally:
        await ctx.close()


async def fetch_text(url: str) -> tuple[str, str]:
    """(final url, readable text) for HTML, plain text or JSON.

    Plain HTTP first; headless Chromium when the page is a JavaScript app,
    has almost no text, or blocks plain HTTP clients."""
    try:
        final, ctype, body = await fetch(url)
    except WebError as e:
        if str(e).startswith("HTTP 4"):
            return await render(url)
        raise
    if ctype in ("text/html", "application/xhtml+xml", ""):
        text = await asyncio.to_thread(readable, body, final)
        if len(text) < RENDER_MIN_CHARS:
            try:
                final, rendered = await render(final)
                if len(rendered) > len(text):
                    text = rendered
            except Exception as e:
                if not text:
                    raise WebError(f"rendering failed: {e}")
        return final, text or "(no readable text found on the page)"
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/rss+xml"):
        return final, body
    raise WebError(f"unsupported content type {ctype}")
