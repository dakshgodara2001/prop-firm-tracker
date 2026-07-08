# PFDI — Prop-Firm Deal Intelligence

Tracks which Indian stocks are being traded by proprietary, HFT and quant
trading firms, built entirely on the official **NSE & BSE bulk and block deal
disclosures**.

**Live:** [propfirmsdealz.com](https://propfirmsdealz.com)

## What it does

- Ingests daily NSE (primary) and BSE (secondary) deal disclosures plus price/volume/delivery data
- Matches disclosed counterparties against a curated watchlist of prop/HFT/quant firms
- Rolls activity up per stock: firms involved, buy/sell/gross/net values and a flow classification
- Scores each stock 0–100 by tracked-firm attention, with a plain-English explanation
- Fires rule-based alerts for high-attention days, multi-firm clusters and first-time mentions
- Serves a command-center dashboard and daily markdown reports

## Disclaimer

Attention scores and alerts rank tracked-firm activity — they are not buy/sell
advice and predict no returns.
