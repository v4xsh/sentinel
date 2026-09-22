"""Resolve case-pack card_ids (e.g. `C13487-K1`) to their PaymentCard
primary_ids in the graph (e.g. `C13487|555.0|150.0|mastercard|117.0|debit`).

Every graph-side query that takes a ``VERTEX<PaymentCard>`` needs the tuple
form, not the case-pack form.
"""

from __future__ import annotations

from functools import lru_cache

from sentinel.data.features import connect


@lru_cache(maxsize=1024)
def card_tuple_id(case_pack_card_id: str) -> str:
    """Return the pipe-separated PaymentCard.primary_id for a case-pack card_id.

    Falls back to the original string if not found (better than raising).
    """
    con = connect()
    row = con.execute(
        "SELECT customer_id, c2, c3, c4, c5, c6 FROM card_map WHERE card_id = ?",
        [case_pack_card_id],
    ).fetchone()
    if not row:
        return case_pack_card_id
    return f"{row[0]}|{row[1]}|{row[2]}|{row[3]}|{row[4]}|{row[5]}"


def txn_vertex_id(txn_id: str | int) -> str:
    """The Transaction vertex primary_id is the string form of TransactionID."""
    return f"T{int(txn_id)}" if not str(txn_id).startswith("T") else str(txn_id)
