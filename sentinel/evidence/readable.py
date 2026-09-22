"""Feature → analyst-note sentence table.

Every alert-model feature gets a plain-English claim so the ledger reads
like an analyst wrote it, not like a coefficient dump. The raw coefficient
lives in the ``ref`` string, not in the ``claim``.

Items whose |coefficient| < 0.05 are dropped in the caller (see
node_score_alert_model), and the model intercept is collapsed into a
single "Alert model baseline" line.
"""

from __future__ import annotations


def _decile_line(k: int, coef: float) -> str:
    dir_ = "toward fraud" if coef > 0 else "toward cleared"
    return (
        f"The bank's model scored this transaction in decile {k}/10; among "
        f"historical alerts this decile leans {dir_} (model weight "
        f"{coef:+.2f})."
    )


FEATURE_SENTENCE = {
    "id_15_new": lambda c: (
        f"Vesta's `id_15` flag is 'New' — a first-seen-on-this-card device. "
        f"In the labelled history this coincides more with cleared alerts "
        f"(travel, new phone) than with confirmed fraud (model weight {c:+.2f})."
    ),
    "proxy_flag": lambda c: (
        f"Connection went through an anonymous or transparent proxy "
        f"(`id_23` starts with IP_PROXY). Historically this correlates "
        f"with cleared reviews as often as fraud (model weight {c:+.2f})."
    ),
    "is_new_device_for_card": lambda c: (
        f"Computed 'new device on this card' — the device wasn't seen on "
        f"the card in the prior 90 days (weight {c:+.2f})."
    ),
    "unseen_productcd": lambda c: (
        f"Purchase category (`ProductCD`) is one this card had never used "
        f"before (weight {c:+.2f})."
    ),
    "unseen_p_emaildomain": lambda c: (
        f"Purchaser email domain is new for this card (weight {c:+.2f})."
    ),
    "out_of_home_region_asof": lambda c: (
        f"In-person charge in a billing region that isn't the card's "
        f"observed home region (weight {c:+.2f})."
    ),
    "had_prior_txn_in_region": lambda c: (
        f"The card had prior activity in this billing region, so the "
        f"out-of-region signal is weakened (weight {c:+.2f})."
    ),
    "prior_cleared_travel_on_card": lambda c: (
        f"This card has a prior cleared 'confirmed travel' case; the "
        f"travel-region false-alarm archetype fits (weight {c:+.2f})."
    ),
    "prior_cleared_new_phone_on_card": lambda c: (
        f"This card has a prior cleared 'new phone' case (weight {c:+.2f})."
    ),
    "prior_fraud_on_card_tuple": lambda c: (
        f"This card tuple has at least one prior *confirmed fraud* case "
        f"closed before this transaction (weight {c:+.2f})."
    ),
    "prior_fraud_on_customer": lambda c: (
        f"The customer has at least one prior confirmed-fraud case on a "
        f"different card (weight {c:+.2f})."
    ),
    "mixed_channel_last_24h": lambda c: (
        f"The card had both online and in-person activity in the 24 hours "
        f"before this transaction (weight {c:+.2f})."
    ),
    "recurring_ge3": lambda c: (
        f"Card has ≥3 prior transactions at the same ProductCD and amount "
        f"(±$0.02) — recurring shape (weight {c:+.2f})."
    ),
    "amt_z_gt_2": lambda c: (
        f"Amount is ≥2σ away from this card's typical spend (weight {c:+.2f})."
    ),
    "amt_z_gt_3": lambda c: (
        f"Amount is ≥3σ away from this card's typical spend (weight {c:+.2f})."
    ),
    "n_online_48h_2_4": lambda c: (
        f"Card had 2-4 online transactions in the last 48 hours (weight {c:+.2f})."
    ),
    "n_online_48h_ge_5": lambda c: (
        f"Card had ≥5 online transactions in the last 48 hours (weight {c:+.2f})."
    ),
    "channel_online": lambda c: (
        f"Alerted transaction is on the online channel (weight {c:+.2f})."
    ),
    "channel_in_person": lambda c: (
        f"Alerted transaction is on the in-person / card-present channel "
        f"(weight {c:+.2f})."
    ),
    "device_neighbors_ge2": lambda c: (
        f"At least 2 other cards touched the same device profile in the "
        f"investigation window (weight {c:+.2f})."
    ),
    "region_cluster_ge2": lambda c: (
        f"At least 2 other cards had activity in this same billing region "
        f"in the window (weight {c:+.2f})."
    ),
    "recipient_cluster_ge2": lambda c: (
        f"At least 2 other cards used the same recipient email domain in "
        f"the window (weight {c:+.2f})."
    ),
    "testing_sequence_fires": lambda c: (
        f"The card-testing sequence signature fires: several sub-$5 online "
        f"charges followed by a larger one (weight {c:+.2f})."
    ),
    "near_threshold_burst": lambda c: (
        f"Card had ≥3 online charges just under a round dollar threshold in "
        f"the window — structuring shape (weight {c:+.2f})."
    ),
}

# Decile handlers.
for _k in range(1, 11):
    FEATURE_SENTENCE[f"risk_decile_{_k}"] = (lambda c, _k=_k: _decile_line(_k, c))

# Device tier × degree bucket templates.
_TIER_DESC = {
    "T1": "the device has ≥1 confirmed-fraud ClosedCase already attached to it",
    "T2": "the device has a confirmed-fraud CC AND this txn is proxied",
    "T3": ("the device has a confirmed-fraud CC, this txn is proxied, AND "
           "≥2 other cards showed New/proxied txns on it within ±14 days"),
    "T4": "the device has confirmed-fraud CCs from ≥2 distinct customers",
}
_BUCKET_DESC = {"le5": "narrow device (≤5 cards)",
                "6-20": "medium device (6-20 cards)",
                "21-100": "broad device (21-100 cards)"}
for _t, _tdesc in _TIER_DESC.items():
    for _b, _bdesc in _BUCKET_DESC.items():
        _name = f"{_t}_{_b}"
        FEATURE_SENTENCE[_name] = (
            lambda c, _t=_t, _tdesc=_tdesc, _bdesc=_bdesc:
            f"Device tier {_t} on a {_bdesc}: {_tdesc} (weight {c:+.2f})."
        )


def sentence_for(feature: str, coef: float) -> str:
    """Human-readable analyst note for one alert-model feature."""
    fn = FEATURE_SENTENCE.get(feature)
    if fn is not None:
        try:
            return fn(coef)
        except Exception:
            pass
    return f"Alert-model feature `{feature}` fired (weight {coef:+.2f})."
