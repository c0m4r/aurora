"""Web search (DuckDuckGo/Bing scraping) + whitelisted direct URL fetching."""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from .base import BaseTool, ToolDefinition
from ._http_guards import UnsafeURLError, safe_httpx_client

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Default whitelist — domains the model may fetch directly without a search query
DEFAULT_WHITELIST: list[str] = [
    # Linux / sysadmin docs
    "man7.org",
    "linux.die.net",
    "kernel.org",
    "archlinux.org",
    "wiki.archlinux.org",
    "debian.org",
    "ubuntu.com",
    "docs.fedoraproject.org",
    # Release pages
    "github.com",
    "gitlab.com",
    "pypi.org",
    "hub.docker.com",
    # General dev / tech docs
    "docs.python.org",
    "stackoverflow.com",
    "superuser.com",
    "serverfault.com",
    "askubuntu.com",
    "unix.stackexchange.com",
    # News
    "bbc.com",
    "cnn.com",
    "apnews.com",
    "reuters.com",
    "aljazeera.com",
    "news.google.com",
    "news.ycombinator.com",
    "rmf24.pl",
    "radiozet.pl",
    "wp.pl",
    "onet.pl",
    "pap.pl",
    # CVE / security
    "nvd.nist.gov",
    "cve.mitre.org",
    "security.archlinux.org",
    # Package info
    "repology.org",
    "pkgs.org",
]


def _extract_main_content(html: str) -> str:
    """Extract readable text from HTML, preferring trafilatura."""
    if not html:
        return ""
    try:
        import trafilatura
        text = trafilatura.extract(
            html,
            include_links=False,
            include_images=False,
            no_fallback=False,
            favor_recall=True,
        )
        if text:
            return text
    except ImportError:
        pass

    # BeautifulSoup fallback
    soup = BeautifulSoup(html, "lxml")
    for el in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        el.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


