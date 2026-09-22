"""Table-driven tests for the policy engine.

One test per rule (R1–R10, §3a, §3b) plus the HHG-017 example verbatim from
the README, and the exposure/route splits.
"""

from __future__ import annotations

import pytest

from sentinel.policy import (
    Action, PolicyDecision, PolicyInput, check_invariants, decide, route_for,
)


# ---------- routing (§2) -----------------------------------------------------


@pytest.mark.parametrize(
    "action,expected",
    [
        ("ALLOW_TRANSACTION",       "auto"),
        ("MONITOR_CARD",            "auto"),
        ("MONITOR_CONNECTED_CARDS", "auto"),
        ("WARN_CUSTOMER",           "auto"),
        ("VERIFY_WITH_CUSTOMER",    "auto"),
        ("STEP_UP_AUTH",            "auto"),
        ("GENERATE_REPORT",         "auto"),
        ("CREATE_CASE",             "auto"),
        ("ESCALATE_TO_ANALYST",     "auto"),
        ("CLOSE_NO_FRAUD",          "auto"),
        ("DECLINE_TRANSACTION",     "L1"),
        ("BLOCK_ALL_CARDS",         "L2"),
        ("FILE_REPORT",             "L2"),
    ],
)
def test_route_for_static(action: str, expected: str) -> None:
    assert route_for(action) == expected


def test_route_for_block_card_split() -> None:
    """BLOCK_CARD @ exposure<=2500 -> L1; >2500 -> L2 (§2)."""
    assert route_for("BLOCK_CARD", exposure_usd=100) == "L1"
    assert route_for("BLOCK_CARD", exposure_usd=2500) == "L1"
    assert route_for("BLOCK_CARD", exposure_usd=2500.01) == "L2"
    assert route_for("BLOCK_CARD", exposure_usd=10_000) == "L2"


# ---------- R1 — verify before block on weak signal ---------------------------


def test_r1_verify_before_block() -> None:
    """Single signal + p<0.70 → VERIFY_WITH_CUSTOMER, no block."""
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.45, exposure_usd=100,
        pattern="none", signal_channels=frozenset(["device"]),
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "VERIFY_WITH_CUSTOMER" in names
    assert "BLOCK_CARD" not in names
    # R1 cited.
    assert any("R1" in a.reason for a in d.actions)


# ---------- R2 — customer denied ---------------------------------------------


@pytest.mark.parametrize(
    "exposure,shared,expect_file_report",
    [
        (100.0,   None,     False),   # small, no shared element → no SAR
        (1500.0,  None,     True),    # exposure>1000 → SAR
        (100.0,   "device", True),    # shared element → SAR
        (100.0,   "region", True),
        (100.0,   "recipient", True),
    ],
)
def test_r2_customer_denied(exposure: float, shared, expect_file_report: bool) -> None:
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.6, exposure_usd=exposure,
        pattern="card_not_present_fraud", customer_response="denied",
        shared_element=shared,
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "BLOCK_CARD" in names
    assert "CREATE_CASE" in names
    if expect_file_report:
        assert "FILE_REPORT" in names
    else:
        assert "FILE_REPORT" not in names
    if shared is not None:
        assert "MONITOR_CONNECTED_CARDS" in names
    # Route on BLOCK_CARD scales with exposure (§2).
    block = [a for a in d.actions if a.action == "BLOCK_CARD"][0]
    assert block.route == ("L1" if exposure <= 2500 else "L2")


# ---------- R3 — customer confirmed ------------------------------------------


def test_r3_customer_confirmed() -> None:
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.5, exposure_usd=100,
        pattern="card_not_present_fraud", customer_response="confirmed",
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert names == ["CLOSE_NO_FRAUD"]


# ---------- R4 — no reply ----------------------------------------------------


@pytest.mark.parametrize(
    "exposure,expect_escalate",
    [(100.0, False), (500.0, False), (500.01, True), (5000.0, True)],
)
def test_r4_no_reply(exposure: float, expect_escalate: bool) -> None:
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.55, exposure_usd=exposure,
        pattern="card_not_present_fraud", customer_response="no_reply",
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "MONITOR_CARD" in names
    assert "DECLINE_TRANSACTION" in names
    if expect_escalate:
        assert "ESCALATE_TO_ANALYST" in names
    else:
        assert "ESCALATE_TO_ANALYST" not in names


# ---------- R5 — card testing ------------------------------------------------


def test_r5_card_testing_basic() -> None:
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.72, exposure_usd=300,
        pattern="card_testing", testing_sequence_fires=True,
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "DECLINE_TRANSACTION" in names
    assert "STEP_UP_AUTH" in names
    assert "BLOCK_CARD" not in names


