"""RSS feed reader tool — fetch and parse RSS/Atom feeds from a curated catalogue."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .base import BaseTool, ToolDefinition

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
}

# Curated feed catalogue, grouped by category
FEED_CATALOGUE: dict[str, dict[str, str]] = {
    "world": {
        "bbc_world":     "http://feeds.bbci.co.uk/news/world/rss.xml",
        "nytimes_world": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "aljazeera":     "https://www.aljazeera.com/xml/rss/all.xml",
        "cnn_top":       "http://rss.cnn.com/rss/edition.rss",
    },
    "world_in_polish" {
        "polsatnews":    "https://www.polsatnews.pl/rss/swiat.xml",
    },
    "technology": {
        "techcrunch":    "https://techcrunch.com/feed/",
        "theverge":      "https://www.theverge.com/rss/index.xml",
        "wired":         "https://www.wired.com/feed/rss",
        "arstechnica":   "https://feeds.arstechnica.com/arstechnica/index",
        "bbc_tech":      "http://feeds.bbci.co.uk/news/technology/rss.xml",
        "hackernews":    "https://news.ycombinator.com/rss",
        "slashdot":      "http://rss.slashdot.org/Slashdot/slashdotMain",
    },
    "science": {
        "nasa":          "https://www.nasa.gov/rss/dyn/breaking_news.rss",
        "nature":        "https://www.nature.com/nature.rss",
        "bbc_science":   "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "physorg":       "https://phys.org/rss-feed/",
    },
    "business": {
        "wsj_markets":   "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "economist_biz": "https://www.economist.com/business/rss.xml",
        "bbc_business":  "http://feeds.bbci.co.uk/news/business/rss.xml",
        "ft_world":      "https://www.ft.com/world?format=rss",
    },
    "linux": {
        "lwn":           "https://lwn.net/headlines/rss",
        "phoronix":      "https://www.phoronix.com/rss.php",
        "linuxtoday":    "https://linuxtoday.com/feed/",
        "omglinux":      "https://www.omglinux.com/feed/",
        "archlinux":     "https://archlinux.org/feeds/news/",
    },
    "security": {
        "schneier":      "https://www.schneier.com/feed/atom/",
        "bleepingcomp":  "https://www.bleepingcomputer.com/feed/",
        "thehackernews": "https://feeds.feedburner.com/TheHackersNews",
    },
    "weather_alerts_in_polish" {
        "lowcy_burz":    "https://lowcyburz.pl/feed/",
    }
}

# Flat name → url lookup built from catalogue
_NAME_TO_URL: dict[str, str] = {
    name: url
    for feeds in FEED_CATALOGUE.values()
    for name, url in feeds.items()
}

# Namespace stripping regex for ElementTree
_NS_RE = re.compile(r"\{[^}]+\}")


def _tag(el: ET.Element) -> str:
    return _NS_RE.sub("", el.tag)


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_date(raw: str) -> str:
    """Try to normalise an RSS/Atom date to ISO-8601; return raw on failure."""
    if not raw:
        return ""
    for parser in (
        parsedate_to_datetime,          # RFC 2822 (RSS)
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),  # ISO-8601 (Atom)
    ):
        try:
            dt = parser(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            continue
    return raw.strip()


def _parse_feed(xml_text: str, max_items: int) -> list[dict[str, str]]:
    """Parse RSS 2.0 or Atom feed; return list of {title, link, date, summary}."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.debug("XML parse error: %s", exc)
        return []

    items: list[dict[str, str]] = []
    root_tag = _tag(root)

    if root_tag == "feed":
        # Atom
        for entry in root.iter():
            if _tag(entry) != "entry":
                continue
            title = summary = link = date = ""
            for child in entry:
                t = _tag(child)
                if t == "title":
                    title = _text(child)
                elif t in ("summary", "content"):
                    summary = re.sub(r"<[^>]+>", "", _text(child))[:300]
                elif t == "link":
                    link = child.get("href", "") or _text(child)
                elif t in ("updated", "published"):
                    if not date:
                        date = _parse_date(_text(child))
            if title or link:
                items.append({"title": title, "link": link, "date": date, "summary": summary})
            if len(items) >= max_items:
                break
    else:
        # RSS 2.0 — find <channel> → <item> elements
        for item in root.iter():
            if _tag(item) != "item":
                continue
            title = summary = link = date = ""
            for child in item:
                t = _tag(child)
                if t == "title":
                    title = _text(child)
                elif t == "description":
                    summary = re.sub(r"<[^>]+>", "", _text(child))[:300]
                elif t == "link":
                    link = _text(child)
                elif t == "pubDate":
                    date = _parse_date(_text(child))
            if title or link:
                items.append({"title": title, "link": link, "date": date, "summary": summary})
            if len(items) >= max_items:
                break

    return items


