"""Tests for the enriched recurring_match query."""

from __future__ import annotations

import os

import pytest

from sentinel.graph.client import TGClient

pytestmark = pytest.mark.skipif(
    not os.getenv("TG_HOST") or not os.getenv("TG_SECRET"),
    reason="Needs Savanna + FraudGraph.",
)


@pytest.fixture(scope="module")
def tg() -> TGClient:
    return TGClient()


def _result(tg, card_tuple: str, prod: str, amt: float, tol: float = 0.02) -> dict:
    r = tg.run_query("FraudGraph", "recurring_match", {
        "p_card": {"id": card_tuple},
        "p_product_cd": prod, "p_amount": amt,
        "p_tolerance_cents": tol, "p_min_days_apart": 20,
    })
    d: dict = {}
    for row in r["results"]:
        d.update(row)
    return d


def test_hhg003_49_returns_multiple_regions(tg):
    """HHG-003's $49 W-product should hit multiple regions with non-trivial cadence."""
    d = _result(tg, "C08623|470.0|150.0|mastercard|137.0|credit", "W", 49.0)
    assert d["n"] >= 5, f"expected many hits, got {d['n']}"
    assert d["fires"] is True
    assert d["n_distinct_addr1"] >= 3, f"expected multiple regions, got {d['n_distinct_addr1']}"
    assert d["cadence_cv"] > 0, "cadence_cv should be a positive number for irregular hits"
    assert "expected_slots" in d and d["expected_slots"] > 0


def test_hhg014_74_96_no_recurring_history(tg):
    """HHG-014's flagged amt $74.96 C-product has no recurring history — should not fire."""
    d = _result(tg, "C13487|555.0|150.0|mastercard|117.0|debit", "C", 74.96)
    assert d["n"] <= 1
    assert d["fires"] is False
    assert d["cadence_cv"] == -1.0


def test_output_shape_is_stable(tg):
    """The enriched fields must be present so the evidence-ledger writer can rely on them."""
    d = _result(tg, "C08623|470.0|150.0|mastercard|137.0|credit", "W", 49.0)
    for k in ("n", "fires", "span_secs", "@@first_ts", "@@last_ts",
             "n_distinct_addr1", "regions_hit",
             "cadence_cv", "n_missed_slots", "expected_slots", "matches"):
        assert k in d, f"missing key {k}"
