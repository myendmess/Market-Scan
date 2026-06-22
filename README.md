# Project — Market Scanners

Two **separate, single-provider** GitHub Actions pipelines, split by strategy
horizon. Each runs on a schedule, generates a Markdown report, and commits it to
`reports/`. Secrets live in repo settings (and a gitignored `.env` locally).

| Pipeline | Provider | Horizon | Output |
|----------|----------|---------|--------|
| [Long-Term Scan](.github/workflows/scan-longterm.yml) | **Finnhub only** | Long — ETFs & index proxies | `reports/YYYY-MM-DD-longterm.md` |
| [Short-Term Watchlist](.github/workflows/scan-shortterm.yml) | **Alpha Vantage only** | Short — penny/small-cap momentum | `reports/YYYY-MM-DD-shortterm.md` |

The two providers are kept strictly isolated — Finnhub never appears in the
short-term script and Alpha Vantage never in the long-term script.

## Long-Term Scan — `scripts/scan_longterm.py`
Broad, liquid exposure (ETFs + index proxies SPY/QQQ/DIA/IWM/VIXY), showing the
day's move and **position within the 52-week range**, plus market headlines.
Runs 22:00 UTC Mon–Fri (after US close). Direct index quotes are premium on
Finnhub free, so indexes are represented by their tracking ETFs.

## Short-Term Watchlist — `scripts/scan_shortterm.py`
The day's top ~5 penny/small-cap movers (price ≤ `$5` by default) as a momentum
**watchlist**, enriched with market cap, shares outstanding, country, and flags
(sub-$1, China/HK, micro-cap). Runs 11:00 UTC Mon–Fri (pre-open).

> **Honest limits (free-only):** Alpha Vantage's free tier is end-of-day, not
> real-time/premarket — this is a watchlist, not a predictor. Modes that need
> short-locate / order-flow data (short interest, borrow fee, shares-to-borrow,
> squeeze, dark-pool) have **no free source** and are skipped; the report lists
> them explicitly. Adding them would require a paid provider (Fintel, Ortex,
> TrendVision, etc.).

## Setup
1. **Secrets** (*Settings → Secrets and variables → Actions*):
   - `FINNHUB_API_KEY` — long-term scan
   - `ALPHAVANTAGE_API_KEY` — short-term scan
2. **Write permission for Actions** (*Settings → Actions → General → Workflow
   permissions* → **Read and write**) so the workflows can commit reports.

## Run locally
```bash
cp .env.example .env          # fill in the two keys
pip install -r requirements.txt
python scripts/scan_longterm.py    # -> reports/<date>-longterm.md
python scripts/scan_shortterm.py   # -> reports/<date>-shortterm.md
```

## Notes
- Alpha Vantage free tier: 25 requests/day. The short-term scan uses ≤6/run.
- `.env` is gitignored; `reports/*.md` are committed (your dated history).
