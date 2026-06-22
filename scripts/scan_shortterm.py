#!/usr/bin/env python3
"""Short-term penny / small-cap momentum watchlist — ALPHA VANTAGE ONLY.

Strategy: surface the day's top penny/small-cap movers as a watchlist of
potential "runners" (max 5). Built from a single TOP_GAINERS_LOSERS call, then
the top candidates are enriched with company fundamentals (market cap, shares
outstanding, country, sector).

HONEST LIMITS (kept free-only, per current constraints):
- Alpha Vantage's free tier is NOT real-time/premarket — data reflects the last
  close. This is a momentum *watchlist*, not a predictor.
- Short interest, borrow fee, shares-to-borrow, squeeze, and dark-pool modes
  require a short-locate / order-flow provider (e.g. Fintel/Ortex/TrendVision,
  all paid). They are intentionally skipped and listed as unavailable.

Call budget (free tier = 25/day, 5/min): 1 movers call + up to 5 OVERVIEW calls.

Env vars:
  ALPHAVANTAGE_API_KEY   required
  PENNY_MAX_PRICE        price ceiling for "penny/small-cap" (default 5.0)
  SHORTTERM_TOP_N        number of candidates (default 5)
  AV_SLEEP               seconds between AV calls (default 13; free = 5/min)
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
log = logging.getLogger("scan_shortterm")

AV = "https://www.alphavantage.co/query"
AV_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
PENNY_MAX = float(os.getenv("PENNY_MAX_PRICE") or "5.0")  # empty env -> default
TOP_N = int(os.getenv("SHORTTERM_TOP_N", "5"))
AV_SLEEP = float(os.getenv("AV_SLEEP", "13"))  # free tier 5 req/min
HTTP_TIMEOUT = 20

# Modes from the target scanner that have NO free data source today.
UNAVAILABLE_MODES = [
    "0 Available Shares To Borrow",
    "Highest Borrow Fee",
    "Highest Short Interest",
    "Recent Surge in Short Interest",
    "Potential Squeeze",
    "Recent Surge in Dark Pool Activity",
    "Strong Watch",
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


def av_get(**params):
    params["apikey"] = AV_KEY
    try:
        r = HTTP.get(AV, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("Alpha Vantage request failed: %s", e)
        return None
    finally:
        time.sleep(AV_SLEEP)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v):
    if isinstance(v, str):
        return _to_float(v.strip().rstrip("%"))
    return _to_float(v)


def _humanize(v):
    n = _to_float(v)
    if n is None:
        return "n/a"
    for unit in ("", "K", "M", "B", "T"):
        if abs(n) < 1000:
            return f"{n:.0f}" if unit == "" else f"{n:.1f}{unit}"
        n /= 1000
    return f"{n:.1f}P"


def get_penny_candidates() -> list[dict]:
    """One TOP_GAINERS_LOSERS call -> penny gainers ranked by % move."""
    if not AV_KEY:
        log.warning("ALPHAVANTAGE_API_KEY not set")
        return []
    j = av_get(function="TOP_GAINERS_LOSERS")
    if not j or "top_gainers" not in j:
        log.warning("Alpha Vantage movers unavailable: %s", j)
        return []

    seen, out = set(), []
    # Gainers first (we want runners), then any actives not already included.
    for bucket in ("top_gainers", "most_actively_traded"):
        for x in j.get(bucket, []):
            t = (x.get("ticker") or "").upper()
            price = _to_float(x.get("price"))
            chg = _pct(x.get("change_percentage"))
            if not t or t in seen or price is None:
                continue
            if price <= PENNY_MAX and (chg or 0) > 0:
                seen.add(t)
                out.append(
                    {"ticker": t, "price": price, "change_pct": chg, "volume": x.get("volume")}
                )
    out.sort(key=lambda r: (r["change_pct"] or 0), reverse=True)
    return out[:TOP_N]


def enrich(sym: str) -> dict:
    o = av_get(function="OVERVIEW", symbol=sym) or {}
    return {
        "name": o.get("Name"),
        "market_cap": o.get("MarketCapitalization"),
        "shares_out": o.get("SharesOutstanding"),
        "country": o.get("Country"),
        "sector": o.get("Sector"),
    }


def _flags(row: dict) -> str:
    f = []
    if isinstance(row["price"], (int, float)):
        if row["price"] < 0.50:
            f.append("sub-$0.50")
        elif row["price"] < 1.0:
            f.append("sub-$1")
    if (row.get("country") or "").lower() in ("china", "hong kong"):
        f.append("China/HK")
    mc = _to_float(row.get("market_cap"))
    if mc is not None and mc < 50_000_000:
        f.append("micro-cap")
    return ", ".join(f) or "—"


def build_report(rows: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    md = f"# Short-Term Penny Watchlist – {now.strftime('%Y-%m-%d')}\n\n"
    md += f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')} · Alpha Vantage · momentum watchlist (reflects last close), NOT predictive, not advice._\n\n"

    md += f"## Top {TOP_N} Potential Runners (≤ ${PENNY_MAX:g})\n\n"
    if rows:
        md += "| # | Ticker | Price | Chg % | Volume | Mkt Cap | Shares Out | Country | Flags |\n"
        md += "|---|--------|-------|-------|--------|---------|------------|---------|-------|\n"
        for i, r in enumerate(rows, 1):
            md += (
                f"| {i} | **{r['ticker']}** | {r['price']:.3f} | {(_pct(r['change_pct']) or 0):+.1f}% | "
                f"{_humanize(r['volume'])} | {_humanize(r.get('market_cap'))} | "
                f"{_humanize(r.get('shares_out'))} | {r.get('country') or 'n/a'} | {_flags(r)} |\n"
            )
    else:
        md += "_No penny candidates in today's movers (or Alpha Vantage rate-limited)._\n"

    md += "\n## Not Yet Available (no free data source)\n\n"
    md += "These scanner modes need short-locate / order-flow data (Fintel, Ortex, "
    md += "TrendVision — all paid) and are skipped under the current free-only setup:\n\n"
    for m in UNAVAILABLE_MODES:
        md += f"- {m}\n"

    md += "\n---\n_Source: Alpha Vantage TOP_GAINERS_LOSERS + OVERVIEW. Free tier is end-of-day, not real-time._\n"
    return md


def main() -> None:
    if not AV_KEY:
        raise SystemExit("ALPHAVANTAGE_API_KEY is not set")

    rows = get_penny_candidates()
    for r in rows:  # enrich top candidates (bounded by TOP_N)
        r.update(enrich(r["ticker"]))

    report = build_report(rows)

    os.makedirs("reports", exist_ok=True)
    fname = f"reports/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-shortterm.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(report)

    log.info("Wrote %s (%d candidates)", fname, len(rows))
    print(f"✓ Short-term scan complete -> {fname}")


if __name__ == "__main__":
    main()
