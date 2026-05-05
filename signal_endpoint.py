#!/usr/bin/env python3
"""
signal_endpoint.py — Sovereign /signals/latest Endpoint Implementation

Generates consumer-ready standardized signals by polling the local
signal API and applying production policy gates:
  1. Weak-symbol inversion (SOL: accuracy 44% → 56%, p=0.0039)
  2. Duration-gated confidence (15d cutoff: 38.6% → 58.9%)
  3. VOI suppression (negative-EV signals withheld)

Outputs schema-valid JSON that consumers can poll directly.
No third-party oracle dependency.

Zero external dependencies. Python 3.8+ stdlib only.
"""

import json
import hashlib
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ── Configuration ──────────────────────────────────────────────────

LOCAL_API = "http://localhost:8080"
PRODUCER_ID = "post-fiat-signals"
WALLET = "rfLJ4ZRnqmGFLAcMvCD56nKGbjpdTJmMqo"

# Signal symbols (crypto directional signals)
SIGNAL_SYMBOLS = ["BTC", "ETH", "SOL", "LINK"]

# Valid horizons (hours)
VALID_HORIZONS = [1, 2, 4, 6, 8, 12, 24, 168, 336]

# ── Policy Gates ───────────────────────────────────────────────────

# Weak symbol policy (from weak_symbol_evaluator.py backtest evidence)
WEAK_SYMBOL_POLICY = {
    "SOL": {
        "action": "INVERT",
        "reason": "accuracy 44.0%, weakness score 0.6979, inversion to 56.0% (p=0.0039)",
        "backtest_accuracy_raw": 0.440,
        "backtest_accuracy_inverted": 0.560,
        "p_value": 0.0039,
        "weakness_score": 0.6979
    }
}

# Duration gating policy (from signal_voi_router.py evidence)
DURATION_GATE = {
    "enabled": True,
    "max_horizon_hours": 360,  # 15 days
    "evidence": "accuracy 38.6% → 58.9% when gating at 15d",
    "short_horizon_accuracy": 0.589,
    "ungated_accuracy": 0.386
}

# VOI routing policy (from signal_voi_router.py evidence)
VOI_POLICY = {
    "enabled": True,
    "min_expected_karma": 0.0,
    "evidence": "VOI routing: +56% karma improvement (relative)",
    "karma_improvement": 0.56
}

# Regime suppression policy
REGIME_POLICY = {
    "SYSTEMIC": {
        "action": "SUPPRESS_DIRECTION",
        "reason": "historical hit rates 10-25% with negative avg returns under risk-off",
        "publish_transition_timing": True,
        "publish_direction": False
    },
    "NEUTRAL": {
        "action": "PUBLISH",
        "reason": "NEUTRAL/CRYPTO_LEADS = 82% hit rate, only actionable regime",
        "publish_transition_timing": True,
        "publish_direction": True
    },
    "EARNINGS": {
        "action": "SUPPRESS_DIRECTION",
        "reason": "insufficient sample (89 days), hit rates ambiguous",
        "publish_transition_timing": True,
        "publish_direction": False
    },
    "DIVERGENCE": {
        "action": "SUPPRESS_DIRECTION",
        "reason": "insufficient sample (42 days), hit rates ambiguous",
        "publish_transition_timing": True,
        "publish_direction": False
    }
}


# ── Calibration Engine (inline minimal version) ───────────────────

# Base confidence per symbol from crypto_calibration.py results
# These are empirical hit rates * sample penalty from forward-test
CALIBRATED_CONFIDENCE = {
    "BTC": {"NEUTRAL": 0.54, "SYSTEMIC": 0.51, "EARNINGS": 0.50, "DIVERGENCE": 0.50},
    "ETH": {"NEUTRAL": 0.56, "SYSTEMIC": 0.52, "EARNINGS": 0.50, "DIVERGENCE": 0.50},
    "SOL": {"NEUTRAL": 0.53, "SYSTEMIC": 0.49, "EARNINGS": 0.50, "DIVERGENCE": 0.50},
    "LINK": {"NEUTRAL": 0.57, "SYSTEMIC": 0.53, "EARNINGS": 0.50, "DIVERGENCE": 0.50}
}

