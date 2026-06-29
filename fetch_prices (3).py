#!/usr/bin/env python3
"""
fetch_prices.py
Pulls the full futures curve (every listed contract month) for KC (arabica,
ICE US) and RC/RM (robusta, ICE London) coffee futures from Barchart and writes
them to prices.json.

Runs on GitHub Actions each morning. Free, no API key, no third-party packages
(standard library only, so the workflow needs no `pip install`).

How it works
------------
Barchart's public site renders the futures-prices table from an internal JSON
endpoint (/proxies/core-api/v1/quotes/get). That endpoint is protected by a
cross-site-request token: the site sets an `XSRF-TOKEN` cookie on the first page
load and expects it echoed back in an `x-xsrf-token` header. So the flow is:

  1. GET the futures-prices page once to collect cookies (incl. XSRF-TOKEN).
  2. URL-decode the XSRF-TOKEN cookie value.
  3. Call the core-api with that value in the x-xsrf-token header, asking for
     every contract in the root (list=futures.contractInRoot&root=KC / RM).
  4. Parse the JSON rows into a clean per-month list.

Notes for Silvio
----------------
- This now grabs the WHOLE curve, not just the front month. prices.json gets a
  `contracts` list per market (month, last, change, settle, OHLC, volume, OI).
- The front-month summary fields (last/previousClose/change/contract) are kept
  so the handbook's existing "use this" button still works unchanged.
- KC is quoted in US cents per pound (e.g. 273.20).
- RC/RM (robusta) is the ICE London contract in USD per tonne (e.g. 3622).
- `previousPrice` is the prior session's settlement, i.e. the pre-open number.
- If Barchart blocks the runner on a given day, the script falls back to Yahoo
  for the KC front month so prices.json is never wiped to empty.
- The two coffee roots on Barchart: arabica = KC (e.g. KCU26),
  robusta = RM (e.g. RMN26).
"""

import json
import sys
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# A normal browser user-agent. Barchart's endpoints are friendlier with one set,
# and the XSRF flow expects a browser-like client.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# The two coffee roots on Barchart and a sample contract used to land on the
# futures-prices page (any live contract works; it only seeds the cookies).
BARCHART_ROOTS = {
    "kc": {"root": "KC", "seed": "KCU26", "currency": "USX", "name": "Coffee (Arabica)"},
    "rc": {"root": "RM", "seed": "RMN26", "currency": "USD", "name": "Coffee (Robusta)"},
}

# Futures month codes -> short month name.
MONTH_CODES = {
    "F": "Jan", "G": "Feb", "H": "Mar", "J": "Apr", "K": "May", "M": "Jun",
    "N": "Jul", "Q": "Aug", "U": "Sep", "V": "Oct", "X": "Nov", "Z": "Dec",
}

# Fields we ask Barchart for. camelCase names match its core-api schema.
BARCHART_FIELDS = ",".join([
    "symbol", "contractName", "lastPrice", "priceChange", "percentChange",
    "openPrice", "highPrice", "lowPrice", "previousPrice",
    "volume", "openInterest", "tradeTime", "expirationDate",
])

CORE_API = "https://www.barchart.com/proxies/core-api/v1/quotes/get"
PAGE_URL = "https://www.barchart.com/futures/quotes/{seed}/futures-prices?viewName=main"


def month_label_from_symbol(symbol):
    """Derive a 'Mon YY' label from a contract symbol, e.g. KCN26 -> 'Jul 26'.

    The last three characters of a futures symbol are always month-code + two
    digit year, regardless of how long the root is (KC, RM, etc.), so we read
    from the end and don't have to know the root length.
    """
    if not symbol or len(symbol) < 3:
        return ""
    code = symbol[-3].upper()
    year = symbol[-2:]
    mon = MONTH_CODES.get(code)
    return f"{mon} {year}" if mon else ""


