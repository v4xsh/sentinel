"""Posterior + calibration helpers.

Given an EvidenceLedger, we:
  - sum log-LRs → posterior log-odds (prior = 0.5 → log-odds 0)
  - convert log-odds → raw probability
  - map raw → calibrated ``fraud_probability`` using the fitted isotonic
    calibrator (``sentinel/evidence/calibrator.json``)

The calibrator was trained on the same LR-table signals across the *whole*
labelled txn population (Phase-4). It maps p_raw ∈ [0, 1] → p_cal ∈ [0, 1]
via piecewise-constant interpolation on the stored (x_thresholds, y_thresholds).
"""

from __future__ import annotations

import bisect
import json
import math
from functools import lru_cache
from pathlib import Path

from sentinel.config import REPO_ROOT
from sentinel.evidence.ledger import EvidenceLedger

CALIBRATOR_PATH = REPO_ROOT / "sentinel" / "evidence" / "calibrator.json"


@lru_cache(maxsize=1)
def _load_calibrator() -> dict:
    return json.loads(CALIBRATOR_PATH.read_text())


def raw_probability_from_log_odds(log_odds: float) -> float:
    """σ(log_odds)."""
    if log_odds > 60:
        return 1.0
    if log_odds < -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-log_odds))


def calibrated_probability(p_raw: float) -> float:
    """Piecewise-constant lookup on the fitted isotonic knots.

    We use bisect to find the largest x ≤ p_raw and return the corresponding
    y. Falls back to y_min / y_max at the boundaries.
    """
    cal = _load_calibrator()
    xs = cal["x_thresholds"]
    ys = cal["y_thresholds"]
    if not xs:
        return p_raw
    if p_raw <= xs[0]:
        return float(cal.get("y_min", ys[0]))
    if p_raw >= xs[-1]:
        return float(cal.get("y_max", ys[-1]))
    idx = bisect.bisect_right(xs, p_raw) - 1
    return float(ys[idx])


LOG_ODDS_CAP = 4.0        # ±4.0 cap before calibration (≈ p ∈ [0.018, 0.982])
P_MIN = 0.02              # clamp calibrated probability into [0.02, 0.98]
P_MAX = 0.98


def score_ledger(ledger: EvidenceLedger, prior_log_odds: float = 0.0) -> dict:
    """Return {log_odds, p_raw, p_cal} for the ledger, with cap + clamp.

    C6v3-M: if the ledger contains an ``alert_model:__intercept__`` entry
    the isotonic calibrator (fitted on the old LR-table) is bypassed —
    the alert model is already well-calibrated (CV Brier 0.09). Otherwise
    the old flow is preserved for callers that don't use the alert_model.
    """
    delta = ledger.log_odds_delta()
    log_odds_raw = prior_log_odds + delta
    log_odds = max(-LOG_ODDS_CAP, min(LOG_ODDS_CAP, log_odds_raw))
    p_raw = raw_probability_from_log_odds(log_odds)
    uses_alert_model = any(
        (e.ref or "").startswith("alert_model:") for e in ledger.items
    )
    if uses_alert_model:
        p_cal = p_raw
    else:
        p_cal = calibrated_probability(p_raw)
    p_cal = max(P_MIN, min(P_MAX, p_cal))
    return {
        "log_odds": log_odds,
        "log_odds_uncapped": log_odds_raw,
        "p_raw": p_raw,
        "fraud_probability": p_cal,
        "log_lr_delta": delta,
        "capped": log_odds != log_odds_raw,
    }


def verdict_for(p: float, n_independent_channels: int = 0,
                customer_response: str | None = None) -> str:
    """C6v3-VR: customer response settles the verdict.

      * ``customer_response == "denied"``    ⇒ **fraud**
      * ``customer_response == "confirmed"`` ⇒ **legitimate**
      * ``customer_response == "no_reply"``  ⇒ **uncertain** (R4 branch)
      * no response yet: two-channel + 0.85/0.15 rule
    """
    if customer_response == "denied":
        return "fraud"
    if customer_response == "confirmed":
        return "legitimate"
    if customer_response == "no_reply":
        return "uncertain"
    # No response — apply two-channel gate.
    if p >= 0.85 and n_independent_channels >= 2:
        return "fraud"
    if p <= 0.15 and n_independent_channels >= 2:
        return "legitimate"
    return "uncertain"
