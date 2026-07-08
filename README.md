# Smart Prop Firm Deal Tracker — stock-first discovery

Answers one question every day: **which stocks were picked or touched by
tracked prop / HFT / quant trading firms?** The source of truth is the
official **bulk deal** and **block deal** disclosures of the NSE (primary) and
BSE (secondary) — no third-party scraping (Screener may be added later purely
as optional verification/enrichment, and only via a permitted API/export
route).

Each day the pipeline fetches the disclosures plus the NSE
price/volume/delivery bhavcopy, matches participant names against a curated
firm watchlist, and rolls everything up **per stock**: which firms appeared,
buy/sell/gross/net values, a flow label, whether several firms showed up
together, whether the stock is a first-time mention, and price/volume context.
Every stock gets a **stock attention score** (0-100) that ranks how much
tracked-firm attention it received — it implies no direction and predicts
nothing — plus a short plain-English explanation, and rule-based **alerts**
flag the days worth a look. Round-trip churn is always shown but labelled as
liquidity activity — never bullish or bearish. The markdown report and the
local **dashboard** exist to help you decide *which stocks to watch*, not to
pretend to predict returns. (A performance/backtesting layer can sit on top
of the stored returns later; it is deliberately out of scope today.)

```
NSE + BSE official data ──> raw snapshots + SQLite
      bulk/block deals  ──> normalize names ──> match watchlist (ISIN-mapped)
      short selling     ──> firm × stock × day ──> STOCK-FIRST rollup:
      bhavcopy+delivery ──> market context        firms · values · flow label
                        ──> attention scores      first-time · multi-firm
                        ──> plain-English notes   clusters · post-deal returns
                        ──> data/reports/daily_YYYY-MM-DD.md
```

## Quick start

Requires Python 3.9+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# one-time: seed ~2 months of price history so ADV and returns have a baseline
.venv/bin/python main.py backfill-prices --from-date 2026-05-01 --to-date 2026-07-04

# the whole daily pipeline for one date:
.venv/bin/python main.py run-daily --date 2026-07-06

