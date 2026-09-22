"""R10 gate — count distinct customer card tuples with prior confirmed fraud.

Returns True iff the customer has ≥2 DISTINCT card tuples with a confirmed-
fraud ClosedCase whose ``closed_at < opened_at``. Optionally add the
current card if the current verdict is fraud (so the second distinct card
in the current case can push it over the threshold).

Data source: ``cc_join`` (derived from ``closed_cases_history.csv``).
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache

from sentinel.agent.id_resolver import card_tuple_id
from sentinel.data.features import connect


def prior_confirmed_fraud_card_tuples(customer_id: str, opened_at: str) -> set[str]:
    """Return the set of DISTINCT card_tuple_ids for confirmed-fraud closed
    cases on this customer with ``closed_at < opened_at``.
    """
    con = connect()
    rows = con.execute(
        """
        SELECT DISTINCT derived_card_id
        FROM cc_join
        WHERE customer_id = ?
          AND outcome = 'confirmed_fraud'
          AND closed_at < ?
        """,
        [customer_id, opened_at],
    ).fetchall()
    return {r[0] for r in rows if r[0]}


def prior_confirmed_fraud_on_two_cards(
    customer_id: str,
    opened_at: str,
    current_card_id: str | None = None,
    current_verdict: str | None = None,
) -> tuple[bool, int, list[str]]:
    """Return (gate_fires, distinct_count, distinct_card_ids).

    Includes ``current_card_id`` in the count iff ``current_verdict == "fraud"``.
    """
    tuples = prior_confirmed_fraud_card_tuples(customer_id, opened_at)
    if current_verdict == "fraud" and current_card_id:
        tuples.add(current_card_id)
    return (len(tuples) >= 2, len(tuples), sorted(tuples))