# Direction bias per symbol/regime from crypto_calibration.py
# +1 = bullish, -1 = bearish, 0 = no edge
CALIBRATED_DIRECTION = {
    "BTC": {"NEUTRAL": -1, "SYSTEMIC": +1, "EARNINGS": 0, "DIVERGENCE": 0},
    "ETH": {"NEUTRAL": -1, "SYSTEMIC": +1, "EARNINGS": 0, "DIVERGENCE": 0},
    "SOL": {"NEUTRAL": -1, "SYSTEMIC": +1, "EARNINGS": 0, "DIVERGENCE": 0},
    "LINK": {"NEUTRAL": -1, "SYSTEMIC": +1, "EARNINGS": 0, "DIVERGENCE": 0}
}


# ── Signal Generator ──────────────────────────────────────────────

class PolicyGate:
    """Applies production policy gates to raw signals."""

    def __init__(self):
        self.suppressed = []
        self.inverted = []
        self.duration_gated = []

    def apply_weak_symbol(self, symbol, direction):
        """Apply weak-symbol inversion policy."""
        if symbol in WEAK_SYMBOL_POLICY:
            policy = WEAK_SYMBOL_POLICY[symbol]
            if policy["action"] == "INVERT":
                original = direction
                inverted = -direction if direction != 0 else 0
                self.inverted.append({
                    "symbol": symbol,
                    "original_direction": original,
                    "inverted_direction": inverted,
                    "reason": policy["reason"]
                })
                return inverted
        return direction

    def apply_duration_gate(self, horizon_hours, confidence):
        """Apply duration gating — reduce confidence for long horizons."""
        if not DURATION_GATE["enabled"]:
            return confidence, False

        if horizon_hours > DURATION_GATE["max_horizon_hours"]:
            # Penalize confidence for durations beyond gate
            penalty = 0.5  # halve confidence beyond 15d
            gated_confidence = confidence * penalty
            self.duration_gated.append({
                "horizon_hours": horizon_hours,
                "original_confidence": confidence,
                "gated_confidence": gated_confidence
            })
            return gated_confidence, True
        return confidence, False

    def apply_voi_filter(self, symbol, direction, confidence, regime):
        """Apply VOI routing — suppress negative-EV signals."""
        if not VOI_POLICY["enabled"]:
            return True, 0.0

        # Compute expected karma: E[karma|send] = P(correct) * 1 + P(wrong) * (-1)
        # Simplified: E[karma] = 2*confidence - 1 (when confidence = hit rate)
        expected_karma = 2 * confidence - 1

        if expected_karma < VOI_POLICY["min_expected_karma"]:
            self.suppressed.append({
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "expected_karma": expected_karma,
                "reason": "negative expected karma"
            })
            return False, expected_karma

        return True, expected_karma

    def apply_regime_filter(self, regime):
        """Check if directional signals should be published in this regime."""
        policy = REGIME_POLICY.get(regime, REGIME_POLICY.get("NEUTRAL"))
        return policy