# browse it:
.venv/bin/python main.py serve        # http://127.0.0.1:8050
```

Output ends with the report path, e.g. `data/reports/daily_2026-07-06.md`.
Without `--date` it uses *today in IST* (what cron should do). Everything is
idempotent — re-running a date re-fetches, dedupes raw rows, and rebuilds all
derived data (matches, aggregates, context, clusters, scores) for that date.

## CLI

| Command | Purpose |
|---|---|
| `run-daily [--date D] [--skip-fetch] [--no-short] [--no-prices] [--sources nse,bse]` | full pipeline for one date |
| `stocks --date D [--new-only]` | terminal view: stocks tracked firms touched, ranked by attention, with explanations |
| `alerts [--date D]` | alert feed for a date (defaults to the latest date with alerts) |
| `serve [--host H] [--port P]` | run the local stock-first dashboard |
| `backfill --from-date A --to-date B` | run-daily over a range (weekends skipped) |
| `backfill-prices --from-date A --to-date B` | load NSE bhavcopies only (seeds ADV / returns) |
| `update-returns [--as-of D] [--lookback-days N]` | refresh post-deal returns from stored history |
| `firm-stats [--since D]` | historical per-firm summary: churn share, direction-adjusted returns, hit rate |
| `refresh-masters [--force]` | refresh NSE/BSE securities masters (ISIN mapping) |
| `fetch --date D --types bulk,block,short,prices` | fetch + store raw data only |
| `process --date D` | re-match, re-aggregate, re-score stored raw data |
| `report --date D` | regenerate the markdown report |
| `init-db` / `seed-firms` / `list-firms` | schema + watchlist management |

Global flags: `-v` (debug logs), `-q` (warnings only). Exit codes: `0`
success, `1` error, `2` = a bulk/block deal fetch failed (short-selling and
bhavcopy failures are soft warnings) — let cron alert on non-zero.

## Data sources (all official exchange endpoints)

**NSE** (primary):

1. `GET /api/historicalOR/bulk-block-short-deals?...&csv=true` — the CSV
   download behind the website's report page; works for any historical date.
   Used for bulk deals, block deals, and short selling.
2. Same endpoint without `csv=true` (JSON, keys `BD_*`/`SS_*`) — fallback.
3. `nsearchives.nseindia.com/content/equities/{bulk,block}.csv` — static
   daily files (latest trading day only); last-resort fallback for the evening
   cron run.
4. `nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv` —
   full bhavcopy **with security-wise delivery data** (one file per day):
   OHLC, volume, turnover, trades, `DELIV_QTY`, `DELIV_PER`.
5. `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` — listed-equities
   master (symbol → ISIN).

**BSE** (secondary):

6. `api.bseindia.com/BseIndiaAPI/api/BulkblockDeal/w?type={1|2}&fromdt=DD/MM/YYYY&todt=...`
   — bulk (`type=1`) and block (`type=2`) deals as JSON. The official CSV
   download endpoint is deliberately **not** used: it omits the scrip
   code/name columns, making rows unattributable to a stock. A response
   without a `Table` key means a bad/blocked request and is treated as a
   failure, never as "no deals".
7. `api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?...` — BSE scrip master
   (scrip code → ISIN).

NSE fronts its endpoints with a bot filter; the fetcher uses browser-like
headers, warms up a session against the homepage, retries with backoff, and
re-warms after 401/403. Every successful response body is stored verbatim
under `data/raw/<date>/` before parsing. Fetchers are modular
(`proptracker/fetchers/`, registry in its `__init__.py`) so further sources
can be added — Screener deliberately isn't one today: it would be scraping;
it may join later only as optional verification/enrichment through a
permitted API/export route.

**BSE → NSE symbol mapping.** BSE deals identify stocks by scrip code. Both
masters are joined on ISIN, so dual-listed stocks aggregate under their NSE
symbol and join NSE price history (~2,250 mappings). BSE-only micro-caps keep
their BSE scrip id — they still match firms and appear in reports, but without
price context (flagged in the report footer). Masters auto-refresh when older
than `MASTERS_MAX_AGE_DAYS` (7).

## Database (SQLite, `data/proptracker.db`)

| Table | Contents |
|---|---|
| `raw_deals` | deals exactly as disclosed (deduped; `source` = NSE/BSE) |
| `short_selling` | NSE security-level short-selling (no client names published) |
| `stock_history` | daily OHLC/volume/turnover/trades/delivery per symbol-series |
| `securities_master` | NSE + BSE masters for ISIN mapping |
| `firms`, `firm_aliases` | watchlist (aliases normalized, with match mode) |
| `matched_deals` | per raw deal: normalized name, matched firm, method, confidence |
| `daily_firm_stock_agg` | per (date, firm, symbol, deal type): qty/value/VWAP + classification |
| `daily_stock_agg` | **stock-first rollup** per (date, symbol): firms, values, flow label, churn split, first-time flag |
| `market_context` | per (date, symbol): close, day move, volume, ADV20, delivery |
| `attention_scores` | per (date, symbol): 0-100 attention score, JSON breakdown, plain-English explanation |
| `clusters` | multi-firm same-stock clusters (BUY/SELL/CHURN) |
| `deal_returns` | post-deal returns T+1/3/5/10 per aggregate |
| `fetch_log` | audit trail of every fetch attempt |

Schema management is additive (`CREATE IF NOT EXISTS` + column backfill), so
Phase 1 databases upgrade in place.

## Name matching

Client names and aliases share one normalization: uppercase, punctuation →
spaces, whitespace collapsed, trailing legal suffixes stripped (PVT, LTD, LLP,
PTE, …). So `"NK Securities Research Private Limited"` →
`"NK SECURITIES RESEARCH"` and `"HRTI PRIVATE LIMITED"` → `"HRTI"`.

Match tiers (per deal, most specific alias first):

1. **exact** (confidence 1.0) — normalized equality; any alias qualifies.
2. **prefix** (0.9) — client starts with `"<alias> "`, e.g.
   `JUMP TRADING FINANCIAL INDIA` ← alias `JUMP TRADING`.
3. **contains** (0.8) — alias appears as whole words inside the name.

Guardrails: single-word aliases shorter than 5 chars (`XTX`, `HRT`, `JUMP`)
and aliases seeded with a `=` prefix (`=MILLENNIUM`, `=MAVERICK` — generic
English words that live data showed matching unrelated brokers like
"MILLENNIUM STOCK BROKING") match on exact equality only. Every match stores
its method + alias, and the report prints the disclosed name, so matches are
auditable. Edit `proptracker/watchlist.py` and run `seed-firms` to change the
25-firm watchlist.

## Classification, context, and scoring

**Stock-level flow label** per (stock, date) — the discovery view, derived
from the directional (non-churn) positions of every tracked firm in the
stock; churn is carried alongside as a separate count/value, never in the
label:

- `Clean buy` / `Clean sell` — tracked firms only bought (or only sold).
- `Partial net buy` / `Partial net sell` — one side dominates after two-sided
  trading.
- `Mixed` — firms took directional positions on *both* sides that largely
  offset (disagreement between firms — different from churn).
- `Round-trip (churn only)` — every firm's activity was intraday round-trip:
  a liquidity/event flag, **not** bullish or bearish.

Plus per stock: firms involved (and whether several appeared together) and a
**first-time mention** flag (★ — symbol never seen in tracked-firm deals in
stored history; meaningful once you've backfilled some months).

**Firm-level classification** per (firm, stock, date, deal type):

- `CLEAN_BUY` / `CLEAN_SELL` — one side only.
- `ROUND_TRIP` — both sides and `|net| ≤ 20% × gross` (`ROUND_TRIP_NET_RATIO`)
  — intraday churn, reported as noise. (HFT reality check: on a typical day
  ~95-100% of tracked-firm bulk-deal rows are round trips.)
- `NET_BUY` / `NET_SELL` — both sides with a meaningful residual.

**Market context** per (date, symbol): close, day move %, volume, turnover,
delivery qty/%, and `ADV20` — mean volume over the prior 20 sessions
(`ADV_WINDOW`, needs ≥ `MIN_ADV_DAYS`=5 sessions of history).

**Stock attention score (v1)** per (stock, date) — ranks which stocks deserve
a look today. It is **not** buy/sell advice: a five-firm churn frenzy scores
high as a liquidity/event flag, and the flow label (not the score) says what
kind of activity it was.

```
score = 100 × weighted_mean(components)      weights in ATTENTION_WEIGHTS
  firms    0.15   how many tracked firms appeared (cap 4)
  multi    0.10   any multi-firm participation
  gross    0.20   total gross traded value (cap ₹100 Cr)
  net      0.15   |net| directional value (cap ₹25 Cr)
  clean    0.15   directional share of activity (1 = no churn)
  context  0.15   volume vs ADV20 and |day move| (needs price history)
  repeat   0.10   appearances in the trailing 30 days
