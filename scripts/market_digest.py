#!/usr/bin/env python3
"""Daily market & news digest.

Fetches spot quotes (Finnhub), market headlines (Finnhub), and tech/market
news (NewsAPI, with a keyless RSS fallback), then writes a Markdown digest to
``digest.md``.

Design goals:
- Never hard-crash CI on partial data. Each source degrades gracefully and the
  digest is written with whatever was collected.
- Works locally (loads ``.env``) and in GitHub Actions (env vars / secrets).

Env vars:
- FINNHUB_API_KEY  required for quotes + Finnhub news
- NEWSAPI_KEY      optional; if missing, falls back to free RSS feeds
- SYMBOLS          optional; comma-separated tickers (e.g. "AAPL,MSFT,NVDA")
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Load .env when running locally; harmless if the file/package is absent (CI).
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("market_digest")

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

DEFAULT_SYMBOLS = ["AAPL", "GOOGL", "MSFT"]
HTTP_TIMEOUT = 15  # seconds

# Free, no-auth finance feeds used when NewsAPI is unavailable.
RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",          # WSJ Markets
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",  # MarketWatch
]


def _session() -> requests.Session:
    """A requests session with sane retries/backoff for flaky free tiers."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


HTTP = _session()


def _symbols() -> list[str]:
    raw = os.getenv("SYMBOLS", "").strip()
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return DEFAULT_SYMBOLS


def get_market_data(symbols: list[str]) -> dict[str, dict]:
    """Finnhub quote endpoint: current price + % change per symbol."""
    if not FINNHUB_KEY:
        log.warning("FINNHUB_API_KEY not set; skipping market quotes")
        return {}

    base = "https://finnhub.io/api/v1/quote"
    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            r = HTTP.get(
                base, params={"symbol": sym, "token": FINNHUB_KEY}, timeout=HTTP_TIMEOUT
            )
            r.raise_for_status()
            q = r.json()
            price = q.get("c")
            # Finnhub returns c == 0 for unknown/invalid symbols.
            if not price:
                log.warning("No quote for %s (got %s)", sym, q)
                continue
            out[sym] = {"price": price, "change_pct": q.get("dp")}
        except requests.RequestException as e:
            log.warning("Quote fetch failed for %s: %s", sym, e)
    return out


def get_market_news(limit: int = 10) -> list[dict]:
    """Finnhub general market news -> list of {headline, url}."""
    if not FINNHUB_KEY:
        return []
    try:
        r = HTTP.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": FINNHUB_KEY},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        items = r.json()
        return items[:limit] if isinstance(items, list) else []
    except requests.RequestException as e:
        log.warning("Finnhub news failed: %s", e)
        return []


def get_tech_news(limit: int = 5) -> list[dict]:
    """NewsAPI tech/market news, or RSS fallback. Returns normalized dicts:
    {title, url, source}."""
    if NEWSAPI_KEY:
        try:
            r = HTTP.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": "technology stocks market",
                    "sortBy": "publishedAt",
                    "language": "en",
                    "apiKey": NEWSAPI_KEY,
                    "pageSize": limit,
                },
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            articles = r.json().get("articles", [])
            return [
                {
                    "title": a.get("title", "(untitled)"),
                    "url": a.get("url", ""),
                    "source": (a.get("source") or {}).get("name", "NewsAPI"),
                }
                for a in articles[:limit]
            ]
        except requests.RequestException as e:
            log.warning("NewsAPI failed (%s); falling back to RSS", e)

    return _get_rss_news(limit)


def _get_rss_news(limit: int) -> list[dict]:
    """Keyless fallback: pull headlines from free finance RSS feeds."""
    try:
        import feedparser  # imported lazily so it's only needed for fallback
    except Exception:
        log.warning("feedparser not installed; no RSS fallback available")
        return []

    out: list[dict] = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", "RSS")
            for entry in feed.entries:
                out.append(
                    {
                        "title": entry.get("title", "(untitled)"),
                        "url": entry.get("link", ""),
                        "source": source,
                    }
                )
        except Exception as e:  # feedparser is tolerant; guard anyway
            log.warning("RSS fetch failed for %s: %s", url, e)
    return out[:limit]


def format_digest(market: dict, news: list[dict], tech: list[dict]) -> str:
    """Build the Markdown digest from collected data."""
    now = datetime.now(timezone.utc)
    md = f"# Market Digest – {now.strftime('%Y-%m-%d')}\n\n"
    md += f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')}_\n\n"

    md += "## Spot Prices\n"
    if market:
        for sym, q in market.items():
            change = q.get("change_pct")
            change_str = f"{change:+.2f}%" if isinstance(change, (int, float)) else "n/a"
            md += f"- **{sym}**: ${q['price']:.2f} ({change_str})\n"
    else:
        md += "_No quote data available._\n"

    md += "\n## Market News\n"
    if news:
        for a in news[:5]:
            title = a.get("headline") or a.get("title") or "(untitled)"
            md += f"- [{title}]({a.get('url', '')})\n"
    else:
        md += "_No market news available._\n"

    md += "\n## Tech & Earnings\n"
    if tech:
        for a in tech[:5]:
            md += f"- [{a['title']}]({a['url']}) – {a['source']}\n"
    else:
        md += "_No tech news available._\n"

    return md


def main() -> None:
    symbols = _symbols()
    log.info("Symbols: %s", ", ".join(symbols))

    market = get_market_data(symbols)
    news = get_market_news()
    tech = get_tech_news()

    digest = format_digest(market, news, tech)
    with open("digest.md", "w", encoding="utf-8") as f:
        f.write(digest)

    log.info(
        "Digest written: %d quotes, %d market headlines, %d tech items",
        len(market),
        len(news),
        len(tech),
    )
    print("✓ Digest generated -> digest.md")


if __name__ == "__main__":
    main()
