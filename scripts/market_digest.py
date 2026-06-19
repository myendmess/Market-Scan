#!/usr/bin/env python3
"""Daily market digest — dynamic top movers + news.

Sections:
- Top 10 stock gainers / losers (Alpha Vantage TOP_GAINERS_LOSERS, whole market)
- Top 10 ETF gainers / losers (computed live from Finnhub quotes over a curated
  liquid-ETF universe — there is no free market-wide ETF movers endpoint)
- Market headlines (Finnhub)
- Tech & earnings (NewsAPI, with a keyless RSS fallback)

Writes the Markdown digest to ``digest.md``. Degrades gracefully: a missing key
or failed source yields a partial digest rather than a hard failure.

Env vars:
  FINNHUB_API_KEY        required for ETF movers + market news
  ALPHAVANTAGE_API_KEY   required for stock movers
  NEWSAPI_KEY            optional; falls back to RSS if absent
  ETF_UNIVERSE           optional comma list overriding the default ETF universe
  RATE_SLEEP             seconds between Finnhub calls (default 1.1; free=60/min)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - optional locally, absent in CI is fine
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("market_digest")

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
AV_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

FINNHUB = "https://finnhub.io/api/v1"
AV = "https://www.alphavantage.co/query"
HTTP_TIMEOUT = 20
RATE_SLEEP = float(os.getenv("RATE_SLEEP", "1.1"))

# Curated liquid ETF universe — movers are computed live each run. There is no
# free market-wide ETF-movers endpoint, so the universe is curated, not the data.
DEFAULT_ETF_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EFA", "EEM",
    "AGG", "BND", "TLT", "IEF", "LQD", "HYG", "GLD", "SLV", "USO", "VIXY",
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
    "XLC", "SMH", "SOXX", "ARKK", "XBI", "IBB", "KRE", "XOP", "GDX", "VNQ",
    "EWJ", "FXI", "INDA", "EWZ", "KWEB", "DBC", "TQQQ", "SQQQ",
]

# Free, no-auth finance feeds used when NewsAPI is unavailable.
RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
]


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


HTTP = _session()


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v):
    """Parse Alpha Vantage's '68.42%' -> 68.42."""
    if isinstance(v, str):
        return _to_float(v.strip().rstrip("%"))
    return _to_float(v)