```

Missing inputs (e.g. no price history) drop out and the remaining weights
renormalize; the breakdown is stored as JSON in `attention_scores.components`
along with a **plain-English explanation** per stock (used verbatim in the
report), e.g. *"Shakti Pumps was touched by Graviton, Microcurves and Quadeye.
Combined gross activity was ₹264.87 Cr but net was only ₹1.89 Cr, so this
looks like liquidity/churn rather than directional accumulation."*

**Clusters**: ≥2 tracked firms directional on the same side of one stock
(BUY/SELL, combined net value), or ≥3 firms all round-tripping it (CHURN — an
event-day flag; those days often coincide with >10× ADV volume).

**Post-deal returns**: close k *sessions* after the deal (T+1/3/5/10) vs the
firm's own entry — buy/sell VWAP for directional trades, deal-day close for
round trips. Horizons fill in as new bhavcopies arrive (`run-daily` refreshes
a trailing `RETURNS_LOOKBACK_DAYS`=21-day window). `firm-stats` aggregates
them direction-adjusted: a sell followed by a fall counts as a win.

## Daily report — the morning brief

`data/reports/daily_YYYY-MM-DD.md` reads like a desk brief, not a data dump —
concise, ranked and opinionated:

1. **Daily Overview** — a one-line stance ("churn-heavy session, no clean
   directional flow, 6 event-scale liquidity days…") plus KPI row.
2. **Top Stocks to Watch** — ranked, each with its plain-English reason;
   liquidity events are flagged as such, never dressed up as direction.
3. **What Changed vs the previous session** — escalations (gross/firm-count
   jumps), churn-to-directional shifts, repeat streaks, names gone quiet.
4. **Clean Net Buy / Sell Activity** · 5. **Multi-Firm Activity** ·
   6. **New & Repeat Mentions** · 7. **Round-Trip / Liquidity Churn** (safe to
   ignore for direction) · 8. **Watchlist Additions**.
9. **Follow-Up Checklist** — concrete `- [ ]` actions for the trading day
   (check news on watchlist adds, identify events behind churn frenzies,
   re-check T+1 on yesterday's directional names, data-health fixes).
10. **Appendix** — the full ranked stock table with every explanation, fetch
    status, raw deals as disclosed with match audit, price follow-up on
    recent directional days, and the largest deals market-wide.

## Dashboard — daily command center

`python main.py serve` (add `--production` for waitress) runs a read-only,
terminal-styled dashboard over the same SQLite database (default
`http://127.0.0.1:8050`):

