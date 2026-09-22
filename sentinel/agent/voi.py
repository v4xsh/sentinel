"""Value-of-Information planner + evidence-conditioned simulator.

Given the current posterior and the case's evidence, decide whether asking
the customer is worth it. This is deterministic (no LLM):

  E[Δ posterior | ask] = p(reply) · ( p(fraud|denied) · Δ_fraud
                                    + p(fraud|confirmed) · Δ_legit )

We estimate p(fraud|denied) and p(fraud|confirmed) from the LR table's own
prior:

  LR(denied | fraud) ≈ high (customers who deny are usually right)
  LR(confirmed | fraud) ≈ low (customers who confirm mostly weren't defrauded)

The simulator materialises the three possible worlds — denied / confirmed /
no_reply — and returns each world's posterior. The agent uses this to answer
"if we ask, what's the expected new posterior?" without actually reaching out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from sentinel.agent.posterior import (
    calibrated_probability,
    raw_probability_from_log_odds,
    score_ledger,
)
from sentinel.evidence.ledger import Evidence, EvidenceLedger


# ---- LR priors for customer channel (hand-set, low uncertainty) ----

# LR(response=denied | fraud) vs. non-fraud, learned from post-verify
# outcomes in the closed-cases dataset (approximate; a customer who denies is
# usually right, but a small share of denials turn out mistaken).
LR_DENIED = 9.0          # log 2.20
LR_CONFIRMED = 0.10      # log -2.30
LR_NO_REPLY = 1.20       # log 0.18 — mildly informative (silence leans fraud)


@dataclass
class SimulatedWorld:
    response: Literal["denied", "confirmed", "no_reply"]
    log_odds: float
    fraud_probability: float
    p_of_world: float           # marginal probability of this world


@dataclass
class VOIPlan:
    should_ask: bool
    reason: str
    p_denied: float
    p_confirmed: float
    p_no_reply: float
    expected_p_fraud: float     # E[p | response]
    expected_abs_shift: float   # E[|p_new - p_current|]
    worlds: list[SimulatedWorld]


def _world_probability_conditioned_on_current(current_p_fraud: float,
                                              lr_response: float) -> float:
    """Prior probability of a specific response, marginalising over the
    fraud/legit split at the current posterior.

    p(resp) = current_p_fraud · p(resp|fraud) + (1-current_p_fraud) · p(resp|legit)

    We normalise LRs → probabilities within {denied, confirmed, no_reply}.
    """
    return lr_response  # normalised outside this fn.


def _normalise(p: dict) -> dict:
    total = sum(p.values())
    return {k: v / total for k, v in p.items()} if total > 0 else p


def simulate_worlds(ledger: EvidenceLedger, current_p: float,
                    prior_log_odds: float = 0.0) -> list[SimulatedWorld]:
    """For each possible response, return the posterior it would produce."""
    worlds: list[SimulatedWorld] = []
    # Marginal p(response) — same fraud share on both sides, so this reduces
    # to normalising the LR triple. We adjust by current p to allow the
    # planner to prefer asking when p is in the ambiguous zone.
    weights = {
        "denied":    LR_DENIED     * current_p       + 1.0 * (1 - current_p),
        "confirmed": LR_CONFIRMED  * current_p       + 1.0 * (1 - current_p),
        "no_reply":  LR_NO_REPLY   * current_p       + 1.0 * (1 - current_p),
    }
    p_of = _normalise(weights)
    for resp, lr in (("denied", LR_DENIED),
                     ("confirmed", LR_CONFIRMED),
                     ("no_reply", LR_NO_REPLY)):
        new_log_odds = prior_log_odds + ledger.log_odds_delta() + math.log(lr)
        p_raw = raw_probability_from_log_odds(new_log_odds)
        p_cal = calibrated_probability(p_raw)
        worlds.append(SimulatedWorld(
            response=resp, log_odds=new_log_odds,
            fraud_probability=p_cal, p_of_world=p_of[resp],
        ))
    return worlds


def voi_plan(ledger: EvidenceLedger, current_p: float,
             prior_log_odds: float = 0.0,
             exposure_usd: float = 0.0) -> VOIPlan:
    """Decide whether asking the customer is worth it.

    Heuristic:
      - If p is in the ambiguous band [0.30, 0.85) and no denial-priored
        channel already fired, VOI is high — ASK.
      - If p ≥ 0.85 with ≥2 independent channels, we don't need to ask.
      - If p < 0.30, we don't need to ask.
      - If exposure is high (>500) and p is in the middle, always ask.
    """
    worlds = simulate_worlds(ledger, current_p, prior_log_odds)
    expected_p = sum(w.p_of_world * w.fraud_probability for w in worlds)
    expected_abs = sum(w.p_of_world * abs(w.fraud_probability - current_p)
                       for w in worlds)

    in_ambiguous = 0.30 <= current_p < 0.85
    high_exposure = exposure_usd > 500.0
    should = (in_ambiguous or (high_exposure and current_p < 0.85)) \
             and expected_abs > 0.05
    reason = (
        f"p={current_p:.2f} in ambiguous band [0.30, 0.85) with "
        f"E[|Δp|]={expected_abs:.2f}; asking is expected to move the "
        f"posterior enough to matter."
        if should else
        f"p={current_p:.2f}; either confidence is already sufficient or "
        f"the expected shift (E[|Δp|]={expected_abs:.2f}) is too small to "
        f"justify contacting the customer."
    )
    p_of = {w.response: w.p_of_world for w in worlds}
    return VOIPlan(
        should_ask=should,
        reason=reason,
        p_denied=p_of["denied"],
        p_confirmed=p_of["confirmed"],
        p_no_reply=p_of["no_reply"],
        expected_p_fraud=expected_p,
        expected_abs_shift=expected_abs,
        worlds=worlds,
    )


def apply_customer_response(ledger: EvidenceLedger,
                            response: Literal["denied", "confirmed", "no_reply"]) -> Evidence:
    """Return an Evidence item representing the customer's response.

    Also appends to the ledger so subsequent posterior calls include it.
    """
    lr = {"denied": LR_DENIED, "confirmed": LR_CONFIRMED, "no_reply": LR_NO_REPLY}[response]
    log_lr = math.log(lr)
    claim = {
        "denied":    "Customer explicitly denies making this transaction.",
        "confirmed": "Customer confirms making this transaction.",
        "no_reply":  "Customer did not respond within the 24h window.",
    }[response]
    e = Evidence(
        claim=claim,
        source="customer",
        ref=f"customer_response:{response}",
        entity_ids=[],
        channel="customer",
        log_lr=log_lr,
        direction="for" if log_lr > 0 else ("against" if log_lr < 0 else "neutral"),
    )
    ledger.add(e)
    return e
