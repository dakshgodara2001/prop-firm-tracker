"""Server-rendered inline SVG charts — no JS charting library, theme-aware.

Two visuals, both dependency-free (native <title> hover, CSS-variable fills so
they track the terminal theme):

  firm_stock_matrix  — rows=firms, cols=stocks, a dot where a firm touched a
                       stock, sized by that firm's gross value there and
                       coloured by its flow class. Answers "who touched what".
  stock_price_svg    — a stock's close over the sessions on record, with a
                       marker on each day tracked firms were active.

Colour is never the only encoding (position identifies the firm×stock/date;
dot size carries magnitude; hover carries the full read), so the flow palette
is safe here even where two hues sit in the CVD floor band.
"""
from __future__ import annotations

import html
import sqlite3
from typing import Optional

from ..aggregate import CLASS_LABELS

# classification -> CSS custom property (defined in base.html :root)
_FLOW_VAR = {
    "CLEAN_BUY": "--buy",
    "CLEAN_SELL": "--sell",
    "NET_BUY": "--pbuy",
    "NET_SELL": "--psell",
    "MIXED": "--mixed",
    "ROUND_TRIP": "--churn",
}
_SERIES_RANK = ("CASE series WHEN 'EQ' THEN 0 WHEN 'BE' THEN 1 WHEN 'BZ' THEN 2 "
                "WHEN 'SM' THEN 3 WHEN 'ST' THEN 4 ELSE 9 END")


def _cr(v: float) -> str:
    return f"₹{v / 1e7:,.1f} Cr"


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _tipattr(text: str) -> str:
    """Escape a multi-line tooltip string into an HTML attribute value
    (newlines as &#10; so the parser preserves them for `white-space: pre-line`)."""
    return _esc(text).replace("\n", "&#10;")


def _radius(gross: float, gmax: float, rmin: float = 3.5, rmax: float = 11.0) -> float:
    if gmax <= 0:
        return rmin
    return rmin + (rmax - rmin) * (gross / gmax) ** 0.5


def firm_stock_matrix(conn: sqlite3.Connection, trade_date: str,
                      max_stocks: int = 16) -> Optional[dict]:
    """Return {svg, n_firms, n_stocks, truncated} or None if nothing to draw."""
    rows = conn.execute(
        """SELECT a.symbol, f.name AS firm, a.classification,
                  a.buy_value, a.sell_value, a.net_value,
                  (a.buy_value + a.sell_value) AS gross
           FROM daily_firm_stock_agg a JOIN firms f ON f.id = a.firm_id
           WHERE a.trade_date = ?""",
        (trade_date,),
    ).fetchall()
    if not rows:
        return None

    # Column order: stocks by attention (fall back to gross), capped.
    stock_gross: dict = {}
    for r in rows:
        stock_gross[r["symbol"]] = stock_gross.get(r["symbol"], 0.0) + r["gross"]
    attn = {
        r["symbol"]: r["score"]
        for r in conn.execute(
            "SELECT symbol, score FROM attention_scores WHERE trade_date = ?",
            (trade_date,),
        )
    }
    all_stocks = sorted(
        stock_gross, key=lambda s: (-(attn.get(s) or 0), -stock_gross[s])
    )
    truncated = len(all_stocks) > max_stocks
    stocks = all_stocks[:max_stocks]
    stock_set = set(stocks)

    # Row order: firms by total gross across the shown stocks (busiest on top).
    firm_gross: dict = {}
    for r in rows:
        if r["symbol"] in stock_set:
            firm_gross[r["firm"]] = firm_gross.get(r["firm"], 0.0) + r["gross"]
    firms = sorted(firm_gross, key=lambda f: -firm_gross[f])
    if not firms or not stocks:
        return None

    cell = {(r["firm"], r["symbol"]): r for r in rows if r["symbol"] in stock_set}
    gmax = max((r["gross"] for r in rows if r["symbol"] in stock_set), default=0.0)

    # geometry (kept tight — the map shares its row with the picks column)
    padL, padT, pitch, padR, padB = 148, 60, 26, 14, 10
    w = padL + len(stocks) * pitch + padR
    h = padT + len(firms) * pitch + padB
    cx = lambda i: padL + i * pitch + pitch / 2
    cy = lambda j: padT + j * pitch + pitch / 2

    svg = [
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        'role="img" aria-label="Firm by stock activity matrix" '
        'font-family="SF Mono, ui-monospace, Menlo, monospace">'
    ]
    # column (stock) labels, rotated
    for i, s in enumerate(stocks):
        x = cx(i)
        svg.append(
            f'<text x="{x:.1f}" y="{padT - 8}" transform="rotate(-45 {x:.1f} {padT - 8})" '
            f'font-size="10" fill="var(--ink-2)" text-anchor="start">{_esc(s)}</text>'
        )
    # row (firm) labels + faint gridline
    for j, f in enumerate(firms):
        y = cy(j)
        label = f if len(f) <= 22 else f[:21] + "…"
        svg.append(
            f'<text x="{padL - 10}" y="{y + 3:.1f}" font-size="10.5" '
            f'fill="var(--ink-2)" text-anchor="end">{_esc(label)}</text>'
        )
        svg.append(
            f'<line x1="{padL}" y1="{y:.1f}" x2="{w - padR}" y2="{y:.1f}" '
            'stroke="var(--line-2)" stroke-width="1"/>'
        )
    # dots
    for i, s in enumerate(stocks):
        for j, f in enumerate(firms):
            r = cell.get((f, s))
            if r is None:
                continue
            rad = _radius(r["gross"], gmax)
            var = _FLOW_VAR.get(r["classification"], "--churn")
            flow = CLASS_LABELS.get(r["classification"], r["classification"])
            tip = (f"{f}  ·  {s}\n{flow}\n"
                   f"buy {_cr(r['buy_value'])}   sell {_cr(r['sell_value'])}\n"
                   f"net {_cr(r['net_value'])}")
            a = _tipattr(tip)
            svg.append(
                f'<circle cx="{cx(i):.1f}" cy="{cy(j):.1f}" r="{rad:.1f}" '
                f'fill="var({var})" fill-opacity="0.9" stroke="var(--surface)" '
                f'stroke-width="1.5" data-tip="{a}" aria-label="{a}"/>'
            )
    svg.append("</svg>")
    return {
        "svg": "".join(svg),
        "n_firms": len(firms),
        "n_stocks": len(stocks),
        "truncated": truncated,
        "total_stocks": len(all_stocks),
    }


