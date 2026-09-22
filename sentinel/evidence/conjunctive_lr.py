"""Estimate joint likelihood ratios from closed-case history.

For a conjunctive predicate (e.g. ``unseen_productcd AND
n_online_last_48h_bucket IN ('2-4', '5+') AND amt_z_asof > 3``), we compute
its LR directly from the fraud vs non-fraud populations rather than summing
the component log-LRs (which double-counts correlations).

Cached to disk in ``data/parquet/conjunctive_lr_cache.json``.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import duckdb

from sentinel.config import REPO_ROOT
from sentinel.data.features import connect

logger = logging.getLogger(__name__)

CACHE_PATH = REPO_ROOT / "data" / "parquet" / "conjunctive_lr_cache.json"


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def joint_lr(
    predicate_sql: str,
    scope_sql: str = "1=1",
    con: duckdb.DuckDBPyConnection | None = None,
    laplace_alpha: float = 1.0,
) -> dict:
    """Return the joint LR of ``predicate_sql`` within ``scope_sql``.

    The SQL should reference ``txn_features`` aliased as ``t`` (as the
    LR table's specs do). Caches on the (predicate, scope) pair.

    Returns a dict with:
      - ``lr``, ``log_lr``
      - ``p_pos``, ``p_neg`` (Laplace-smoothed)
      - ``a_pos``, ``b_neg`` raw counts
      - ``n_pos``, ``n_neg`` scope totals
    """
    cache = _load_cache()
    key = f"{scope_sql}|{predicate_sql}"
    if key in cache:
        return cache[key]

    if con is None:
        con = connect()

    n_pos = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM fraud_txn_labels) "
        f"AND ({scope_sql})"
    ).fetchone()[0]
    n_neg = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM negative_txn_labels) "
        f"AND ({scope_sql})"
    ).fetchone()[0]
    a = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM fraud_txn_labels) "
        f"AND ({scope_sql}) AND ({predicate_sql})"
    ).fetchone()[0]
    b = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM negative_txn_labels) "
        f"AND ({scope_sql}) AND ({predicate_sql})"
    ).fetchone()[0]

    p_pos = (a + laplace_alpha) / (n_pos + 2 * laplace_alpha)
    p_neg = (b + laplace_alpha) / (n_neg + 2 * laplace_alpha)
    lr = p_pos / p_neg if p_neg > 0 else float("inf")
    log_lr = math.log(lr) if lr > 0 else float("-inf")
    result = {
        "lr": round(lr, 4), "log_lr": round(log_lr, 4),
        "p_pos": round(p_pos, 6), "p_neg": round(p_neg, 6),
        "a_pos": a, "b_neg": b, "n_pos": n_pos, "n_neg": n_neg,
    }
    cache[key] = result
    _save_cache(cache)
    return result


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Smoke test: CNP burst joint LR.
    r = joint_lr(
        "t.unseen_productcd = TRUE AND t.n_online_last_48h_bucket IN ('2-4', '5+') "
        "AND t.amt_z_asof IS NOT NULL AND abs(t.amt_z_asof) > 3.0",
        scope_sql="t.channel = 'online'",
    )
    print("CNP burst joint LR:", r)

    # And a card-testing conjunction.
    r2 = joint_lr(
        "t.n_online_last_48h_bucket = '5+' AND t.TransactionAmt < 5",
        scope_sql="t.channel = 'online'",
    )
    print("card-testing joint LR:", r2)