# --------------------------------------------------------------------------- #
# Stock movers (Alpha Vantage)
# --------------------------------------------------------------------------- #
def get_stock_movers(limit: int = 10) -> dict[str, list[dict]]:
    empty = {"gainers": [], "losers": []}
    if not AV_KEY:
        log.warning("ALPHAVANTAGE_API_KEY not set; skipping stock movers")
        return empty
    try:
        r = HTTP.get(
            AV,
            params={"function": "TOP_GAINERS_LOSERS", "apikey": AV_KEY},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        j = r.json()
    except requests.RequestException as e:
        log.warning("Alpha Vantage request failed: %s", e)
        return empty

    if "top_gainers" not in j:
        # Rate-limit / invalid key responses come back as {"Information": ...}.
        log.warning("Alpha Vantage unexpected response: %s", j)
        return empty

    def row(x):
        return {
            "ticker": x.get("ticker"),
            "price": _to_float(x.get("price")),
            "change_pct": _pct(x.get("change_percentage")),
            "volume": x.get("volume"),
        }

    return {
        "gainers": [row(x) for x in j.get("top_gainers", [])[:limit]],
        "losers": [row(x) for x in j.get("top_losers", [])[:limit]],
    }


# --------------------------------------------------------------------------- #
# ETF movers (Finnhub quotes over curated universe)
# --------------------------------------------------------------------------- #
def get_etf_movers(limit: int = 10) -> dict[str, list[dict]]:
    empty = {"gainers": [], "losers": []}
    if not FINNHUB_KEY:
        log.warning("FINNHUB_API_KEY not set; skipping ETF movers")
        return empty

    override = os.getenv("ETF_UNIVERSE", "").strip()
    universe = (
        [s.strip().upper() for s in override.split(",") if s.strip()]
        if override
        else DEFAULT_ETF_UNIVERSE
    )

    quotes: list[dict] = []
    for sym in universe:
        try:
            r = HTTP.get(
                f"{FINNHUB}/quote",
                params={"symbol": sym, "token": FINNHUB_KEY},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            q = r.json()
            price, dp = q.get("c"), q.get("dp")
            if price and dp is not None:
                quotes.append({"ticker": sym, "price": price, "change_pct": dp})
        except requests.RequestException as e:
            log.warning("ETF quote failed for %s: %s", sym, e)
        finally:
            time.sleep(RATE_SLEEP)  # stay under 60 req/min

    if not quotes:
        return empty
    ranked = sorted(quotes, key=lambda r: r["change_pct"], reverse=True)
    return {"gainers": ranked[:limit], "losers": ranked[-limit:][::-1]}


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #
def get_market_news(limit: int = 8) -> list[dict]:
    if not FINNHUB_KEY:
        return []
    try:
        r = HTTP.get(
            f"{FINNHUB}/news",
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
    try:
        import feedparser
    except Exception:
        log.warning("feedparser not installed; no RSS fallback")
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
        except Exception as e:
            log.warning("RSS fetch failed for %s: %s", url, e)
    return out[:limit]


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "n/a"


def _movers_table(rows: list[dict], with_volume: bool) -> str:
    if not rows:
        return "_No data available._\n"
    if with_volume:
        md = "| # | Ticker | Price | Change % | Volume |\n|---|--------|-------|----------|--------|\n"
        for i, r in enumerate(rows, 1):
            md += f"| {i} | **{r['ticker']}** | {_fmt(r['price'])} | {_fmt(r['change_pct'])}% | {r.get('volume','n/a')} |\n"
    else:
        md = "| # | Ticker | Price | Change % |\n|---|--------|-------|----------|\n"
        for i, r in enumerate(rows, 1):
            md += f"| {i} | **{r['ticker']}** | {_fmt(r['price'])} | {_fmt(r['change_pct'])}% |\n"
    return md


def format_digest(stocks, etfs, news, tech) -> str:
    now = datetime.now(timezone.utc)
    md = f"# Market Digest – {now.strftime('%Y-%m-%d')}\n\n"
    md += f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')}_\n\n"

    md += "## 📈 Top 10 Stock Gainers\n" + _movers_table(stocks["gainers"], True)
    md += "\n## 📉 Top 10 Stock Losers\n" + _movers_table(stocks["losers"], True)
    md += "\n## 🧺 Top 10 ETF Gainers\n" + _movers_table(etfs["gainers"], False)
    md += "\n## 🧺 Top 10 ETF Losers\n" + _movers_table(etfs["losers"], False)

    md += "\n## Market Headlines\n"
    if news:
        for a in news[:8]:
            md += f"- [{a.get('headline','(untitled)')}]({a.get('url','')})\n"
    else:
        md += "_No market news available._\n"

    md += "\n## Tech & Earnings\n"
    if tech:
        for a in tech[:5]:
            md += f"- [{a['title']}]({a['url']}) – {a['source']}\n"
    else:
        md += "_No tech news available._\n"

    md += "\n---\n_Stock movers: Alpha Vantage. ETF movers: Finnhub (curated universe). Research only, not financial advice._\n"
    return md


def main() -> None:
    stocks = get_stock_movers()
    etfs = get_etf_movers()
    news = get_market_news()
    tech = get_tech_news()

    digest = format_digest(stocks, etfs, news, tech)
    with open("digest.md", "w", encoding="utf-8") as f:
        f.write(digest)

    log.info(
        "Digest: %d/%d stock movers, %d/%d ETF movers, %d news",
        len(stocks["gainers"]),
        len(stocks["losers"]),
        len(etfs["gainers"]),
        len(etfs["losers"]),
        len(news),
    )
    print("✓ Digest generated -> digest.md")


if __name__ == "__main__":
    main()
