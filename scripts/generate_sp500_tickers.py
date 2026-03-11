#!/usr/bin/env python3
"""
Generate an S&P 500 ticker list to data/tickers_top200.txt (default 500 symbols).

Primary source: Wikipedia S&P 500 companies table.
If blocked (e.g., 403), falls back to a public CSV dataset.

Usage (PowerShell):
  python scripts/generate_sp500_tickers.py --out data/tickers_top200.txt --limit 500
"""

from __future__ import annotations

import argparse
import csv
import io
import os
from typing import List

import requests
from bs4 import BeautifulSoup


WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FALLBACK_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

HEADERS = {
    # Spoof a common browser UA to avoid 403s from Wikipedia/CDNs
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate S&P 500 ticker list")
    p.add_argument("--out", default="data/tickers_top200.txt", help="Output path")
    p.add_argument("--limit", type=int, default=500, help="Number of tickers to write")
    return p.parse_args()


def fetch_sp500_tickers() -> List[str]:
    # First try Wikipedia (preferred source)
    try:
        resp = requests.get(WIKI_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", {"id": "constituents"})
        if not table:
            raise RuntimeError("Could not find constituents table on Wikipedia page")
        symbols: List[str] = []
        for row in table.select("tbody tr"):
            cols = row.find_all("td")
            if not cols:
                continue
            sym = cols[0].get_text(strip=True)
            # Wikipedia uses dot for certain tickers (e.g., BRK.B) → Polygon uses . as-is
            symbols.append(sym.upper())
        if symbols:
            return symbols
    except Exception:
        # Fall back to public CSV if Wikipedia blocks us (e.g., 403)
        pass

    # Fallback CSV dataset
    resp = requests.get(FALLBACK_CSV_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    text_stream = io.StringIO(resp.text)
    reader = csv.DictReader(text_stream)
    symbols_csv: List[str] = []
    for row in reader:
        sym = (row.get("Symbol") or row.get("symbol") or "").strip()
        if not sym:
            continue
        symbols_csv.append(sym.upper())
    if not symbols_csv:
        raise RuntimeError("Failed to fetch S&P 500 tickers from both Wikipedia and fallback CSV")
    return symbols_csv


def main() -> None:
    args = parse_args()
    symbols = fetch_sp500_tickers()
    out_dir = os.path.dirname(args.out) or "."
    os.makedirs(out_dir, exist_ok=True)
    use = symbols[: args.limit]
    with open(args.out, "w", encoding="utf-8") as f:
        for s in use:
            f.write(s + "\n")
    print(f"Wrote {len(use)} tickers to {args.out}")


if __name__ == "__main__":
    main()


