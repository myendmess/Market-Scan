# Market-Scan — project context

Two **independent** zero-cost GitHub Actions pipelines that pull market data from
free-tier APIs and produce Markdown. Free-tier only — never introduce a paid data
provider or paid endpoint.

## The two pipelines (don't conflate them)

| | Long-term scan | Daily digest |
|---|---|---|
| Script | `scripts/daily_scan.py` | `scripts/market_digest.py` |
| Workflow | `.github/workflows/daily_scan.yml` | `.github/workflows/daily-market-digest.yml` |
| Schedule (cron UTC) | `0 22 * * 1-5` (after US close) | `0 9 * * 1-5` |
| Output | `reports/YYYY-MM-DD-scan.md` | `digest.md` |
| How output is kept | **committed & pushed** back to repo (`permissions: contents: write`) | uploaded as **artifact** (7-day) + run summary; NOT committed |
| What it does | dynamic universe → Finnhub valuation metrics → heuristic long-term value score + suggested entry zone; optional congressional trades | top stock movers (Alpha Vantage) + ETF movers (computed from Finnhub quotes) + market/tech news |

> Note: `README.md` documents only the **digest**, not the scan. Keep that in mind
> if README and code disagree — the code is ground truth.

## Data sources & limits (all free tier)
- **Finnhub** `/quote`, `/stock/metric`, `/news` — free = **60 req/min**; calls are
  rate-limited via `RATE_SLEEP` (default 1.1s). Sleep stays even on failures.
- **Alpha Vantage** `TOP_GAINERS_LOSERS` — free is ~25 req/day; used to build the
  dynamic universe / stock movers. Falls back to a curated large-cap list if absent.
- **NewsAPI** — optional; **keyless RSS fallback** (feedparser) when `NEWSAPI_KEY` unset.
- **Congressional trading** (`/stock/congressional-trading`) is **premium** → returns
  empty on free tier; the script degrades gracefully, never hard-fails.

## Secrets / env vars
- GitHub Actions secrets: `FINNHUB_API_KEY` (required), `ALPHAVANTAGE_API_KEY`,
  `NEWSAPI_KEY` (optional). Set in repo Settings → Secrets, NOT read from `.env` in CI.
- Local: copy `.env.example` → `.env` (gitignored). Overrides: `WATCHLIST`,
  `ETF_UNIVERSE`, `SCAN_LIMIT`, `CONGRESS_TOP`, `MIN_MARKET_CAP`, `RATE_SLEEP`.

## Run locally
```bash
pip install -r requirements.txt          # requests, python-dotenv, feedparser
python scripts/daily_scan.py             # -> reports/<date>-scan.md
python scripts/market_digest.py          # -> digest.md (gitignored)
WATCHLIST=AAPL,MSFT,NVDA python scripts/daily_scan.py   # ad-hoc tickers
```
Python 3.11. Output is a mechanical heuristic screen — **not financial advice**;
keep that disclaimer in generated reports.

## Conventions
- Scripts must **degrade gracefully**: a missing key or failed source = partial
  output, never a crash. Preserve the `try/except + log.warning + return None` pattern.
- `digest.md` is gitignored (generated). `reports/` IS committed (by the scan workflow).
- Use `actions/upload-artifact@v4` (v3 is sunset).
