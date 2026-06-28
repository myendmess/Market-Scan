# Market-Scan — project context

Two **independent** zero-cost GitHub Actions pipelines that pull market data from
free-tier APIs and produce dated Markdown reports. **Free-tier only** — never
introduce a paid data provider or paid endpoint.

## The two pipelines (don't conflate them)

| | Long-term | Short-term |
|---|---|---|
| Script | `scripts/scan_longterm.py` | `scripts/scan_shortterm.py` |
| Workflow | `.github/workflows/scan-longterm.yml` | `.github/workflows/scan-shortterm.yml` |
| Schedule (cron UTC) | `0 22 * * 1-5` (after US close) | `0 11 * * 1-5` (pre-open) |
| Data source | **Finnhub** | **Alpha Vantage** |
| What it does | ETF & index leaders/laggards + 52-week position + market news | penny / small-cap momentum watchlist (reflects last close) |
| Output | `reports/longterm/<YYYY-MM>/<YYYY-MM-DD>-longterm.md` | `reports/shortterm/<YYYY-MM>/<YYYY-MM-DD>-shortterm.md` |
| Override env | `ETF_UNIVERSE` | `PENNY_MAX_PRICE` |

Both workflows have `permissions: contents: write` and **commit & push** their report
via `git add reports/` (recursive — picks up the month subfolders automatically).

## Reports layout (reorganized 2026-06-28)
```
reports/
├─ longterm/<YYYY-MM>/<date>-longterm.md
└─ shortterm/<YYYY-MM>/<date>-shortterm.md
```
The scripts compute `today = datetime.now(timezone.utc)` and write into
`reports/<type>/<YYYY-MM>/`. Goal: accumulate a clean dated archive to build datasets
from later.

## `mapping/` — interactive global market heatmap (separate sub-project)
A finviz-style heatmap project lives in the sibling `mapping/` folder (its own
`scripts/`, `data/`, `dashboard/`). v1 = S&P 500; built to scale to a global
Region→Country→Index hierarchy. See `mapping/README.md` for the full charter, canonical
lineage, and architecture (batch-built JSON → static web app, no client-side keys).

## Data sources & limits (all free tier)
- **Finnhub** — free = **60 req/min**; calls are rate-limited (sleep stays even on failure).
- **Alpha Vantage** — free ≈ **25 req/day**; used by the short-term scan.
- Premium endpoints (e.g. congressional trading) return empty on free tier — scripts
  **degrade gracefully**: a missing key or failed source = partial output, never a crash.

## Secrets / env vars
- GitHub Actions secrets: `FINNHUB_API_KEY` (long-term), `ALPHAVANTAGE_API_KEY` (short-term).
  Set in repo Settings → Secrets; **not** read from `.env` in CI.
- Local: copy `.env.example` → `.env` (gitignored).

## Running
Real runtime is **GitHub Actions, Python 3.11**. Deps: `requests`, `python-dotenv`
(see `requirements.txt`). NOTE: the dev Windows machine has **no real Python installed**
(only the MS Store stub), so scans can't be executed locally — verify script changes by
reading + CI, or trigger a run via `workflow_dispatch`.

## Conventions
- Preserve the `try/except + log.warning + return None` graceful-degradation pattern.
- Output is a mechanical heuristic screen — **not financial advice**; keep that disclaimer
  in generated reports.
- `reports/` IS committed (by the workflows). Use `actions/upload-artifact@v4` if artifacts
  are ever added (v3 is sunset).
