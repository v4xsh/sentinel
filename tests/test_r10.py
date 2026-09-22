"""R10 gate: ≥2 DISTINCT card tuples with prior confirmed fraud on this
customer (plus current card if current verdict is fraud).

Uses ``cc_join`` from DuckDB (real closed-case history). Picks a customer
who is known to have two distinct-tuple fraud closures.
"""

from __future__ import annotations

import pytest

from sentinel.agent.r10 import (
    prior_confirmed_fraud_card_tuples,
    prior_confirmed_fraud_on_two_cards,
)
from sentinel.data.features import connect


@pytest.fixture(scope="module")
def two_card_customer() -> str | None:
    """Find a customer with ≥2 distinct card tuples in confirmed-fraud closures."""
    con = connect()
    rows = con.execute(
        """
        SELECT customer_id, COUNT(DISTINCT derived_card_id) AS n_distinct
        FROM cc_join
        WHERE outcome = 'confirmed_fraud'
        GROUP BY customer_id
        HAVING n_distinct >= 2
        LIMIT 1
        """
    ).fetchall()
    return rows[0][0] if rows else None


@pytest.fixture(scope="module")
def one_card_customer() -> str | None:
    """Find a customer with exactly 1 distinct card tuple in fraud closures."""
    con = connect()
    rows = con.execute(
        """
        SELECT customer_id, COUNT(DISTINCT derived_card_id) AS n_distinct
        FROM cc_join
        WHERE outcome = 'confirmed_fraud'
        GROUP BY customer_id
        HAVING n_distinct = 1
        LIMIT 1
        """
    ).fetchall()
    return rows[0][0] if rows else None


def test_two_card_customer_fires(two_card_customer):
    if not two_card_customer:
        pytest.skip("no two-card customer in dataset")
    gate, n, cards = prior_confirmed_fraud_on_two_cards(
        two_card_customer, "2026-01-01 00:00:00")
    assert gate is True, f"expected R10 to fire for {two_card_customer}, got {n=} {cards=}"
    assert n >= 2
    assert len(cards) >= 2


def test_one_card_customer_does_not_fire(one_card_customer):
    if not one_card_customer:
        pytest.skip("no one-card customer in dataset")
    gate, n, cards = prior_confirmed_fraud_on_two_cards(
        one_card_customer, "2026-01-01 00:00:00")
    assert gate is False, f"R10 should NOT fire for a single-card customer, got {n=}"
    assert n == 1


def test_current_card_counts_when_verdict_fraud(one_card_customer):
    """If this card + verdict=fraud, the current card pushes count to 2 iff
    it's a DIFFERENT tuple from the prior one."""
    if not one_card_customer:
        pytest.skip("no one-card customer in dataset")

    prior_tuples = prior_confirmed_fraud_card_tuples(
        one_card_customer, "2026-01-01 00:00:00")
    prior = next(iter(prior_tuples))

    # Case A: current card is the SAME tuple → still 1.
    gate_same, n_same, _ = prior_confirmed_fraud_on_two_cards(
        one_card_customer, "2026-01-01 00:00:00",
        current_card_id=prior, current_verdict="fraud")
    assert n_same == 1
    assert gate_same is False

    # Case B: current card is DIFFERENT → count becomes 2, gate fires.
    fake = prior + "|synthetic"
    gate_diff, n_diff, _ = prior_confirmed_fraud_on_two_cards(
        one_card_customer, "2026-01-01 00:00:00",
        current_card_id=fake, current_verdict="fraud")
    assert n_diff == 2
    assert gate_diff is True


def test_current_card_does_not_count_when_verdict_uncertain(one_card_customer):
    """Even if the current card is a new tuple, verdict must be fraud to count."""
    if not one_card_customer:
        pytest.skip("no one-card customer in dataset")
    gate, n, _ = prior_confirmed_fraud_on_two_cards(
        one_card_customer, "2026-01-01 00:00:00",
        current_card_id="never|seen|before|tuple|abc|xyz",
        current_verdict="uncertain")
    assert n == 1
    assert gate is False


def test_time_gating(two_card_customer):
    """A closed_at BEFORE 2016-07-01 should still count (all data is
    July-October 2016). An opened_at BEFORE all closed cases should give 0.
    """
    if not two_card_customer:
        pytest.skip("no two-card customer in dataset")
    gate_early, n_early, _ = prior_confirmed_fraud_on_two_cards(
        two_card_customer, "2016-01-01 00:00:00")
    assert n_early == 0
    assert gate_early is False
