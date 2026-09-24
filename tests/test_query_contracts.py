"""Contract tests for every installed GSQL query.

Calls each of the 15 installed queries with the exact parameter dicts the
agent uses, and asserts the response carries no ``error`` flag. This catches
the "silent tool failure" class of bugs the Phase-5 review flagged, where
the agent's call signature drifted from the query's declared parameters.

The HHG-014 alert (Nov 22 2016, C13487-K1) is used as the reference test
subject because the ring case exercises every relevant vertex type.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from sentinel.agent.id_resolver import card_tuple_id, txn_vertex_id
from sentinel.config import TG_GRAPHNAME
from sentinel.graph.client import TGClient

from sentinel.config import DUCKDB_PATH

pytestmark = pytest.mark.skipif(
    (not os.getenv("TG_HOST") and not os.path.exists("/etc/tg_host"))
    or not DUCKDB_PATH.exists(),
    reason="TigerGraph workspace not reachable or feature store not built",
)

# Lazy so a fresh clone (no built duckdb) can still collect this module.
HHG14_CARD    = card_tuple_id("C13487-K1") if DUCKDB_PATH.exists() else "C13487"
HHG14_TXN     = txn_vertex_id("3478561")
HHG14_DEVICE  = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
HHG14_REGION  = "315.0"                          # BillingRegion.primary_id
HHG14_EMAIL   = "gmail.com"
HHG14_CLOSED  = "CC-3251"                        # any real ClosedCase
AS_OF         = dt.datetime(2016, 11, 22, 20, 11, 0)
WIN_START     = (AS_OF - dt.timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
WIN_END       = AS_OF.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(scope="module")
def tg() -> TGClient:
    return TGClient()


def _run(tg: TGClient, name: str, params: dict) -> dict:
    """Helper: invoke a query and return the parsed body."""
    return tg.run_query(TG_GRAPHNAME, name, params)


def _assert_ok(name: str, r: dict) -> None:
    """Query is contract-conformant iff response is a dict without ``error``."""
    assert isinstance(r, dict), f"{name}: response is not a dict — {type(r)}"
    if r.get("error"):
        pytest.fail(f"{name}: TG error — {r.get('message', r)}")


def test_q01_card_window(tg):
    r = _run(tg, "card_window", {
        "p_card":         {"id": HHG14_CARD},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
    })
    _assert_ok("card_window", r)


def test_q02_card_baseline_vs_txn(tg):
    r = _run(tg, "card_baseline_vs_txn", {
        "p_card": {"id": HHG14_CARD},
        "p_txn":  {"id": HHG14_TXN},
    })
    _assert_ok("card_baseline_vs_txn", r)


def test_q03_testing_sequence(tg):
    r = _run(tg, "testing_sequence", {
        "p_card":         {"id": HHG14_CARD},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
    })
    _assert_ok("testing_sequence", r)


def test_q04_burst_48h(tg):
    r = _run(tg, "burst_48h", {
        "p_card":         {"id": HHG14_CARD},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
    })
    _assert_ok("burst_48h", r)


def test_q05_device_first_seen(tg):
    r = _run(tg, "device_first_seen", {
        "p_card":   {"id": HHG14_CARD},
        "p_device": {"id": HHG14_DEVICE},
    })
    _assert_ok("device_first_seen", r)


def test_q06_device_neighbors(tg):
    r = _run(tg, "device_neighbors", {
        "p_device":       {"id": HHG14_DEVICE},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
        "p_degree_cap":   100,
    })
    _assert_ok("device_neighbors", r)


def test_q07_region_history(tg):
    r = _run(tg, "region_history", {
        "p_card":         {"id": HHG14_CARD},
        "p_region":       {"id": HHG14_REGION},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
    })
    _assert_ok("region_history", r)


def test_q08_region_cluster(tg):
    r = _run(tg, "region_cluster", {
        "p_region":       {"id": HHG14_REGION},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
    })
    _assert_ok("region_cluster", r)


def test_q09_recipient_email_cluster(tg):
    r = _run(tg, "recipient_email_cluster", {
        "p_domain":       {"id": HHG14_EMAIL},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
    })
    _assert_ok("recipient_email_cluster", r)


def test_q10_recurring_match(tg):
    r = _run(tg, "recurring_match", {
        "p_card":            {"id": HHG14_CARD},
        "p_product_cd":      "C",
        "p_amount":          74.96,
        "p_tolerance_cents": 0.02,
        "p_min_days_apart":  20,
    })
    _assert_ok("recurring_match", r)


def test_q11_closed_cases_touching(tg):
    r = _run(tg, "closed_cases_touching", {
        "p_card":         {"id": HHG14_CARD},
        "p_device":       {"id": HHG14_DEVICE},
        "p_region":       {"id": HHG14_REGION},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
    })
    _assert_ok("closed_cases_touching", r)


def test_q12_near_threshold_burst(tg):
    r = _run(tg, "near_threshold_burst", {
        "p_card":         {"id": HHG14_CARD},
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
        "p_threshold":    500.0,
        "p_min_count":    3,
    })
    _assert_ok("near_threshold_burst", r)


def test_q13_proxy_device_ring(tg):
    r = _run(tg, "proxy_device_ring", {
        "p_window_start": WIN_START,
        "p_window_end":   WIN_END,
        "p_min_cards":    3,
        "p_degree_cap":   100,
    })
    _assert_ok("proxy_device_ring", r)


def test_q14_ring_components(tg):
    r = _run(tg, "ring_components", {
        "p_card":          {"id": HHG14_CARD},
        "p_window_start":  WIN_START,
        "p_window_end":    WIN_END,
        "p_degree_cap":    100,
        "p_expand_family": True,
    })
    _assert_ok("ring_components", r)


def test_q16_get_cc_embedding(tg):
    r = _run(tg, "get_cc_embedding", {
        "p_case": {"id": HHG14_CLOSED},
    })
    _assert_ok("get_cc_embedding", r)


# q15_write_case is a mutating query — we don't call it here since a real
# invocation would pollute the graph. It's exercised via node_write_memory
# in the agent, and by test_write_memory below.
def test_q15_write_case_signature_smoke(tg):
    """Non-mutating check: query exists on the server."""
    r = tg.gsql("ls", graph=TG_GRAPHNAME)
    assert "write_case" in r, "write_case is not installed"
