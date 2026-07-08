"""Markdown daily report — a desk-style morning brief, not a data dump.

Concise, ranked and opinionated: a stance line, the top stocks to watch with
reasons, what changed versus the previous session, curated activity tables,
what to ignore as churn, and a follow-up checklist for the trading day. The
complete tables and the raw-deal audit trail live in the appendix.

Nothing here is buy/sell advice — the brief ranks tracked-firm attention so
the reader can decide what to look at before the open.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional

from . import brief, config, masters
from .aggregate import CLASS_LABELS as _CLASS_LABELS
from .aggregate import DIRECTIONAL_CLASSES, ROUND_TRIP, STOCK_DIRECTIONAL_CLASSES
from .dates import ist_now, parse_iso
from .models import DEAL_BULK


def _fmt_int(n) -> str:
    return f"{int(n):,}" if n is not None else "-"


def _fmt_cr(value) -> str:
    """Rupee value shown in crore (1 Cr = 1e7)."""
    return f"{value / 1e7:,.2f}" if value is not None else "-"


def _fmt_price(p) -> str:
    return f"{p:,.2f}" if p is not None else "-"


def _fmt_pct(p, signed: bool = True) -> str:
    if p is None:
        return "-"
    return f"{p:+.1f}%" if signed else f"{p:.1f}%"


def _fmt_x(v) -> str:
    return f"{v:.1f}×" if v is not None else "-"


def _fmt_attention(s) -> str:
    return f"{s:.0f}" if s is not None else "-"


def _flow(row) -> str:
    label = _CLASS_LABELS.get(row["classification"], row["classification"])
    if row["classification"] != ROUND_TRIP and row["churn_firm_count"]:
        label += " +churn"
    return label


def _table(headers: List[str], rows: List[List[str]]) -> List[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


def generate_report(conn: sqlite3.Connection, trade_date: str) -> str:
    day = parse_iso(trade_date)
    lines: List[str] = []
    add = lines.append

    stocks = brief.enrich_stocks(conn, trade_date)
    kpis = brief.day_kpis(conn, trade_date, stocks)
    directional = sorted(
        (s for s in stocks if s["classification"] in STOCK_DIRECTIONAL_CLASSES),
        key=lambda s: -abs(s["net_value"]),
    )
    churn = sorted(
        (s for s in stocks if s["classification"] == ROUND_TRIP),
        key=lambda s: -s["gross_value"],
    )
    watchlist = [
        s for s in directional
        if (s["attention"] or 0) >= config.WATCHLIST_MIN_ATTENTION or s["first_time"]
    ]
    picks = brief.top_picks(stocks)
    changes = brief.changes_vs_previous(conn, trade_date, stocks)
    checklist = brief.follow_up_checklist(conn, trade_date, stocks, watchlist)

    add(f"# Morning Brief — Tracked Prop-Firm Deal Activity — {trade_date} ({day.strftime('%A')})")
    add("")
    add(
        f"_Generated {ist_now().strftime('%Y-%m-%d %H:%M IST')} · Sources: NSE (primary) "
        "& BSE (secondary) official disclosures · Attention ranks activity; it is "
        "**not** buy/sell advice._"
    )
    add("")

    # --- 1 · Daily Overview -----------------------------------------------------
    add("## 1 · Daily Overview")
    add("")
    if not stocks:
        note = " (weekend)" if day.weekday() >= 5 else ""
        add(f"**No tracked prop/HFT firms appeared in NSE/BSE bulk or block deals on {trade_date}{note}.**")
        add("")
    else:
        add(f"**{brief.stance_line(stocks, kpis)}**")
        add("")
        lines.extend(
            _table(
                ["Stocks", "Firms", "Gross ₹Cr", "Directional |net| ₹Cr", "Churn share",
                 "Multi-firm", "New", "Repeat", "Alerts"],
                [[
                    kpis["stocks"], kpis["firms"], _fmt_cr(kpis["gross"]),
                    _fmt_cr(kpis["directional_net"]), f"{kpis['churn_share']:.0%}",
                    kpis["multi_firm"], kpis["new_mentions"], kpis["repeat_mentions"],
                    kpis["alerts"],
                ]],
            )
        )
        add("")

    # --- 2 · Top Stocks to Watch ---------------------------------------------------
    add("## 2 · Top Stocks to Watch")
    add("")
    if picks:
        for i, s in enumerate(picks, 1):
            kind = (
                " *(liquidity event, not directional)*"
                if s["pick_kind"] == "liquidity_event"
                else ""
            )
            add(
                f"{i}. **{s['symbol']}** — attention {_fmt_attention(s['attention'])}, "
                f"{_flow(s)}{kind}. {s['explanation'] or ''}"
            )
        add("")
    elif stocks:
        add("_Nothing warrants elevated attention — routine churn only (section 7)._")
        add("")
    else:
        add("_None._")
        add("")

    # --- 3 · What Changed ---------------------------------------------------------------
    prev_label = f" vs {changes['prev_date']}" if changes["prev_date"] else ""
    add(f"## 3 · What Changed{prev_label}")
    add("")
    if changes["prev_date"] and changes["bullets"]:
        for bullet in changes["bullets"]:
            add(f"- {bullet}")
        add("")
    elif changes["prev_date"]:
        add("_No overlap with the previous session — an entirely fresh set of names._")
        add("")
    else:
        add("_First stored session — nothing to compare against yet._")
        add("")

    # --- 4 · Clean Net Buy / Sell Activity ----------------------------------------------
    add("## 4 · Clean Net Buy / Sell Activity")
    add("")
    if directional:
        lines.extend(
            _table(
                ["Symbol", "Flow", "Attention", "Deals", "Net ₹Cr", "Gross ₹Cr", "New", "Who"],
                [
                    [f"**{s['symbol']}**", _flow(s), _fmt_attention(s["attention"]),
                     s["deal_types"] or "-", _fmt_cr(s["net_value"]), _fmt_cr(s["gross_value"]),
                     "★" if s["first_time"] else "", s["firm_names"]]
                    for s in directional
                ],
            )
        )
        add("")
        add(
            "_Clean = one side only. Partial net = residual after two-sided trading. "
            "Mixed = firms directionally on both sides. \"+churn\" = other tracked "
            "firms round-tripped the same stock._"
        )
    else:
        add("_None — every tracked-firm appearance today was round-trip churn._")
    add("")

    # --- 5 · Multi-Firm Activity ------------------------------------------------------------
    add("## 5 · Multi-Firm Activity")
    add("")
    multi = sorted(
        (s for s in stocks if s["firm_count"] >= 2),
        key=lambda s: (-s["firm_count"], -(s["attention"] or 0)),
    )
    if multi:
        lines.extend(
            _table(
                ["Symbol", "Firms", "Attention", "Flow", "Gross ₹Cr", "Who"],
                [
                    [f"**{s['symbol']}**", s["firm_count"], _fmt_attention(s["attention"]),
                     _flow(s), _fmt_cr(s["gross_value"]), s["firm_names"]]
                    for s in multi
                ],
            )
        )
        add("")
        add(
            "_Several tracked firms in one stock on one day usually marks an event "
            "(result, block window, index change) — worth knowing regardless of direction._"
        )
    else:
        add("_None._")
    add("")

    # --- 6 · New & Repeat Mentions --------------------------------------------------------------
    add("## 6 · New & Repeat Mentions")
    add("")
    new_rows = [s for s in stocks if s["first_time"]]
    repeat_rows = sorted(
        (s for s in stocks if not s["first_time"] and (s["recent_count"] or 0) > 0),
        key=lambda s: -(s["recent_count"] or 0),
    )
    if new_rows:
        add(f"**New (★, first time in stored history): {len(new_rows)}**")
        add("")
        lines.extend(
            _table(
                ["Symbol", "Attention", "Flow", "Gross ₹Cr", "Who"],
                [
                    [f"**{s['symbol']}**", _fmt_attention(s["attention"]), _flow(s),
                     _fmt_cr(s["gross_value"]), s["firm_names"]]
                    for s in new_rows
                ],
            )
        )
        add("")
    else:
        add("_No new mentions._")
        add("")
    if repeat_rows:
        add("**Repeat mentions:** " + "; ".join(
            f"**{s['symbol']}** ({(s['recent_count'] or 0) + 1} sessions in "
            f"{config.ATTENTION_REPEAT_WINDOW_DAYS}d)"
            for s in repeat_rows
        ))
        add("")

    # --- 7 · Round-Trip / Liquidity Churn ------------------------------------------------------------
    add("## 7 · Round-Trip / Liquidity Churn — ignore unless event-hunting")
    add("")
    if churn:
        lines.extend(
            _table(
                ["Symbol", "Firms", "Deals", "Gross ₹Cr", "Net ₹Cr", "Day move",
                 "Vol vs ADV", "New", "Who"],
                [
                    [f"**{s['symbol']}**", s["firm_count"], s["deal_types"] or "-",
                     _fmt_cr(s["gross_value"]), _fmt_cr(s["net_value"]),
                     _fmt_pct(s["day_return_pct"]), _fmt_x(s["volume_vs_adv"]),
                     "★" if s["first_time"] else "", s["firm_names"]]
                    for s in churn
                ],
            )
        )
        add("")
        add(
            "_These firms bought and sold roughly equal quantities intraday — "
            "market-making / arbitrage turnover. Read it as a liquidity or event-day "
            "flag, **not** as a bullish or bearish signal. Safe to ignore for "
            "direction-hunting._"
        )
    else:
        add("_None._")
    add("")

    # --- 8 · Watchlist Additions --------------------------------------------------------------------
    add("## 8 · Watchlist Additions")
    add("")
    if watchlist:
        add(
            "Directional stocks worth putting on today's watch list "
            f"(attention ≥ {config.WATCHLIST_MIN_ATTENTION:.0f} or first-time):"
        )
        add("")
        for s in watchlist:
            add(f"- **{s['symbol']}** (attention {_fmt_attention(s['attention'])}) — "
                f"{s['explanation'] or ''}")
        add("")
        add("_These rank attention, not expected returns — decide with your own process._")
    else:
        add("_None today — nothing directional cleared the bar._")
    add("")

    # --- 9 · Follow-Up Checklist -------------------------------------------------------------------------
    add("## 9 · Follow-Up Checklist")
    add("")
    for item in checklist:
        add(f"- [ ] {item}")
    add("")

    # --- 10 · Appendix ---------------------------------------------------------------------------------------
    add("---")
    add("")
    add("## 10 · Appendix — Full Data & Audit Trail")
    add("")

    if stocks:
        add("### All stocks touched (ranked by attention)")
        add("")
        lines.extend(
            _table(
                ["Symbol", "Attention", "Flow", "Deals", "Firms", "Buy ₹Cr", "Sell ₹Cr",
                 "Gross ₹Cr", "Net ₹Cr", "Day move", "Vol vs ADV", "New"],
                [
                    [f"**{s['symbol']}**", _fmt_attention(s["attention"]), _flow(s),
                     s["deal_types"] or "-", s["firm_count"], _fmt_cr(s["buy_value"]),
                     _fmt_cr(s["sell_value"]), _fmt_cr(s["gross_value"]),
                     _fmt_cr(s["net_value"]), _fmt_pct(s["day_return_pct"]),
                     _fmt_x(s["volume_vs_adv"]), "★" if s["first_time"] else ""]
                    for s in stocks
                ],
            )
        )
        add("")
        add("**What happened, stock by stock:**")
        add("")
        for s in stocks:
            add(f"- **{s['symbol']}** (attention {_fmt_attention(s['attention'])}) — "
                f"{s['explanation'] or 'No explanation computed.'}")
        add("")

    fetch_rows = conn.execute(
        """SELECT source, deal_type, status, strategy, row_count, message, MAX(id)
           FROM fetch_log WHERE trade_date = ?
           GROUP BY source, deal_type""",
        (trade_date,),
    ).fetchall()
    add("### Fetch status")
    add("")
    if fetch_rows:
        lines.extend(
            _table(
                ["Source", "Report", "Status", "Strategy", "Rows", "Note"],
                [
                    [r["source"], r["deal_type"], r["status"], r["strategy"] or "-",
                     r["row_count"], (r["message"] or "-").replace("|", "/")]
                    for r in fetch_rows
                ],
            )
        )
    else:
        add("_No fetch attempts recorded for this date (processed from cache?)._")
    add("")

    overview = conn.execute(
        """SELECT source, deal_type,
                  COUNT(*)                                deal_rows,
                  COUNT(DISTINCT symbol)                  symbols,
                  COUNT(DISTINCT client_name)             clients,
                  SUM(quantity * COALESCE(price, 0))      gross_value
           FROM raw_deals WHERE trade_date = ?
           GROUP BY source, deal_type ORDER BY source, deal_type""",
        (trade_date,),
    ).fetchall()
    short_count = conn.execute(
        "SELECT COUNT(*) FROM short_selling WHERE trade_date = ?", (trade_date,)
    ).fetchone()[0]
    add("### Market overview (all participants)")
    add("")
    if overview:
        lines.extend(
            _table(
                ["Source", "Report", "Deal rows", "Symbols", "Clients", "Gross value (₹ Cr)"],
                [
                    [r["source"], r["deal_type"], r["deal_rows"], r["symbols"], r["clients"],
                     _fmt_cr(r["gross_value"] or 0)]
                    for r in overview
                ],
            )
        )
    else:
        add(f"_No bulk/block deals stored for {trade_date}._")
    if short_count:
        add("")
        add(f"Short-selling disclosures: **{short_count}** symbols.")
    add("")

    detail = conn.execute(
        """SELECT f.name AS firm_name, r.source, r.symbol, r.deal_type, r.side,
                  r.quantity, r.price, r.client_name, m.match_method, m.matched_alias
           FROM matched_deals m
           JOIN raw_deals r ON r.id = m.raw_deal_id
           JOIN firms f ON f.id = m.firm_id
           WHERE r.trade_date = ?
           ORDER BY f.name, r.symbol, r.deal_type, r.side""",
        (trade_date,),
    ).fetchall()
    if detail:
        add("### Raw deals — tracked firms, as disclosed (match audit)")
        add("")
        current_firm = None
        for r in detail:
            if r["firm_name"] != current_firm:
                if current_firm is not None:
                    add("")
                current_firm = r["firm_name"]
                add(f"**{current_firm}**")
                add("")
                lines.extend(
                    _table(
                        ["Symbol", "Src", "Type", "Side", "Qty", "Price", "Value (₹ Cr)",
                         "Disclosed client name", "Match"],
                        [],
                    )
                )
            value = r["quantity"] * (r["price"] or 0)
            add(
                "| "
                + " | ".join(
                    [
                        r["symbol"], r["source"], r["deal_type"], r["side"],
                        _fmt_int(r["quantity"]), _fmt_price(r["price"]), _fmt_cr(value),
                        r["client_name"], f"{r['match_method']} ({r['matched_alias']})",
                    ]
                )
                + " |"
            )
        add("")

    placeholders_cls = ",".join("?" * len(DIRECTIONAL_CLASSES))
    recent = conn.execute(
        f"""SELECT a.trade_date, f.name AS firm_name, a.symbol, a.net_qty, a.net_value,
                   r.r1, r.r3, r.r5, r.r10
            FROM daily_firm_stock_agg a
            JOIN firms f ON f.id = a.firm_id
            LEFT JOIN deal_returns r ON r.agg_id = a.id
            WHERE a.trade_date < ? AND a.trade_date >= date(?, '-14 days')
              AND a.classification IN ({placeholders_cls})
            ORDER BY a.trade_date DESC, ABS(a.net_value) DESC
            LIMIT 15""",
        [trade_date, trade_date, *DIRECTIONAL_CLASSES],
    ).fetchall()
    if recent:
        add("### Price follow-up on recent directional days (last 14 days)")
        add("")
        rows = []
        for r in recent:
            direction = "BUY" if r["net_qty"] > 0 else "SELL"
            rows.append(
                [
                    r["trade_date"], r["firm_name"], r["symbol"], direction,
                    _fmt_cr(abs(r["net_value"])),
                    _fmt_pct(r["r1"]), _fmt_pct(r["r3"]), _fmt_pct(r["r5"]), _fmt_pct(r["r10"]),
                ]
            )
        lines.extend(
            _table(
                ["Date", "Firm", "Symbol", "Dir", "|Net| ₹ Cr", "T+1", "T+3", "T+5", "T+10"],
                rows,
            )
        )
        add("")
        add(
            "_How the stock's close moved k sessions after the firm's own entry "
            "(VWAP); '-' = not enough sessions elapsed yet. Descriptive history, "
            "not a prediction._"
        )
        add("")

    top = conn.execute(
        """SELECT source, symbol, client_name, side, quantity, price,
                  quantity * COALESCE(price, 0) AS value
           FROM raw_deals
           WHERE trade_date = ? AND deal_type = ?
           ORDER BY value DESC LIMIT 5""",
        (trade_date, DEAL_BULK),
    ).fetchall()
    if top:
        add("### Largest bulk deals market-wide (all participants)")
        add("")
        lines.extend(
            _table(
                ["Source", "Symbol", "Client", "Side", "Qty", "Price", "Value (₹ Cr)"],
                [
                    [r["source"], r["symbol"], r["client_name"], r["side"],
                     _fmt_int(r["quantity"]), _fmt_price(r["price"]), _fmt_cr(r["value"])]
                    for r in top
                ],
            )
        )
        add("")

    unmapped = masters.unmapped_symbols(conn, trade_date)
    add("---")
    if unmapped:
        add(
            f"_BSE-only symbols without an NSE ISIN mapping (no price context): "
            f"{', '.join(unmapped)}._"
        )
        add("")
    add(
        "_Definitions: bulk deal = client crosses 0.5% of a company's listed equity "
        "in a day (aggregated disclosure); block deal = single negotiated trade in "
        "the block window (≥ ₹10 Cr). Values are quantity × disclosed price (WATP "
        "for bulk deals), so they approximate turnover. ADV20 = mean traded volume "
        "over the prior 20 sessions from the NSE bhavcopy. Attention scores rank "
        "tracked-firm activity; they are not investment advice._"
    )
    add("")
    return "\n".join(lines)


def write_report(conn: sqlite3.Connection, trade_date: str,
                 reports_dir: Optional[Path] = None) -> Path:
    out_dir = Path(reports_dir or config.REPORTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"daily_{trade_date}.md"
    path.write_text(generate_report(conn, trade_date), encoding="utf-8")
    return path