class WebSearchTool(BaseTool):
    def __init__(
        self,
        max_results: int = 5,
        fetch_content: bool = True,
        max_content_length: int = 4000,
        whitelist: list[str] | None = None,
    ):
        self.max_results = max_results
        self.fetch_content = fetch_content
        self.max_content_length = max_content_length
        self.whitelist: list[str] = (
            [d.lower().removeprefix("www.") for d in whitelist]
            if whitelist is not None
            else [d.lower().removeprefix("www.") for d in DEFAULT_WHITELIST]
        )

    def _is_whitelisted(self, url: str) -> bool:
        try:
            host = urlparse(url).netloc.lower().removeprefix("www.")
            return any(host == w or host.endswith("." + w) for w in self.whitelist)
        except Exception:
            return False

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web",
            description=(
                "Fetch a specific URL directly OR search the web.\n\n"
                "## IMPORTANT: When to use `url` vs `query`\n\n"
                "**Use `url` (direct fetch) when the user asks for:**\n"
                "- Latest/recent news → fetch `https://news.ycombinator.com` or `https://news.google.com`\n"
                "- Latest CVEs or security advisories → fetch `https://nvd.nist.gov/vuln/search` or `https://security.archlinux.org`\n"
                "- Latest release of a GitHub project → fetch the releases page, e.g. `https://github.com/owner/repo/releases`\n"
                "- Latest package version → fetch `https://pypi.org/pypi/<package>/json`\n"
                "- A specific man page → fetch e.g. `https://man7.org/linux/man-pages/man1/ls.1.html`\n"
                "- Arch Linux news → fetch `https://archlinux.org/news/`\n"
                "- CNN / AP / Reuters headlines → fetch e.g. `https://apnews.com` or `https://reuters.com`\n\n"
                "Direct fetch gives you the actual current page content — always prefer it over search "
                "when you know which URL to use.\n\n"
                "**Use `query` (web search) when:**\n"
                "- You don't know the exact URL\n"
                "- The topic is open-ended or requires multiple sources\n"
                "- The domain is not on the whitelist\n\n"
                "Whitelisted domains (for direct fetch): "
                + ", ".join(self.whitelist)
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "Direct URL to fetch. Must be on a whitelisted domain "
                            "(the operator configures the whitelist in config.yaml). "
                            "Do NOT provide `query` when using this."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Web search query. Only use when you don't have a specific URL. "
                            "Do NOT provide `url` when using this."
                        ),
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of search results to return (1–10, default 5). Only for `query` mode.",
                    },
                    "fetch_content": {
                        "type": "boolean",
                        "description": "Extract full page text from top results (default true). Only for `query` mode.",
                    },
                },
            },
        )

    async def execute(
        self,
        query: str | None = None,
        url: str | None = None,
        num_results: int | None = None,
        fetch_content: bool | None = None,
        **_,
    ) -> str:
        if url:
            return await self._fetch_url(url)
        if query:
            return await self._search(
                query,
                n=min(int(num_results or self.max_results), 10),
                want_content=fetch_content if fetch_content is not None else self.fetch_content,
            )
        return "Provide either `query` or `url`."

    # ── Direct URL fetch ──────────────────────────────────────────────────────

    async def _fetch_url(self, url: str) -> str:
        if not self._is_whitelisted(url):
            host = urlparse(url).netloc
            return (
                f"⚠️ Domain '{host}' is not on the whitelist.\n\n"
                "Direct fetching of non-whitelisted domains is disabled for safety. "
                "Either add this domain to tools.websearch.whitelist in config.yaml, "
                "or use the `query` parameter to search instead."
            )
        html = await self._get_html(url)
        if not html:
            return f"Failed to fetch {url}"
        content = _extract_main_content(html)
        if not content:
            return f"Could not extract readable content from {url}"
        if len(content) > self.max_content_length * 2:
            content = content[: self.max_content_length * 2] + "\n\n[…content truncated]"
        return f"Content from {url}:\n\n{content}"

    # ── Search ────────────────────────────────────────────────────────────────

    async def _search(self, query: str, n: int, want_content: bool) -> str:
        results = await self._ddg(query, n) or await self._bing(query, n)
        if not results:
            return f"No results found for: {query}"

        parts = [f"### Search: {query}\n"]
        for i, r in enumerate(results[:n], 1):
            parts.append(f"**{i}. {r['title']}**")
            parts.append(f"URL: {r['url']}")
            if r.get("snippet"):
                parts.append(r["snippet"])
            if want_content and i <= 3:
                content = await self._fetch_page_content(r["url"])
                if content:
                    parts.append(f"\n*Excerpt:*\n{content[:self.max_content_length]}")
            parts.append("")
        return "\n".join(parts)

    async def _ddg(self, query: str, n: int) -> list[dict]:
        try:
            async with safe_httpx_client(timeout=15.0, headers=_HEADERS) as c:
                resp = await c.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "us-en"})
                resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            results = []
            for el in soup.select(".result__body"):
                a = el.select_one(".result__a")
                sn = el.select_one(".result__snippet")
                if not a:
                    continue
                url = self._ddg_url(a.get("href", ""))
                if not url or not url.startswith("http"):
                    continue
                results.append({"title": a.get_text(strip=True), "url": url,
                                 "snippet": sn.get_text(strip=True) if sn else ""})
                if len(results) >= n:
                    break
            return results
        except Exception as exc:
            logger.debug("DDG search failed: %s", exc)
            return []

    async def _bing(self, query: str, n: int) -> list[dict]:
        try:
            async with safe_httpx_client(timeout=15.0, headers=_HEADERS) as c:
                resp = await c.get("https://www.bing.com/search", params={"q": query, "count": n})
                resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            results = []
            for li in soup.select("li.b_algo"):
                a = li.select_one("h2 a")
                sn = li.select_one(".b_caption p")
                if not a:
                    continue
                url = a.get("href", "")
                if not url.startswith("http"):
                    continue
                results.append({"title": a.get_text(strip=True), "url": url,
                                 "snippet": sn.get_text(strip=True) if sn else ""})
                if len(results) >= n:
                    break
            return results
        except Exception as exc:
            logger.debug("Bing search failed: %s", exc)
            return []

    async def _fetch_page_content(self, url: str) -> str:
        html = await self._get_html(url)
        return _extract_main_content(html)[:self.max_content_length] if html else ""

    async def _get_html(self, url: str) -> str:
        try:
            async with safe_httpx_client(timeout=12.0, headers=_HEADERS) as c:
                resp = await c.get(url)
                resp.raise_for_status()
                return resp.text
        except UnsafeURLError as exc:
            logger.info("blocked unsafe URL %s: %s", url, exc)
            return ""
        except Exception as exc:
            logger.debug("fetch failed for %s: %s", url, exc)
            return ""

    @staticmethod
    def _ddg_url(href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        m = re.search(r"uddg=([^&]+)", href)
        return unquote(m.group(1)) if m else ""
