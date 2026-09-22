"""Phase 1 tests: card baselines for three hand-picked cards.

We spot-check by running the same aggregates directly against the Parquet source
and comparing to the tables materialized by ``sentinel.data.features``.
"""

from __future__ import annotations

import duckdb
import pytest

from sentinel.config import DUCKDB_PATH, PARQUET
from sentinel.data.features import (
    CardTuple,
    build_all,
    compare_txn_to_baseline,
    connect,
    emit_card_id,
    resolve_card_by_txn,
)

TX = f"read_parquet('{PARQUET / 'transactions.parquet'}')"


# Fixtures ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def con() -> duckdb.DuckDBPyConnection:
    """Rebuild the feature store once, then re-open read-only for tests.

    Keeping the R/W connection open would block every other session
    connection (duckdb's exclusive lock semantics), so we close it after
    the build and hand out a read-only view for the assertions.
    """
    writer = connect(read_only=False)
    build_all(writer)
    writer.close()
    c = connect(read_only=True)
    yield c
    c.close()


# HHG-003 is C08623-K2, flagged txn 3530164.
# HHG-007 is C09933-K2, flagged txn 3514948.
# HHG-011 is C11923-K2, flagged txn 3583368.
HAND_PICKED = [
    ("HHG-003", 3530164, "C08623-K2"),
    ("HHG-007", 3514948, "C09933-K2"),
    ("HHG-011", 3583368, "C11923-K2"),
]


# Tests -------------------------------------------------------------------------


def test_card_tuples_resolve_to_expected_kn(con):
    """resolve_card_by_txn + emit_card_id should reproduce the case-pack card_id."""
    for case, txn, expected in HAND_PICKED:
        tup = resolve_card_by_txn(con, txn)
        assert tup is not None, case
        emit = emit_card_id(con, tup)
        assert emit.card_id == expected, (
            f"{case}: expected {expected}, got {emit.card_id} (source={emit.source})"
        )
        assert emit.source == "case_pack"


def test_baseline_matches_direct_aggregate(con):
    """Baselines must equal a direct DuckDB aggregate for each hand-picked card."""
    for _case, txn, _expected in HAND_PICKED:
        tup = resolve_card_by_txn(con, txn)
        assert tup is not None

        # Direct aggregate on ALL txns (raw baseline).
        direct = con.execute(
            f"""
            SELECT COUNT(*), MEDIAN(TransactionAmt), MAX(TransactionAmt),
                   SUM(CASE WHEN channel='online' THEN 1 ELSE 0 END)
                    / CAST(COUNT(*) AS DOUBLE)
            FROM {TX} t
            WHERE {tup.sql_where()}
            """
        ).fetchone()
        stored = con.execute(
            """
            SELECT n_txns, median_amt, max_amt, online_share
            FROM card_baseline_raw
            WHERE customer_id=? AND c2=? AND c3=? AND c4=? AND c5=? AND c6=?
            """,
            [tup.customer_id, tup.c2, tup.c3, tup.c4, tup.c5, tup.c6],
        ).fetchone()
        assert stored is not None
        assert stored[0] == direct[0]
        assert stored[1] == pytest.approx(direct[1], rel=1e-6)
        assert stored[2] == pytest.approx(direct[2], rel=1e-6)
        assert stored[3] == pytest.approx(direct[3], rel=1e-6)


def test_clean_baseline_excludes_fraud_episode(con):
    """The clean baseline must count fewer or equal txns than the raw one.

    For any card whose txns overlap with confirmed-fraud txn_ids, the clean
    baseline must strictly drop those overlapping rows.
    """
    for _case, txn, _expected in HAND_PICKED:
        tup = resolve_card_by_txn(con, txn)
        n_all = con.execute(
            f"SELECT COUNT(*) FROM {TX} t WHERE {tup.sql_where()}"
        ).fetchone()[0]
        n_fraud = con.execute(
            f"""
            SELECT COUNT(*) FROM {TX} t
            WHERE {tup.sql_where()}
              AND TransactionID IN (SELECT TransactionID FROM fraud_txn_ids)
            """
        ).fetchone()[0]
        row = con.execute(
            """
            SELECT n_txns FROM card_baseline_clean
            WHERE customer_id=? AND c2=? AND c3=? AND c4=? AND c5=? AND c6=?
            """,
            [tup.customer_id, tup.c2, tup.c3, tup.c4, tup.c5, tup.c6],
        ).fetchone()
        clean_n = row[0] if row else 0
        assert clean_n == n_all - n_fraud, (
            f"card {tup}: expected {n_all - n_fraud} clean rows, got {clean_n}"
        )


def test_txn_baseline_comparison_smoke(con):
    """compare_txn_to_baseline() returns the derived deltas without error."""
    for _case, txn, _expected in HAND_PICKED:
        r = compare_txn_to_baseline(con, txn, baseline="clean")
        assert "amt_z" in r
        assert "is_new_device_for_card" in r
        assert "is_out_of_home_region" in r
        assert "is_unseen_product" in r
        # is_new_device_for_card only defined for online txns; must be bool either way.
        assert isinstance(r["is_new_device_for_card"], bool)


def test_hhg014_matches_u1_ring_device(con):
    """HHG-014's flagged txn should carry the exact U1 ring device signature."""
    r = compare_txn_to_baseline(con, 3478561, baseline="clean")
    assert r["device_profile"] is not None
    assert "SM-G935F" in r["device_profile"]
    assert "chrome 62.0 for android" in r["device_profile"]
    assert r["proxy_flag"] is True  # IP_PROXY:ANONYMOUS


def test_card_tuple_placeholders_for_nulls(con):
    """The null-tuple pattern (all-null card2..card6) resolves to placeholder '_'s.

    Each of card2..card6 emits one COALESCE(t.cardN::VARCHAR, '_') = '_', which
    contains two occurrences of the literal '_' -> 10 total across 5 columns.
    """
    tup = CardTuple("C00000-does-not-exist", "_", "_", "_", "_", "_")
    where = tup.sql_where()
    assert where.count("COALESCE") == 5
    assert where.count("'_'") == 10
