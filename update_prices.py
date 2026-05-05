#!/usr/bin/env python3
"""
update_prices.py — Deterministic Fresh-Price Updater

Fetches daily close prices from Yahoo Finance v8 API for the dashboard
correlation arrays (CORR_EQUITY + CORR_CRYPTO), covering the missing
window from March 7, 2026 through present.

Outputs:
  - price_update.json: structured price data with metadata
  - dashboard_patch.js: JS snippet to paste into ai_semi_dashboard.html

Zero external dependencies. Python 3.8+ stdlib only.
"""

import json
import hashlib
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta


# ── Configuration ──────────────────────────────────────────────────

# Tickers aligned with dashboard CORR_EQUITY / CORR_CRYPTO
EQUITY_TICKERS = {
    "NVDA": "NVDA",
    "AMD": "AMD",
    "AVGO": "AVGO",
    "TSM": "TSM",
    "MRVL": "MRVL"
}

# Dashboard uses AI crypto tokens (TAO, RNDR, AKT, FET) not BTC/ETH
CRYPTO_TICKERS = {
    "TAO": "TAO22974-USD",
    "RNDR": "RENDER-USD",
    "AKT": "AKT-USD",
    "FET": "FET-USD"
}

# Dashboard data ends at 2026-03-06. We need March 7 onward.
EXISTING_END_DATE = "2026-03-06"
FETCH_START = "2026-03-07"

YAHOO_BASE = "https://query2.finance.yahoo.com/v8/finance/chart"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


# ── Fetcher ────────────────────────────────────────────────────────

def fetch_yahoo_prices(ticker, start_date, end_date=None):
    """Fetch daily closes from Yahoo Finance v8 API."""
    start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp())

    if end_date:
        end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()) + 86400
    else:
        end_ts = int(datetime.now(timezone.utc).timestamp())

    url = (f"{YAHOO_BASE}/{ticker}?interval=1d"
           f"&period1={start_ts}&period2={end_ts}"
           f"&events=history")

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "ticker": ticker}
    except urllib.error.URLError as e:
        return {"error": str(e.reason), "ticker": ticker}
    except Exception as e:
        return {"error": str(e), "ticker": ticker}

    result = data.get("chart", {}).get("result", [])
    if not result:
        return {"error": "no data in response", "ticker": ticker}

    r = result[0]
    timestamps = r.get("timestamp", [])
    quotes = r.get("indicators", {}).get("quote", [{}])[0]
    closes = quotes.get("close", [])

    if not timestamps or not closes:
        return {"error": "empty timestamps or closes", "ticker": ticker}

    # Build date -> close mapping (trading days only)
    prices = []
    dates = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        # Only include dates after our existing end
        if date_str > EXISTING_END_DATE:
            dates.append(date_str)
            prices.append(round(close, 4))

    return {
        "ticker": ticker,
        "dates": dates,
        "closes": prices,
        "count": len(prices),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None
    }


