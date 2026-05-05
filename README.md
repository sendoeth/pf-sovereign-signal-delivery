# pf-sovereign-signal-delivery

Sovereign Signal Delivery Pipeline v0 — removes legacy oracle dependency, refreshes frozen price inputs, and applies production policy gates.

## What This Does

1. **Self-hosted signal endpoint** (`signal_endpoint.py`) — generates consumer-ready `/signals/latest` payloads with regime context, confidence scores, VOI inclusion/suppression, and content hashes. No third-party oracle dependency.

2. **Fresh price updater** (`update_prices.py`) — fetches daily closes from Yahoo Finance for all 9 dashboard correlation tickers (NVDA, AMD, AVGO, TSM, MRVL + TAO, RNDR, AKT, FET). Deterministic, outputs `price_update.json` + `dashboard_patch.js`.

3. **Production policy gates** — weak-symbol inversion (SOL: 44% → 56%, p=0.0039), duration-gated confidence (15d cutoff), VOI routing (suppress negative-EV signals), regime filter (suppress direction during SYSTEMIC).

4. **Node.js route patch** (`signals_latest_route.js`) — drop-in integration for existing `signal_api.js`.

## Quick Start

```bash
# Fetch fresh prices
python3 update_prices.py

# Generate signal sample
python3 signal_endpoint.py

# Generate delivery report
python3 generate_report.py

# Verify all artifacts
python3 verify_delivery.py

# Run tests
python3 -m pytest tests/test_delivery.py -v
```

## Verification

```
Total checks: 177
Passed: 177
Grade: A (100.0%)
Tests: 81/81 passing
```

## Schema

Signals endpoint returns `pf-sovereign-signals/v1`:
- `regime`: state, confidence, duration, proximity, policy action
- `signals.published`: consumer-ready signals (VOI-positive)
- `signals.suppressed`: withheld signals (negative expected karma)
- `policy_summary`: inversions, duration gates, suppressions applied
- `metadata.oracle_dependency`: null (sovereign)
- `content_hash`: SHA-256 of signal + regime payload

## Dependencies

Zero. Python 3.8+ stdlib only.
