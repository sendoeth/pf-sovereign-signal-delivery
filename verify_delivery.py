#!/usr/bin/env python3
"""
verify_delivery.py — Zero-Trust Verifier for Sovereign Signal Delivery

Validates:
  1. /signals/latest payload schema compliance
  2. Price update coverage and freshness
  3. Policy gate correctness (inversion, duration, VOI, regime)
  4. No legacy oracle dependency
  5. Content hash integrity
  6. Regime engine freshness (not operating on frozen inputs)

Zero external dependencies. Python 3.8+ stdlib only.
"""

import json
import hashlib
import os
import sys
from datetime import datetime, timezone


# ── Schema Definitions ─────────────────────────────────────────────

REQUIRED_SIGNAL_FIELDS = [
    "signal_id", "symbol", "direction", "signal_type",
    "confidence", "expected_karma", "horizon_hours",
    "regime", "regime_confidence", "regime_duration_days",
    "proximity", "voi_included", "voi_suppressed",
    "duration_gated", "weak_symbol_inverted",
    "policy_gates_applied", "timestamp"
]

REQUIRED_PAYLOAD_FIELDS = [
    "schema", "producer_id", "source_wallet", "generated_at",
    "regime", "signals", "policy_summary", "metadata", "content_hash"
]

REQUIRED_REGIME_FIELDS = [
    "state", "confidence", "duration_days", "proximity", "regime_policy"
]

REQUIRED_METADATA_FIELDS = [
    "api_source", "api_reachable", "oracle_dependency",
    "legacy_oracle_removed", "self_hosted"
]

VALID_DIRECTIONS = ["BULLISH", "BEARISH", "NEUTRAL"]
VALID_SIGNAL_TYPES = ["DIRECTIONAL", "TRANSITION_TIMING"]
VALID_REGIMES = ["NEUTRAL", "SYSTEMIC", "EARNINGS", "DIVERGENCE", "UNKNOWN"]
VALID_SYMBOLS = ["BTC", "ETH", "SOL", "LINK"]

PRICE_UPDATE_REQUIRED_FIELDS = [
    "schema", "generated_at", "source", "existing_data_end",
    "new_data_start", "new_data_end", "trading_days_added",
    "coverage", "aligned_dates", "equity_closes", "crypto_closes",
    "content_hash"
]


# ── Verifier Classes ──────────────────────────────────────────────

class CheckResult:
    def __init__(self, category, check_name, passed, detail=""):
        self.category = category
        self.check_name = check_name
        self.passed = passed
        self.detail = detail

    def __repr__(self):
        icon = "PASS" if self.passed else "FAIL"
        return f"[{icon}] {self.category}/{self.check_name}: {self.detail}"


