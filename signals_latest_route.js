/**
 * signals_latest_route.js — /signals/latest endpoint patch for signal_api.js
 *
 * Drop-in route that exposes sovereign signal delivery.
 * No oracle dependency. Consumers poll this endpoint directly.
 *
 * Integration: Add to the route handler switch in signal_api.js:
 *   case '/signals/latest': return handleSignalsLatest(req, res);
 *
 * Also add '/signals/latest' to the availableEndpoints array.
 */

// ── Policy Configuration ──────────────────────────────────────────

const WEAK_SYMBOL_POLICY = {
  SOL: {
    action: 'INVERT',
    reason: 'accuracy 44.0%, weakness score 0.6979, inversion to 56.0% (p=0.0039)',
    backtest_accuracy_raw: 0.440,
    backtest_accuracy_inverted: 0.560,
    p_value: 0.0039
  }
};

const DURATION_GATE = {
  enabled: true,
  max_horizon_hours: 360,  // 15 days
  evidence: 'accuracy 38.6% → 58.9% when gating at 15d'
};

const VOI_POLICY = {
  enabled: true,
  min_expected_karma: 0.0,
  evidence: 'VOI routing: +56% karma improvement (relative)'
};

const REGIME_POLICY = {
  SYSTEMIC: { action: 'SUPPRESS_DIRECTION', publish_direction: false },
  NEUTRAL: { action: 'PUBLISH', publish_direction: true },
  EARNINGS: { action: 'SUPPRESS_DIRECTION', publish_direction: false },
  DIVERGENCE: { action: 'SUPPRESS_DIRECTION', publish_direction: false }
};

// Calibrated confidence per symbol/regime (from crypto_calibration.py)
const CALIBRATED = {
  BTC: { NEUTRAL: 0.54, SYSTEMIC: 0.51, EARNINGS: 0.50, DIVERGENCE: 0.50 },
  ETH: { NEUTRAL: 0.56, SYSTEMIC: 0.52, EARNINGS: 0.50, DIVERGENCE: 0.50 },
  SOL: { NEUTRAL: 0.53, SYSTEMIC: 0.49, EARNINGS: 0.50, DIVERGENCE: 0.50 },
  LINK: { NEUTRAL: 0.57, SYSTEMIC: 0.53, EARNINGS: 0.50, DIVERGENCE: 0.50 }
};

// Direction bias per symbol/regime (from crypto_calibration.py)
// +1 = bullish, -1 = bearish, 0 = no edge
const DIRECTION = {
  BTC: { NEUTRAL: -1, SYSTEMIC: 1, EARNINGS: 0, DIVERGENCE: 0 },
  ETH: { NEUTRAL: -1, SYSTEMIC: 1, EARNINGS: 0, DIVERGENCE: 0 },
  SOL: { NEUTRAL: -1, SYSTEMIC: 1, EARNINGS: 0, DIVERGENCE: 0 },
  LINK: { NEUTRAL: -1, SYSTEMIC: 1, EARNINGS: 0, DIVERGENCE: 0 }
};

const SYMBOLS = ['BTC', 'ETH', 'SOL', 'LINK'];

// ── Signal Generation ─────────────────────────────────────────────

