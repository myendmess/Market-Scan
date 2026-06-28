#!/usr/bin/env python3
"""Build the S&P 500 dataset for the market-map heatmap.

Pipeline (all free tier; sources validated 2026-06-28):
1. Constituents + GICS sector/sub-industry  -> datahub constituents.csv (1 bulk call)
2. Market cap + price                        -> NASDAQ screener download (1 bulk call)
3. 52-week high/low                          -> NASDAQ per-symbol summary (rate-limited),
                                                with Finnhub /stock/metric as fallback
                                                (uses FINNHUB_API_KEY if present).

Writes ``mapping/dashboard/data/sp500.json`` — a flat array the web app groups
Sector -> Sub-Industry -> Stock. Degrades gracefully: a symbol missing market cap is
skipped; a symbol missing 52-week keeps size but gets a null (grey) position.

Env vars:
  RATE_SLEEP        seconds between NASDAQ per-symbol calls (default 0.25)
  FINNHUB_API_KEY   optional; only used as a 52-week fallback
  LIMIT             cap number of symbols (for testing; default all)

Output is mechanical market data for visualization — NOT financial advice.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - optional locally, absent in CI is fine
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("build_sp500")

CONSTITUENTS_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)
NASDAQ_SCREENER = "https://api.nasdaq.com/api/screener/stocks?tableonly=false&limit=10000&download=true"
NASDAQ_SUMMARY = "https://api.nasdaq.com/api/quote/{sym}/summary?assetclass=stocks"
FINNHUB_METRIC = "https://finnhub.io/api/v1/stock/metric"

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
RATE_SLEEP = float(os.getenv("RATE_SLEEP", "0.25"))
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
LIMIT = int(os.getenv("LIMIT", "0"))  # 0 = all
HTTP_TIMEOUT = 20

OUT_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "data" / "sp500.json"


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


def _money(s: str | None) -> float | None:
    """'$317.4' / '4,167,977,885,680' -> float; None/'' /'0.00' -> None."""
    if not s:
        return None
    v = s.replace("$", "").replace(",", "").strip()
    try:
        f = float(v)
        return f if f > 0 else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
def get_constituents() -> list[dict]:
    r = HTTP.get(CONSTITUENTS_URL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    out = [
        {
            "ticker": row["Symbol"].strip(),
            "name": row["Security"].strip(),
            "gics_sector": row.get("GICS Sector", "").strip() or "Unknown",
            "gics_sub_industry": row.get("GICS Sub-Industry", "").strip() or "Other",
        }
        for row in rows
        if row.get("Symbol")
    ]
    log.info("Constituents: %d", len(out))
    return out


def get_bulk_quotes() -> dict[str, dict]:
    """symbol -> {market_cap, price} from the NASDAQ screener (one bulk call)."""
    try:
        r = HTTP.get(NASDAQ_SCREENER, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json().get("data", {}).get("rows") or []
    except (requests.RequestException, ValueError) as e:
        log.warning("NASDAQ screener failed: %s", e)
        return {}
    quotes = {}
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if sym:
            quotes[sym] = {
                "market_cap": _money(row.get("marketCap")),
                "price": _money(row.get("lastsale")),
            }
    log.info("Bulk quotes: %d symbols", len(quotes))
    return quotes


def nasdaq_52w(symbol: str) -> tuple[float, float] | None:
    try:
        r = HTTP.get(NASDAQ_SUMMARY.format(sym=symbol), headers=UA, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        sd = (r.json().get("data") or {}).get("summaryData") or {}
        raw = (sd.get("FiftTwoWeekHighLow") or {}).get("value")
        if not raw or "/" not in raw:
            return None
        hi, lo = (x.replace("$", "").replace(",", "").strip() for x in raw.split("/", 1))
        return float(hi), float(lo)
    except (requests.RequestException, ValueError):
        return None
    finally:
        time.sleep(RATE_SLEEP)


def finnhub_52w(symbol: str) -> tuple[float, float] | None:
    if not FINNHUB_KEY:
        return None
    try:
        r = HTTP.get(
            FINNHUB_METRIC,
            params={"symbol": symbol, "metric": "all", "token": FINNHUB_KEY},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        m = r.json().get("metric") or {}
        hi, lo = m.get("52WeekHigh"), m.get("52WeekLow")
        if hi and lo:
            return float(hi), float(lo)
    except (requests.RequestException, ValueError):
        return None
    finally:
        time.sleep(RATE_SLEEP)
    return None


def position(price, low, high) -> float | None:
    if not (price and low and high and high > low):
        return None
    return round(max(0.0, min(1.0, (price - low) / (high - low))), 3)


# --------------------------------------------------------------------------- #
def main() -> None:
    names = get_constituents()
    if LIMIT:
        names = names[:LIMIT]
    quotes = get_bulk_quotes()

    out: list[dict] = []
    missing_52w = 0
    for i, c in enumerate(names, 1):
        sym = c["ticker"]
        q = quotes.get(sym.upper(), {})
        mcap, price = q.get("market_cap"), q.get("price")
        if not mcap:
            log.warning("skip %s (no market cap)", sym)
            continue

        hl = nasdaq_52w(sym) or finnhub_52w(sym.replace(".", "."))
        if hl:
            high, low = hl
        else:
            high = low = None
            missing_52w += 1

        out.append(
            {
                **c,
                "market_cap": mcap,
                "price": price,
                "wk52_low": low,
                "wk52_high": high,
                "wk52_position": position(price, low, high),
            }
        )
        if i % 50 == 0:
            log.info("…%d/%d processed", i, len(names))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=1), encoding="utf-8")
    log.info(
        "Wrote %s (%d names, %d without 52-week)", OUT_PATH, len(out), missing_52w
    )
    print(f"✓ S&P 500 map data -> {OUT_PATH} ({len(out)} names)")


if __name__ == "__main__":
    main()