class SignalPayloadVerifier:
    """Verifies /signals/latest payload."""

    def __init__(self, payload):
        self.payload = payload
        self.results = []

    def check(self, category, name, condition, detail=""):
        self.results.append(CheckResult(category, name, condition, detail))

    def verify_structure(self):
        """Verify top-level structure."""
        cat = "structure"

        self.check(cat, "is_dict",
                   isinstance(self.payload, dict),
                   f"type={type(self.payload).__name__}")

        if not isinstance(self.payload, dict):
            return

        for field in REQUIRED_PAYLOAD_FIELDS:
            self.check(cat, f"has_{field}",
                       field in self.payload,
                       f"{'present' if field in self.payload else 'MISSING'}")

        self.check(cat, "schema_version",
                   self.payload.get("schema") == "pf-sovereign-signals/v1",
                   f"got: {self.payload.get('schema')}")

        self.check(cat, "producer_id",
                   self.payload.get("producer_id") == "post-fiat-signals",
                   f"got: {self.payload.get('producer_id')}")

    def verify_regime(self):
        """Verify regime block."""
        cat = "regime"
        regime = self.payload.get("regime", {})

        for field in REQUIRED_REGIME_FIELDS:
            self.check(cat, f"has_{field}",
                       field in regime,
                       f"{'present' if field in regime else 'MISSING'}")

        state = regime.get("state", "")
        self.check(cat, "valid_state",
                   state in VALID_REGIMES,
                   f"got: {state}")

        conf = regime.get("confidence", -1)
        self.check(cat, "confidence_range",
                   0 <= conf <= 100,
                   f"got: {conf}")

        duration = regime.get("duration_days", -1)
        self.check(cat, "duration_non_negative",
                   duration >= 0,
                   f"got: {duration}")

        proximity = regime.get("proximity", -1)
        self.check(cat, "proximity_range",
                   0 <= proximity <= 1,
                   f"got: {proximity}")

    def verify_signals(self):
        """Verify signal arrays."""
        cat = "signals"
        signals = self.payload.get("signals", {})

        self.check(cat, "has_published",
                   "published" in signals,
                   "present" if "published" in signals else "MISSING")

        self.check(cat, "has_suppressed",
                   "suppressed" in signals,
                   "present" if "suppressed" in signals else "MISSING")

        published = signals.get("published", [])
        suppressed = signals.get("suppressed", [])
        total = signals.get("total_generated", 0)

        self.check(cat, "total_matches",
                   len(published) + len(suppressed) == total,
                   f"published={len(published)} + suppressed={len(suppressed)} vs total={total}")

        self.check(cat, "count_consistency",
                   signals.get("total_published", 0) == len(published),
                   f"total_published={signals.get('total_published')} vs actual={len(published)}")

        # Verify each signal
        all_signals = published + suppressed
        for i, sig in enumerate(all_signals):
            self._verify_single_signal(sig, i)

    def _verify_single_signal(self, sig, idx):
        """Verify individual signal structure."""
        cat = f"signal_{idx}"

        for field in REQUIRED_SIGNAL_FIELDS:
            self.check(cat, f"has_{field}",
                       field in sig,
                       f"{'present' if field in sig else 'MISSING'}")

        symbol = sig.get("symbol", "")
        self.check(cat, "valid_symbol",
                   symbol in VALID_SYMBOLS,
                   f"got: {symbol}")

        direction = sig.get("direction", "")
        self.check(cat, "valid_direction",
                   direction in VALID_DIRECTIONS,
                   f"got: {direction}")

        sig_type = sig.get("signal_type", "")
        self.check(cat, "valid_signal_type",
                   sig_type in VALID_SIGNAL_TYPES,
                   f"got: {sig_type}")

        confidence = sig.get("confidence", -1)
        self.check(cat, "confidence_range",
                   0 <= confidence <= 1,
                   f"got: {confidence}")

        karma = sig.get("expected_karma", -99)
        self.check(cat, "karma_range",
                   -1 <= karma <= 1,
                   f"got: {karma}")

        # Verify VOI consistency
        voi_inc = sig.get("voi_included", None)
        voi_sup = sig.get("voi_suppressed", None)
        self.check(cat, "voi_consistency",
                   voi_inc is not None and voi_sup is not None and voi_inc != voi_sup,
                   f"included={voi_inc}, suppressed={voi_sup}")

        # Verify karma/VOI relationship
        if voi_sup:
            self.check(cat, "voi_karma_negative",
                       karma < 0,
                       f"suppressed but karma={karma}")

    def verify_policy(self):
        """Verify policy gates were applied correctly."""
        cat = "policy"
        ps = self.payload.get("policy_summary", {})

        self.check(cat, "has_weak_symbol",
                   "weak_symbol_inversions" in ps,
                   "present" if "weak_symbol_inversions" in ps else "MISSING")

        # Check SOL was inverted
        inversions = ps.get("weak_symbol_inversions", [])
        sol_inverted = any(i.get("symbol") == "SOL" for i in inversions)
        self.check(cat, "sol_inverted",
                   sol_inverted,
                   f"SOL inversion applied: {sol_inverted}")

        # Check regime filter is active during SYSTEMIC
        regime_state = self.payload.get("regime", {}).get("state", "")
        if regime_state == "SYSTEMIC":
            # All signals should be TRANSITION_TIMING type
            published = self.payload.get("signals", {}).get("published", [])
            suppressed = self.payload.get("signals", {}).get("suppressed", [])
            all_sigs = published + suppressed
            all_transition = all(
                s.get("signal_type") == "TRANSITION_TIMING"
                for s in all_sigs
            )
            self.check(cat, "systemic_suppresses_direction",
                       all_transition,
                       f"all TRANSITION_TIMING: {all_transition}")

    def verify_sovereignty(self):
        """Verify no oracle dependency."""
        cat = "sovereignty"
        meta = self.payload.get("metadata", {})

        for field in REQUIRED_METADATA_FIELDS:
            self.check(cat, f"has_{field}",
                       field in meta,
                       f"{'present' if field in meta else 'MISSING'}")

        self.check(cat, "no_oracle_dependency",
                   meta.get("oracle_dependency") is None,
                   f"got: {meta.get('oracle_dependency')}")

        self.check(cat, "legacy_removed",
                   meta.get("legacy_oracle_removed") is True,
                   f"got: {meta.get('legacy_oracle_removed')}")

        self.check(cat, "self_hosted",
                   meta.get("self_hosted") is True,
                   f"got: {meta.get('self_hosted')}")

    def verify_hash(self):
        """Verify content hash integrity."""
        cat = "integrity"

        content_hash = self.payload.get("content_hash")
        self.check(cat, "has_content_hash",
                   content_hash is not None and len(content_hash) == 64,
                   f"length={len(content_hash) if content_hash else 0}")

        # Recompute
        signals = self.payload.get("signals", {})
        all_sigs = signals.get("published", []) + signals.get("suppressed", [])
        hash_data = {
            "signals": all_sigs,
            "regime": self.payload.get("regime", {}),
            "generated_at": self.payload.get("generated_at", "")
        }
        computed = hashlib.sha256(
            json.dumps(hash_data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        self.check(cat, "hash_matches",
                   content_hash == computed,
                   f"stored={content_hash[:16]}... computed={computed[:16]}...")

    def verify_all(self):
        """Run all verification checks."""
        self.verify_structure()
        if not isinstance(self.payload, dict):
            return self.results

        self.verify_regime()
        self.verify_signals()
        self.verify_policy()
        self.verify_sovereignty()
        self.verify_hash()

        return self.results


class PriceUpdateVerifier:
    """Verifies price_update.json."""

    def __init__(self, data):
        self.data = data
        self.results = []

    def check(self, category, name, condition, detail=""):
        self.results.append(CheckResult(category, name, condition, detail))

    def verify_structure(self):
        """Verify top-level structure."""
        cat = "price_structure"

        self.check(cat, "is_dict",
                   isinstance(self.data, dict),
                   f"type={type(self.data).__name__}")

        if not isinstance(self.data, dict):
            return

        for field in PRICE_UPDATE_REQUIRED_FIELDS:
            self.check(cat, f"has_{field}",
                       field in self.data,
                       f"{'present' if field in self.data else 'MISSING'}")

        self.check(cat, "schema_version",
                   self.data.get("schema") == "pf-price-update/v1",
                   f"got: {self.data.get('schema')}")

    def verify_coverage(self):
        """Verify price coverage."""
        cat = "price_coverage"

        days = self.data.get("trading_days_added", 0)
        self.check(cat, "has_trading_days",
                   days > 0,
                   f"trading_days_added={days}")

        # Should cover at least some of March-May 2026
        start = self.data.get("new_data_start", "")
        end = self.data.get("new_data_end", "")
        self.check(cat, "start_after_march_6",
                   start > "2026-03-06" if start else False,
                   f"start={start}")

        self.check(cat, "end_recent",
                   end >= "2026-04-01" if end else False,
                   f"end={end}")

        # Check equity coverage
        equity = self.data.get("equity_closes", {})
        expected_equity = ["NVDA", "AMD", "AVGO", "TSM", "MRVL"]
        for ticker in expected_equity:
            self.check(cat, f"equity_{ticker}",
                       ticker in equity and len(equity.get(ticker, [])) > 0,
                       f"{'present' if ticker in equity else 'MISSING'}, "
                       f"n={len(equity.get(ticker, []))}")

        # Check crypto coverage
        crypto = self.data.get("crypto_closes", {})
        expected_crypto = ["TAO", "RNDR", "AKT", "FET"]
        for ticker in expected_crypto:
            self.check(cat, f"crypto_{ticker}",
                       ticker in crypto and len(crypto.get(ticker, [])) > 0,
                       f"{'present' if ticker in crypto else 'MISSING'}, "
                       f"n={len(crypto.get(ticker, []))}")

    def verify_freshness(self):
        """Verify data is not stale."""
        cat = "price_freshness"

        end = self.data.get("new_data_end", "")
        if end:
            end_date = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            staleness = (now - end_date).days

            self.check(cat, "staleness_acceptable",
                       staleness <= 7,
                       f"staleness={staleness} days (end={end})")

            self.check(cat, "not_frozen_march",
                       end > "2026-03-06",
                       f"end={end} (was frozen at 2026-03-06)")
        else:
            self.check(cat, "has_end_date", False, "no end date")

    def verify_hash(self):
        """Verify content hash."""
        cat = "price_integrity"

        content_hash = self.data.get("content_hash")
        self.check(cat, "has_content_hash",
                   content_hash is not None and len(content_hash) == 64,
                   f"length={len(content_hash) if content_hash else 0}")

        # Recompute
        hash_payload = {
            "dates": self.data.get("aligned_dates", []),
            "equity": self.data.get("equity_closes", {}),
            "crypto": self.data.get("crypto_closes", {})
        }
        computed = hashlib.sha256(
            json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        self.check(cat, "hash_matches",
                   content_hash == computed,
                   f"stored={content_hash[:16]}... computed={computed[:16]}...")

    def verify_all(self):
        """Run all checks."""
        self.verify_structure()
        if not isinstance(self.data, dict):
            return self.results
        self.verify_coverage()
        self.verify_freshness()
        self.verify_hash()
        return self.results


class DeliveryReportVerifier:
    """Verifies sovereign_signal_delivery_report.json."""

    def __init__(self, report):
        self.report = report
        self.results = []

    def check(self, category, name, condition, detail=""):
        self.results.append(CheckResult(category, name, condition, detail))

    def verify_all(self):
        cat = "report"

        self.check(cat, "is_dict",
                   isinstance(self.report, dict),
                   f"type={type(self.report).__name__}")

        if not isinstance(self.report, dict):
            return self.results

        required = [
            "schema", "generated_at", "before_state", "after_state",
            "price_refresh", "regime_status", "signal_sample",
            "policy_gates", "source_hashes"
        ]

        for field in required:
            self.check(cat, f"has_{field}",
                       field in self.report,
                       f"{'present' if field in self.report else 'MISSING'}")

        # Check before/after shows improvement
        before = self.report.get("before_state", {})
        after = self.report.get("after_state", {})

        self.check(cat, "before_has_oracle_dep",
                   before.get("oracle_dependency") is not None,
                   f"before oracle: {before.get('oracle_dependency')}")

        self.check(cat, "after_no_oracle_dep",
                   after.get("oracle_dependency") is None,
                   f"after oracle: {after.get('oracle_dependency')}")

        self.check(cat, "before_frozen_data",
                   before.get("price_data_end") == "2026-03-06",
                   f"before price end: {before.get('price_data_end')}")

        self.check(cat, "after_fresh_data",
                   (after.get("price_data_end", "") or "") > "2026-03-06",
                   f"after price end: {after.get('price_data_end')}")

        # Check signal sample
        sample = self.report.get("signal_sample", {})
        self.check(cat, "has_sample_signals",
                   "published" in sample or "signals" in sample,
                   "has signal data")

        # Check source hashes
        hashes = self.report.get("source_hashes", {})
        self.check(cat, "has_price_hash",
                   "price_update" in hashes,
                   f"{'present' if 'price_update' in hashes else 'MISSING'}")

        self.check(cat, "has_signal_hash",
                   "signal_payload" in hashes,
                   f"{'present' if 'signal_payload' in hashes else 'MISSING'}")

        return self.results


# ── Main Verification Runner ──────────────────────────────────────

def verify_all(base_dir=None):
    """Run all verifiers against generated artifacts."""
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    all_results = []
    categories = {}

    print("=" * 60)
    print("Sovereign Signal Delivery — Zero-Trust Verifier")
    print("=" * 60)

    # 1. Verify signal payload
    signal_path = os.path.join(base_dir, "signals_latest_sample.json")
    if os.path.exists(signal_path):
        print(f"\n── Verifying: signals_latest_sample.json ──")
        with open(signal_path) as f:
            payload = json.load(f)
        v = SignalPayloadVerifier(payload)
        results = v.verify_all()
        all_results.extend(results)
        for r in results:
            categories.setdefault(r.category, []).append(r)
    else:
        print(f"\n  SKIP: {signal_path} not found")

    # 2. Verify price update
    price_path = os.path.join(base_dir, "price_update.json")
    if os.path.exists(price_path):
        print(f"\n── Verifying: price_update.json ──")
        with open(price_path) as f:
            price_data = json.load(f)
        v = PriceUpdateVerifier(price_data)
        results = v.verify_all()
        all_results.extend(results)
        for r in results:
            categories.setdefault(r.category, []).append(r)
    else:
        print(f"\n  SKIP: {price_path} not found")

    # 3. Verify delivery report
    report_path = os.path.join(base_dir, "sovereign_signal_delivery_report.json")
    if os.path.exists(report_path):
        print(f"\n── Verifying: sovereign_signal_delivery_report.json ──")
        with open(report_path) as f:
            report = json.load(f)
        v = DeliveryReportVerifier(report)
        results = v.verify_all()
        all_results.extend(results)
        for r in results:
            categories.setdefault(r.category, []).append(r)
    else:
        print(f"\n  SKIP: {report_path} not found")

    # Summary
    total = len(all_results)
    passed = sum(1 for r in all_results if r.passed)
    failed = sum(1 for r in all_results if not r.passed)

    print(f"\n{'=' * 60}")
    print(f"VERIFICATION SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total checks: {total}")
    print(f"  Passed: {passed}")
    print(f"  Failed: {failed}")
    print(f"  Grade: {'A' if failed == 0 else 'B' if failed <= 3 else 'C' if failed <= 10 else 'F'}")
    print(f"  Score: {passed}/{total} ({100*passed/total:.1f}%)" if total > 0 else "  Score: N/A")

    # Per-category breakdown
    print(f"\n── Per-Category Results ──")
    for cat, results in sorted(categories.items()):
        cat_passed = sum(1 for r in results if r.passed)
        cat_total = len(results)
        status = "PASS" if cat_passed == cat_total else "FAIL"
        print(f"  [{status}] {cat}: {cat_passed}/{cat_total}")

    # Print failures
    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n── Failures ──")
        for r in failures:
            print(f"  {r}")

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "grade": "A" if failed == 0 else "B" if failed <= 3 else "C" if failed <= 10 else "F",
        "categories": {cat: {"passed": sum(1 for r in res if r.passed),
                            "total": len(res)}
                      for cat, res in categories.items()}
    }


if __name__ == "__main__":
    result = verify_all()
    sys.exit(0 if result["failed"] == 0 else 1)
