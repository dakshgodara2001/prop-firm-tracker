"""Local stock-first dashboard (Flask, read-only over the SQLite database).

  /                 home dashboard — the seven discovery panels
  /stock/<symbol>   THE main page: firms, values, flow, history, raw deals,
                    price/volume context, alerts for one stock
  /stocks           searchable index of every stock ever touched
  /firm/<id>        secondary: one firm's recent stocks and totals
  /firms            firm index
  /alerts           alert feed

Run with:  python main.py serve   (default http://127.0.0.1:8050)
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from flask import Flask, g, jsonify, render_template, request

from . import charts
from .. import brief, config
from .. import db as db_module
from ..aggregate import CLASS_LABELS, ROUND_TRIP, STOCK_DIRECTIONAL_CLASSES
from ..alerts import ALERT_LABELS

_SERIES_RANK = ("CASE series WHEN 'EQ' THEN 0 WHEN 'BE' THEN 1 WHEN 'BZ' THEN 2 "
                "WHEN 'SM' THEN 3 WHEN 'ST' THEN 4 ELSE 9 END")


def create_app(db_path=None) -> Flask:
    app = Flask(__name__)
    app.config["PFT_DB_PATH"] = str(db_path or config.DB_PATH)

    # --- plumbing -------------------------------------------------------------

    def conn() -> sqlite3.Connection:
        if "conn" not in g:
            g.conn = db_module.connect(app.config["PFT_DB_PATH"])
        return g.conn

    @app.teardown_appcontext
    def _close(_exc):
        connection = g.pop("conn", None)
        if connection is not None:
            connection.close()

    app.jinja_env.filters.update(
        cr=lambda v: f"{v / 1e7:,.2f}" if v is not None else "–",
        pct=lambda v: f"{v:+.1f}%" if v is not None else "–",
        xadv=lambda v: f"{v:.1f}×" if v is not None else "–",
        num=lambda v: f"{int(v):,}" if v is not None else "–",
        attn=lambda v: f"{v:.0f}" if v is not None else "–",
        price=lambda v: f"{v:,.2f}" if v is not None else "–",
        flow=lambda c: CLASS_LABELS.get(c, c or "–"),
        alert_label=lambda t: ALERT_LABELS.get(t, t),
    )

    @app.context_processor
    def _globals():
        return {
            "HIGH_ATTENTION_MIN": config.HIGH_ATTENTION_MIN,
            "WATCHLIST_MIN_ATTENTION": config.WATCHLIST_MIN_ATTENTION,
        }

    # --- shared queries ----------------------------------------------------------

    def available_dates():
        return [
            r["trade_date"]
            for r in conn().execute(
                "SELECT DISTINCT trade_date FROM daily_stock_agg "
                "ORDER BY trade_date DESC LIMIT 90"
            )
        ]

    # --- routes ----------------------------------------------------------------------

    @app.route("/")
    def dashboard():
        dates = available_dates()
        requested = request.args.get("date")
        the_date: Optional[str] = requested if requested in dates else (dates[0] if dates else None)
        connection = conn()
        stocks = brief.enrich_stocks(connection, the_date) if the_date else []
        kpis = brief.day_kpis(connection, the_date, stocks) if the_date else {}
        alerts_count = kpis.get("alerts", 0) if the_date else 0
        directional = sorted(
            (s for s in stocks if s["classification"] in STOCK_DIRECTIONAL_CLASSES),
            key=lambda s: -abs(s["net_value"]),
        )
        sections = {
            "high": [s for s in stocks if (s["attention"] or 0) >= config.HIGH_ATTENTION_MIN],
            "multi": sorted(
                (s for s in stocks if s["firm_count"] >= 2),
                key=lambda s: (-s["firm_count"], -(s["attention"] or 0)),
            ),
            "directional": directional,
            "churn": sorted(
                (s for s in stocks if s["classification"] == ROUND_TRIP),
                key=lambda s: -s["gross_value"],
            ),
            "new": [s for s in stocks if s["first_time"]],
            "repeat": sorted(
                (s for s in stocks if not s["first_time"] and (s["recent_count"] or 0) > 0),
                key=lambda s: -(s["recent_count"] or 0),
            ),
            "watch": [
                s
                for s in directional
                if (s["attention"] or 0) >= config.WATCHLIST_MIN_ATTENTION or s["first_time"]
            ],
        }
        watch_symbols = {s["symbol"] for s in sections["watch"]}
        repeat_symbols = {s["symbol"] for s in sections["repeat"]}
        for s in stocks:  # row facets for the client-side filter chips
            s["is_high"] = (s["attention"] or 0) >= config.HIGH_ATTENTION_MIN
            s["is_watch"] = s["symbol"] in watch_symbols
            s["is_repeat"] = s["symbol"] in repeat_symbols
            s["is_dir"] = s["classification"] in STOCK_DIRECTIONAL_CLASSES
        week = list(
            reversed(
                connection.execute(
                    """SELECT trade_date, SUM(gross_value) AS gross, COUNT(*) AS n
                       FROM daily_stock_agg GROUP BY trade_date
                       ORDER BY trade_date DESC LIMIT 10"""
                ).fetchall()
            )
        )
        week_max = max((r["gross"] for r in week), default=0) or 1
        stance = brief.stance_line(stocks, kpis) if the_date and stocks else ""
        picks = brief.top_picks(stocks) if stocks else []
        changes = (
            brief.changes_vs_previous(connection, the_date, stocks)
            if the_date
            else {"prev_date": None, "bullets": []}
        )
        raw_rows = (
            connection.execute(
                """SELECT r.*, f.name AS firm_name, f.id AS firm_id
                   FROM raw_deals r
                   JOIN matched_deals m ON m.raw_deal_id = r.id
                   JOIN firms f ON f.id = m.firm_id
                   WHERE r.trade_date = ?
                   ORDER BY r.symbol, f.name, r.side LIMIT 200""",
                (the_date,),
            ).fetchall()
            if the_date
            else []
        )
        fetch_rows = (
            connection.execute(
                """SELECT source, deal_type, status, strategy, row_count, MAX(id)
                   FROM fetch_log WHERE trade_date = ? GROUP BY source, deal_type""",
                (the_date,),
            ).fetchall()
            if the_date
            else []
        )
        matrix = charts.firm_stock_matrix(connection, the_date) if the_date else None
        return render_template(
            "dashboard.html",
            the_date=the_date,
            dates=dates,
            stocks=stocks,
            kpis=kpis,
            stance=stance,
            picks=picks,
            changes=changes,
            sections=sections,
            alerts_count=alerts_count,
            raw_rows=raw_rows,
            fetch_rows=fetch_rows,
            week=week,
            week_max=week_max,
            matrix=matrix,
        )

    @app.route("/google2b499825738e898a.html")
    def google_site_verification():
        return app.send_static_file("google2b499825738e898a.html")

    @app.route("/healthz")
    def healthz():
        try:
            last = conn().execute(
                "SELECT MAX(trade_date) AS d FROM daily_stock_agg"
            ).fetchone()["d"]
            return jsonify({"status": "ok", "last_data_date": last})
        except Exception as exc:  # noqa: BLE001 - health endpoint reports, not raises
            return jsonify({"status": "error", "error": str(exc)}), 500

    @app.route("/stock/<symbol>")
    def stock(symbol: str):
        symbol = symbol.upper()
        appearances = conn().execute(
            """SELECT s.*, a.score AS attention, a.explanation,
                      mc.close, mc.day_return_pct, mc.volume_vs_adv, mc.deliv_per
               FROM daily_stock_agg s
               LEFT JOIN attention_scores a
                 ON a.trade_date = s.trade_date AND a.symbol = s.symbol
               LEFT JOIN market_context mc
                 ON mc.trade_date = s.trade_date AND mc.symbol = s.symbol
               WHERE s.symbol = ?
               ORDER BY s.trade_date DESC""",
            (symbol,),
        ).fetchall()
        if not appearances:
            return render_template("notfound.html", what=f"stock {symbol}"), 404
        latest = appearances[0]
        firm_rows = conn().execute(
            """SELECT a.*, f.id AS firm_id, f.name AS firm_name
               FROM daily_firm_stock_agg a JOIN firms f ON f.id = a.firm_id
               WHERE a.symbol = ? AND a.trade_date = ?
               ORDER BY (a.buy_value + a.sell_value) DESC""",
            (symbol, latest["trade_date"]),
        ).fetchall()
        raw_rows = conn().execute(
            """SELECT r.*, f.name AS firm_name, f.id AS firm_id
               FROM raw_deals r
               JOIN matched_deals m ON m.raw_deal_id = r.id
               JOIN firms f ON f.id = m.firm_id
               WHERE r.symbol = ?
               ORDER BY r.trade_date DESC, f.name, r.side
               LIMIT 200""",
            (symbol,),
        ).fetchall()
        price_all = conn().execute(
            f"""SELECT * FROM stock_history WHERE symbol = ?
                ORDER BY trade_date DESC, {_SERIES_RANK} ASC LIMIT 60""",
            (symbol,),
        ).fetchall()
        prices = []
        for r in price_all:  # best series per date, newest 10 sessions
            if not prices or prices[-1]["trade_date"] != r["trade_date"]:
                prices.append(r)
            if len(prices) >= 10:
                break
        stock_alerts = conn().execute(
            "SELECT * FROM alerts WHERE symbol = ? ORDER BY trade_date DESC, priority LIMIT 25",
            (symbol,),
        ).fetchall()
        pricechart = charts.stock_price_svg(conn(), symbol)
        return render_template(
            "stock.html",
            symbol=symbol,
            latest=latest,
            appearances=appearances,
            firm_rows=firm_rows,
            raw_rows=raw_rows,
            prices=prices,
            stock_alerts=stock_alerts,
            pricechart=pricechart,
        )

    @app.route("/stocks")
    def stocks_index():
        rows = conn().execute(
            """SELECT symbol, MAX(security_name) AS name, COUNT(*) AS appearances,
                      MAX(trade_date) AS last_seen, SUM(gross_value) AS total_gross,
                      SUM(net_value) AS total_net
               FROM daily_stock_agg
               GROUP BY symbol
               ORDER BY last_seen DESC, total_gross DESC"""
        ).fetchall()
        return render_template("stocks.html", rows=rows)

    @app.route("/firm/<int:firm_id>")
    def firm(firm_id: int):
        firm_row = conn().execute("SELECT * FROM firms WHERE id = ?", (firm_id,)).fetchone()
        if firm_row is None:
            return render_template("notfound.html", what=f"firm #{firm_id}"), 404
        aliases = conn().execute(
            "SELECT alias, match_mode FROM firm_aliases WHERE firm_id = ? ORDER BY alias",
            (firm_id,),
        ).fetchall()
        summary = conn().execute(
            """SELECT COUNT(DISTINCT trade_date) AS days,
                      COUNT(DISTINCT symbol)     AS symbols,
                      SUM(buy_value + sell_value) AS gross,
                      SUM(net_value)              AS net,
                      SUM(CASE WHEN classification = 'ROUND_TRIP' THEN 1 ELSE 0 END) AS churn_rows,
                      SUM(CASE WHEN classification != 'ROUND_TRIP' THEN 1 ELSE 0 END) AS directional_rows
               FROM daily_firm_stock_agg WHERE firm_id = ?""",
            (firm_id,),
        ).fetchone()
        recent = conn().execute(
            """SELECT * FROM daily_firm_stock_agg
               WHERE firm_id = ?
               ORDER BY trade_date DESC, (buy_value + sell_value) DESC
               LIMIT 40""",
            (firm_id,),
        ).fetchall()
        return render_template(
            "firm.html", firm=firm_row, aliases=aliases, summary=summary, recent=recent
        )

    @app.route("/firms")
    def firms_index():
        rows = conn().execute(
            """SELECT f.id, f.name,
                      COUNT(DISTINCT a.trade_date) AS days,
                      COUNT(DISTINCT a.symbol)     AS symbols,
                      COALESCE(SUM(a.buy_value + a.sell_value), 0) AS gross,
                      COALESCE(SUM(a.net_value), 0)                AS net,
                      SUM(CASE WHEN a.classification = 'ROUND_TRIP' THEN 1 ELSE 0 END) AS churn_rows,
                      SUM(CASE WHEN a.classification != 'ROUND_TRIP' THEN 1 ELSE 0 END) AS directional_rows
               FROM firms f LEFT JOIN daily_firm_stock_agg a ON a.firm_id = f.id
               GROUP BY f.id
               ORDER BY gross DESC, f.name"""
        ).fetchall()
        return render_template("firms.html", rows=rows)

    @app.route("/alerts")
    def alerts_page():
        rows = conn().execute(
            """SELECT * FROM alerts
               ORDER BY trade_date DESC, priority ASC, id ASC LIMIT 400"""
        ).fetchall()
        by_date: dict = {}
        for r in rows:
            by_date.setdefault(r["trade_date"], []).append(r)
        return render_template("alerts.html", by_date=by_date)

    return app
