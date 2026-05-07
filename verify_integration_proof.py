#!/usr/bin/env python3
"""
verify_integration_proof.py — Independent Verifier for Signal Stack Integration Proof

Validates a signal_stack_integration_proof.json artifact against 12 verification
categories and 80+ checks. Produces a dated verdict with pass/fail counts.

Zero external dependencies. Python 3.8+ stdlib only.

Usage:
    python3 verify_integration_proof.py signal_stack_integration_proof.json
    python3 verify_integration_proof.py signal_stack_integration_proof.json --live
"""

import hashlib
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone


# ── Helpers ────────────────────────────────────────────────────────

def sha256(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def check(check_id, passed, detail, severity="LOW", remediation=None):
    return {
        "check_id": check_id,
        "passed": passed,
        "detail": detail,
        "severity": severity,
        "remediation": remediation
    }


# ── Verification Categories ───────────────────────────────────────

def verify_structure(proof):
    """Category 1: Top-level structure and required fields."""
    checks = []
    required = [
        "schema", "generated_at", "date", "proof_type",
        "endpoint_evidence", "endpoint_survey", "consumer_audit",
        "integration_summary", "source_hashes", "rerun_instructions"
    ]
    for field in required:
        checks.append(check(
            f"STRUCT_{field.upper()}",
            field in proof,
            f"Required field '{field}' {'present' if field in proof else 'missing'}",
            severity="CRITICAL" if field not in proof else "LOW"
        ))

    checks.append(check(
        "STRUCT_SCHEMA_VALUE",
        proof.get("schema") == "pf-signal-stack-integration-proof/v1",
        f"Schema: {proof.get('schema')}",
        severity="HIGH"
    ))

    checks.append(check(
        "STRUCT_PROOF_TYPE",
        proof.get("proof_type") == "live_integration_audit",
        f"Proof type: {proof.get('proof_type')}",
        severity="HIGH"
    ))

    return {"category": "structure", "checks": checks}


def verify_timestamp(proof):
    """Category 2: Date and timestamp validity."""
    checks = []

    gen_at = proof.get("generated_at", "")
    try:
        ts = datetime.fromisoformat(gen_at)
        checks.append(check("TS_PARSEABLE", True, f"Timestamp parseable: {gen_at}"))
        # Check not in the future (with 5 min tolerance)
        now = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        future = (ts - now).total_seconds() > 300
        checks.append(check(
            "TS_NOT_FUTURE",
            not future,
            f"Timestamp {'is in the future' if future else 'is not in the future'}",
            severity="HIGH" if future else "LOW"
        ))
        # Check not too old (30 days)
        stale = (now - ts).total_seconds() > 30 * 86400
        checks.append(check(
            "TS_NOT_STALE",
            not stale,
            f"Timestamp {'is >30d old' if stale else 'within 30d'}",
            severity="MEDIUM" if stale else "LOW"
        ))
    except (ValueError, TypeError):
        checks.append(check("TS_PARSEABLE", False, f"Cannot parse: {gen_at}", severity="CRITICAL"))

    date_str = proof.get("date", "")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        checks.append(check("TS_DATE_FORMAT", True, f"Date format valid: {date_str}"))
    except ValueError:
        checks.append(check("TS_DATE_FORMAT", False, f"Invalid date: {date_str}", severity="HIGH"))

    return {"category": "timestamp", "checks": checks}


def verify_endpoint_evidence(proof):
    """Category 3: Endpoint evidence completeness and validity."""
    checks = []
    ev = proof.get("endpoint_evidence", {})

    # Required fields
    ep_required = [
        "endpoint", "public_endpoint", "external_access_verified",
        "curl_command", "curl_timestamp", "http_status",
        "response_body_sha256", "content_hash", "schema_version",
        "producer_id", "source_wallet", "regime_state",
        "signals_published", "self_hosted", "signals_detail",
        "policy_summary"
    ]
    for field in ep_required:
        checks.append(check(
            f"EP_{field.upper()}",
            field in ev and ev[field] is not None,
            f"'{field}' {'present' if field in ev else 'missing'}",
            severity="HIGH" if field not in ev else "LOW"
        ))

    # HTTP status should be 200
    checks.append(check(
        "EP_HTTP_200",
        ev.get("http_status") == 200,
        f"HTTP status: {ev.get('http_status')}",
        severity="CRITICAL"
    ))

    # Self-hosted should be true
    checks.append(check(
        "EP_SELF_HOSTED",
        ev.get("self_hosted") is True,
        f"Self-hosted: {ev.get('self_hosted')}",
        severity="HIGH"
    ))

    # External access verified
    checks.append(check(
        "EP_EXTERNAL_ACCESS",
        ev.get("external_access_verified") is True,
        f"External access: {ev.get('external_access_verified')}",
        severity="MEDIUM"
    ))

    # Oracle dependency should be null/None
    checks.append(check(
        "EP_NO_ORACLE",
        ev.get("oracle_dependency") is None,
        f"Oracle dependency: {ev.get('oracle_dependency')}",
        severity="MEDIUM"
    ))

    # Legacy oracle removed
    checks.append(check(
        "EP_LEGACY_REMOVED",
        ev.get("legacy_oracle_removed") is True,
        f"Legacy oracle removed: {ev.get('legacy_oracle_removed')}",
        severity="MEDIUM"
    ))

    # Content hash format (hex64)
    content_hash = ev.get("content_hash", "")
    checks.append(check(
        "EP_HASH_FORMAT",
        len(content_hash) == 64 and all(c in "0123456789abcdef" for c in content_hash),
        f"Content hash format: {'valid hex64' if len(content_hash) == 64 else 'invalid'}",
        severity="MEDIUM"
    ))

    # Response body SHA-256 format
    body_hash = ev.get("response_body_sha256", "")
    checks.append(check(
        "EP_BODY_HASH_FORMAT",
        len(body_hash) == 64 and all(c in "0123456789abcdef" for c in body_hash),
        f"Body hash format: {'valid hex64' if len(body_hash) == 64 else 'invalid'}",
        severity="MEDIUM"
    ))

    # Source wallet format (XRP address starts with r)
    wallet = ev.get("source_wallet", "")
    checks.append(check(
        "EP_WALLET_FORMAT",
        wallet.startswith("r") and 25 <= len(wallet) <= 35,
        f"Source wallet: {wallet}",
        severity="LOW"
    ))

    return {"category": "endpoint_evidence", "checks": checks}


def verify_signals(proof):
    """Category 4: Signal content validity."""
    checks = []
    ev = proof.get("endpoint_evidence", {})
    detail = ev.get("signals_detail", {})
    published = detail.get("published", [])

    checks.append(check(
        "SIG_HAS_PUBLISHED",
        len(published) > 0,
        f"Published signals: {len(published)}",
        severity="CRITICAL"
    ))

    valid_symbols = {"BTC", "ETH", "SOL", "LINK", "XRP", "DOGE", "ADA", "DOT", "AVAX", "MATIC"}
    valid_directions = {"BULLISH", "BEARISH", "NEUTRAL"}
    valid_types = {"DIRECTIONAL", "TRANSITION_TIMING", "REGIME_CHANGE"}

    for i, sig in enumerate(published):
        sym = sig.get("symbol", "")
        checks.append(check(
            f"SIG_{i}_SYMBOL",
            isinstance(sym, str) and len(sym) > 0,
            f"Signal {i} symbol: {sym}",
            severity="HIGH"
        ))

        direction = sig.get("direction", "")
        checks.append(check(
            f"SIG_{i}_DIRECTION",
            direction in valid_directions,
            f"Signal {i} direction: {direction}",
            severity="HIGH"
        ))

        conf = sig.get("confidence", -1)
        checks.append(check(
            f"SIG_{i}_CONFIDENCE",
            isinstance(conf, (int, float)) and 0 <= conf <= 1,
            f"Signal {i} confidence: {conf}",
            severity="HIGH"
        ))

        sig_type = sig.get("signal_type", "")
        checks.append(check(
            f"SIG_{i}_TYPE",
            isinstance(sig_type, str) and len(sig_type) > 0,
            f"Signal {i} type: {sig_type}",
            severity="LOW"
        ))

        karma = sig.get("expected_karma")
        if karma is not None:
            checks.append(check(
                f"SIG_{i}_KARMA",
                isinstance(karma, (int, float)),
                f"Signal {i} expected_karma: {karma}",
                severity="LOW"
            ))

    return {"category": "signals", "checks": checks}


def verify_policy_gates(proof):
    """Category 5: Policy gate configuration evidence."""
    checks = []
    ev = proof.get("endpoint_evidence", {})
    policy = ev.get("policy_summary", {})

    checks.append(check(
        "POL_REGIME_FILTER",
        "regime_filter" in policy,
        f"Regime filter: {policy.get('regime_filter')}",
        severity="MEDIUM"
    ))

    checks.append(check(
        "POL_VOI_FIELD",
        "voi_suppressions" in policy,
        f"VOI suppressions field present: {'voi_suppressions' in policy}",
        severity="LOW"
    ))

    checks.append(check(
        "POL_DURATION_FIELD",
        "duration_gated_signals" in policy,
        f"Duration gate field present: {'duration_gated_signals' in policy}",
        severity="LOW"
    ))

    inversions = policy.get("weak_symbol_inversions", [])
    checks.append(check(
        "POL_WEAK_SYMBOL_LIST",
        isinstance(inversions, list),
        f"Weak symbol inversions: {len(inversions)} entries",
        severity="LOW"
    ))

    for i, inv in enumerate(inversions):
        checks.append(check(
            f"POL_INVERSION_{i}_SYMBOL",
            "symbol" in inv and isinstance(inv["symbol"], str),
            f"Inversion {i} symbol: {inv.get('symbol')}",
            severity="LOW"
        ))
        checks.append(check(
            f"POL_INVERSION_{i}_REASON",
            "reason" in inv and isinstance(inv["reason"], str),
            f"Inversion {i} has reason: {'reason' in inv}",
            severity="LOW"
        ))

    return {"category": "policy_gates", "checks": checks}


def verify_endpoint_survey(proof):
    """Category 6: Endpoint survey completeness."""
    checks = []
    survey = proof.get("endpoint_survey", {})

    checks.append(check(
        "SURV_TOTAL",
        isinstance(survey.get("total_endpoints"), int) and survey["total_endpoints"] > 0,
        f"Total endpoints: {survey.get('total_endpoints')}",
        severity="MEDIUM"
    ))

    checks.append(check(
        "SURV_ALL_REACHABLE",
        survey.get("unreachable", 1) == 0,
        f"Unreachable: {survey.get('unreachable')}",
        severity="MEDIUM"
    ))

    expected_endpoints = [
        "/health", "/regime/current", "/signals/filtered",
        "/signals/latest", "/signals/reliability"
    ]

    endpoints = survey.get("endpoints", {})
    for ep in expected_endpoints:
        ep_data = endpoints.get(ep, {})
        checks.append(check(
            f"SURV_{ep.replace('/', '_').upper()}",
            ep_data.get("reachable") is True,
            f"Endpoint {ep}: {'reachable' if ep_data.get('reachable') else 'unreachable'}",
            severity="HIGH" if ep == "/signals/latest" else "MEDIUM"
        ))

    return {"category": "endpoint_survey", "checks": checks}


def verify_consumer_audit(proof):
    """Category 7: Consumer audit presence and completeness."""
    checks = []
    audit = proof.get("consumer_audit", {})

    checks.append(check(
        "AUDIT_TOOL",
        "pf-consumer-audit" in str(audit.get("audit_tool", "")),
        f"Audit tool: {audit.get('audit_tool')}",
        severity="CRITICAL"
    ))

    checks.append(check(
        "AUDIT_GRADE",
        audit.get("grade") in ("A", "B", "C", "D", "F"),
        f"Audit grade: {audit.get('grade')}",
        severity="HIGH"
    ))

    checks.append(check(
        "AUDIT_VERDICT",
        audit.get("verdict") is not None and isinstance(audit.get("verdict"), str),
        f"Audit verdict: {audit.get('verdict')}",
        severity="HIGH"
    ))

    checks.append(check(
        "AUDIT_TRUST_SCORE",
        isinstance(audit.get("trust_score"), (int, float)) and 0 <= audit["trust_score"] <= 1,
        f"Trust score: {audit.get('trust_score')}",
        severity="HIGH"
    ))

    checks.append(check(
        "AUDIT_EXIT_CODE",
        audit.get("exit_code") is not None,
        f"Exit code: {audit.get('exit_code')}",
        severity="MEDIUM"
    ))

    checks.append(check(
        "AUDIT_SIGNALS_COUNTED",
        isinstance(audit.get("signals_audited"), int) and audit["signals_audited"] > 0,
        f"Signals audited: {audit.get('signals_audited')}",
        severity="HIGH"
    ))

    full_report = audit.get("full_audit_report", {})
    checks.append(check(
        "AUDIT_FULL_REPORT",
        isinstance(full_report, dict) and len(full_report) > 0,
        f"Full report: {'present' if full_report else 'missing'}",
        severity="HIGH"
    ))

    # Check trust_grade block inside full report
    tg = full_report.get("trust_grade", {})
    checks.append(check(
        "AUDIT_TG_COMPONENTS",
        isinstance(tg.get("components"), dict) and len(tg.get("components", {})) >= 5,
        f"Trust grade components: {len(tg.get('components', {}))} dimensions",
        severity="MEDIUM"
    ))

    # Signal schema should PASS
    sc = full_report.get("schema_compliance", {})
    sig_schema = sc.get("signal_schema", {})
    checks.append(check(
        "AUDIT_SIGNAL_SCHEMA_PASS",
        sig_schema.get("status") == "PASS",
        f"Signal schema status: {sig_schema.get('status')}",
        severity="HIGH"
    ))

    # Check valid signals count matches
    checks.append(check(
        "AUDIT_VALID_SIGNALS",
        sig_schema.get("valid_signals", 0) == sig_schema.get("total_signals", -1),
        f"Valid: {sig_schema.get('valid_signals')}/{sig_schema.get('total_signals')}",
        severity="MEDIUM"
    ))

    # Findings present
    findings = full_report.get("findings", [])
    checks.append(check(
        "AUDIT_FINDINGS",
        isinstance(findings, list),
        f"Findings: {len(findings)} items",
        severity="LOW"
    ))

    # Limitations present
    limitations = full_report.get("limitations", [])
    checks.append(check(
        "AUDIT_LIMITATIONS",
        isinstance(limitations, list) and len(limitations) >= 1,
        f"Limitations: {len(limitations)} items",
        severity="LOW"
    ))

    return {"category": "consumer_audit", "checks": checks}


def verify_integration_summary(proof):
    """Category 8: Integration summary correctness."""
    checks = []
    summary = proof.get("integration_summary", {})

    for field in ["endpoint_live", "external_accessible", "schema_valid",
                  "no_oracle_dependency", "self_hosted", "consumer_audit_ran"]:
        checks.append(check(
            f"INTG_{field.upper()}",
            summary.get(field) is True,
            f"{field}: {summary.get(field)}",
            severity="HIGH" if not summary.get(field) else "LOW"
        ))

    gates = summary.get("policy_gates_active", {})
    checks.append(check(
        "INTG_GATES_PRESENT",
        isinstance(gates, dict) and len(gates) >= 3,
        f"Policy gates: {len(gates)} configured",
        severity="MEDIUM"
    ))

    health = summary.get("health", {})
    checks.append(check(
        "INTG_HEALTH_PRESENT",
        isinstance(health, dict) and health.get("status") == "ok",
        f"Health status: {health.get('status')}",
        severity="MEDIUM"
    ))

    return {"category": "integration_summary", "checks": checks}


def verify_source_hashes(proof):
    """Category 9: Hash integrity."""
    checks = []
    hashes = proof.get("source_hashes", {})

    for field in ["signal_payload_content_hash", "signal_payload_body_sha256", "proof_sha256"]:
        h = hashes.get(field, "")
        valid = isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdef" for c in h)
        checks.append(check(
            f"HASH_{field.upper()}",
            valid,
            f"{field}: {'valid hex64' if valid else f'invalid ({h[:20]}...)'}",
            severity="HIGH"
        ))

    # Verify proof self-hash
    proof_for_hash = {k: v for k, v in proof.items() if k != "source_hashes"}
    recomputed = sha256(json.dumps(proof_for_hash, sort_keys=True, separators=(",", ":")))
    claimed = hashes.get("proof_sha256", "")
    checks.append(check(
        "HASH_PROOF_RECOMPUTE",
        recomputed == claimed,
        f"Proof hash recomputation: {'MATCH' if recomputed == claimed else 'MISMATCH'}",
        severity="CRITICAL",
        remediation="Proof artifact may have been tampered with." if recomputed != claimed else None
    ))

    # Content hash should match what endpoint evidence claims
    ev = proof.get("endpoint_evidence", {})
    ev_hash = ev.get("content_hash", "")
    src_hash = hashes.get("signal_payload_content_hash", "")
    checks.append(check(
        "HASH_CONTENT_CONSISTENT",
        ev_hash == src_hash,
        f"Content hash cross-ref: {'consistent' if ev_hash == src_hash else 'INCONSISTENT'}",
        severity="HIGH"
    ))

    ev_body_hash = ev.get("response_body_sha256", "")
    src_body_hash = hashes.get("signal_payload_body_sha256", "")
    checks.append(check(
        "HASH_BODY_CONSISTENT",
        ev_body_hash == src_body_hash,
        f"Body hash cross-ref: {'consistent' if ev_body_hash == src_body_hash else 'INCONSISTENT'}",
        severity="HIGH"
    ))

    return {"category": "source_hashes", "checks": checks}


def verify_rerun_instructions(proof):
    """Category 10: Rerun instructions presence."""
    checks = []
    rerun = proof.get("rerun_instructions", {})

    checks.append(check(
        "RERUN_COMMAND",
        isinstance(rerun.get("command"), str) and len(rerun["command"]) > 0,
        f"Command: {rerun.get('command')}",
        severity="MEDIUM"
    ))

    checks.append(check(
        "RERUN_PREREQS",
        isinstance(rerun.get("prerequisites"), str) and len(rerun["prerequisites"]) > 0,
        f"Prerequisites: {'present' if rerun.get('prerequisites') else 'missing'}",
        severity="LOW"
    ))

    return {"category": "rerun_instructions", "checks": checks}


def verify_regime_context(proof):
    """Category 11: Regime context validity."""
    checks = []
    ev = proof.get("endpoint_evidence", {})

    valid_regimes = {"SYSTEMIC", "NEUTRAL", "EARNINGS", "DIVERGENCE"}
    regime = ev.get("regime_state", "")
    checks.append(check(
        "REGIME_STATE_VALID",
        regime in valid_regimes,
        f"Regime state: {regime}",
        severity="HIGH"
    ))

    conf = ev.get("regime_confidence")
    checks.append(check(
        "REGIME_CONFIDENCE_RANGE",
        isinstance(conf, (int, float)) and 0 <= conf <= 100,
        f"Regime confidence: {conf}",
        severity="MEDIUM"
    ))

    duration = ev.get("regime_duration_days")
    checks.append(check(
        "REGIME_DURATION_POSITIVE",
        isinstance(duration, (int, float)) and duration >= 0,
        f"Regime duration: {duration}d",
        severity="LOW"
    ))

    proximity = ev.get("regime_proximity")
    if proximity is not None:
        checks.append(check(
            "REGIME_PROXIMITY_RANGE",
            isinstance(proximity, (int, float)) and 0 <= proximity <= 1,
            f"Regime proximity: {proximity}",
            severity="LOW"
        ))

    return {"category": "regime_context", "checks": checks}


def verify_cross_references(proof):
    """Category 12: Cross-reference consistency between sections."""
    checks = []

    ev = proof.get("endpoint_evidence", {})
    summary = proof.get("integration_summary", {})
    audit = proof.get("consumer_audit", {})

    # Audit grade consistency
    checks.append(check(
        "XREF_AUDIT_GRADE",
        summary.get("audit_grade") == audit.get("grade"),
        f"Summary grade ({summary.get('audit_grade')}) vs audit grade ({audit.get('grade')})",
        severity="HIGH"
    ))

    # Signals published count consistency
    pub_count = ev.get("signals_published")
    detail_count = len(ev.get("signals_detail", {}).get("published", []))
    checks.append(check(
        "XREF_SIGNAL_COUNT",
        pub_count == detail_count,
        f"Published count ({pub_count}) vs detail count ({detail_count})",
        severity="HIGH"
    ))

    # Audit signals vs endpoint signals
    audit_count = audit.get("signals_audited")
    checks.append(check(
        "XREF_AUDIT_SIGNAL_COUNT",
        audit_count == pub_count,
        f"Audited ({audit_count}) vs published ({pub_count})",
        severity="MEDIUM"
    ))

    # Self-hosted consistency
    checks.append(check(
        "XREF_SELF_HOSTED",
        ev.get("self_hosted") == summary.get("self_hosted"),
        f"Endpoint ({ev.get('self_hosted')}) vs summary ({summary.get('self_hosted')})",
        severity="LOW"
    ))

    # Endpoint live consistency
    checks.append(check(
        "XREF_ENDPOINT_LIVE",
        summary.get("endpoint_live") is True and ev.get("http_status") == 200,
        f"Summary live ({summary.get('endpoint_live')}) + HTTP status ({ev.get('http_status')})",
        severity="MEDIUM"
    ))

    # Producer ID consistency between endpoint and audit
    ev_producer = ev.get("producer_id")
    audit_meta = audit.get("full_audit_report", {}).get("meta", {})
    audit_producer = audit_meta.get("producer_id")
    checks.append(check(
        "XREF_PRODUCER_ID",
        ev_producer == audit_producer,
        f"Endpoint producer ({ev_producer}) vs audit producer ({audit_producer})",
        severity="MEDIUM"
    ))

    return {"category": "cross_references", "checks": checks}


def verify_live_endpoint(proof):
    """Category 13 (optional): Live endpoint re-verification."""
    checks = []
    ev = proof.get("endpoint_evidence", {})
    endpoint = ev.get("endpoint", "")
    public_endpoint = ev.get("public_endpoint", "")

    # Test local endpoint
    try:
        req = urllib.request.Request(endpoint)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            data = json.loads(raw)
            checks.append(check(
                "LIVE_LOCAL_REACHABLE",
                True,
                f"Local endpoint reachable: {endpoint}",
                severity="LOW"
            ))
            checks.append(check(
                "LIVE_SCHEMA_MATCH",
                data.get("schema") == ev.get("schema_version"),
                f"Live schema: {data.get('schema')} vs claimed: {ev.get('schema_version')}",
                severity="MEDIUM"
            ))
            checks.append(check(
                "LIVE_PRODUCER_MATCH",
                data.get("producer_id") == ev.get("producer_id"),
                f"Live producer: {data.get('producer_id')}",
                severity="MEDIUM"
            ))
            checks.append(check(
                "LIVE_SIGNALS_PRESENT",
                data.get("signals", {}).get("total_published", 0) > 0,
                f"Live signals: {data.get('signals', {}).get('total_published', 0)}",
                severity="HIGH"
            ))
    except Exception as e:
        checks.append(check(
            "LIVE_LOCAL_REACHABLE",
            False,
            f"Cannot reach local endpoint: {e}",
            severity="HIGH",
            remediation="Ensure signal API is running on port 8080."
        ))

    # Test external endpoint
    try:
        req = urllib.request.Request(public_endpoint)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            data = json.loads(raw)
            checks.append(check(
                "LIVE_EXTERNAL_REACHABLE",
                True,
                f"External endpoint reachable: {public_endpoint}",
                severity="LOW"
            ))
    except Exception as e:
        checks.append(check(
            "LIVE_EXTERNAL_REACHABLE",
            False,
            f"Cannot reach external endpoint: {e}",
            severity="MEDIUM",
            remediation="Ensure port 8080 is open to the internet."
        ))

    return {"category": "live_verification", "checks": checks}


# ── Main Verification ────────────────────────────────────────────

def run_verification(proof_path, live=False):
    """Run all verification categories against a proof artifact."""

    with open(proof_path) as f:
        proof = json.load(f)

    categories = [
        verify_structure,
        verify_timestamp,
        verify_endpoint_evidence,
        verify_signals,
        verify_policy_gates,
        verify_endpoint_survey,
        verify_consumer_audit,
        verify_integration_summary,
        verify_source_hashes,
        verify_rerun_instructions,
        verify_regime_context,
        verify_cross_references,
    ]

    if live:
        categories.append(verify_live_endpoint)

    results = []
    total_passed = 0
    total_failed = 0
    total_checks = 0
    critical_failures = []

    for verify_fn in categories:
        result = verify_fn(proof)
        cat_passed = sum(1 for c in result["checks"] if c["passed"])
        cat_failed = sum(1 for c in result["checks"] if not c["passed"])
        cat_total = len(result["checks"])
        result["passed"] = cat_passed
        result["failed"] = cat_failed
        result["total"] = cat_total
        results.append(result)
        total_passed += cat_passed
        total_failed += cat_failed
        total_checks += cat_total

        for c in result["checks"]:
            if not c["passed"] and c["severity"] == "CRITICAL":
                critical_failures.append(c)

    # Compute grade
    pct = (total_passed / total_checks * 100) if total_checks > 0 else 0
    if pct >= 99:
        grade = "A"
    elif pct >= 90:
        grade = "B"
    elif pct >= 75:
        grade = "C"
    elif pct >= 50:
        grade = "D"
    else:
        grade = "F"

    # Verdict
    if critical_failures:
        verdict = "FAIL"
    elif total_failed == 0:
        verdict = "PASS"
    elif total_failed <= 3:
        verdict = "PASS_WITH_WARNINGS"
    else:
        verdict = "MARGINAL"

    report = {
        "schema": "pf-integration-proof-verification/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proof_path": proof_path,
        "proof_date": proof.get("date"),
        "proof_generated_at": proof.get("generated_at"),
        "live_verification": live,
        "summary": {
            "total_checks": total_checks,
            "passed": total_passed,
            "failed": total_failed,
            "pass_rate": round(pct, 2),
            "grade": grade,
            "verdict": verdict,
            "critical_failures": len(critical_failures)
        },
        "categories": results,
        "critical_failures": critical_failures if critical_failures else []
    }

    return report


def print_report(report):
    """Print human-readable verification report."""
    print("=" * 60)
    print("INTEGRATION PROOF VERIFICATION REPORT")
    print("=" * 60)
    print(f"  Proof date: {report['proof_date']}")
    print(f"  Verified at: {report['generated_at']}")
    print(f"  Live checks: {'YES' if report['live_verification'] else 'NO'}")
    print()

    for cat in report["categories"]:
        status = "PASS" if cat["failed"] == 0 else "FAIL"
        print(f"  [{status:4}] {cat['category']:30} {cat['passed']}/{cat['total']}")
        if cat["failed"] > 0:
            for c in cat["checks"]:
                if not c["passed"]:
                    sev = c["severity"]
                    print(f"         [{sev}] {c['check_id']}: {c['detail']}")
                    if c.get("remediation"):
                        print(f"                  → {c['remediation']}")

    summary = report["summary"]
    print()
    print("-" * 60)
    print(f"  Total: {summary['passed']}/{summary['total_checks']} "
          f"({summary['pass_rate']}%)")
    print(f"  Grade: {summary['grade']}")
    print(f"  Verdict: {summary['verdict']}")
    if summary["critical_failures"] > 0:
        print(f"  Critical failures: {summary['critical_failures']}")
    print("=" * 60)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 verify_integration_proof.py <proof.json> [--live] [--json]")
        sys.exit(1)

    proof_path = sys.argv[1]
    live = "--live" in sys.argv
    json_out = "--json" in sys.argv

    if not os.path.exists(proof_path):
        print(f"ERROR: {proof_path} not found")
        sys.exit(1)

    report = run_verification(proof_path, live=live)

    if json_out:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)

    # Exit code based on verdict
    if report["summary"]["verdict"] in ("PASS", "PASS_WITH_WARNINGS"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