function generateSovereignSignals(cache) {
  const now = new Date().toISOString();
  const regime = cache && cache.regime ? cache.regime.id || 'NEUTRAL' : 'NEUTRAL';
  const regimeConf = cache && cache.regime ? cache.regime.confidence || 0 : 0;
  const proximity = cache && cache.regimeProximity ? cache.regimeProximity.score || 0 : 0;
  const duration = cache && cache.regimeProximity ?
    cache.regimeProximity.regimeDurationDays || 0 : 0;
  const horizonHours = 24;

  const regimePolicy = REGIME_POLICY[regime] || REGIME_POLICY.NEUTRAL;
  const signals = [];
  const suppressions = [];
  const inversions = [];

  for (const symbol of SYMBOLS) {
    let confidence = (CALIBRATED[symbol] || {})[regime] || 0.50;
    let direction = (DIRECTION[symbol] || {})[regime] || 0;
    let inverted = false;
    let durationGated = false;

    // Gate 1: Weak-symbol inversion
    if (WEAK_SYMBOL_POLICY[symbol] && WEAK_SYMBOL_POLICY[symbol].action === 'INVERT') {
      const origDir = direction;
      direction = -direction;
      confidence = WEAK_SYMBOL_POLICY[symbol].backtest_accuracy_inverted;
      inverted = true;
      inversions.push({
        symbol, original_direction: origDir,
        inverted_direction: direction,
        reason: WEAK_SYMBOL_POLICY[symbol].reason
      });
    }

    // Gate 2: Duration gating
    if (DURATION_GATE.enabled && horizonHours > DURATION_GATE.max_horizon_hours) {
      confidence *= 0.5;
      durationGated = true;
    }

    // Gate 3: VOI filter
    const expectedKarma = 2 * confidence - 1;
    const voiSuppressed = VOI_POLICY.enabled && expectedKarma < VOI_POLICY.min_expected_karma;

    // Gate 4: Regime filter
    const directionLabel = !regimePolicy.publish_direction ? 'NEUTRAL' :
      (direction > 0 ? 'BULLISH' : (direction < 0 ? 'BEARISH' : 'NEUTRAL'));
    const signalType = regimePolicy.publish_direction ? 'DIRECTIONAL' : 'TRANSITION_TIMING';

    const sig = {
      signal_id: `pf-${symbol}-${Date.now()}`,
      symbol,
      direction: directionLabel,
      signal_type: signalType,
      confidence: Math.round(confidence * 10000) / 10000,
      expected_karma: Math.round(expectedKarma * 10000) / 10000,
      horizon_hours: horizonHours,
      regime,
      regime_confidence: regimeConf,
      regime_duration_days: duration,
      proximity,
      voi_included: !voiSuppressed,
      voi_suppressed: voiSuppressed,
      duration_gated: durationGated,
      weak_symbol_inverted: inverted,
      policy_gates_applied: {
        weak_symbol: inverted,
        duration_gate: durationGated,
        voi_filter: voiSuppressed,
        regime_filter: !regimePolicy.publish_direction
      },
      timestamp: now
    };

    if (voiSuppressed) {
      suppressions.push(sig);
    }
    signals.push(sig);
  }

  const published = signals.filter(s => s.voi_included);
  const suppressed = signals.filter(s => !s.voi_included);

  // Content hash
  const crypto = require('crypto');
  const hashPayload = JSON.stringify({ signals, regime, generated_at: now });
  const contentHash = crypto.createHash('sha256').update(hashPayload).digest('hex');

  return {
    schema: 'pf-sovereign-signals/v1',
    producer_id: 'post-fiat-signals',
    source_wallet: 'rfLJ4ZRnqmGFLAcMvCD56nKGbjpdTJmMqo',
    generated_at: now,
    regime: {
      state: regime,
      confidence: regimeConf,
      duration_days: duration,
      proximity,
      regime_policy: regimePolicy.action
    },
    signals: {
      published,
      suppressed,
      total_generated: signals.length,
      total_published: published.length,
      total_suppressed: suppressed.length
    },
    policy_summary: {
      weak_symbol_inversions: inversions,
      duration_gated_signals: signals.filter(s => s.duration_gated).length,
      voi_suppressions: suppressions.length,
      regime_filter: regimePolicy.action
    },
    metadata: {
      api_source: 'localhost:8080',
      api_reachable: !!cache,
      oracle_dependency: null,
      legacy_oracle_removed: true,
      self_hosted: true,
      horizon_hours: horizonHours
    },
    content_hash: contentHash
  };
}

// ── Route Handler ─────────────────────────────────────────────────

/**
 * Integration instructions:
 *
 * 1. In signal_api.js, add to the URL routing switch:
 *      case '/signals/latest':
 *        const payload = generateSovereignSignals(cachedData);
 *        return jsonResponse(res, 200, payload);
 *
 * 2. Add '/signals/latest' to availableEndpoints array
 *
 * 3. No other dependencies needed — uses existing cachedData from
 *    the Puppeteer refresh cycle.
 */

// Export for integration
if (typeof module !== 'undefined') {
  module.exports = { generateSovereignSignals };
}
