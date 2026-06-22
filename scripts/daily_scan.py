#!/usr/bin/env python3
"""Daily market scan -> long-term investing opportunities.

Pipeline:
1. Build a DYNAMIC universe from Finnhub `/stock/symbol` (US common stock),
   then take a day-rotating slice so coverage changes daily (no hardcoded list).
   An optional WATCHLIST env var overrides the dynamic universe.
2. For each candidate: pull `/stock/metric` (52-wk range, P/E, P/B, ROE, mkt cap)
   and `/quote` (live price). Compute a long-term value score + suggested entry.
3. For the top-ranked names: pull `/stock/congressional-trading` (premium; the
   script degrades gracefully if the plan doesn't include it).
4. Pull `/news?category=general` for market headlines.
5. Write `reports/YYYY-MM-DD-scan.md`.

All API calls are rate-limited to stay under Finnhub's free tier (60 req/min).

Env vars:
  FINNHUB_API_KEY   required
  WATCHLIST         optional, comma-separated tickers to scan instead of universe
  SCAN_LIMIT        how many symbols to screen (default 25)
  CONGRESS_TOP      run congressional lookup for the top N opportunities (default 8)
  MIN_MARKET_CAP    skip names below this market cap, $M (default 2000 = $2B)
  RATE_SLEEP        seconds to sleep between API calls (default 1.1)

Output is a heuristic scan, NOT financial advice.
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
log = logging.getLogger("daily_scan")

API = "https://finnhub.io/api/v1"
AV = "https://www.alphavantage.co/query"
KEY = os.getenv("FINNHUB_API_KEY")
AV_KEY = os.getenv("ALPHAVANTAGE_API_KEY")

# Fallback liquid large-cap universe if Alpha Vantage is unavailable/rate-limited.
DEFAULT_LIQUID_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "JPM", "V", "MA",
    "UNH", "HD", "PG", "JNJ", "XOM", "CVX", "KO", "PEP", "COST", "WMT",
    "BAC", "ABBV", "MRK", "AVGO", "ORCL", "CRM", "ADBE", "NFLX", "AMD", "INTC",
    "CSCO", "QCOM", "TXN", "DIS", "NKE", "MCD", "LLY", "PFE", "VZ", "WFC",
    "GS", "MS", "CAT", "BA", "HON",
]

SCAN_LIMIT = int(os.getenv("SCAN_LIMIT", "25"))
CONGRESS_TOP = int(os.getenv("CONGRESS_TOP", "8"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "2000"))  # $M
RATE_SLEEP = float(os.getenv("RATE_SLEEP", "1.1"))
HTTP_TIMEOUT = 20


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


def fh_get(path: str, **params):
    """Rate-limited Finnhub GET. Returns parsed JSON or None on failure."""
    params["token"] = KEY
    try:
        r = HTTP.get(f"{API}{path}", params=params, timeout=HTTP_TIMEOUT)
        if r.status_code in (401, 403):
            log.warning("%s -> %s (likely premium/forbidden)", path, r.status_code)
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("GET %s failed: %s", path, e)
        return None
    finally:
        time.sleep(RATE_SLEEP)  # stay under 60 req/min


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
def _av_candidate_pool() -> list[str]:
    """Liquid, dynamic candidate pool from Alpha Vantage TOP_GAINERS_LOSERS.

    Combines most-actively-traded + top gainers + top losers (in that order of
    priority) into a deduped list of real, liquid tickers refreshed daily.
    """
    if not AV_KEY:
        log.warning("ALPHAVANTAGE_API_KEY not set; cannot build dynamic pool")
        return []
    try:
        r = HTTP.get(
            AV,
            params={"function": "TOP_GAINERS_LOSERS", "apikey": AV_KEY},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        j = r.json()
    except requests.RequestException as e:
        log.warning("Alpha Vantage pool request failed: %s", e)
        return []
    finally:
        time.sleep(RATE_SLEEP)

    if "most_actively_traded" not in j and "top_gainers" not in j:
        log.warning("Alpha Vantage unexpected response: %s", j)
        return []

    pool: list[str] = []
    seen: set[str] = set()
    # Most-actively-traded first (most liquid), then the day's biggest movers.
    for bucket in ("most_actively_traded", "top_gainers", "top_losers"):
        for item in j.get(bucket, []):
            t = (item.get("ticker") or "").upper()
            # Skip warrants/units/rights (Nasdaq 5th-letter codes W/U/R) — not
            # investable common stock for a long-term screen.
            if (
                t
                and t.isalpha()
                and len(t) <= 5
                and t not in seen
                and not (len(t) == 5 and t[-1] in ("W", "U", "R"))
            ):
                seen.add(t)
                pool.append(t)
    return pool


def build_universe() -> list[str]:
    """Dynamic, liquid candidate universe for the value screen.

    Priority: WATCHLIST override -> Alpha Vantage liquid pool -> curated
    large-cap fallback. Bounded to SCAN_LIMIT to respect Finnhub rate limits.
    """
    override = os.getenv("WATCHLIST", "").strip()
    if override:
        syms = [s.strip().upper() for s in override.split(",") if s.strip()]
        log.info("Using WATCHLIST override: %d symbols", len(syms))
        return syms[:SCAN_LIMIT]

    pool = _av_candidate_pool()
    if pool:
        log.info("Dynamic Alpha Vantage pool: %d liquid candidates", len(pool))
    else:
        pool = DEFAULT_LIQUID_UNIVERSE
        log.warning("Falling back to curated large-cap universe (%d names)", len(pool))

    return pool[:SCAN_LIMIT]


# --------------------------------------------------------------------------- #
# Per-symbol analysis
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def analyze(symbol: str) -> dict | None:
    metric = fh_get("/stock/metric", symbol=symbol, metric="all")
    if not metric or "metric" not in metric:
        return None
    m = metric["metric"]

    quote = fh_get("/quote", symbol=symbol)
    price = (quote or {}).get("c")
    if not price:
        return None

    high = m.get("52WeekHigh")
    low = m.get("52WeekLow")
    pe = m.get("peTTM")
    pb = m.get("pbAnnual") or m.get("pbQuarterly")
    roe = m.get("roeTTM")
    mcap = m.get("marketCapitalization")
    div = m.get("dividendYieldIndicatedAnnual")

    # Require a real market cap >= floor. A missing cap (None) is typical of
    # leveraged/inverse ETFs and micro-cap warrants, which have no business in a
    # long-term, large-cap-oriented value screen — drop them.
    if mcap is None or mcap < MIN_MARKET_CAP:
        return None
    if not (high and low and high > low):
        return None

    # Position in 52-wk range: 0 = at the low (cheap), 1 = at the high.
    position = _clamp((price - low) / (high - low))

    # Heuristic long-term value score (0-100): cheaper valuation, stronger
    # quality, and proximity to 52-wk low score higher.
    pe_score = _clamp(1 - (pe / 40)) if isinstance(pe, (int, float)) and pe > 0 else 0.0
    pb_score = _clamp(1 - (pb / 8)) if isinstance(pb, (int, float)) and pb > 0 else 0.0
    roe_score = _clamp(roe / 30) if isinstance(roe, (int, float)) and roe > 0 else 0.0
    pos_score = _clamp(1 - position)
    score = round(100 * (0.30 * pe_score + 0.20 * pb_score + 0.25 * roe_score + 0.25 * pos_score), 1)

    # Suggested long-term entry zone: a value band just above the 52-wk low.
    zone_lo = round(low * 1.02, 2)
    zone_hi = round(low + 0.15 * (high - low), 2)
    suggested = round((zone_lo + zone_hi) / 2, 2)

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "change_pct": (quote or {}).get("dp"),
        "high_52w": round(high, 2),
        "low_52w": round(low, 2),
        "position": round(position, 2),
        "pe": pe,
        "pb": pb,
        "roe": roe,
        "div_yield": div,
        "mcap_m": mcap,
        "score": score,
        "entry_low": zone_lo,
        "entry_high": zone_hi,
        "suggested_entry": suggested,
    }


def congressional(symbol: str) -> list[dict]:
    data = fh_get("/stock/congressional-trading", symbol=symbol)
    if not data:
        return []
    return data.get("data", [])[:5]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _fmt(v, suffix="", nd=2):
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}{suffix}"
    return "n/a"


def build_report(rows: list[dict], congress: dict[str, list], news: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    md = f"# Daily Market Scan – {now.strftime('%Y-%m-%d')}\n\n"
    md += f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')} · heuristic screen, not financial advice._\n\n"

    md += f"Screened **{len(rows)}** names; ranked by a long-term value score "
    md += "(valuation, quality/ROE, and proximity to the 52-week low).\n\n"

    # Top opportunities table
    md += "## Top Long-Term Opportunities\n\n"
    if rows:
        md += "| # | Ticker | Price | 52w Range | Pos | P/E | ROE% | Score | Suggested Entry |\n"
        md += "|---|--------|-------|-----------|-----|-----|------|-------|-----------------|\n"
        for i, r in enumerate(rows, 1):
            rng = f"{_fmt(r['low_52w'])}–{_fmt(r['high_52w'])}"
            entry = f"{_fmt(r['entry_low'])}–{_fmt(r['entry_high'])} (~{_fmt(r['suggested_entry'])})"
            md += (
                f"| {i} | **{r['symbol']}** | {_fmt(r['price'])} | {rng} | "
                f"{_fmt(r['position'])} | {_fmt(r['pe'])} | {_fmt(r['roe'],'',1)} | "
                f"{r['score']} | {entry} |\n"
            )
    else:
        md += "_No candidates passed the screen today._\n"

    # Congressional interest
    md += "\n## Congressional Trading Interest\n\n"
    any_congress = any(congress.values())
    if any_congress:
        for sym, trades in congress.items():
            if not trades:
                continue
            md += f"**{sym}**\n\n"
            for t in trades:
                who = t.get("name", "Unknown")
                tx = t.get("transactionType", "?")
                date = t.get("transactionDate", "?")
                amt = f"${t.get('amountFrom','?')}–${t.get('amountTo','?')}"
                md += f"- {date} · {who} · {tx} · {amt}\n"
            md += "\n"
    else:
        md += (
            "_No congressional trading data returned. Finnhub's "
            "`/stock/congressional-trading` endpoint is a **premium** feature; "
            "on the free tier this section stays empty._\n"
        )

    # Market news
    md += "\n## Market Headlines\n\n"
    if news:
        for a in news[:8]:
            md += f"- [{a.get('headline','(untitled)')}]({a.get('url','')})\n"
    else:
        md += "_No market news available._\n"

    md += "\n---\n_Source: Finnhub. Scores and entry zones are mechanical heuristics for research only._\n"
    return md


# --------------------------------------------------------------------------- #
def main() -> None:
    if not KEY:
        raise SystemExit("FINNHUB_API_KEY is not set")

    universe = build_universe()
    rows: list[dict] = []
    for sym in universe:
        r = analyze(sym)
        if r:
            rows.append(r)
    rows.sort(key=lambda r: r["score"], reverse=True)

    top = rows[:CONGRESS_TOP]
    congress = {r["symbol"]: congressional(r["symbol"]) for r in top}

    news = fh_get("/news", category="general") or []

    report = build_report(rows, congress, news)

    os.makedirs("reports", exist_ok=True)
    fname = f"reports/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-scan.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(report)

    log.info("Wrote %s (%d ranked, %d news)", fname, len(rows), len(news))
    print(f"✓ Scan complete -> {fname}")


if __name__ == "__main__":
    main()