def compute_hash(data):
    """SHA-256 of canonical JSON."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── Main ───────────────────────────────────────────────────────────

def main():
    output_dir = os.path.dirname(os.path.abspath(__file__))

    print("=" * 60)
    print("Sovereign Signal Delivery — Fresh Price Updater")
    print("=" * 60)
    print(f"Existing data ends: {EXISTING_END_DATE}")
    print(f"Fetching from: {FETCH_START}")
    print(f"Fetching to: today ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})")
    print()

    all_results = {"equity": {}, "crypto": {}}
    all_dates = set()
    errors = []

    # Fetch equity
    print("── Equity Tickers ──")
    for name, ticker in EQUITY_TICKERS.items():
        result = fetch_yahoo_prices(ticker, FETCH_START)
        if "error" in result:
            print(f"  {name} ({ticker}): ERROR - {result['error']}")
            errors.append({"ticker": name, "error": result["error"]})
            all_results["equity"][name] = {"error": result["error"]}
        else:
            print(f"  {name}: {result['count']} days "
                  f"({result['first_date']} → {result['last_date']})")
            all_results["equity"][name] = result
            all_dates.update(result["dates"])

    print()
    print("── Crypto Tickers ──")
    for name, ticker in CRYPTO_TICKERS.items():
        result = fetch_yahoo_prices(ticker, FETCH_START)
        if "error" in result:
            print(f"  {name} ({ticker}): ERROR - {result['error']}")
            errors.append({"ticker": name, "error": result["error"]})
            all_results["crypto"][name] = {"error": result["error"]}
        else:
            print(f"  {name}: {result['count']} days "
                  f"({result['first_date']} → {result['last_date']})")
            all_results["crypto"][name] = result
            all_dates.update(result["dates"])

    # Compute unified date index (sorted trading days present in ALL tickers)
    successful_equity = {k: v for k, v in all_results["equity"].items()
                        if "error" not in v}
    successful_crypto = {k: v for k, v in all_results["crypto"].items()
                        if "error" not in v}

    # Find common dates across all successful fetches
    if successful_equity or successful_crypto:
        date_sets = []
        for v in successful_equity.values():
            date_sets.append(set(v["dates"]))
        for v in successful_crypto.values():
            date_sets.append(set(v["dates"]))

        if date_sets:
            common_dates = sorted(set.intersection(*date_sets)) if date_sets else []
        else:
            common_dates = []
    else:
        common_dates = []

    print()
    print(f"── Summary ──")
    print(f"  Common trading days: {len(common_dates)}")
    if common_dates:
        print(f"  Range: {common_dates[0]} → {common_dates[-1]}")
    print(f"  Errors: {len(errors)}")

    # Build aligned price arrays (only common dates)
    aligned = {"equity": {}, "crypto": {}, "dates": common_dates}

    for name, data in successful_equity.items():
        date_price = dict(zip(data["dates"], data["closes"]))
        aligned["equity"][name] = [date_price.get(d) for d in common_dates]

    for name, data in successful_crypto.items():
        date_price = dict(zip(data["dates"], data["closes"]))
        aligned["crypto"][name] = [date_price.get(d) for d in common_dates]

    # Generate output
    now = datetime.now(timezone.utc).isoformat()

    price_update = {
        "schema": "pf-price-update/v1",
        "generated_at": now,
        "source": "Yahoo Finance v8 API",
        "existing_data_end": EXISTING_END_DATE,
        "new_data_start": common_dates[0] if common_dates else None,
        "new_data_end": common_dates[-1] if common_dates else None,
        "trading_days_added": len(common_dates),
        "coverage": {
            "equity_tickers": list(EQUITY_TICKERS.keys()),
            "crypto_tickers": list(CRYPTO_TICKERS.keys()),
            "equity_success": list(successful_equity.keys()),
            "crypto_success": list(successful_crypto.keys()),
            "errors": errors
        },
        "aligned_dates": common_dates,
        "equity_closes": aligned["equity"],
        "crypto_closes": aligned["crypto"],
        "content_hash": None  # filled below
    }

    # Compute content hash over price data only
    hash_payload = {
        "dates": common_dates,
        "equity": aligned["equity"],
        "crypto": aligned["crypto"]
    }
    price_update["content_hash"] = compute_hash(hash_payload)

    # Write price_update.json
    price_path = os.path.join(output_dir, "price_update.json")
    with open(price_path, "w") as f:
        json.dump(price_update, f, indent=2)
    print(f"\n  Written: {price_path}")

    # Generate dashboard patch (JS arrays to append)
    patch_lines = []
    patch_lines.append("// ── Fresh Price Data Patch ──")
    patch_lines.append(f"// Generated: {now}")
    patch_lines.append(f"// Covers: {common_dates[0] if common_dates else '?'} → "
                      f"{common_dates[-1] if common_dates else '?'}")
    patch_lines.append(f"// Trading days added: {len(common_dates)}")
    patch_lines.append("")
    patch_lines.append("// Append these dates to BT_DATES:")
    patch_lines.append(f"const FRESH_DATES = {json.dumps(common_dates)};")
    patch_lines.append("")
    patch_lines.append("// Append these closes to CORR_EQUITY arrays:")
    for name, closes in aligned["equity"].items():
        rounded = [round(c, 2) if c else 0 for c in closes]
        patch_lines.append(f"const FRESH_{name} = {json.dumps(rounded)};")
    patch_lines.append("")
    patch_lines.append("// Append these closes to CORR_CRYPTO arrays:")
    for name, closes in aligned["crypto"].items():
        rounded = [round(c, 4) if c else 0 for c in closes]
        patch_lines.append(f"const FRESH_{name} = {json.dumps(rounded)};")

    patch_path = os.path.join(output_dir, "dashboard_patch.js")
    with open(patch_path, "w") as f:
        f.write("\n".join(patch_lines) + "\n")
    print(f"  Written: {patch_path}")

    # Print freshness verdict
    print()
    print("── Freshness Verdict ──")
    if common_dates:
        last = datetime.strptime(common_dates[-1], "%Y-%m-%d")
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        staleness = (today - last.replace(tzinfo=timezone.utc)).days
        print(f"  Latest price date: {common_dates[-1]}")
        print(f"  Staleness: {staleness} day(s)")
        if staleness <= 3:
            print(f"  Verdict: FRESH")
        elif staleness <= 7:
            print(f"  Verdict: ACCEPTABLE")
        else:
            print(f"  Verdict: STALE (>{staleness} days)")
    else:
        print("  Verdict: NO_DATA")

    return price_update


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result.get("trading_days_added", 0) > 0 else 1)
