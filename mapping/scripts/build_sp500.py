#!/usr/bin/env python3
"""Build the S&P 500 dataset for the market-map heatmap (with period performance).

Pipeline (all free tier; sources validated 2026-06-28):
1. Constituents + GICS sector/sub-industry -> datahub constituents.csv (1 bulk call)
2. Market cap                              -> NASDAQ screener download   (1 bulk call)
3. Per-stock daily history (~1 year)       -> NASDAQ /chart (per symbol, rate-limited)
   From the history we derive: latest price, 52-week close range + position,
   seven period returns (1D/1W/1M/3M/6M/1Y/MTD), and a downsampled sparkline.

Writes ``mapping/dashboard/data/sp500.json`` — a flat array the web app groups
Sector -> Sub-Industry -> Stock. Degrades gracefully: no market cap or no chart -> skip.

Env vars:
  RATE_SLEEP   seconds between NASDAQ calls (default 0.25)
  LIMIT        cap number of symbols (testing; default all)

Output is mechanical market data for visualization — NOT financial advice.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
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
NASDAQ_CHART = "https://api.nasdaq.com/api/quote/{sym}/chart"

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
RATE_SLEEP = float(os.getenv("RATE_SLEEP", "0.25"))
LIMIT = int(os.getenv("LIMIT", "0"))
# Parallel chart fetches. NASDAQ's limit is undocumented — if a CI run shows
# mass 429s/blocks, set MAX_WORKERS=1 to fall back to sequential.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
HTTP_TIMEOUT = 25
SPARK_POINTS = 30  # downsampled sparkline length

OUT_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "data" / "sp500.json"


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}))
    # pool_maxsize must cover the worker count or threads serialize on sockets.
    s.mount("https://", HTTPAdapter(max_retries=retry,
                                    pool_maxsize=max(10, MAX_WORKERS)))
    return s


HTTP = _session()


def _money(s):
    if not s:
        return None
    try:
        f = float(str(s).replace("$", "").replace(",", "").strip())
        return f if f > 0 else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
def get_constituents() -> list[dict]:
    r = HTTP.get(CONSTITUENTS_URL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = [
        {"ticker": row["Symbol"].strip(), "name": row["Security"].strip(),
         "gics_sector": (row.get("GICS Sector") or "Unknown").strip() or "Unknown",
         "gics_sub_industry": (row.get("GICS Sub-Industry") or "Other").strip() or "Other"}
        for row in csv.DictReader(io.StringIO(r.text)) if row.get("Symbol")
    ]
    log.info("Constituents: %d", len(out))
    return out


def get_marketcaps() -> dict[str, float]:
    try:
        r = HTTP.get(NASDAQ_SCREENER, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json().get("data", {}).get("rows") or []
    except (requests.RequestException, ValueError) as e:
        log.warning("NASDAQ screener failed: %s", e)
        return {}
    caps = {}
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        mc = _money(row.get("marketCap"))
        if sym and mc:
            caps[sym] = mc
    log.info("Market caps: %d", len(caps))
    return caps


def fetch_series(symbol: str):
    """~1 year of (date, close), ascending. None on failure."""
    frm = (date.today() - timedelta(days=372)).isoformat()
    to = date.today().isoformat()
    try:
        r = HTTP.get(NASDAQ_CHART.format(sym=symbol), headers=UA,
                     params={"assetclass": "stocks", "fromdate": frm, "todate": to},
                     timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        chart = (r.json().get("data") or {}).get("chart") or []
        series = []
        for pt in chart:
            x, y = pt.get("x"), pt.get("y")
            if x is None or y is None:
                continue
            series.append((datetime.utcfromtimestamp(x / 1000).date(), float(y)))
        series.sort(key=lambda t: t[0])
        return series if len(series) >= 5 else None
    except (requests.RequestException, ValueError, TypeError):
        return None
    finally:
        time.sleep(RATE_SLEEP)


def _close_on_or_before(series, target):
    best = None
    for d, c in series:
        if d <= target:
            best = c
        else:
            break
    return best


def _downsample(closes, n):
    """Exactly n evenly spaced points, always including first and last close."""
    if len(closes) <= n:
        return [round(c, 2) for c in closes]
    step = (len(closes) - 1) / (n - 1)
    return [round(closes[round(i * step)], 2) for i in range(n)]


def analyze(series) -> dict:
    closes = [c for _, c in series]
    cur, cur_date = closes[-1], series[-1][0]
    low, high = min(closes), max(closes)

    def ret(days=None, target=None):
        if target is None:
            target = cur_date - timedelta(days=days)
        base = _close_on_or_before(series, target)
        return round((cur - base) / base * 100, 2) if base and base > 0 else None

    p1d = round((cur - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 and closes[-2] > 0 else None
    mtd_base = cur_date.replace(day=1) - timedelta(days=1)  # last close of prior month
    return {
        "price": round(cur, 2),
        "wk52_low": round(low, 2), "wk52_high": round(high, 2),
        "wk52_position": round((cur - low) / (high - low), 3) if high > low else None,
        "perf": {"1d": p1d, "1w": ret(7), "1m": ret(30), "3m": ret(91),
                 "6m": ret(182), "1y": ret(365), "mtd": ret(target=mtd_base)},
        "spark": _downsample(closes, SPARK_POINTS),
    }


# --------------------------------------------------------------------------- #
def main() -> None:
    names = get_constituents()
    if LIMIT:
        names = names[:LIMIT]
    caps = get_marketcaps()

    # Split cap-less names out first, then fetch charts in parallel. ex.map
    # preserves input order, so results zip back to their constituents.
    kept = [(c, caps.get(c["ticker"].upper())) for c in names]
    skipped = sum(1 for _, mc in kept if not mc)
    kept = [(c, mc) for c, mc in kept if mc]

    out = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        series_iter = ex.map(fetch_series, (c["ticker"] for c, _ in kept))
        for i, ((c, mcap), series) in enumerate(zip(kept, series_iter), 1):
            if series:
                out.append({**c, "market_cap": mcap, **analyze(series)})
            else:
                skipped += 1
            if i % 50 == 0:
                log.info("…%d/%d (kept %d)", i, len(kept), len(out))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    log.info("Wrote %s (%d names, %d skipped)", OUT_PATH, len(out), skipped)
    print(f"✓ S&P 500 map data -> {OUT_PATH} ({len(out)} names)")


if __name__ == "__main__":
    main()