- `/` — the command center: stance line, KPI tiles, top stocks to watch,
  what changed vs the previous session, follow-up checklist, alerts, and the
  ten stock-first sections (overview, high attention, all stocks picked,
  multi-firm, clean net, churn, new mentions, repeat mentions, watchlist
  additions, raw-deal audit trail). `?date=YYYY-MM-DD` for other sessions.
- `/stock/<symbol>` — **the main page.** Everything about one stock: all
  tracked firms that touched it with firm-wise buy/sell/gross/net values, the
  combined stock-level activity and flow label, attention score and
  plain-English read, appearance history, raw deal rows as disclosed, recent
  price/volume/delivery context, and its alert history.
- `/stocks` — searchable index of every stock ever touched.
- `/firm/<id>`, `/firms` — secondary firm pages: aliases, active days, gross
  and net totals, directional vs round-trip row counts, recent stocks touched.
- `/alerts` — the alert feed, grouped by day.
- `/healthz` — JSON health check (DB reachable + last data date).

## Alerts

Rebuilt idempotently for each date at the end of `run-daily`, stored in the
`alerts` table, and surfaced in the dashboard, `python main.py alerts`, and
the run-daily summary. They are discovery prompts, never trade advice:

| Type | Fires when | Priority |
|---|---|---|
| High attention | attention ≥ `HIGH_ATTENTION_MIN` (60) | 1 |
| Clean net activity | clean/partial directional flow with \|net\| ≥ `ALERT_MIN_NET_CR` (₹1 Cr) | 1 |
| Churn → directional | ≥ `ALERT_CHURN_MIN_PRIOR` (2) prior days were pure round-trip, today is directional | 1 |
| Multi-firm | ≥ `ALERT_MULTI_FIRM_MIN` (2) tracked firms in one stock | 2 |
| Large gross activity | ≥ `ALERT_LARGE_GROSS_CR` (₹100 Cr) gross in one day (suppressed when high-attention already fired for the stock) | 2 |
| First-time mention | first appearance in stored history, with an attention floor (`ALERT_FIRST_TIME_MIN_ATTENTION`, 25) so tiny first-timers don't spam | 2 |
| Repeat mention | ≥ `ALERT_REPEAT_MIN` (3) appearances in the trailing 30 days | 3 |

Every alert message explains in plain English why it fired, and duplicate
reasons are suppressed (one loud alert per cause per stock per day).

## Deployment

**Docker (recommended):**

```bash
docker compose up -d --build
# web:       command center on http://localhost:8050 (waitress, healthcheck on /healthz)
# scheduler: runs `run-daily` every weekday at 19:30 IST (PFT_RUN_AT to change)
# data:      ./data on the host (SQLite + raw snapshots + reports)
```

