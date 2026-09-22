"""Answer-file invariants.

Every case JSON must satisfy the invariants below before being written to
``cases/``. See ``docs/POLICY_EXTRACT.md`` §"Invariants".

  1. ``sar.file == ("FILE_REPORT" in final actions)``.
  2. ``verdict == "legitimate"`` → ``affected_txn_ids == []``, ``exposure_usd == 0``,
     ``sar.file == False``.
  3. ``pattern == "undocumented"`` → ``pattern_description`` non-empty;
     otherwise ``pattern_description == ""``.
  4. ``evidence_requests == []`` → ``final == initial`` AND
     ``what_changed == "nothing"``.
  5. ``exposure_usd == round(sum(|amt|) for txn in affected_txn_ids, 2)``.
  6. Every ID in the file exists in the dataset.
  7. Every action's ``route`` matches §2 exactly.
  8. Every action's ``reason`` cites at least one rule (Rn or §3a/§3b/§4).

Public API: ``check_invariants(answer, txn_amt_lookup, id_universe)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from sentinel.policy.policy_engine import (
    ACTIONS_AUTO, ACTIONS_L1_TABLE, ACTIONS_L2_TABLE, route_for,
)


PATTERN_ENUM = {
    "card_testing", "card_not_present_fraud", "card_not_present_new_device",
    "out_of_region_use", "account_takeover", "undocumented", "none",
}
VERDICT_ENUM = {"fraud", "legitimate", "uncertain"}
STATUS_ENUM  = {"open", "closed_fraud", "closed_legitimate", "escalated"}


@dataclass
class Violation:
    invariant: str
    detail: str


def _final_action_names(answer: dict) -> list[str]:
    return [a["action"] for a in answer.get("next_best_actions", {}).get("final", [])]


def _initial_action_names(answer: dict) -> list[str]:
    return [a["action"] for a in answer.get("next_best_actions", {}).get("initial", [])]


def check_invariants(
    answer: dict,
    *,
    txn_amt_lookup: Callable[[str], float] | None = None,
    id_universe: dict[str, set[str]] | None = None,
) -> list[Violation]:
    """Validate an answer JSON. Return a (possibly empty) list of violations.

    ``txn_amt_lookup(txn_id) -> float`` is used to sum exposure. If omitted,
    invariant #5 is skipped.
    ``id_universe`` optional dict of {"txn": set(), "card": set(), "customer": set(),
    "closed_case": set()} — if provided, invariant #6 checks membership.
    """
    v: list[Violation] = []
    case = answer.get("case", {})
    sar  = answer.get("sar", {})
    nba  = answer.get("next_best_actions", {})
    evreqs = answer.get("evidence_requests", [])

    # ------- 1. sar.file matches FILE_REPORT presence -----------------------
    file_report_in_final = "FILE_REPORT" in _final_action_names(answer)
    if bool(sar.get("file")) != file_report_in_final:
        v.append(Violation(
            "I1",
            f"sar.file={sar.get('file')} but 'FILE_REPORT' in final = {file_report_in_final}",
        ))

    # ------- 2. legitimate ⇒ empty affected txns + 0 exposure + no SAR ------
    if case.get("verdict") == "legitimate":
        if case.get("affected_txn_ids"):
            v.append(Violation("I2", "verdict=legitimate but affected_txn_ids is non-empty"))
        if case.get("exposure_usd", 0) != 0:
            v.append(Violation("I2", "verdict=legitimate but exposure_usd != 0"))
        if sar.get("file"):
            v.append(Violation("I2", "verdict=legitimate but sar.file=true"))

    # ------- 3. undocumented ⇔ pattern_description non-empty ---------------
    pat = case.get("pattern")
    pd  = case.get("pattern_description", "")
    if pat == "undocumented":
        if not pd:
            v.append(Violation("I3", "pattern=undocumented but pattern_description is empty"))
    elif pat is not None:
        if pd:
            v.append(Violation("I3", f"pattern={pat} but pattern_description is non-empty"))
    if pat is not None and pat not in PATTERN_ENUM:
        v.append(Violation("I3", f"pattern={pat!r} not in enum"))

    # ------- 4. evidence_requests == [] ⇒ final == initial + what_changed=nothing ---
    if not evreqs:
        initial = _initial_action_names(answer)
        final   = _final_action_names(answer)
        if initial != final:
            v.append(Violation("I4",
                f"no evidence requests but initial != final ({initial} != {final})"))
        wc = nba.get("what_changed", "")
        if wc != "nothing":
            v.append(Violation("I4",
                f"no evidence requests but what_changed = {wc!r} (expected 'nothing')"))

    # ------- 5. exposure_usd == sum(|amt|) of affected_txn_ids --------------
    if txn_amt_lookup is not None:
        ids = case.get("affected_txn_ids", []) or []
        try:
            total = round(sum(abs(txn_amt_lookup(tid)) for tid in ids), 2)
        except LookupError as e:
            v.append(Violation("I5", f"missing txn amount for {e}"))
            total = None
        if total is not None:
            reported = round(case.get("exposure_usd", 0) or 0, 2)
            if abs(total - reported) > 0.01:
                v.append(Violation("I5",
                    f"exposure_usd={reported} but sum(|amt|) over affected_txn_ids={total}"))

    # ------- 6. IDs exist in the dataset -----------------------------------
    if id_universe is not None:
        for tid in case.get("affected_txn_ids", []) or []:
            if tid and tid not in id_universe.get("txn", set()):
                v.append(Violation("I6", f"unknown TransactionID {tid}"))
        for cid in case.get("connected_card_ids", []) or []:
            if cid and cid not in id_universe.get("card", set()):
                v.append(Violation("I6", f"unknown card_id {cid}"))
        for pid in case.get("similar_prior_cases", []) or []:
            if pid and pid not in id_universe.get("closed_case", set()):
                v.append(Violation("I6", f"unknown ClosedCase {pid}"))
        for sid in sar.get("subjects", []) or []:
            if not sid:
                continue
            if not (sid in id_universe.get("txn", set())
                    or sid in id_universe.get("card", set())
                    or sid in id_universe.get("customer", set())):
                v.append(Violation("I6", f"unknown subject id {sid} in sar.subjects"))
        first = case.get("first_suspicious_txn_id")
        if first and first not in id_universe.get("txn", set()):
            v.append(Violation("I6", f"unknown first_suspicious_txn_id {first}"))

    # ------- 7. routes match §2 --------------------------------------------
    for section in ("initial", "final"):
        for row in nba.get(section, []) or []:
            expected = route_for(row["action"],
                                 exposure_usd=case.get("exposure_usd", 0) or 0)
            if row["route"] != expected:
                v.append(Violation("I7",
                    f"{section}: action={row['action']!r} has route={row['route']!r} "
                    f"but §2 says {expected!r} (exposure={case.get('exposure_usd', 0)})"))

    # ------- 8. every action carries a rule citation -----------------------
    citer = re.compile(r"(?:\bR(?:10|[1-9])\b|§3[ab]|§[4-7])")
    for section in ("initial", "final"):
        for i, row in enumerate(nba.get(section, []) or []):
            if not citer.search(row.get("reason", "")):
                v.append(Violation("I8",
                    f"{section}[{i}] action={row['action']}: reason has no rule citation "
                    f"({row.get('reason','')[:80]!r})"))

    # ------- enum sanity ----------------------------------------------------
    if case.get("verdict") not in VERDICT_ENUM:
        v.append(Violation("I3", f"verdict={case.get('verdict')!r} not in enum"))
    if case.get("status") not in STATUS_ENUM:
        v.append(Violation("I3", f"status={case.get('status')!r} not in enum"))

    # ------- 9. legitimate ⇒ pattern == "none" ------------------------------
    if case.get("verdict") == "legitimate" and case.get("pattern") not in (None, "", "none"):
        v.append(Violation("I9",
            f"verdict=legitimate but pattern={case.get('pattern')!r} (must be 'none')"))

    # ------- 10. BLOCK_ALL_CARDS gate (R10) ---------------------------------
    # BLOCK_ALL_CARDS may only appear when verdict=fraud AND the action list
    # does NOT contain CLOSE_NO_FRAUD (they contradict). R10 itself requires
    # ≥2 distinct card tuples with prior confirmed fraud — the policy engine
    # enforces that upstream; here we check the surface constraint.
    for section in ("initial", "final"):
        actions = nba.get(section, []) or []
        names = [a["action"] for a in actions]
        if "BLOCK_ALL_CARDS" in names:
            if case.get("verdict") != "fraud":
                v.append(Violation("I10",
                    f"{section}: BLOCK_ALL_CARDS present but verdict={case.get('verdict')!r} "
                    f"(only allowed when verdict=fraud)"))
            if "CLOSE_NO_FRAUD" in names:
                v.append(Violation("I10",
                    f"{section}: BLOCK_ALL_CARDS and CLOSE_NO_FRAUD cannot appear together"))

    # ------- 11. Probability agrees with settled verdict --------------------
    p = case.get("fraud_probability")
    if isinstance(p, (int, float)):
        if case.get("verdict") == "fraud" and p < 0.5:
            v.append(Violation("I11",
                f"verdict=fraud but fraud_probability={p:.3f} < 0.5"))
        if case.get("verdict") == "legitimate" and p > 0.5:
            v.append(Violation("I11",
                f"verdict=legitimate but fraud_probability={p:.3f} > 0.5"))

    # ------- 12. No invented case IDs in prose fields -----------------------
    # Every CC-#### or CASE-* token in ``summary``, ``pattern_description``,
    # or ``sar.narrative`` must be either this case's own ID or an entry in
    # ``similar_prior_cases``. The explain node's citation guard enforces
    # this at generation time; I12 is the persisted-answer check.
    import re as _re
    _ID_RE = _re.compile(r"\b(?:CC-\d{4,}|CASE-[A-Z0-9]+-\d+)\b")
    allowed_ids = {answer.get("case_id", "")}
    allowed_ids.update(str(x) for x in (case.get("similar_prior_cases") or []))
    prose_sources = {
        "case.summary":              case.get("summary", "") or "",
        "case.pattern_description":  case.get("pattern_description", "") or "",
        "sar.narrative":             (answer.get("sar") or {}).get("narrative", "") or "",
    }
    for field, text in prose_sources.items():
        for tok in _ID_RE.findall(text):
            if tok not in allowed_ids:
                v.append(Violation("I12",
                    f"{field} cites {tok!r} which is not in "
                    f"similar_prior_cases (allowed={sorted(allowed_ids)})"))

    # ------- 13. shared_element ⇒ SAR + monitor on a fraud verdict ---------
    # The shared_element signal fires when either (i) ≥2 genuine peers
    # appear in connected_card_ids or (ii) ring_components confirms a
    # narrow-device neighbour with a prior confirmed-fraud ClosedCase
    # (§3a "another card's fraud" leg). On a fraud verdict this must
    # fire FILE_REPORT and MONITOR_CONNECTED_CARDS.
    #
    # We test shared_element via a mirror of ``compute_shared_element``
    # against the persisted answer (which doesn't carry raw graph_signals)
    # by using the peer definition itself: ≥2 entries in the
    # connected_card_ids list (already filtered to genuine peers by
    # ``compute_connected_cards``). Single-peer answers no longer trip I13.
    ccids = case.get("connected_card_ids") or []
    if case.get("verdict") == "fraud" and len(ccids) >= 2:
        final_names = [a["action"] for a in (nba.get("final") or [])]
        if "FILE_REPORT" not in final_names:
            v.append(Violation("I13",
                f"verdict=fraud with {len(ccids)} connected peers (shared_element) "
                "but no FILE_REPORT in final actions (§3a)"))
        if "MONITOR_CONNECTED_CARDS" not in final_names:
            v.append(Violation("I13",
                f"verdict=fraud with {len(ccids)} connected peers (shared_element) "
                "but no MONITOR_CONNECTED_CARDS in final actions (R6)"))

    return v
