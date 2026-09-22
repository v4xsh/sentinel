"""I12 (no invented case IDs in prose) + I13 (connected_card_ids⇒§3a+R6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.policy.invariants import check_invariants


def _ans(**over) -> dict:
    base = {
        "case_id": "HHG-999",
        "case": {
            "status": "closed_fraud", "verdict": "fraud",
            "fraud_probability": 0.9, "pattern": "none",
            "pattern_description": "", "affected_txn_ids": [],
            "first_suspicious_txn_id": "", "connected_card_ids": [],
            "connected_device_profiles": [], "exposure_usd": 0.0,
            "evidence": [], "similar_prior_cases": [], "summary": "x",
            "written_to_graph": False, "graph_case_id": "",
        },
        "evidence_requests": [],
        "next_best_actions": {
            "initial": [{"action": "BLOCK_CARD", "route": "L1",
                          "reason": "R2: test"}],
            "final":   [{"action": "BLOCK_CARD", "route": "L1",
                          "reason": "R2: test"}],
            "what_changed": "nothing",
        },
        "sar": {"file": False, "reason": "", "narrative": "",
                "subjects": [], "total_amount_usd": 0.0, "activity_dates": []},
        "stop_reason": "test", "tool_calls": 0, "tokens": 0, "latency_s": 0.0,
    }
    for k, v in over.items():
        # crude deep-merge for the two sections we edit in tests
        if k in ("case", "sar", "next_best_actions"):
            base[k].update(v)
        else:
            base[k] = v
    return base


# ---- I12 -----------------------------------------------------------------


def test_i12_invented_case_id_in_summary_fails():
    a = _ans(case={"summary": "This looks like CC-9999 all over again."})
    codes = [x.invariant for x in check_invariants(a)]
    assert "I12" in codes


def test_i12_invented_case_id_in_pattern_description_fails():
    a = _ans(case={"pattern": "undocumented",
                    "pattern_description": "Ring pattern similar to CC-1234."})
    codes = [x.invariant for x in check_invariants(a)]
    assert "I12" in codes


def test_i12_invented_case_id_in_sar_narrative_fails():
    a = _ans(sar={"file": True, "narrative": "Prior fraud confirmed in CC-4321."})
    # nba.final also needs FILE_REPORT to satisfy I1 — add it so we
    # only test I12.
    a["next_best_actions"]["final"] = [
        {"action": "BLOCK_CARD", "route": "L1", "reason": "R2: test"},
        {"action": "FILE_REPORT", "route": "L2", "reason": "§3a: test"},
    ]
    a["next_best_actions"]["initial"] = a["next_best_actions"]["final"]
    codes = [x.invariant for x in check_invariants(a)]
    assert "I12" in codes


def test_i12_whitelisted_id_passes():
    a = _ans(case={"summary": "This resembles CC-1234 (see prior).",
                    "similar_prior_cases": ["CC-1234"]})
    codes = [x.invariant for x in check_invariants(a)]
    assert "I12" not in codes


def test_i12_own_id_passes():
    a = _ans(case={"summary": "Case HHG-999 closed as fraud."})
    codes = [x.invariant for x in check_invariants(a)]
    assert "I12" not in codes


def test_i12_no_case_ids_passes():
    a = _ans(case={"summary": "Fraud verdict driven by device tier T4."})
    assert "I12" not in [x.invariant for x in check_invariants(a)]


# ---- I13 -----------------------------------------------------------------


def test_i13_connected_cards_fraud_without_file_report_fails():
    a = _ans(case={"connected_card_ids": ["CARD-A", "CARD-B"]})
    codes = [x.invariant for x in check_invariants(a)]
    assert "I13" in codes  # missing FILE_REPORT and MONITOR_CONNECTED_CARDS


def test_i13_connected_cards_fraud_with_both_passes():
    a = _ans(
        case={"connected_card_ids": ["CARD-A"]},
        sar={"file": True, "narrative": "n"},
        next_best_actions={
            "initial": [
                {"action": "BLOCK_CARD", "route": "L1", "reason": "R2: test"},
                {"action": "FILE_REPORT", "route": "L2", "reason": "§3a"},
                {"action": "MONITOR_CONNECTED_CARDS", "route": "L1", "reason": "R6"},
            ],
            "final": [
                {"action": "BLOCK_CARD", "route": "L1", "reason": "R2: test"},
                {"action": "FILE_REPORT", "route": "L2", "reason": "§3a"},
                {"action": "MONITOR_CONNECTED_CARDS", "route": "L1", "reason": "R6"},
            ],
            "what_changed": "nothing",
        },
    )
    codes = [x.invariant for x in check_invariants(a)]
    assert "I13" not in codes


def test_i13_connected_cards_legitimate_no_op():
    """Legitimate verdicts don't need FILE_REPORT/MONITOR — I13 only fires on fraud."""
    a = _ans(case={"verdict": "legitimate", "fraud_probability": 0.1,
                    "pattern": "none", "connected_card_ids": ["CARD-A"]},
              next_best_actions={
                  "initial": [{"action": "CLOSE_NO_FRAUD", "route": "auto", "reason": "§6"}],
                  "final":   [{"action": "CLOSE_NO_FRAUD", "route": "auto", "reason": "§6"}],
                  "what_changed": "nothing",
              })
    codes = [x.invariant for x in check_invariants(a)]
    assert "I13" not in codes


# ---- I12 sweep over produced files ---------------------------------------


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("dirname", ["cases", "cases_extra"])
def test_i12_sweep_all_produced_files(dirname: str):
    d = REPO / dirname
    if not d.exists():
        pytest.skip(f"{dirname} not present")
    files = sorted(d.glob("*.json"))
    files = [p for p in files if p.name.startswith(("HHG-", "EXTRA-"))]
    if not files:
        pytest.skip(f"no answer files in {dirname}")
    violations = []
    for p in files:
        a = json.loads(p.read_text())
        for x in check_invariants(a):
            if x.invariant == "I12":
                violations.append(f"{p.name}: {x.detail[:90]}")
    assert not violations, "\n".join(violations)
