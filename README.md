# Project — Daily Market & News Digest

A lightweight, **zero-cost** GitHub Actions workflow that runs every weekday,
pulls market quotes and news from free-tier APIs, formats a Markdown digest,
and stores it as a build artifact (and in the run summary).

This avoids per-execution token burn while keeping runs reliable and auditable.

## How it works

| Piece | File |
|-------|------|
| Scheduled workflow (cron + manual) | [`.github/workflows/daily-market-digest.yml`](.github/workflows/daily-market-digest.yml) |
| Digest generator | [`scripts/market_digest.py`](scripts/market_digest.py) |
| Dependencies | [`requirements.txt`](requirements.txt) |

**Data sources**
- **Quotes** — Finnhub `/quote` (free tier: 60 req/min)
- **Market headlines** — Finnhub `/news`
- **Tech & earnings** — NewsAPI `/everything`, with a **keyless RSS fallback**
  (WSJ Markets, MarketWatch) when `NEWSAPI_KEY` is not set

**Output** — `digest.md`, uploaded as an artifact (7-day retention) and printed
to the GitHub Actions run summary.

## Setup

1. **Add repository secrets** (*Settings → Secrets and variables → Actions*):
   - `FINNHUB_API_KEY` — required ([finnhub.io](https://finnhub.io))
   - `NEWSAPI_KEY` — optional ([newsapi.org](https://newsapi.org)); without it the
     script uses free RSS feeds instead.

   > The local `.env` is **not** read by GitHub Actions — secrets must be set in
   > the repo. See `.env.example` for the variable names.

2. **Adjust the schedule** if needed — the cron is `0 9 * * 1-5` (09:00 UTC,
   Mon–Fri). Verify your timezone with [crontab.guru](https://crontab.guru).

3. **Customize tickers** — edit `DEFAULT_SYMBOLS` in the script, or pass
   `symbols` when triggering the workflow manually (Actions → Run workflow), or
   set the `SYMBOLS` env var locally.

## Run locally

```bash
cp .env.example .env          # then fill in FINNHUB_API_KEY (NEWSAPI_KEY optional)
pip install -r requirements.txt
python scripts/market_digest.py
# -> writes digest.md (gitignored)
```

You can also override tickers ad hoc:

```bash
SYMBOLS=AAPL,MSFT,NVDA python scripts/market_digest.py
```

## Notes

- The script **degrades gracefully**: missing keys or failed sources produce a
  partial digest rather than a hard failure.
- `digest.md` is gitignored (it's generated output).
- `actions/upload-artifact@v4` is used because v3 is deprecated.
