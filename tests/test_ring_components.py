"""Ring detector integration tests (v3).

Covers:
  (a) dual exposure buckets — ring_exposure_usd distinct from prior_fraud
  (b) device_family + p_expand_family behaviour
  (c) HHG-014 windowed Aug-Dec hits 24/24 U1 overlap
"""

from __future__ import annotations

import os

import pytest

from sentinel.config import RAW
from sentinel.data.features import build_all, connect
from sentinel.graph.client import TGClient

pytestmark = pytest.mark.skipif(
    not os.getenv("TG_HOST") or not os.getenv("TG_SECRET"),
    reason="Needs Savanna and a loaded FraudGraph.",
)

HHG14_CARD_TUPLE_ID = "C13487|555.0|150.0|mastercard|117.0|debit"
U1_CASES = ("CC-2649", "CC-2971", "CC-2985", "CC-3035")
WINDOW = {
    "p_window_start": "2016-08-01 00:00:00",
    "p_window_end":   "2016-12-31 23:59:59",
    "p_degree_cap":   100,
}


@pytest.fixture(scope="module")
def u1_reference_tuple_ids() -> set[str]:
    con = connect()
    build_all(con)
    ids = con.execute(
        f"""
        WITH cc AS (
          SELECT case_id, card_id AS own_card, connected_card_ids
          FROM read_csv_auto('{RAW / "closed_cases_history.csv"}', header=true)
          WHERE case_id IN {U1_CASES!r}
        ),
        expanded AS (
          SELECT own_card AS s FROM cc
          UNION
          SELECT TRIM(x.c) AS s FROM cc, LATERAL (SELECT unnest(split(connected_card_ids, '|')) c) x
        )
        SELECT DISTINCT cm.customer_id||'|'||cm.c2||'|'||cm.c3||'|'||cm.c4||'|'||cm.c5||'|'||cm.c6
        FROM expanded e
        JOIN card_map cm ON cm.card_id = e.s
        """
    ).fetchall()
    return {r[0] for r in ids}


@pytest.fixture(scope="module")
def tg() -> TGClient:
    return TGClient()


@pytest.fixture(scope="module")
def hhg14_ring(tg) -> dict:
    r = tg.run_query("FraudGraph", "ring_components", {
        "p_card": {"id": HHG14_CARD_TUPLE_ID},
        "p_expand_family": False,
        **WINDOW,
    })
    d: dict = {}
    for row in r["results"]:
        d.update(row)
    return d


@pytest.fixture(scope="module")
def hhg14_ring_family(tg) -> dict:
    r = tg.run_query("FraudGraph", "ring_components", {
        "p_card": {"id": HHG14_CARD_TUPLE_ID},
        "p_expand_family": True,
        **WINDOW,
    })
    d: dict = {}
    for row in r["results"]:
        d.update(row)
    return d


def test_u1_reference_is_24(u1_reference_tuple_ids):
    assert len(u1_reference_tuple_ids) == 24


def test_ring_covers_all_u1(hhg14_ring, u1_reference_tuple_ids):
    cards = {c["v_id"] if isinstance(c, dict) else c for c in hhg14_ring["cards"]}
    overlap = cards & u1_reference_tuple_ids
    assert overlap == u1_reference_tuple_ids, (
        f"only {len(overlap)}/{len(u1_reference_tuple_ids)} U1 cards captured"
    )
    assert len(cards) < 100, f"ring too broad: {len(cards)}"


def test_ring_family_expansion_broadens(hhg14_ring, hhg14_ring_family):
    """p_expand_family should widen the ring (more narrow profs, ≥ n_cards)."""
    assert hhg14_ring_family["n_cards"] >= hhg14_ring["n_cards"]
    assert len(hhg14_ring_family["narrow_devices"]) >= len(hhg14_ring["narrow_devices"])


def test_ring_exposure_separate_from_prior_fraud(hhg14_ring):
    """(a) ring_exposure_usd and prior_fraud_exposure_usd are separate metrics."""
    ring_exp = hhg14_ring["ring_exposure_usd"]
    prior_exp = hhg14_ring["@@prior_fraud_exposure_usd"]
    assert ring_exp > 0
    assert prior_exp > 0
    # Values should not equal each other by coincidence — they measure different things.
    assert ring_exp != prior_exp
    # Ring exposure has txn + closed-case components.
    assert hhg14_ring["@@ring_txn_exposure_usd"] > 0
    assert hhg14_ring["@@ring_cc_exposure_usd"] > 0
    assert (
        abs(
            ring_exp
            - hhg14_ring["@@ring_txn_exposure_usd"]
            - hhg14_ring["@@ring_cc_exposure_usd"]
        )
        < 0.01
    )


def test_new_device_and_proxy_buckets_populated(hhg14_ring):
    assert hhg14_ring["n_new_device_cards"] > 0
    assert hhg14_ring["n_proxied_cards"] > 0
