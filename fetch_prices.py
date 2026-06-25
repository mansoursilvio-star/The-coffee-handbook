#!/usr/bin/env python3
"""
fetch_prices.py
Pulls the front-month settlement / last price for KC (arabica) and RC (robusta)
coffee futures and writes them to prices.json.

Runs on GitHub Actions each morning. Free, no API key.
Primary source: Yahoo Finance chart endpoint (KC=F, RC=F).

Notes for Silvio:
- This grabs the FRONT-MONTH contract only (whatever is currently active).
  Yahoo does not expose individual back months for free, so for a specific
  contract (e.g. Dec when the front is Sep) use the paste box in the handbook.
- KC is quoted in US cents per pound (e.g. 275.95).
- RC (robusta) on Yahoo is the ICE London contract in USD per tonne (e.g. 3622).
- "previousClose" is effectively yesterday's settle, which is the pre-open
  number you want. We store both the latest price and the previous close.
"""

import json
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# Yahoo symbols for the two markets (front-month continuous)
SYMBOLS = {
    "kc": "KC=F",   # ICE US arabica, US cents/lb
    "rc": "RC=F",   # ICE London robusta, USD/tonne
}

# a normal browser user-agent: Yahoo's endpoint is friendlier with one set
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d"

# Stooq is a second free source (CSV, very script-friendly). Symbols differ:
# Stooq uses kc.f / rc.f style continuous futures tickers.
STOOQ_SYMBOLS = {"kc": "kc.f", "rc": "rc.f"}
STOOQ_URL = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"


def fetch_stooq(key):
    """Fallback: pull last close from Stooq CSV. Returns same dict shape or None."""
    sym = STOOQ_SYMBOLS.get(key)
    if not sym:
        return None
    url = STOOQ_URL.format(sym=sym)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8").strip()
    except Exception as e:
        print(f"  [warn] Stooq fetch failed for {key}: {e}", file=sys.stderr)
        return None
    # CSV: Symbol,Date,Time,Open,High,Low,Close,Volume
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    cols = lines[1].split(",")
    try:
        close = float(cols[6])
        return {
            "last": round(close, 2),
            "previousClose": None,   # Stooq daily close is the settle itself
            "change": None,
            "contract": "front month",
            "currency": "",
            "via": "stooq",
        }
    except (IndexError, ValueError):
        return None


def fetch_yahoo(symbol):
    """Return a dict with price info for one Yahoo symbol, or None on failure."""
    url = YAHOO_URL.format(sym=urllib.parse.quote(symbol))
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f"  [warn] Yahoo fetch failed for {symbol}: {e}", file=sys.stderr)
        return None

    try:
        result = data["chart"]["result"][0]
        meta = result["meta"]
        # the most recent traded/settle price
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        # contract label if Yahoo provides one (e.g. "Coffee Sep 26")
        name = meta.get("shortName") or meta.get("longName") or ""
        # currency for sanity
        currency = meta.get("currency", "")

        if last is None and prev is None:
            print(f"  [warn] no price fields for {symbol}", file=sys.stderr)
            return None

        # change on the day, if we can compute it
        change = None
        if last is not None and prev is not None:
            change = round(last - prev, 2)

        return {
            "last": round(last, 2) if last is not None else None,
            "previousClose": round(prev, 2) if prev is not None else None,
            "change": change,
            "contract": name,
            "currency": currency,
        }
    except (KeyError, IndexError, TypeError) as e:
        print(f"  [warn] could not parse Yahoo response for {symbol}: {e}", file=sys.stderr)
        return None


def main():
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Yahoo Finance (front-month, delayed)",
        "note": "Front-month only. previousClose is the pre-open settle. "
                "For a specific back month, use the paste box in the handbook.",
        "markets": {},
    }

    any_ok = False
    for key, sym in SYMBOLS.items():
        print(f"Fetching {key.upper()} ({sym})...")
        info = fetch_yahoo(sym)
        if not info:
            print(f"  Yahoo failed, trying Stooq fallback for {key.upper()}...")
            info = fetch_stooq(key)
        if info:
            out["markets"][key] = info
            any_ok = True
            print(f"  {key.upper()}: last={info['last']} prevClose={info.get('previousClose')} "
                  f"chg={info.get('change')} ({info.get('contract','')})")
        else:
            # keep a placeholder so the handbook can show "unavailable" gracefully
            out["markets"][key] = {
                "last": None, "previousClose": None, "change": None,
                "contract": "", "currency": "", "error": "fetch failed",
            }
            print(f"  {key.upper()}: FAILED (both sources)")

    if not any_ok:
        # do not overwrite a good file with a fully-empty one: exit non-zero so
        # the workflow is marked failed and the previous prices.json is kept.
        print("All fetches failed. Not writing prices.json.", file=sys.stderr)
        sys.exit(1)

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("Wrote prices.json")


if __name__ == "__main__":
    main()