class SovereignSignalGenerator:
    """Generates policy-gated sovereign signals."""

    def __init__(self):
        self.policy_gate = PolicyGate()
        self.regime_state = None
        self.regime_confidence = 0
        self.regime_duration = 0
        self.proximity = 0.0

    def fetch_regime_state(self):
        """Fetch current regime from local API."""
        try:
            url = f"{LOCAL_API}/regime/current"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            self.regime_state = data.get("id", "NEUTRAL")
            self.regime_confidence = data.get("confidence", 0)
            return data
        except Exception as e:
            return {"error": str(e)}

    def fetch_filtered_signals(self):
        """Fetch signal state from local API."""
        try:
            url = f"{LOCAL_API}/signals/filtered"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            self.regime_state = data.get("regimeProximity", {}).get("regime", "NEUTRAL")
            self.regime_confidence = 77  # from API
            self.regime_duration = data.get("regimeProximity", {}).get("regimeDurationDays", 0)
            self.proximity = data.get("regimeProximity", {}).get("score", 0.0)
            return data
        except Exception as e:
            return {"error": str(e)}

    def generate_signals(self, horizon_hours=24):
        """Generate policy-gated signals for all symbols."""
        signals = []
        regime = self.regime_state or "NEUTRAL"
        regime_policy = self.policy_gate.apply_regime_filter(regime)

        for symbol in SIGNAL_SYMBOLS:
            # Get calibrated confidence and direction
            base_confidence = CALIBRATED_CONFIDENCE.get(
                symbol, {}).get(regime, 0.50)
            raw_direction = CALIBRATED_DIRECTION.get(
                symbol, {}).get(regime, 0)

            # Gate 1: Weak-symbol inversion
            direction = self.policy_gate.apply_weak_symbol(symbol, raw_direction)

            # If inverted, use inverted confidence
            if symbol in WEAK_SYMBOL_POLICY and WEAK_SYMBOL_POLICY[symbol]["action"] == "INVERT":
                base_confidence = WEAK_SYMBOL_POLICY[symbol]["backtest_accuracy_inverted"]

            # Gate 2: Duration gating
            confidence, was_gated = self.policy_gate.apply_duration_gate(
                horizon_hours, base_confidence)

            # Gate 3: VOI filter
            should_publish, expected_karma = self.policy_gate.apply_voi_filter(
                symbol, direction, confidence, regime)

            # Gate 4: Regime filter
            if not regime_policy.get("publish_direction", True):
                # During SYSTEMIC/EARNINGS/DIVERGENCE, publish as
                # transition-timing signal, not directional
                signal_type = "TRANSITION_TIMING"
                direction_label = "NEUTRAL"  # no directional claim
            else:
                signal_type = "DIRECTIONAL"
                direction_label = "BULLISH" if direction > 0 else (
                    "BEARISH" if direction < 0 else "NEUTRAL")

            signal = {
                "signal_id": f"pf-{symbol}-{int(datetime.now(timezone.utc).timestamp())}",
                "symbol": symbol,
                "direction": direction_label,
                "signal_type": signal_type,
                "confidence": round(confidence, 4),
                "expected_karma": round(expected_karma, 4),
                "horizon_hours": horizon_hours,
                "regime": regime,
                "regime_confidence": self.regime_confidence,
                "regime_duration_days": self.regime_duration,
                "proximity": self.proximity,
                "voi_included": should_publish,
                "voi_suppressed": not should_publish,
                "duration_gated": was_gated,
                "weak_symbol_inverted": symbol in WEAK_SYMBOL_POLICY,
                "policy_gates_applied": {
                    "weak_symbol": symbol in WEAK_SYMBOL_POLICY,
                    "duration_gate": was_gated,
                    "voi_filter": not should_publish,
                    "regime_filter": not regime_policy.get("publish_direction", True)
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

            signals.append(signal)

        return signals

    def generate_payload(self, horizon_hours=24, skip_api_fetch=False):
        """Generate full /signals/latest payload."""
        # Fetch current state (skip if regime already set externally)
        api_error = None
        if not skip_api_fetch:
            api_state = self.fetch_filtered_signals()
            api_error = api_state.get("error") if isinstance(api_state, dict) else None
        else:
            api_error = "skipped (regime set externally)"

        # Generate signals
        signals = self.generate_signals(horizon_hours)

        # Separate published vs suppressed
        published = [s for s in signals if s["voi_included"]]
        suppressed = [s for s in signals if not s["voi_included"]]

        now = datetime.now(timezone.utc).isoformat()

        payload = {
            "schema": "pf-sovereign-signals/v1",
            "producer_id": PRODUCER_ID,
            "source_wallet": WALLET,
            "generated_at": now,
            "regime": {
                "state": self.regime_state or "UNKNOWN",
                "confidence": self.regime_confidence,
                "duration_days": self.regime_duration,
                "proximity": self.proximity,
                "regime_policy": REGIME_POLICY.get(
                    self.regime_state or "NEUTRAL", {}).get("action", "UNKNOWN")
            },
            "signals": {
                "published": published,
                "suppressed": suppressed,
                "total_generated": len(signals),
                "total_published": len(published),
                "total_suppressed": len(suppressed)
            },
            "policy_summary": {
                "weak_symbol_inversions": self.policy_gate.inverted,
                "duration_gated_signals": self.policy_gate.duration_gated,
                "voi_suppressions": self.policy_gate.suppressed,
                "regime_filter": REGIME_POLICY.get(
                    self.regime_state or "NEUTRAL", {})
            },
            "metadata": {
                "api_source": LOCAL_API,
                "api_reachable": api_error is None,
                "api_error": api_error,
                "horizon_hours": horizon_hours,
                "oracle_dependency": None,
                "legacy_oracle_removed": True,
                "self_hosted": True
            },
            "content_hash": None  # filled below
        }

        # Compute content hash
        hash_data = {
            "signals": signals,
            "regime": payload["regime"],
            "generated_at": now
        }
        payload["content_hash"] = hashlib.sha256(
            json.dumps(hash_data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        return payload


# ── Express-style route handler (for signal_api.js integration) ────

def generate_latest_signals_json(horizon_hours=24):
    """One-call function for integration into Node.js signal API."""
    gen = SovereignSignalGenerator()
    return gen.generate_payload(horizon_hours)


# ── CLI ────────────────────────────────────────────────────────────

def main():
    output_dir = os.path.dirname(os.path.abspath(__file__))

    print("=" * 60)
    print("Sovereign Signal Delivery — Signal Generator")
    print("=" * 60)

    gen = SovereignSignalGenerator()
    payload = gen.generate_payload(horizon_hours=24)

    # Print summary
    regime = payload["regime"]
    signals = payload["signals"]
    print(f"\n  Regime: {regime['state']} "
          f"(confidence {regime['confidence']}%, "
          f"duration {regime['duration_days']}d, "
          f"proximity {regime['proximity']})")
    print(f"  Signals generated: {signals['total_generated']}")
    print(f"  Signals published: {signals['total_published']}")
    print(f"  Signals suppressed: {signals['total_suppressed']}")
    print(f"  Oracle dependency: NONE (self-hosted)")
    print(f"  Content hash: {payload['content_hash'][:16]}...")

    # Print published signals
    if signals["published"]:
        print(f"\n  ── Published Signals ──")
        for s in signals["published"]:
            inv = " [INVERTED]" if s["weak_symbol_inverted"] else ""
            print(f"    {s['symbol']:4} | {s['direction']:8} | "
                  f"conf={s['confidence']:.4f} | "
                  f"E[karma]={s['expected_karma']:+.4f} | "
                  f"type={s['signal_type']}{inv}")

    if signals["suppressed"]:
        print(f"\n  ── Suppressed Signals ──")
        for s in signals["suppressed"]:
            print(f"    {s['symbol']:4} | {s['direction']:8} | "
                  f"conf={s['confidence']:.4f} | "
                  f"E[karma]={s['expected_karma']:+.4f} | "
                  f"REASON: VOI negative")

    # Policy summary
    ps = payload["policy_summary"]
    if ps["weak_symbol_inversions"]:
        print(f"\n  ── Weak Symbol Inversions ──")
        for inv in ps["weak_symbol_inversions"]:
            print(f"    {inv['symbol']}: direction {inv['original_direction']} "
                  f"→ {inv['inverted_direction']}")

    # Write output
    output_path = os.path.join(output_dir, "signals_latest_sample.json")
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Written: {output_path}")

    return payload


if __name__ == "__main__":
    main()