def _num(row, key):
    """Read a numeric field from a Barchart row, preferring the raw value.

    With raw=1, each row carries a `raw` object of native numbers alongside the
    formatted display strings. Prefer raw; fall back to parsing the string.
    """
    raw = row.get("raw") or {}
    if key in raw and raw[key] is not None:
        try:
            return float(raw[key])
        except (TypeError, ValueError):
            pass
    val = row.get(key)
    if val in (None, "", "N/A", "unch"):
        return None
    try:
        return float(str(val).replace(",", "").replace("s", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def _round(v, nd=2):
    return round(v, nd) if isinstance(v, (int, float)) else None


def barchart_opener():
    """Build a urllib opener with a cookie jar so cookies persist across calls."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", UA),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    return opener, jar


def fetch_barchart_root(opener, jar, seed, root):
    """Return a list of contract dicts for one root, or [] on failure.

    Seeds cookies by loading the futures-prices page, then calls the core-api
    with the XSRF token echoed back in the header.
    """
    # 1. Land on the futures-prices page to collect cookies (incl. XSRF-TOKEN).
    page = PAGE_URL.format(seed=seed)
    try:
        with opener.open(page, timeout=30) as resp:
            resp.read()  # body unused; we only need the Set-Cookie headers
    except Exception as e:
        print(f"  [warn] Barchart page load failed for {root}: {e}", file=sys.stderr)
        return []

    # 2. Pull the XSRF-TOKEN cookie and URL-decode it (it is percent-encoded).
    xsrf = None
    for c in jar:
        if c.name == "XSRF-TOKEN":
            xsrf = urllib.parse.unquote(c.value)
            break
    if not xsrf:
        print(f"  [warn] no XSRF-TOKEN cookie for {root}; cannot call core-api", file=sys.stderr)
        return []

    # 3. Ask the core-api for every contract in the root.
    params = urllib.parse.urlencode({
        "list": "futures.contractInRoot",
        "root": root,
        "fields": BARCHART_FIELDS,
        "meta": "field.shortName,field.type,field.description",
        "hasOptions": "true",
        "raw": "1",
    })
    api_url = f"{CORE_API}?{params}"
    req = urllib.request.Request(api_url, headers={
        "Accept": "application/json",
        "X-XSRF-TOKEN": xsrf,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": page,
    })
    try:
        with opener.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [warn] Barchart core-api failed for {root}: {e}", file=sys.stderr)
        return []

    rows = payload.get("data") or []
    contracts = []
    for row in rows:
        symbol = row.get("symbol") or (row.get("raw") or {}).get("symbol") or ""
        month = month_label_from_symbol(symbol)
        last = _num(row, "lastPrice")
        prev = _num(row, "previousPrice")
        change = _num(row, "priceChange")
        if change is None and last is not None and prev is not None:
            change = last - prev
        contracts.append({
            "symbol": symbol,
            "month": month,
            "contract": row.get("contractName") or "",
            "last": _round(last),
            "change": _round(change),
            "percentChange": _round(_num(row, "percentChange")),
            "open": _round(_num(row, "openPrice")),
            "high": _round(_num(row, "highPrice")),
            "low": _round(_num(row, "lowPrice")),
            # previousPrice is the prior session's settlement = the pre-open number
            "previousClose": _round(prev),
            "volume": _round(_num(row, "volume"), 0),
            "openInterest": _round(_num(row, "openInterest"), 0),
            "tradeTime": row.get("tradeTime") or "",
        })
    # keep only rows that actually have a month + a price
    contracts = [c for c in contracts if c["month"] and (c["last"] is not None or c["previousClose"] is not None)]
    return contracts


def market_summary(contracts, currency, name):
    """Build the front-month summary block the handbook UI already reads,
    plus attach the full `contracts` curve."""
    front = next((c for c in contracts if c["last"] is not None or c["previousClose"] is not None), None)
    if front is None:
        return None
    return {
        "last": front["last"],
        "previousClose": front["previousClose"],
        "change": front["change"],
        "contract": front["contract"] or front["month"],
        "month": front["month"],
        "currency": currency,
        "name": name,
        "via": "barchart",
        "contracts": contracts,
    }


# ---------------------------------------------------------------------------
# Yahoo fallback (KC front month only) so prices.json is never wiped to empty
# if Barchart blocks the runner on a given day.
# ---------------------------------------------------------------------------
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d"


def fetch_yahoo(symbol):
    url = YAHOO_URL.format(sym=urllib.parse.quote(symbol))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        meta = data["chart"]["result"][0]["meta"]
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if last is None and prev is None:
            return None
        change = round(last - prev, 2) if (last is not None and prev is not None) else None
        return {
            "last": round(last, 2) if last is not None else None,
            "previousClose": round(prev, 2) if prev is not None else None,
            "change": change,
            "contract": meta.get("shortName") or meta.get("longName") or "",
            "currency": meta.get("currency", ""),
            "via": "yahoo (fallback, front-month only)",
            "contracts": [],
        }
    except Exception as e:
        print(f"  [warn] Yahoo fallback failed for {symbol}: {e}", file=sys.stderr)
        return None


def main():
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Barchart futures-prices (full curve, delayed)",
        "note": "Full futures curve per market. previousClose is the prior "
                "session settle (the pre-open number). `contracts` lists every "
                "listed month.",
        "markets": {},
    }

    opener, jar = barchart_opener()
    any_ok = False

    for key, cfg in BARCHART_ROOTS.items():
        print(f"Fetching {key.upper()} (root {cfg['root']}) from Barchart...")
        contracts = fetch_barchart_root(opener, jar, cfg["seed"], cfg["root"])
        summary = market_summary(contracts, cfg["currency"], cfg["name"]) if contracts else None

        if summary:
            out["markets"][key] = summary
            any_ok = True
            print(f"  {key.upper()}: {len(contracts)} contracts. "
                  f"front {summary['month']} last={summary['last']} "
                  f"settle={summary['previousClose']} chg={summary['change']}")
            continue

        # Barchart gave nothing. For KC, try the Yahoo front-month fallback.
        print(f"  Barchart returned no rows for {key.upper()}.")
        if key == "kc":
            print("  Trying Yahoo front-month fallback for KC...")
            yk = fetch_yahoo("KC=F")
            if yk:
                out["markets"][key] = yk
                any_ok = True
                print(f"  KC (Yahoo): last={yk['last']} settle={yk['previousClose']} chg={yk['change']}")
                continue

        out["markets"][key] = {
            "last": None, "previousClose": None, "change": None,
            "contract": "", "currency": cfg["currency"], "name": cfg["name"],
            "contracts": [], "error": "fetch failed",
        }
        print(f"  {key.upper()}: FAILED")

    if not any_ok:
        # Do not overwrite a good file with a fully-empty one: exit non-zero so
        # the workflow is marked failed and the previous prices.json is kept.
        print("All fetches failed. Not writing prices.json.", file=sys.stderr)
        sys.exit(1)

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("Wrote prices.json")


if __name__ == "__main__":
    main()
