#!/usr/bin/env python3
"""Smart Prop Firm Bulk/Block Deal Tracker — CLI.

Daily driver:
    python main.py run-daily                       # today (IST); ideal for cron
    python main.py run-daily --date 2026-07-03

One-time / maintenance:
    python main.py backfill-prices --from-date 2026-05-01 --to-date 2026-07-04
    python main.py backfill --from-date 2026-06-01 --to-date 2026-06-30
    python main.py update-returns --as-of 2026-07-06
    python main.py firm-stats --since 2026-01-01
    python main.py refresh-masters --force

Plumbing:
    python main.py fetch --date 2026-07-03 --types bulk,block,short,prices
    python main.py process --date 2026-07-03    # re-match/re-aggregate stored raw data
    python main.py report --date 2026-07-03     # regenerate the markdown report
    python main.py init-db / seed-firms / list-firms

Exit codes: 0 = success, 1 = bad usage or unexpected error,
            2 = completed but a bulk/block fetch failed (alert your cron).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta

from proptracker import config, db, masters, pipeline, stats, watchlist
from proptracker.aggregate import aggregate_date, aggregate_stocks_date
from proptracker.dates import ist_now, ist_today, parse_iso
from proptracker.report import write_report
from proptracker.returns import update_deal_returns

log = logging.getLogger("proptracker.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FETCH_FAILED = 2


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _resolve_date(value) -> date:
    if value is None:
        resolved = ist_today()
        log.info("no --date given; using today in IST: %s", resolved)
    else:
        try:
            resolved = parse_iso(value)
        except ValueError:
            raise SystemExit(f"error: --date must be YYYY-MM-DD, got {value!r}")
    if resolved > ist_today():
        raise SystemExit(f"error: {resolved} is in the future (IST)")
    return resolved


def _parse_sources(value):
    if not value:
        return None
    sources = tuple(s.strip().lower() for s in value.split(",") if s.strip())
    unknown = set(sources) - {"nse", "bse"}
    if unknown:
        raise SystemExit(f"error: unknown --sources {sorted(unknown)} (use nse,bse)")
    return sources


def _print_summary(summary: pipeline.RunSummary) -> None:
    print(f"Date:            {summary.trade_date}")
    for outcome in summary.fetch_outcomes:
        print(
            f"Fetch {outcome.deal_type:<10} {outcome.status:<8} "
            f"{outcome.strategy or '-':<18} {outcome.message}"
        )
    print(f"Raw deals:       +{summary.raw_inserted} new, {summary.raw_duplicates} duplicate")
    if summary.bse_mapped:
        print(f"BSE mapping:     {summary.bse_mapped} deal rows mapped to NSE symbols")
    if summary.short_inserted:
        print(f"Short-selling:   +{summary.short_inserted} rows")
    if summary.prices_inserted:
        print(f"Price history:   +{summary.prices_inserted} symbol-day rows")
    print(
        f"Matched:         {summary.matched_rows}/{summary.processed_rows} deal rows "
        f"→ {summary.firms_matched} tracked firm(s), {summary.agg_rows} firm-stock aggregate(s)"
    )
    print(
        f"Stocks touched:  {summary.stock_rows}"
        + (f" ({summary.new_stocks} first-time)" if summary.new_stocks else "")
    )
    print(
        f"Analytics:       {summary.context_rows} context row(s), "
        f"{summary.cluster_rows} cluster(s), {summary.attention_rows} attention score(s), "
        f"{summary.returns_updated} return row(s) refreshed"
    )
    if summary.alert_rows:
        print(f"Alerts:          {summary.alert_rows}")
        for message in summary.top_alerts:
            print(f"  • {message}")
    for warning in summary.warnings:
        print(f"WARNING:         {warning}")
    print(f"Report:          {summary.report_path}")


# --- command handlers ---------------------------------------------------------


def cmd_run_daily(args) -> int:
    trade_date = _resolve_date(args.date)
    summary = pipeline.run_daily(
        trade_date,
        skip_fetch=args.skip_fetch,
        include_short=False if args.no_short else None,
        include_prices=False if args.no_prices else None,
        sources=_parse_sources(args.sources),
    )
    _print_summary(summary)
    if (
        summary.processed_rows == 0
        and trade_date == ist_today()
        and ist_now().hour < 19
        and not summary.hard_fetch_failure
    ):
        print(
            "NOTE: exchanges usually publish bulk/block deals after ~18:30 IST; "
            "today's data may simply not be out yet."
        )
    return EXIT_FETCH_FAILED if summary.hard_fetch_failure else EXIT_OK


def cmd_fetch(args) -> int:
    trade_date = _resolve_date(args.date)
    types = {t.strip().lower() for t in args.types.split(",")}
    unknown = types - {"bulk", "block", "short", "prices"}
    if unknown:
        raise SystemExit(f"error: unknown --types {sorted(unknown)} (use bulk,block,short,prices)")

    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        fetchers = pipeline.build_fetchers(sources=_parse_sources(args.sources))
        iso = trade_date.isoformat()
        failed = False
        masters_refreshed = False
        for fetcher in fetchers:
            steps = []
            if "bulk" in types:
                steps.append(fetcher.fetch_bulk_deals)
            if "block" in types:
                steps.append(fetcher.fetch_block_deals)
            for step in steps:
                outcome = step(trade_date)
                if outcome.status == "ok" and outcome.deals and fetcher.source == "BSE":
                    if not masters_refreshed:
                        masters.refresh_masters(conn)
                        masters_refreshed = True
                    mapped = masters.map_bse_deals(conn, outcome.deals)
                    outcome.message += f" ({mapped} mapped to NSE symbols)"
                db.log_fetch(conn, iso, fetcher.source, outcome)
                if outcome.status == "ok":
                    n, _ = db.insert_raw_deals(conn, outcome.deals)
                    print(f"{fetcher.source} {outcome.deal_type}: {outcome.message} via "
                          f"{outcome.strategy} (+{n} new)")
                else:
                    failed = True
                    print(f"{fetcher.source} {outcome.deal_type}: {outcome.status} — {outcome.message}")
            if "short" in types and fetcher.source == "NSE":
                outcome = fetcher.fetch_short_selling(trade_date)
                db.log_fetch(conn, iso, fetcher.source, outcome)
                if outcome.status == "ok":
                    n = db.insert_short_selling(conn, outcome.deals)
                    print(f"{fetcher.source} SHORT_SELL: {outcome.message} (+{n} new)")
                else:
                    print(f"{fetcher.source} SHORT_SELL: {outcome.status} — {outcome.message}")
            if "prices" in types and hasattr(fetcher, "fetch_price_history"):
                outcome = fetcher.fetch_price_history(trade_date)
                db.log_fetch(conn, iso, fetcher.source, outcome)
                if outcome.status == "ok":
                    n, _ = db.insert_stock_history(conn, outcome.deals)
                    print(f"{fetcher.source} PRICES: {outcome.message} (+{n} new)")
                else:
                    print(f"{fetcher.source} PRICES: {outcome.status} — {outcome.message}")
        return EXIT_FETCH_FAILED if failed else EXIT_OK
    finally:
        conn.close()


def cmd_process(args) -> int:
    trade_date = _resolve_date(args.date).isoformat()
    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        rows, matched, firms = pipeline.process_date(conn, trade_date)
        agg = aggregate_date(conn, trade_date)
        stocks_touched = aggregate_stocks_date(conn, trade_date)
        analytics = pipeline.analyze_date(conn, trade_date)
        print(
            f"{trade_date}: {rows} raw rows, {matched} matched → {firms} firm(s), "
            f"{agg} aggregate(s), {stocks_touched} stock(s), "
            f"{analytics['context_rows']} context, "
            f"{analytics['cluster_rows']} cluster(s), "
            f"{analytics['attention_rows']} attention score(s)"
        )
        return EXIT_OK
    finally:
        conn.close()


def cmd_report(args) -> int:
    trade_date = _resolve_date(args.date).isoformat()
    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        path = write_report(conn, trade_date)
        print(path)
        return EXIT_OK
    finally:
        conn.close()


def cmd_backfill(args) -> int:
    start = _resolve_date(args.from_date)
    end = _resolve_date(args.to_date)
    if start > end:
        raise SystemExit("error: --from-date is after --to-date")
    worst = EXIT_OK
    current = start
    while current <= end:
        if current.weekday() >= 5 and not args.include_weekends:
            current += timedelta(days=1)
            continue
        print(f"=== {current.isoformat()} ===")
        summary = pipeline.run_daily(
            current,
            include_short=False if args.no_short else None,
            include_prices=False if args.no_prices else None,
            sources=_parse_sources(args.sources),
        )
        _print_summary(summary)
        if summary.hard_fetch_failure:
            worst = EXIT_FETCH_FAILED
        current += timedelta(days=1)
        if current <= end:
            time.sleep(args.sleep)  # be polite to the exchanges
    return worst


def cmd_backfill_prices(args) -> int:
    start = _resolve_date(args.from_date)
    end = _resolve_date(args.to_date)
    if start > end:
        raise SystemExit("error: --from-date is after --to-date")
    days, rows = pipeline.backfill_prices(start, end, sleep_seconds=args.sleep)
    print(f"loaded {days} trading day(s), +{rows} price rows")
    return EXIT_OK


def cmd_update_returns(args) -> int:
    as_of = _resolve_date(args.as_of).isoformat() if args.as_of else None
    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        n = update_deal_returns(conn, as_of=as_of, lookback_days=args.lookback_days)
        print(f"refreshed returns for {n} aggregate row(s)")
        return EXIT_OK
    finally:
        conn.close()


def cmd_firm_stats(args) -> int:
    since = _resolve_date(args.since).isoformat() if args.since else None
    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        rows = stats.firm_performance(conn, since=since)
        if not rows:
            print("no tracked-firm activity stored" + (f" since {since}" if since else ""))
            return EXIT_OK
        header = (
            f"{'Firm':<38} {'Days':>4} {'Syms':>4} {'Rows':>4} {'Sig':>4} "
            f"{'Churn%':>6} {'Gross ₹Cr':>10} {'Net ₹Cr':>9} "
            f"{'r5 adj':>7} {'hit r5':>6} {'n':>3}"
        )
        print(header)
        print("-" * len(header))
        for s in rows:
            avg5 = f"{s['avg_adj_r5']:+.1f}%" if s["avg_adj_r5"] is not None else "-"
            hit5 = f"{s['hit_rate_r5']:.0%}" if s["hit_rate_r5"] is not None else "-"
            print(
                f"{s['firm']:<38} {s['days']:>4} {s['symbols']:>4} {s['rows']:>4} "
                f"{s['signal_rows']:>4} {s['churn_share']:>6.0%} "
                f"{s['gross_value'] / 1e7:>10,.1f} {s['net_value'] / 1e7:>9,.1f} "
                f"{avg5:>7} {hit5:>6} {s['n_r5']:>3}"
            )
        print(
            "\nr5 adj = mean direction-adjusted return 5 sessions after their "
            "directional deals (sell followed by a fall counts positive)."
        )
        return EXIT_OK
    finally:
        conn.close()


_STOCK_LABELS = {
    "CLEAN_BUY": "Clean buy",
    "CLEAN_SELL": "Clean sell",
    "NET_BUY": "Partial net buy",
    "NET_SELL": "Partial net sell",
    "MIXED": "Mixed",
    "ROUND_TRIP": "Churn only",
}


def cmd_stocks(args) -> int:
    """Stock-first discovery view for one date, straight to the terminal."""
    trade_date = _resolve_date(args.date).isoformat()
    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        where_new = " AND s.first_time = 1" if args.new_only else ""
        rows = conn.execute(
            f"""SELECT s.*, a.score AS attention, a.explanation
                FROM daily_stock_agg s
                LEFT JOIN attention_scores a
                  ON a.trade_date = s.trade_date AND a.symbol = s.symbol
                WHERE s.trade_date = ?{where_new}
                ORDER BY COALESCE(a.score, 0) DESC, s.gross_value DESC""",
            (trade_date,),
        ).fetchall()
        if not rows:
            print(f"no tracked-firm stock activity stored for {trade_date}"
                  + (" (new only)" if args.new_only else ""))
            return EXIT_OK
        header = (
            f"{'Symbol':<12} {'Attn':>4} {'Flow':<17} {'Firms':>5} {'Gross ₹Cr':>10} "
            f"{'Net ₹Cr':>9} {'New':>3}"
        )
        print(f"Stocks touched by tracked firms — {trade_date}")
        print("(attention ranks activity; it is not buy/sell advice)\n")
        print(header)
        print("-" * len(header))
        for r in rows:
            attn = f"{r['attention']:.0f}" if r["attention"] is not None else "-"
            print(
                f"{r['symbol']:<12} {attn:>4} "
                f"{_STOCK_LABELS.get(r['classification'], r['classification']):<17} "
                f"{r['firm_count']:>5} {r['gross_value'] / 1e7:>10,.2f} "
                f"{r['net_value'] / 1e7:>9,.2f} {'★' if r['first_time'] else '':>3}"
            )
            if r["explanation"]:
                print(f"{'':<5}{r['explanation']}")
        print("\nChurn only = round-trip liquidity activity, not bullish or bearish.")
        return EXIT_OK
    finally:
        conn.close()


def cmd_alerts(args) -> int:
    from proptracker.alerts import ALERT_LABELS, alerts_for_date, latest_alert_date

    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        if args.date:
            trade_date = _resolve_date(args.date).isoformat()
        else:
            trade_date = latest_alert_date(conn)
            if not trade_date:
                print("no alerts stored yet — run `python main.py run-daily` first")
                return EXIT_OK
        rows = alerts_for_date(conn, trade_date)
        if not rows:
            print(f"no alerts for {trade_date}")
            return EXIT_OK
        print(f"Alerts — {trade_date} (discovery prompts, not trade advice)\n")
        marker = {1: "!!", 2: " !", 3: "  "}
        for r in rows:
            label = ALERT_LABELS.get(r["alert_type"], r["alert_type"])
            print(f"{marker.get(r['priority'], '  ')} [{r['symbol']:<12}] {label:<20} {r['message']}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_serve(args) -> int:
    from proptracker.web import create_app

    app = create_app()
    print(f"Stock discovery command center: http://{args.host}:{args.port}/")
    if args.production:
        try:
            from waitress import serve as waitress_serve
        except ImportError:
            raise SystemExit("error: --production needs waitress "
                             "(pip install -r requirements.txt)")
        waitress_serve(app, host=args.host, port=args.port, threads=8)
    else:
        app.run(host=args.host, port=args.port, debug=args.debug)
    return EXIT_OK


def cmd_refresh_masters(args) -> int:
    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        results = masters.refresh_masters(conn, force=args.force)
        for source in ("NSE", "BSE"):
            age = masters.master_age_days(conn, source)
            refreshed = f"+{results[source]} rows" if source in results else "fresh"
            print(f"{source} master: {refreshed} (age: {age:.1f} d)" if age is not None
                  else f"{source} master: missing and fetch failed")
        mapped = len(masters.build_bse_to_nse_map(conn))
        print(f"BSE→NSE ISIN mappings available: {mapped}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_init_db(_args) -> int:
    conn = db.connect()
    try:
        db.init_db(conn)
        print(f"initialized {config.DB_PATH}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_seed_firms(_args) -> int:
    conn = db.connect()
    try:
        db.init_db(conn)
        firms, aliases = watchlist.seed_firms(conn)
        print(f"seeded {firms} new firm(s), {aliases} new alias(es)")
        return EXIT_OK
    finally:
        conn.close()


def cmd_list_firms(_args) -> int:
    conn = db.connect()
    try:
        pipeline.prepare_db(conn)
        rows = conn.execute(
            """SELECT f.name, f.category, f.active, GROUP_CONCAT(a.alias, ' | ') AS aliases
               FROM firms f LEFT JOIN firm_aliases a ON a.firm_id = f.id
               GROUP BY f.id ORDER BY f.name"""
        ).fetchall()
        for r in rows:
            flag = "" if r["active"] else "  [inactive]"
            print(f"{r['name']}{flag}\n    aliases: {r['aliases'] or '-'}")
        print(f"\n{len(rows)} firm(s) tracked")
        return EXIT_OK
    finally:
        conn.close()


# --- parser ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Track Indian prop/HFT/quant firms in NSE & BSE bulk/block deals.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings only")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_sources(p):
        p.add_argument("--sources", help=f"comma list of nse,bse (default: {','.join(config.SOURCES)})")

    p = sub.add_parser("run-daily", help="fetch, match, aggregate, score and report for one date")
    p.add_argument("--date", help="trade date YYYY-MM-DD (default: today IST)")
    p.add_argument("--skip-fetch", action="store_true", help="reprocess stored raw data only")
    p.add_argument("--no-short", action="store_true", help="skip short-selling data")
    p.add_argument("--no-prices", action="store_true", help="skip the price/delivery bhavcopy")
    add_sources(p)
    p.set_defaults(func=cmd_run_daily)

    p = sub.add_parser("fetch", help="fetch and store raw data only")
    p.add_argument("--date", help="trade date YYYY-MM-DD (default: today IST)")
    p.add_argument("--types", default="bulk,block,short,prices",
                   help="comma list: bulk,block,short,prices")
    add_sources(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("process", help="normalize, match, aggregate and score stored raw data")
    p.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("report", help="regenerate the markdown report from the database")
    p.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("backfill", help="run the daily pipeline over a date range")
    p.add_argument("--from-date", required=True, help="start date YYYY-MM-DD")
    p.add_argument("--to-date", required=True, help="end date YYYY-MM-DD")
    p.add_argument("--sleep", type=float, default=2.0, help="seconds between days (default 2)")
    p.add_argument("--include-weekends", action="store_true")
    p.add_argument("--no-short", action="store_true")
    p.add_argument("--no-prices", action="store_true")
    add_sources(p)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("backfill-prices",
                       help="load NSE price/delivery bhavcopies over a date range (seeds ADV & returns)")
    p.add_argument("--from-date", required=True, help="start date YYYY-MM-DD")
    p.add_argument("--to-date", required=True, help="end date YYYY-MM-DD")
    p.add_argument("--sleep", type=float, default=1.0, help="seconds between days (default 1)")
    p.set_defaults(func=cmd_backfill_prices)

    p = sub.add_parser("update-returns", help="recompute post-deal returns over the trailing window")
    p.add_argument("--as-of", help="window end date YYYY-MM-DD (default: today IST)")
    p.add_argument("--lookback-days", type=int, default=None,
                   help=f"calendar days back (default {config.RETURNS_LOOKBACK_DAYS})")
    p.set_defaults(func=cmd_update_returns)

    p = sub.add_parser("firm-stats", help="historical performance summary per tracked firm")
    p.add_argument("--since", help="only include deals on/after this date YYYY-MM-DD")
    p.set_defaults(func=cmd_firm_stats)

    p = sub.add_parser("stocks", help="stock-first view: which stocks tracked firms touched")
    p.add_argument("--date", help="trade date YYYY-MM-DD (default: today IST)")
    p.add_argument("--new-only", action="store_true", help="only first-time mentions")
    p.set_defaults(func=cmd_stocks)

    p = sub.add_parser("alerts", help="show stock alerts (high attention, multi-firm, churn→clean, …)")
    p.add_argument("--date", help="trade date YYYY-MM-DD (default: latest date with alerts)")
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("serve", help="run the stock-first dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--production", action="store_true",
                   help="serve with waitress instead of the Flask dev server")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("refresh-masters", help="refresh NSE/BSE securities masters (ISIN mapping)")
    p.add_argument("--force", action="store_true", help="refresh even if fresh")
    p.set_defaults(func=cmd_refresh_masters)

    p = sub.add_parser("init-db", help="create the SQLite schema")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("seed-firms", help="load/refresh the firm watchlist (idempotent)")
    p.set_defaults(func=cmd_seed_firms)

    p = sub.add_parser("list-firms", help="print the tracked firms and aliases")
    p.set_defaults(func=cmd_list_firms)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.quiet)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR
    except Exception:  # noqa: BLE001 - last-resort guard so cron gets a clean exit code
        log.exception("unhandled error")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
