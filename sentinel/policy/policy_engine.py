"""Sentinel's policy engine — pure functions.

Encodes the Fraud Policy (v1.0) from ``docs/POLICY_EXTRACT.md``:
  - Section 1: 14 canonical actions.
  - Section 2: approval routing (auto / L1 / L2), including the $2,500 split
    on ``BLOCK_CARD``.
  - Rules R1 through R10.
  - Section 3a: case-open triggers + SAR triggers.
  - Section 3b: initial vs final action recomputation.

The engine takes a :class:`PolicyInput` describing the current investigation
state and returns a :class:`PolicyDecision` containing an ordered list of
:class:`Action` (action name + route + reason with rule citation).

All logic is deterministic. The LLM never enters this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Literal, Optional


# ---------- exact string identifiers ------------------------------------------


VerdictEnum = Literal["fraud", "legitimate", "uncertain"]
PatternEnum = Literal[
    "card_testing",
    "card_not_present_fraud",
    "card_not_present_new_device",
    "out_of_region_use",
    "account_takeover",
    "undocumented",
    "none",
]
Route = Literal["auto", "L1", "L2"]
CustomerResponse = Literal["denied", "confirmed", "no_reply"]

ACTIONS_AUTO: set[str] = {
    "ALLOW_TRANSACTION",
    "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER",
    "VERIFY_WITH_CUSTOMER",
    "STEP_UP_AUTH",
    "GENERATE_REPORT",
    "CREATE_CASE",
    "ESCALATE_TO_ANALYST",
    "CLOSE_NO_FRAUD",
}
# BLOCK_CARD depends on exposure — handled in route_for().
ACTIONS_L1_TABLE: set[str] = {"DECLINE_TRANSACTION"}
ACTIONS_L2_TABLE: set[str] = {"BLOCK_ALL_CARDS", "FILE_REPORT"}


# ---------- data classes ------------------------------------------------------


@dataclass(frozen=True)
class Action:
    action: str
    route: Route
    reason: str

    def as_dict(self) -> dict:
        return {"action": self.action, "route": self.route, "reason": self.reason}


@dataclass
class PolicyInput:
    """Everything the policy engine needs to decide.

    Fields intentionally overlap with the answer-JSON's ``case`` block so a
    Phase-5 agent can populate this from evidence + LR + calibrator.
    """
    verdict: VerdictEnum
    fraud_probability: float
    exposure_usd: float
    pattern: PatternEnum
    signal_channels: frozenset[str] = field(default_factory=frozenset)
    shared_element: Optional[Literal["device", "region", "recipient"]] = None
    customer_response: Optional[CustomerResponse] = None
    is_dispute: bool = False
    is_recurring_match: bool = False
    prior_confirmed_fraud_on_two_cards: bool = False
    credentials_confirmed_compromised: bool = False
    undocumented_coordinated: bool = False
    testing_sequence_fires: bool = False
    testing_sequence_over_100_cleared: bool = False
    # New in P5v4-3: trigger context steers the "uncertain, no reply yet" branch.
    trigger_type: Optional[str] = None      # 'risk_score' | 'customer_report' | 'analyst_request'
    channel: Optional[str] = None           # 'online' | 'in_person'
    id_15_new: bool = False                 # Vesta's device-new flag on this txn


@dataclass
class PolicyDecision:
    actions: list[Action]
    case_should_open: bool
    sar_should_file: bool
    escalate: bool


# ---------- routing ------------------------------------------------------------


def route_for(action: str, *, exposure_usd: float = 0.0) -> Route:
    """Return the approval route for ``action`` under §2 of the policy."""
    if action in ACTIONS_AUTO:
        return "auto"
    if action == "BLOCK_CARD":
        return "L1" if exposure_usd <= 2500.0 else "L2"
    if action in ACTIONS_L1_TABLE:
        return "L1"
    if action in ACTIONS_L2_TABLE:
        return "L2"
    raise ValueError(f"unknown action: {action}")


def _mk(action: str, reason: str, *, exposure_usd: float = 0.0) -> Action:
    return Action(action=action, route=route_for(action, exposure_usd=exposure_usd), reason=reason)


# ---------- rule primitives ---------------------------------------------------


def _independent_signal_count(channels: Iterable[str]) -> int:
    return len({c for c in channels if c})


def _sar_should_file(inp: PolicyInput) -> bool:
    """§3a SAR trigger.

    Sarcastically strict: fraud is confirmed OR strongly suspected (p >= 0.85),
    AND one of:
      - exposure_usd > 1000
      - activity connects to a shared device profile / region cluster / other card's fraud
      - pattern is undocumented / coordinated (R9)
    """
    strongly_suspected = (
        inp.verdict == "fraud"
        or (inp.verdict != "legitimate" and inp.fraud_probability >= 0.85)
    )
    if not strongly_suspected:
        return False
    if inp.exposure_usd > 1000.0:
        return True
    if inp.shared_element is not None:
        return True
    if inp.pattern == "undocumented" or inp.undocumented_coordinated:
        return True
    return False


def _case_should_open(inp: PolicyInput, evidence_requests_present: bool) -> bool:
    """§3a case-open trigger."""
    if inp.fraud_probability >= 0.30:
        return True
    if inp.is_dispute:
        return True
    if evidence_requests_present:
        return True
    return False


# ---------- decide() ----------------------------------------------------------


def _ensure_block_card_on_fraud(decision: "PolicyDecision", inp: PolicyInput,
                                r7_fired: bool) -> "PolicyDecision":
    """Finalise a fraud-verdict decision.

    Guarantees, unless R7 fired:
      * BLOCK_CARD present (routed by exposure per §2).
      * §3a: FILE_REPORT if exposure > $1,000 (regardless of shared element).
      * R6:  MONITOR_CONNECTED_CARDS if shared_element is set.
      * R5:  STEP_UP_AUTH if pattern == card_testing (testing-sequence bucket).

    Already-present actions are not duplicated.
    """
    if r7_fired or inp.verdict != "fraud":
        return decision

    actions = list(decision.actions)
    names = {a.action for a in actions}
    sar_should_file = decision.sar_should_file

    if "BLOCK_CARD" not in names:
        actions.append(_mk("BLOCK_CARD",
                           "R2 (guaranteed on fraud verdict): block + reissue",
                           exposure_usd=inp.exposure_usd))
        names.add("BLOCK_CARD")

    if inp.exposure_usd > 1000.0 and "FILE_REPORT" not in names:
        actions.append(_mk("FILE_REPORT",
                           f"§3a: fraud verdict AND exposure ${inp.exposure_usd:.2f} > $1,000"))
        names.add("FILE_REPORT")
        sar_should_file = True

    if (inp.shared_element or inp.pattern in ("undocumented", "card_testing")) \
            and "MONITOR_CONNECTED_CARDS" not in names:
        reason = (f"R6: monitor cards sharing this {inp.shared_element}"
                  if inp.shared_element
                  else f"R6: monitor cards linked by the {inp.pattern} shape")
        actions.append(_mk("MONITOR_CONNECTED_CARDS", reason))
        names.add("MONITOR_CONNECTED_CARDS")

    if inp.pattern == "card_testing" and "STEP_UP_AUTH" not in names:
        actions.append(_mk("STEP_UP_AUTH",
                           "R5: card_testing pattern — require one-time passcode "
                           "on further activity"))
        names.add("STEP_UP_AUTH")

    return PolicyDecision(
        actions=actions,
        case_should_open=decision.case_should_open,
        sar_should_file=sar_should_file,
        escalate=decision.escalate,
    )


def decide(inp: PolicyInput, *, evidence_requests_present: bool = False) -> PolicyDecision:
    """Wrapper — runs :func:`_decide_impl` then enforces BLOCK_CARD-on-fraud."""
    r7_fires = bool(
        inp.is_recurring_match and inp.is_dispute
        and (inp.trigger_type is None or inp.trigger_type == "customer_report")
    )
    decision = _decide_impl(inp, evidence_requests_present=evidence_requests_present)
    return _ensure_block_card_on_fraud(decision, inp, r7_fired=r7_fires)


def _decide_impl(inp: PolicyInput, *, evidence_requests_present: bool = False) -> PolicyDecision:
    """Return the ordered action list under Fraud Policy v1.0.

    Ordering (first-to-last): what happens first in the workflow.
    """
    actions: list[Action] = []
    exp = inp.exposure_usd

    # ---- R7 — disputed but legitimate --------------------------------------
    # Customer disputes a charge that matches a recurring pattern → CASE +
    # VERIFY_WITH_CUSTOMER + WARN_CUSTOMER. Do NOT block.
    # P5v4-5: R7 requires is_dispute (customer_report trigger). When
    # trigger_type is unset (older callers/tests) we fall back to is_dispute
    # alone; the agent sets is_dispute only for customer_report so the
    # end-to-end path is consistent.
    if inp.is_recurring_match and inp.is_dispute and (
            inp.trigger_type is None or inp.trigger_type == "customer_report"):
        actions.append(_mk("CREATE_CASE",
            "R7: dispute against a recurring charge — case opened for the record"))
        actions.append(_mk("VERIFY_WITH_CUSTOMER",
            "R7: verify the customer really disputes this recurring charge"))
        actions.append(_mk("WARN_CUSTOMER",
            "R7: send a recurring-charge reminder"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=False,
            escalate=False,
        )

    # ---- Verdict-driven paths ------------------------------------------------

    if inp.verdict == "legitimate":
        # R3 requires customer_confirmed; otherwise cite §6 stop threshold.
        if inp.customer_response == "confirmed":
            reason = "R3: customer confirmed the purchase"
        else:
            reason = "§6: posterior below the stop threshold with ≥2 channels — close as legitimate"
        actions.append(_mk("CLOSE_NO_FRAUD", reason))
        return PolicyDecision(
            actions=actions,
            case_should_open=_case_should_open(inp, evidence_requests_present),
            sar_should_file=False,
            escalate=False,
        )

    # ---- R2 — customer denied the transaction --------------------------------
    if inp.customer_response == "denied":
        actions.append(_mk("BLOCK_CARD", "R2: customer denied — block + reissue",
                           exposure_usd=exp))
        actions.append(_mk("CREATE_CASE", "R2: customer denied"))
        # "R2 lite": if there is NO named pattern, SAR requires §3a to hold
        # genuinely (exposure > $1000 OR shared element OR undocumented).
        # We do NOT force-inflate the probability the way we used to. That
        # avoids the "denial ⇒ SAR everywhere" failure mode from CHECKPOINT
        # 5 (revised).
        if inp.pattern in (None, "none"):
            # Only fire SAR if §3a is genuinely satisfied (unchanged inputs).
            sar = _sar_should_file(inp)
        else:
            sar = _sar_should_file(
                PolicyInput(
                    verdict="fraud" if inp.verdict != "legitimate" else inp.verdict,
                    fraud_probability=max(inp.fraud_probability, 0.85),
                    exposure_usd=exp,
                    pattern=inp.pattern,
                    shared_element=inp.shared_element,
                    undocumented_coordinated=inp.undocumented_coordinated,
                )
            )
        if sar:
            actions.append(_mk("FILE_REPORT", "R2 + §3a: exposure>$1,000 or shared element/undocumented"))
        if inp.shared_element is not None:
            actions.append(_mk("MONITOR_CONNECTED_CARDS",
                f"R2/R6: monitor cards sharing this {inp.shared_element}"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=sar,
            escalate=False,
        )

    # ---- R3 — customer confirmed --------------------------------------------
    if inp.customer_response == "confirmed":
        actions.append(_mk("CLOSE_NO_FRAUD", "R3: customer confirmed the purchase"))
        return PolicyDecision(
            actions=actions,
            case_should_open=_case_should_open(inp, evidence_requests_present),
            sar_should_file=False,
            escalate=False,
        )

    # ---- R4 — no reply -------------------------------------------------------
    if inp.customer_response == "no_reply":
        actions.append(_mk("MONITOR_CARD", "R4: no customer reply within 24h"))
        actions.append(_mk("DECLINE_TRANSACTION",
            "R4: decline pending authorizations while awaiting reply"))
        escalate = exp > 500.0
        if escalate:
            actions.append(_mk("ESCALATE_TO_ANALYST", "R4: exposure > $500"))
        return PolicyDecision(
            actions=actions,
            case_should_open=_case_should_open(inp, evidence_requests_present),
            sar_should_file=False,
            escalate=escalate,
        )

    # ---- Uncertain + no reply yet, non-dispute trigger (P5v4-3) -------------
    # Applies when the case is still open (no customer_response) AND the
    # verdict is uncertain AND the trigger did NOT come from the customer
    # themselves (a customer_report IS a denial — R2/R7 handle it below).
    # Deferred so R5 (testing sequence) and R9 (undocumented) can take
    # precedence — those have their own VERIFY/DECLINE recipes.
    if (inp.verdict == "uncertain"
            and inp.customer_response is None
            and inp.trigger_type != "customer_report"
            and not inp.testing_sequence_fires
            and inp.pattern not in ("undocumented",)
            and not inp.undocumented_coordinated):
        # STEP_UP_AUTH is preferred when the alert is online + id_15=New
        # (§5 online step-up guidance).
        if inp.channel == "online" and inp.id_15_new:
            actions.append(_mk("STEP_UP_AUTH",
                "R1/§5: online alert with id_15='New' — require step-up before continuing"))
        else:
            actions.append(_mk("VERIFY_WITH_CUSTOMER",
                "R1/§5: uncertain verdict — verify with customer before acting"))
        if exp > 500.0:
            actions.append(_mk("ESCALATE_TO_ANALYST",
                "R8: uncertain verdict with exposure > $500"))
        actions.append(_mk("CREATE_CASE",
            "§3a: p>=0.30 or evidence requested — open a case"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=False,
            escalate=(exp > 500.0),
        )

    # ---- R9 — undocumented / coordinated ------------------------------------
    if inp.pattern == "undocumented" or inp.undocumented_coordinated:
        # P5v5-5: when the verdict is uncertain AND we have no customer
        # response yet, add VERIFY (or STEP_UP online+id_15=New) so the
        # simulator runs before we escalate/file.
        if inp.verdict == "uncertain" and inp.customer_response is None:
            if inp.channel == "online" and inp.id_15_new:
                actions.append(_mk("STEP_UP_AUTH",
                    "R1/§5: uncertain undocumented ring on online + id_15='New' — step up before file"))
            else:
                actions.append(_mk("VERIFY_WITH_CUSTOMER",
                    "R1/§5: uncertain undocumented ring — verify with customer before file"))
        # P5v5-BLOCK: any fraud verdict — even in R9 — must issue BLOCK_CARD.
        if inp.verdict == "fraud":
            actions.append(_mk("BLOCK_CARD",
                "R2/R9: fraud verdict on undocumented ring — block + reissue",
                exposure_usd=exp))
        actions.append(_mk("CREATE_CASE",
            "R9: undocumented / coordinated abuse — open a case"))
        actions.append(_mk("FILE_REPORT",
            "R9: coordinated cross-customer activity requires a SAR"))
        actions.append(_mk("ESCALATE_TO_ANALYST",
            "R9: hand to a human analyst for review"))
        if inp.shared_element is not None:
            actions.append(_mk("MONITOR_CONNECTED_CARDS",
                f"R9/R6: monitor cards sharing this {inp.shared_element}"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=True,
            escalate=True,
        )

    # ---- R5 — card testing ---------------------------------------------------
    if inp.testing_sequence_fires:
        actions.append(_mk("DECLINE_TRANSACTION",
            "R5: testing sequence — decline this authorization"))
        actions.append(_mk("STEP_UP_AUTH",
            "R5: require a one-time passcode before further activity"))
        # R1 gate: single-signal + not-yet-confirmed → verify before blocking.
        # The README HHG-017 example applies R1 at p=0.72 with only the R5
        # sequence signal — literal "<0.70" would leak; we treat "not confirmed"
        # (p<0.85 per §6 stop threshold) as the ceiling for R1 on single-signal
        # cases. Confirmed fraud (p>=0.85 or denial) drops out of R1.
        r1_holds = (
            inp.fraud_probability < 0.85
            and _independent_signal_count(inp.signal_channels) <= 1
        )
        if inp.testing_sequence_over_100_cleared and not r1_holds:
            actions.append(_mk("BLOCK_CARD",
                "R5: a purchase over $100 already cleared — block card",
                exposure_usd=exp))
        if r1_holds:
            actions.append(_mk("VERIFY_WITH_CUSTOMER",
                "R1 + §6: single signal below stop threshold — confirm before blocking"))
        return PolicyDecision(
            actions=actions,
            case_should_open=_case_should_open(inp, evidence_requests_present),
            sar_should_file=False,
            escalate=False,
        )

    # ---- R6 — shared origin --------------------------------------------------
    if inp.shared_element is not None and inp.verdict == "fraud":
        actions.append(_mk("CREATE_CASE", "R6: shared-origin fraud"))
        actions.append(_mk("FILE_REPORT",
            f"R6: shared {inp.shared_element} across compromised cards"))
        actions.append(_mk("MONITOR_CONNECTED_CARDS",
            f"R6: monitor cards sharing this {inp.shared_element}"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=True,
            escalate=False,
        )

    # ---- R8 — uncertain and exposed ------------------------------------------
    if inp.verdict == "uncertain" and exp > 500.0:
        actions.append(_mk("ESCALATE_TO_ANALYST",
            "R8: uncertain verdict with exposure > $500"))
        # An escalated uncertain still often needs a case for the record.
        actions.append(_mk("CREATE_CASE", "§3a: evidence requested / dispute open"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=False,
            escalate=True,
        )

    # ---- R1 — single-signal below §6 stop threshold: verify before blocking -
    if inp.fraud_probability < 0.85 and _independent_signal_count(inp.signal_channels) <= 1:
        actions.append(_mk("VERIFY_WITH_CUSTOMER",
            "R1 + §6: single signal below stop threshold — verify before any block"))
        # If the case would already be open (p>=0.30 or dispute), do it now.
        if _case_should_open(inp, evidence_requests_present):
            actions.append(_mk("CREATE_CASE",
                "§3a: fraud probability at or above 0.30, or dispute open"))
        return PolicyDecision(
            actions=actions,
            case_should_open=_case_should_open(inp, evidence_requests_present),
            sar_should_file=False,
            escalate=False,
        )

    # ---- Confirmed fraud without a specific rule firing above ---------------
    if inp.verdict == "fraud":
        actions.append(_mk("BLOCK_CARD",
            "R2/R6: confirmed fraud — block + reissue", exposure_usd=exp))
        actions.append(_mk("CREATE_CASE", "R2: confirmed fraud — open case"))
        sar = _sar_should_file(inp)
        if sar:
            actions.append(_mk("FILE_REPORT",
                "R2 + §3a: confirmed fraud AND (exposure > $1,000 OR shared element OR undocumented)"))
        if inp.shared_element is not None:
            actions.append(_mk("MONITOR_CONNECTED_CARDS",
                f"R6: monitor cards sharing this {inp.shared_element}"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=sar,
            escalate=False,
        )

    # ---- Fallback: strong signals but not confirmed --------------------------
    if inp.fraud_probability >= 0.70:
        actions.append(_mk("MONITOR_CARD", "R1: elevated probability but not confirmed — monitor before block"))
        actions.append(_mk("VERIFY_WITH_CUSTOMER",
            "R1: verify before any block on unconfirmed fraud"))
        actions.append(_mk("CREATE_CASE", "§3a: p>=0.30"))
        return PolicyDecision(
            actions=actions,
            case_should_open=True,
            sar_should_file=False,
            escalate=False,
        )

    # ---- Uncertain with low exposure or no evidence yet ----------------------
    actions.append(_mk("MONITOR_CARD", "R8 fallback: insufficient signal — keep watching"))
    if _case_should_open(inp, evidence_requests_present):
        actions.append(_mk("CREATE_CASE", "§3a: evidence requested or dispute open"))
    return PolicyDecision(
        actions=actions,
        case_should_open=_case_should_open(inp, evidence_requests_present),
        sar_should_file=False,
        escalate=False,
    )


# ---------- BLOCK_ALL_CARDS guardrail (R10) -----------------------------------


def add_block_all_cards_if_permitted(
    actions: list[Action], inp: PolicyInput
) -> list[Action]:
    """R10: never BLOCK_ALL_CARDS unless (verdict is fraud) AND (the customer
    already has ≥2 distinct card tuples with confirmed fraud OR credentials
    are confirmed compromised). Never alongside CLOSE_NO_FRAUD.
    """
    if inp.verdict != "fraud":
        return actions
    names = {a.action for a in actions}
    if "CLOSE_NO_FRAUD" in names:
        return actions
    if inp.prior_confirmed_fraud_on_two_cards or inp.credentials_confirmed_compromised:
        actions = actions + [
            _mk("BLOCK_ALL_CARDS",
                "R10: at least two of this customer's cards show confirmed fraud "
                "OR credentials confirmed compromised")
        ]
    return actions