def _format_items(source: str, items: list[dict[str, str]]) -> str:
    if not items:
        return f"No items found in feed: {source}"
    lines = [f"### {source} — {len(items)} items\n"]
    for i, it in enumerate(items, 1):
        lines.append(f"**{i}. {it['title'] or '(no title)'}**")
        if it["date"]:
            lines.append(f"  {it['date']}")
        if it["summary"]:
            lines.append(f"  {it['summary'].strip()}")
        if it["link"]:
            lines.append(f"  {it['link']}")
        lines.append("")
    return "\n".join(lines)


class RSSFeedTool(BaseTool):
    def __init__(self, max_items: int = 10, extra_feeds: dict[str, str] | None = None):
        self.max_items = max_items
        self._feeds = dict(_NAME_TO_URL)
        if extra_feeds:
            self._feeds.update({k.lower(): v for k, v in extra_feeds.items()})

    def definition(self) -> ToolDefinition:
        catalogue_summary = "\n".join(
            f"  **{cat}**: " + ", ".join(feeds.keys())
            for cat, feeds in FEED_CATALOGUE.items()
        )
        return ToolDefinition(
            name="rss",
            description=(
                "Read RSS/Atom news feeds from a curated catalogue of sources.\n\n"
                "Provide either a `feed` name from the catalogue, a `category` to read "
                "multiple feeds at once, or a custom `url` for any RSS/Atom feed.\n\n"
                "## When to use this tool\n"
                "- User asks for latest news (world, tech, science, business, Linux, security)\n"
                "- User asks what's new in a specific publication (BBC, NYT, Ars Technica, etc.)\n"
                "- User asks about recent CVEs or security advisories\n"
                "- User asks for latest Arch Linux or Linux kernel news\n\n"
                "## Catalogue\n"
                + catalogue_summary + "\n\n"
                "Use `category` to fetch headlines from all feeds in a category at once."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "feed": {
                        "type": "string",
                        "description": (
                            "Name of a specific feed from the catalogue, e.g. 'bbc_world', "
                            "'arstechnica', 'hackernews', 'archlinux', 'krebs'."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": list(FEED_CATALOGUE.keys()),
                        "description": (
                            "Fetch headlines from all feeds in a category. "
                            "One of: " + ", ".join(FEED_CATALOGUE.keys()) + "."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": "Custom RSS/Atom feed URL (for feeds not in the catalogue).",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Max items to return per feed (default 10, max 25).",
                    },
                },
            },
        )

    async def execute(
        self,
        feed: str | None = None,
        category: str | None = None,
        url: str | None = None,
        max_items: int | None = None,
        **_: Any,
    ) -> str:
        n = min(int(max_items or self.max_items), 25)

        if url:
            return await self._fetch_and_format(url, url, n)

        if feed:
            feed_key = feed.lower().strip()
            feed_url = self._feeds.get(feed_key)
            if not feed_url:
                available = ", ".join(sorted(self._feeds.keys()))
                return f"Unknown feed '{feed}'. Available: {available}"
            return await self._fetch_and_format(feed_url, feed_key, n)

        if category:
            cat = category.lower().strip()
            feeds = FEED_CATALOGUE.get(cat)
            if not feeds:
                return f"Unknown category '{category}'. Available: {', '.join(FEED_CATALOGUE.keys())}"
            # Fetch all feeds in the category concurrently
            import asyncio
            tasks = [self._fetch_and_format(feed_url, name, n) for name, feed_url in feeds.items()]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            parts = []
            for r in results:
                if isinstance(r, Exception):
                    parts.append(f"(error: {r})")
                else:
                    parts.append(r)
            return "\n\n---\n\n".join(parts)

        return "Provide `feed`, `category`, or `url`."

    async def _fetch_and_format(self, url: str, label: str, n: int) -> str:
        xml = await self._get(url)
        if not xml:
            return f"Failed to fetch feed: {url}"
        items = _parse_feed(xml, n)
        return _format_items(label, items)

    async def _get(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=_HEADERS) as c:
                resp = await c.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as exc:
            logger.debug("RSS fetch failed for %s: %s", url, exc)
            return ""
