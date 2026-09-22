"""I10 (BLOCK_ALL_CARDS gate) + I11 (probability ↔ verdict agreement)."""

from __future__ import annotations

import pytest

from sentinel.policy.invariants import check_invariants


def _ans(verdict: str, actions_final: list[str],
         p: float = 0.5, exposure: float = 0.0) -> dict:
    """Minimal answer shape for invariant checks."""
    return {
        "case_id": "T",
        "case": {
            "status": "closed_fraud" if verdict == "fraud"
                      else ("closed_legitimate" if verdict == "legitimate" else "open"),
            "verdict": verdict, "fraud_probability": p, "pattern": "none",
            "pattern_description": "", "affected_txn_ids": [],
            "first_suspicious_txn_id": "", "connected_card_ids": [],
            "connected_device_profiles": [], "exposure_usd": exposure,
            "evidence": [], "similar_prior_cases": [], "summary": "x",
            "written_to_graph": False, "graph_case_id": "",
        },
        "evidence_requests": [],
        "next_best_actions": {
            "initial": [{"action": a, "route": "L2",
                          "reason": "R2/R6: test"} for a in actions_final],
            "final":   [{"action": a, "route": "L2",
                          "reason": "R2/R6: test"} for a in actions_final],
            "what_changed": "nothing",
        },
        "sar": {"file": False, "reason": "", "narrative": "",
                "subjects": [], "total_amount_usd": 0.0, "activity_dates": []},
        "stop_reason": "test", "tool_calls": 0, "tokens": 0, "latency_s": 0.0,
    }


# ---- I10 ----------------------------------------------------------------


def test_i10_block_all_cards_on_legitimate_fails():
    a = _ans("legitimate", ["CLOSE_NO_FRAUD", "BLOCK_ALL_CARDS"], p=0.05)
    v = check_invariants(a)
    codes = [x.invariant for x in v]
    assert "I10" in codes, f"expected I10 violation; got {codes}"


def test_i10_block_all_cards_with_close_no_fraud_fails():
    a = _ans("fraud", ["CLOSE_NO_FRAUD", "BLOCK_ALL_CARDS"], p=0.9)
    v = check_invariants(a)
    codes = [x.invariant for x in v]
    assert "I10" in codes, f"expected I10 violation; got {codes}"


def test_i10_block_all_cards_on_fraud_alone_ok():
    a = _ans("fraud", ["BLOCK_CARD", "CREATE_CASE", "BLOCK_ALL_CARDS"], p=0.9)
    v = check_invariants(a)
    assert "I10" not in [x.invariant for x in v]


def test_i10_uncertain_with_block_all_fails():
    a = _ans("uncertain", ["MONITOR_CARD", "BLOCK_ALL_CARDS"], p=0.6)
    v = check_invariants(a)
    assert "I10" in [x.invariant for x in v]


# ---- I11 ----------------------------------------------------------------


def test_i11_fraud_low_p_fails():
    a = _ans("fraud", ["BLOCK_CARD", "CREATE_CASE"], p=0.12)
    v = check_invariants(a)
    assert "I11" in [x.invariant for x in v]


def test_i11_legit_high_p_fails():
    a = _ans("legitimate", ["CLOSE_NO_FRAUD"], p=0.77)
    v = check_invariants(a)
    assert "I11" in [x.invariant for x in v]


def test_i11_fraud_ok_at_0_85():
    a = _ans("fraud", ["BLOCK_CARD", "CREATE_CASE"], p=0.85)
    v = check_invariants(a)
    assert "I11" not in [x.invariant for x in v]


def test_i11_legit_ok_at_0_15():
    a = _ans("legitimate", ["CLOSE_NO_FRAUD"], p=0.15)
    v = check_invariants(a)
    assert "I11" not in [x.invariant for x in v]