**Bare metal / VM:** `deploy/systemd/` ships `proptracker-web.service`
(waitress via the venv) and `proptracker-daily.{service,timer}`
(`OnCalendar=Mon..Fri 19:30 Asia/Kolkata`). Copy to `/etc/systemd/system/`,
adjust paths/user, `systemctl enable --now`.

**Anything WSGI:** `wsgi.py` exposes `app`
(`waitress-serve --port 8050 wsgi:app`, gunicorn, etc.). The dashboard is
read-only; if you expose it beyond localhost, put it behind your reverse
proxy / auth of choice. Ops notes: SQLite runs in WAL mode so the daily write
and dashboard reads coexist; every pipeline step is idempotent so retries are
safe; exit code 2 from `run-daily` (deal fetch failed) is the signal to alert
on; `/healthz` reports the last data date for staleness monitoring.

## Configuration

Three layers (later wins):

1. Defaults in `proptracker/config.py` (documented inline).
2. Optional JSON file `config.local.json` (or `PFT_CONFIG_FILE=...`) with the
   same UPPERCASE keys, e.g. `{"ROUND_TRIP_NET_RATIO": 0.15,
   "ATTENTION_WEIGHTS": {"context": 0.25}}` — dicts merge key-by-key.
3. Environment variables: `PFT_DATA_DIR`, `PFT_DB_PATH`, `PFT_SOURCES`
   (default `nse,bse`), `PFT_ROUND_TRIP_NET_RATIO`, `PFT_FETCH_PRICES`,
   `PFT_INCLUDE_SHORT_SELLING`, `PFT_MASTERS_MAX_AGE_DAYS`,
   `PFT_RETURNS_LOOKBACK_DAYS`, `PFT_HIGH_ATTENTION_MIN`,
   `PFT_WATCHLIST_MIN_ATTENTION`, `PFT_ALERT_MULTI_FIRM_MIN`,
   `PFT_ALERT_MIN_NET_CR`, `PFT_ALERT_REPEAT_MIN`,
   `PFT_ALERT_CHURN_MIN_PRIOR`, `PFT_MAX_RETRIES`, `PFT_USER_AGENT`, …

## Cron

NSE publishes bulk/block deals ~18:30 IST; the bhavcopy with delivery data
lands around the same time. Schedule after 19:00 IST on weekdays:

```cron
CRON_TZ=Asia/Kolkata
30 19 * * 1-5 /path/to/prop-firm-tracker/scripts/cron_daily.sh >> /path/to/prop-firm-tracker/logs/cron.log 2>&1
```

`scripts/cron_daily.sh` resolves the project dir, prefers `.venv`, and
forwards arguments. Exit code 2 = deal fetch failed. Runs are safe to retry.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

86 tests, no network: parsers run against payload shapes captured from the
live NSE/BSE endpoints; the pipeline runs end-to-end with fake fetchers
(including the NSE+BSE merge, the stock-first rollup with churn/mixed/
first-time cases, attention scoring with explanations, and empty days); every
alert rule has fire/no-fire and anti-spam cases; the morning-brief helpers
(KPIs, stance, day-over-day changes, checklist) are covered; and the
dashboard routes render against a seeded database via Flask's test client.

## Limitations & notes

- Bulk deals are disclosed only when a client crosses 0.5% of listed equity in
  a day (block deals: ≥ ₹10 Cr negotiated trades), so this sees a *sample* of
  prop-firm activity, not their book.
- Values are quantity × disclosed price (WATP for bulk deals) — approximate
  turnover, not exact consideration.
- The NSE short-selling report is security-level (no client names), so it is
  context, not firm attribution. BSE publishes no equivalent.
- Returns are absolute (not index-relative) in this version.
- BSE-only listings have no NSE price history, hence no context/ADV/returns;
  their aggregates still appear and score on the value component alone.
- macOS system Python (LibreSSL) triggers a harmless urllib3 warning; the
  fetcher suppresses it.
