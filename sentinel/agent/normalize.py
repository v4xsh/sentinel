"""Ledger de-duplication rules applied before posterior scoring.

Prevents double-counting correlated signals:

  * Prior-fraud family: if `prior_fraud_on_card_tuple` fires, drop
    `prior_fraud_on_customer` (the tuple-level signal already implies the
    customer-level one and they are strongly correlated).
  * New-device family: keep at most one of `is_new_device_for_card` and
    `id_15_new`. If a conjunctive `new_device + proxy` evidence item exists
    (from `detect_cnp_new_device`), prefer that and drop the single-signal
    versions. Otherwise keep the item with the larger |log_lr|.

Applied in-place. Returns the modified ledger for convenience.
"""

from __future__ import annotations

from sentinel.evidence.ledger import Evidence, EvidenceLedger


PRIOR_FRAUD_TUPLE_REFS = {"signal:prior_fraud_on_card_tuple"}
PRIOR_FRAUD_CUST_REFS  = {"signal:prior_fraud_on_customer"}

NEW_DEVICE_SINGLE_REFS = {
    "signal:is_new_device_for_card",
    "signal:id_15_new",
    "lr_table:is_new_device_for_card",
    "lr_table:id_15_new",
}
NEW_DEVICE_CONJ_REFS = {"conjunctive-lr:new_device", "conjunctive-lr:new_device + proxy"}


def deduplicate(ledger: EvidenceLedger) -> EvidenceLedger:
    """Apply dedupe rules in-place."""
    items = list(ledger.items)

    # ---- Rule 1: prior_fraud_on_card_tuple dominates prior_fraud_on_customer.
    has_tuple = any(_matches(e, PRIOR_FRAUD_TUPLE_REFS) for e in items)
    if has_tuple:
        items = [e for e in items if not _matches(e, PRIOR_FRAUD_CUST_REFS)]

    # ---- Rule 2: new-device family — keep only the strongest signal.
    conj_evs   = [e for e in items if _matches(e, NEW_DEVICE_CONJ_REFS)]
    single_evs = [e for e in items if _matches(e, NEW_DEVICE_SINGLE_REFS)]
    if conj_evs:
        # Conjunctive item wins; drop the single ones.
        items = [e for e in items if not _matches(e, NEW_DEVICE_SINGLE_REFS)]
    elif len(single_evs) > 1:
        # Multiple singles — keep only the one with the largest |log_lr|.
        keeper = max(single_evs, key=lambda e: abs(e.log_lr))
        to_drop = [id(e) for e in single_evs if e is not keeper]
        items = [e for e in items if id(e) not in to_drop]

    ledger.items = items
    return ledger


def _matches(e: Evidence, ref_set: set[str]) -> bool:
    """True if ``e.ref`` contains any of ``ref_set``'s substrings."""
    ref = e.ref or ""
    return any(target in ref for target in ref_set)