def test_r5_card_testing_with_over_100_cleared() -> None:
    inp = PolicyInput(
        verdict="fraud", fraud_probability=0.86, exposure_usd=268.43,
        pattern="card_testing", testing_sequence_fires=True,
        testing_sequence_over_100_cleared=True,
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "BLOCK_CARD" in names
    assert "DECLINE_TRANSACTION" in names
    assert "STEP_UP_AUTH" in names


# ---------- R6 — shared origin -----------------------------------------------


def test_r6_shared_origin_confirmed_fraud() -> None:
    inp = PolicyInput(
        verdict="fraud", fraud_probability=0.90, exposure_usd=1200,
        pattern="card_not_present_new_device", shared_element="device",
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert {"CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS"} <= set(names)


# ---------- R7 — disputed but legitimate (recurring) -------------------------


def test_r7_disputed_recurring() -> None:
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.4, exposure_usd=49,
        pattern="none", is_dispute=True, is_recurring_match=True,
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert names == ["CREATE_CASE", "VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER"]
    assert "BLOCK_CARD" not in names


# ---------- R8 — uncertain + exposed -----------------------------------------


def test_r8_uncertain_and_exposed() -> None:
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.5, exposure_usd=800,
        pattern="none", signal_channels=frozenset(["device", "region"]),
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "ESCALATE_TO_ANALYST" in names


def test_r8_uncertain_but_small_no_escalate() -> None:
    inp = PolicyInput(
        verdict="uncertain", fraud_probability=0.5, exposure_usd=100,
        pattern="none", signal_channels=frozenset(["device", "region"]),
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "ESCALATE_TO_ANALYST" not in names


# ---------- R9 — undocumented ------------------------------------------------


def test_r9_undocumented() -> None:
    inp = PolicyInput(
        verdict="fraud", fraud_probability=0.88, exposure_usd=390.04,
        pattern="undocumented", shared_element="device",
        undocumented_coordinated=True,
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert {"CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"} <= set(names)


# ---------- R10 — BLOCK_ALL_CARDS guardrail ----------------------------------


def test_r10_block_all_cards_requires_conditions() -> None:
    from sentinel.policy.policy_engine import add_block_all_cards_if_permitted

    inp = PolicyInput(
        verdict="fraud", fraud_probability=0.9, exposure_usd=1000,
        pattern="account_takeover",
    )
    d = decide(inp)
    # Not permitted (no two-card fraud, no compromised credentials).
    updated = add_block_all_cards_if_permitted(d.actions, inp)
    assert "BLOCK_ALL_CARDS" not in [a.action for a in updated]

    # Permitted when two prior confirmed frauds exist.
    inp2 = PolicyInput(
        verdict="fraud", fraud_probability=0.9, exposure_usd=1000,
        pattern="account_takeover", prior_confirmed_fraud_on_two_cards=True,
    )
    updated2 = add_block_all_cards_if_permitted(d.actions, inp2)
    assert "BLOCK_ALL_CARDS" in [a.action for a in updated2]


# ---------- §3a case-open + SAR triggers -------------------------------------


@pytest.mark.parametrize(
    "p,is_dispute,ev_present,expect",
    [
        (0.10, False, False, False),
        (0.29, False, False, False),
        (0.30, False, False, True),
        (0.10, True,  False, True),
        (0.10, False, True,  True),
    ],
)
def test_case_should_open_matrix(p: float, is_dispute: bool,
                                 ev_present: bool, expect: bool) -> None:
    from sentinel.policy.policy_engine import _case_should_open

    inp = PolicyInput(
        verdict="uncertain", fraud_probability=p, exposure_usd=100,
        pattern="none", is_dispute=is_dispute,
    )
    assert _case_should_open(inp, ev_present) is expect


@pytest.mark.parametrize(
    "verdict,p,exp,shared,pat,expect",
    [
        # confirmed + exp>1000 → SAR
        ("fraud", 0.9, 1500, None, "card_not_present_fraud", True),
        # strongly suspected + shared → SAR
        ("uncertain", 0.90, 100, "device", "account_takeover", True),
        # confirmed + undocumented (even small) → SAR
        ("fraud", 0.9, 100, None, "undocumented", True),
        # confirmed but tiny + no shared → no SAR
        ("fraud", 0.9, 100, None, "card_not_present_fraud", False),
        # low prob, no matter what → no SAR
        ("uncertain", 0.5, 5000, "device", "card_testing", False),
    ],
)
def test_sar_should_file_matrix(verdict, p, exp, shared, pat, expect) -> None:
    from sentinel.policy.policy_engine import _sar_should_file

    inp = PolicyInput(
        verdict=verdict, fraud_probability=p, exposure_usd=exp,
        pattern=pat, shared_element=shared,
        undocumented_coordinated=(pat == "undocumented"),
    )
    assert _sar_should_file(inp) is expect


# ---------- HHG-017 README example reproduction ------------------------------


def test_hhg017_initial_matches_readme() -> None:
    """Before the customer replies: R5 fires (testing sequence + $259.98 cleared).
    R1 gate applies because probability 0.72 on pattern alone — BLOCK is held
    pending VERIFY (README example: initial = DECLINE + VERIFY only)."""
    inp = PolicyInput(
        verdict="uncertain",
        fraud_probability=0.72,          # README example value
        exposure_usd=268.43,
        pattern="card_testing",
        testing_sequence_fires=True,
        testing_sequence_over_100_cleared=True,
        signal_channels=frozenset(["sequence"]),
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "DECLINE_TRANSACTION" in names
    assert "STEP_UP_AUTH" in names
    assert "VERIFY_WITH_CUSTOMER" in names
    # R1 gate holds → BLOCK_CARD is NOT in initial actions.
    assert "BLOCK_CARD" not in names


def test_hhg017_final_after_denial_matches_readme() -> None:
    """After the customer denies: R2 fires; shared device with C00877-K1 triggers
    FILE_REPORT + MONITOR_CONNECTED_CARDS."""
    inp = PolicyInput(
        verdict="fraud",
        fraud_probability=0.86,          # README example value after denial
        exposure_usd=268.43,
        pattern="card_testing",
        customer_response="denied",
        shared_element="device",
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert "BLOCK_CARD" in names
    assert "CREATE_CASE" in names
    assert "FILE_REPORT" in names
    assert "MONITOR_CONNECTED_CARDS" in names
    block = [a for a in d.actions if a.action == "BLOCK_CARD"][0]
    assert block.route == "L1"           # exposure $268 ≤ $2,500 → L1
    file_rep = [a for a in d.actions if a.action == "FILE_REPORT"][0]
    assert file_rep.route == "L2"        # SAR always L2


# ---------- invariants smoke test -------------------------------------------


def _mk_answer_minimal(**overrides) -> dict:
    default = {
        "case_id": "HHG-XXX",
        "case": {
            "status": "closed_fraud",
            "verdict": "fraud",
            "fraud_probability": 0.9,
            "pattern": "card_testing",
            "pattern_description": "",
            "affected_txn_ids": [],
            "first_suspicious_txn_id": "",
            "connected_card_ids": [],
            "connected_device_profiles": [],
            "exposure_usd": 0,
            "evidence": [],
            "similar_prior_cases": [],
            "summary": "test",
            "written_to_graph": False,
            "graph_case_id": "",
        },
        "evidence_requests": [],
        "next_best_actions": {
            "initial": [
                {"action": "CREATE_CASE", "route": "auto", "reason": "R6"},
            ],
            "final":   [
                {"action": "CREATE_CASE", "route": "auto", "reason": "R6"},
            ],
            "what_changed": "nothing",
        },
        "sar": {
            "file": False, "reason": "", "narrative": "",
            "subjects": [], "total_amount_usd": 0, "activity_dates": [],
        },
        "stop_reason": "test",
        "tool_calls": 0, "tokens": 0, "latency_s": 0.1,
    }
    default.update(overrides)
    return default


def test_invariant_1_sar_agreement() -> None:
    a = _mk_answer_minimal()
    # If sar.file=True but final has no FILE_REPORT, violation.
    a["sar"]["file"] = True
    violations = check_invariants(a)
    assert any(v.invariant == "I1" for v in violations)


def test_invariant_2_legitimate_shape() -> None:
    a = _mk_answer_minimal()
    a["case"]["verdict"] = "legitimate"
    a["case"]["affected_txn_ids"] = ["T1"]   # violation
    violations = check_invariants(a)
    assert any(v.invariant == "I2" for v in violations)


def test_invariant_3_undocumented_needs_description() -> None:
    a = _mk_answer_minimal()
    a["case"]["pattern"] = "undocumented"
    a["case"]["pattern_description"] = ""
    violations = check_invariants(a)
    assert any(v.invariant == "I3" for v in violations)


def test_invariant_4_no_ev_requests_means_final_equals_initial() -> None:
    a = _mk_answer_minimal()
    a["next_best_actions"]["final"] = [
        {"action": "BLOCK_CARD", "route": "L1", "reason": "R2"},
    ]
    violations = check_invariants(a)
    assert any(v.invariant == "I4" for v in violations)


def test_invariant_7_wrong_route() -> None:
    a = _mk_answer_minimal()
    a["next_best_actions"]["final"] = [
        {"action": "BLOCK_CARD", "route": "auto", "reason": "R2"},  # wrong route
    ]
    a["next_best_actions"]["initial"] = [
        {"action": "BLOCK_CARD", "route": "auto", "reason": "R2"},
    ]
    violations = check_invariants(a)
    assert any(v.invariant == "I7" for v in violations)


def test_invariant_8_missing_rule_citation() -> None:
    a = _mk_answer_minimal()
    a["next_best_actions"]["final"] = [
        {"action": "CREATE_CASE", "route": "auto", "reason": "no citation here"},
    ]
    a["next_best_actions"]["initial"] = a["next_best_actions"]["final"]
    violations = check_invariants(a)
    assert any(v.invariant == "I8" for v in violations)
