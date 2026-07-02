#!/usr/bin/env python3
"""Long-term market scan — FINNHUB ONLY.

Strategy: a long-horizon view built from ETFs and index proxies (broad, liquid
exposure rather than single-stock bets). Shows day move + position within the
52-week range (near highs = strong trend; near lows = potential value), plus
market headlines.

Note: direct index quotes (^GSPC, etc.) are premium on Finnhub's free tier, so
indexes are represented by their tracking ETFs.

Env vars:
  FINNHUB_API_KEY   required
  ETF_UNIVERSE      optional comma list overriding the default ETF universe
  LONGTERM_TOP_N    leaders/laggards to show (default 10)
  RATE_SLEEP        seconds between calls (default 1.1; free tier = 60/min)
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
except Exception:  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("scan_longterm")

FINNHUB = "https://finnhub.io/api/v1"
KEY = os.getenv("FINNHUB_API_KEY")
RATE_SLEEP = float(os.getenv("RATE_SLEEP", "1.1"))
HTTP_TIMEOUT = 20
TOP_N = int(os.getenv("LONGTERM_TOP_N", "10"))

# Index proxies (direct index quotes are premium on Finnhub free).
INDEX_PROXIES = [
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq 100"),
    ("DIA", "Dow 30"),
    ("IWM", "Russell 2000"),
    ("VIXY", "VIX (short-term futures)"),
]

DEFAULT_ETF_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EFA", "EEM",
    "AGG", "BND", "TLT", "IEF", "LQD", "HYG", "GLD", "SLV", "USO", "VIXY",
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
    "XLC", "SMH", "SOXX", "ARKK", "XBI", "IBB", "KRE", "XOP", "GDX", "VNQ",
    "EWJ", "FXI", "INDA", "EWZ", "KWEB", "DBC", "TQQQ", "SQQQ",
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


def finnhub_get(path: str, **params):
    params["token"] = KEY
    try:
        r = HTTP.get(f"{FINNHUB}{path}", params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("GET %s failed: %s", path, e)
        return None
    finally:
        time.sleep(RATE_SLEEP)  # stay under 60 req/min


# Per-run caches: index proxies overlap the ETF universe (and can land in
# leaders/laggards), so each symbol is fetched at most once per endpoint.
_quote_cache: dict[str, tuple] = {}
_metric_cache: dict[str, dict | None] = {}


def get_quote(sym: str):
    if sym not in _quote_cache:
        q = finnhub_get("/quote", symbol=sym) or {}
        _quote_cache[sym] = (q.get("c"), q.get("dp"))
    return _quote_cache[sym]


def get_52w_position(sym: str, price):
    """Return position in 52-week range (0=low, 1=high), or None."""
    if sym not in _metric_cache:
        m = finnhub_get("/stock/metric", symbol=sym, metric="all") or {}
        md = m.get("metric") if isinstance(m, dict) else None
        _metric_cache[sym] = md if isinstance(md, dict) else None
    md = _metric_cache[sym]
    if md is None:
        return None
    hi, lo = md.get("52WeekHigh"), md.get("52WeekLow")
    if not (price and hi and lo and hi > lo):
        return None
    return max(0.0, min(1.0, (price - lo) / (hi - lo)))


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "n/a"


def _pos(p):
    return f"{p*100:.0f}%" if isinstance(p, (int, float)) else "n/a"


def get_market_news(limit: int = 8):
    items = finnhub_get("/news", category="general")
    return items[:limit] if isinstance(items, list) else []


def build_report(indexes, leaders, laggards, news) -> str:
    now = datetime.now(timezone.utc)
    md = f"# Long-Term Scan — ETFs & Indexes – {now.strftime('%Y-%m-%d')}\n\n"
    md += f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')} · Finnhub · research only, not advice._\n\n"

    md += "## Index Proxies\n"
    md += "| Proxy | Tracks | Price | Day % | 52w Position |\n|---|---|---|---|---|\n"
    for r in indexes:
        md += f"| **{r['symbol']}** | {r['tracks']} | {_fmt(r['price'])} | {_fmt(r['change_pct'])}% | {_pos(r['position'])} |\n"

    md += "\n## ETF Leaders (today)\n"
    md += _etf_table(leaders)
    md += "\n## ETF Laggards (today)\n"
    md += _etf_table(laggards)

    md += "\n## Market Headlines\n"
    if news:
        for a in news:
            md += f"- [{a.get('headline','(untitled)')}]({a.get('url','')})\n"
    else:
        md += "_No market news available._\n"

    md += "\n---\n_Indexes shown via tracking ETFs (direct index quotes are premium on Finnhub free). 52w position: 0%=at 52-week low, 100%=at 52-week high._\n"
    return md


def _etf_table(rows) -> str:
    if not rows:
        return "_No data available._\n"
    md = "| ETF | Price | Day % | 52w Position |\n|---|---|---|---|\n"
    for r in rows:
        md += f"| **{r['symbol']}** | {_fmt(r['price'])} | {_fmt(r['change_pct'])}% | {_pos(r['position'])} |\n"
    return md


def main() -> None:
    if not KEY:
        raise SystemExit("FINNHUB_API_KEY is not set")

    # Index proxies: quote + 52w position.
    indexes = []
    for sym, tracks in INDEX_PROXIES:
        price, dp = get_quote(sym)
        pos = get_52w_position(sym, price) if price else None
        indexes.append(
            {"symbol": sym, "tracks": tracks, "price": price, "change_pct": dp, "position": pos}
        )

    # ETF universe: quote all, rank by day move.
    override = os.getenv("ETF_UNIVERSE", "").strip()
    universe = (
        [s.strip().upper() for s in override.split(",") if s.strip()]
        if override
        else DEFAULT_ETF_UNIVERSE
    )
    quotes = []
    for sym in universe:
        price, dp = get_quote(sym)
        if price and dp is not None:
            quotes.append({"symbol": sym, "price": price, "change_pct": dp})
    ranked = sorted(quotes, key=lambda r: r["change_pct"], reverse=True)
    leaders = ranked[:TOP_N]
    laggards = ranked[-TOP_N:][::-1] if len(ranked) > TOP_N else []

    # 52w position only for displayed ETFs (keeps call count bounded).
    for r in leaders + laggards:
        r["position"] = get_52w_position(r["symbol"], r["price"])

    news = get_market_news()
    report = build_report(indexes, leaders, laggards, news)

    today = datetime.now(timezone.utc)
    out_dir = f"reports/longterm/{today.strftime('%Y-%m')}"
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{out_dir}/{today.strftime('%Y-%m-%d')}-longterm.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(report)

    log.info("Wrote %s (%d ETFs ranked, %d news)", fname, len(ranked), len(news))
    print(f"✓ Long-term scan complete -> {fname}")


if __name__ == "__main__":
    main()
