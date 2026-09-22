"""Fraud verdict must include BLOCK_CARD from ANY branch (R2, R6, R9, ...)
unless R7 fires. Route by exposure per §2.
"""

from __future__ import annotations

from sentinel.policy.policy_engine import PolicyDecision, PolicyInput, decide


def _has(actions, name):
    return any(a.action == name for a in actions)


def _routed(actions, name):
    for a in actions:
        if a.action == name:
            return a.route
    return None


def test_block_on_r9_undocumented_fraud_verdict():
    inp = PolicyInput(
        verdict="fraud",
        fraud_probability=0.95,
        exposure_usd=200.0,          # ≤ $2,500 ⇒ L1
        pattern="undocumented",
        undocumented_coordinated=True,
        signal_channels=frozenset(["device", "history"]),
    )
    d = decide(inp)
    assert _has(d.actions, "BLOCK_CARD"), \
        f"R9 fraud verdict must issue BLOCK_CARD, got: {[a.action for a in d.actions]}"
    assert _routed(d.actions, "BLOCK_CARD") == "L1"


def test_block_on_r9_uncertain_does_not_force_block():
    """R9 fires with uncertain verdict → BLOCK_CARD must NOT auto-inject."""
    inp = PolicyInput(
        verdict="uncertain",
        fraud_probability=0.60,
        exposure_usd=200.0,
        pattern="undocumented",
        undocumented_coordinated=True,
    )
    d = decide(inp)
    assert not _has(d.actions, "BLOCK_CARD"), \
        f"R9 with uncertain verdict must not force BLOCK_CARD, got: {[a.action for a in d.actions]}"


def test_block_on_r6_shared_origin_fraud():
    """R6 shared-origin fraud path — verdict fraud + shared_element."""
    inp = PolicyInput(
        verdict="fraud",
        fraud_probability=0.92,
        exposure_usd=800.0,
        pattern="account_takeover",
        shared_element="device",
        signal_channels=frozenset(["device", "customer"]),
    )
    d = decide(inp)
    assert _has(d.actions, "BLOCK_CARD")
    assert _routed(d.actions, "BLOCK_CARD") == "L1"


def test_block_routes_l2_when_exposure_over_2500():
    inp = PolicyInput(
        verdict="fraud",
        fraud_probability=0.90,
        exposure_usd=3000.0,
        pattern="card_not_present_fraud",
        signal_channels=frozenset(["amount", "customer"]),
    )
    d = decide(inp)
    assert _has(d.actions, "BLOCK_CARD")
    assert _routed(d.actions, "BLOCK_CARD") == "L2", \
        f"exposure > $2,500 must route BLOCK_CARD to L2, got: {_routed(d.actions,'BLOCK_CARD')}"


def test_r7_dispute_does_not_force_block():
    """R7 is the one exception — do NOT block on the dispute-recurring path."""
    inp = PolicyInput(
        verdict="uncertain",
        fraud_probability=0.65,
        exposure_usd=49.0,
        pattern="none",
        is_dispute=True,
        is_recurring_match=True,
        trigger_type="customer_report",
    )
    d = decide(inp)
    assert not _has(d.actions, "BLOCK_CARD"), \
        f"R7 must not block; got: {[a.action for a in d.actions]}"


def test_no_double_block_card():
    """BLOCK_CARD should only appear once even when a branch already issued it."""
    inp = PolicyInput(
        verdict="fraud",
        fraud_probability=0.95,
        exposure_usd=200.0,
        pattern="card_not_present_fraud",
        signal_channels=frozenset(["amount", "device"]),
    )
    d = decide(inp)
    names = [a.action for a in d.actions]
    assert names.count("BLOCK_CARD") == 1, f"expected exactly one BLOCK_CARD, got {names}"