def stock_price_svg(conn: sqlite3.Connection, symbol: str,
                    sessions: int = 40) -> Optional[dict]:
    """Close-price line with markers on days tracked firms were active."""
    rows = conn.execute(
        f"""SELECT trade_date, close FROM stock_history
            WHERE symbol = ? AND close IS NOT NULL
            ORDER BY trade_date DESC, {_SERIES_RANK} ASC""",
        (symbol,),
    ).fetchall()
    # best series per date, newest N, back to ascending
    seen, series = set(), []
    for r in rows:
        if r["trade_date"] in seen:
            continue
        seen.add(r["trade_date"])
        series.append((r["trade_date"], r["close"]))
        if len(series) >= sessions:
            break
    series.reverse()
    if len(series) < 2:
        return None

    touches = {
        r["trade_date"]: r
        for r in conn.execute(
            """SELECT trade_date, classification, firm_count, firm_names,
                      (buy_value + sell_value) AS gross
               FROM daily_stock_agg WHERE symbol = ?""",
            (symbol,),
        )
    }

    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0

    w, h = 720, 240
    padL, padR, padT, padB = 46, 12, 14, 26
    iw, ih = w - padL - padR, h - padT - padB
    n = len(series)
    px = lambda i: padL + (iw * i / (n - 1))
    py = lambda v: padT + ih * (1 - (v - lo) / span)

    svg = [
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
        'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{_esc(symbol)} close price with tracked-firm activity markers" '
        'font-family="SF Mono, ui-monospace, Menlo, monospace">'
    ]
    # y gridlines + labels (lo, mid, hi)
    for frac in (0.0, 0.5, 1.0):
        val = lo + span * frac
        y = py(val)
        svg.append(
            f'<line x1="{padL}" y1="{y:.1f}" x2="{w - padR}" y2="{y:.1f}" '
            'stroke="var(--line-2)" stroke-width="1"/>'
        )
        svg.append(
            f'<text x="{padL - 6}" y="{y + 3:.1f}" font-size="9.5" fill="var(--ink-3)" '
            f'text-anchor="end">{val:,.0f}</text>'
        )
    # price line
    pts = " ".join(f"{px(i):.1f},{py(c):.1f}" for i, c in enumerate(closes))
    svg.append(
        f'<polyline points="{pts}" fill="none" stroke="var(--ink-2)" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    # x labels: first, middle, last
    for i in (0, n // 2, n - 1):
        svg.append(
            f'<text x="{px(i):.1f}" y="{h - 8}" font-size="9" fill="var(--ink-3)" '
            f'text-anchor="middle">{_esc(dates[i][5:])}</text>'
        )
    # markers on firm-touch days
    gmax = max((t["gross"] for t in touches.values()), default=0.0)
    marker_count = 0
    for i, d in enumerate(dates):
        t = touches.get(d)
        if t is None:
            continue
        marker_count += 1
        rad = _radius(t["gross"], gmax, rmin=4.0, rmax=10.0)
        var = _FLOW_VAR.get(t["classification"], "--churn")
        flow = CLASS_LABELS.get(t["classification"], t["classification"])
        tip = (f"{d}\n{flow}  ·  {t['firm_count']} firm(s)\n"
               f"gross {_cr(t['gross'])}\n{t['firm_names']}")
        a = _tipattr(tip)
        svg.append(
            f'<circle cx="{px(i):.1f}" cy="{py(closes[i]):.1f}" r="{rad:.1f}" '
            f'fill="var({var})" fill-opacity="0.92" stroke="var(--bg)" stroke-width="1.5" '
            f'data-tip="{a}" aria-label="{a}"/>'
        )
    svg.append("</svg>")
    return {"svg": "".join(svg), "sessions": n, "markers": marker_count}
