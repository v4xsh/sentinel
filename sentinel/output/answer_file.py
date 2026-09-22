"""Pydantic answer-file writer.

Every case's answer JSON must exactly match the README's shape:

  top-level: case_id, case, evidence_requests, next_best_actions, sar,
             stop_reason, tool_calls, tokens, latency_s

Invariants are enforced separately by ``sentinel.policy.invariants.check_invariants``.
This module just assembles the object and writes it to ``cases/<case_id>.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from sentinel.config import REPO_ROOT
from sentinel.evidence.ledger import Evidence, EvidenceLedger
from sentinel.policy.policy_engine import PolicyDecision


def compute_connected_cards(state: dict) -> list[str]:
    """Return the ``connected_card_ids`` list the answer file will publish.
    Shared with ``_shared_element_from_signals`` so I13 holds: if this list
    ends up non-empty, shared_element is set at decide-time and the policy
    engine fires §3a (FILE_REPORT) + R6 (MONITOR_CONNECTED_CARDS)."""
    gs = state.get("graph_signals") or {}
    ring = gs.get("ring_components") or {}
    seed = state.get("card_id")
    def _clean(seq):
        return [c for c in (seq or []) if c and c != seed]
    ring_cards = _clean(ring.get("cards"))
    dn_cards   = _clean((gs.get("device_neighbors")   or {}).get("cards"))
    rc_cards   = _clean((gs.get("region_cluster")     or {}).get("cards"))
    out: list[str] = []
    seen: set[str] = set()
    for src in (ring_cards, dn_cards, rc_cards):
        for c in src:
            if c not in seen:
                out.append(c); seen.add(c)
    for h in (state.get("memory_hits") or []):
        cid = h.attrs.get("card_id") if hasattr(h, "attrs") else None
        if cid and cid != seed and cid not in seen:
            out.append(cid); seen.add(cid)
    return out[:25]


def compute_similar_prior_cases(hits: list) -> list[str]:
    """Shared similar_prior_cases selector — used both by the answer-file
    builder and by ``node_explain`` (so the LLM's citation whitelist
    matches what the answer file will publish).

    Structurally linked closed_case hits first (kept in retrieval order),
    then semantic closed_case hits with score above the median, cap 5.
    """
    out: list[str] = []
    seen: set[str] = set()
    struct = [h for h in hits if getattr(h, "source", "") == "structural"
              and getattr(h, "kind", "") == "closed_case"]
    for h in struct:
        if h.id and h.id not in seen:
            out.append(h.id); seen.add(h.id)
    sem = [h for h in hits if getattr(h, "source", "") == "semantic"
           and getattr(h, "kind", "") == "closed_case"]
    if sem:
        scores = [float(getattr(h, "score", 0.0)) for h in sem]
        med = sorted(scores)[len(scores) // 2] if scores else 0.0
        for h in sem:
            if float(getattr(h, "score", 0.0)) > med and h.id and h.id not in seen:
                out.append(h.id); seen.add(h.id)
    return out[:5]


# ---- pydantic shapes --------------------------------------------------------


class ActionEntry(BaseModel):
    action: str
    route: Literal["auto", "L1", "L2"]
    reason: str


class EvidenceEntry(BaseModel):
    claim: str
    source: Literal["graph", "document", "customer", "external"]
    ref: str
    entity_ids: list[str] = Field(default_factory=list)


class Case(BaseModel):
    status: Literal["open", "closed_fraud", "closed_legitimate", "escalated"]
    verdict: Literal["fraud", "legitimate", "uncertain"]
    fraud_probability: float
    pattern: str
    pattern_description: str = ""
    affected_txn_ids: list[str] = Field(default_factory=list)
    first_suspicious_txn_id: str = ""
    connected_card_ids: list[str] = Field(default_factory=list)
    connected_device_profiles: list[str] = Field(default_factory=list)
    exposure_usd: float = 0.0
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    similar_prior_cases: list[str] = Field(default_factory=list)
    summary: str = ""
    written_to_graph: bool = False
    graph_case_id: str = ""


class EvidenceRequest(BaseModel):
    type: Literal["customer_validation", "step_up_auth", "analyst_info"]
    asked_after_step: int
    assumed_response: str


class NextBestActions(BaseModel):
    initial: list[ActionEntry] = Field(default_factory=list)
    final: list[ActionEntry] = Field(default_factory=list)
    what_changed: str = "nothing"


class SAR(BaseModel):
    file: bool = False
    reason: str = ""
    narrative: str = ""
    subjects: list[str] = Field(default_factory=list)
    total_amount_usd: float = 0.0
    activity_dates: list[str] = Field(default_factory=list)


class AnswerFile(BaseModel):
    case_id: str
    case: Case
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)
    next_best_actions: NextBestActions
    sar: SAR
    stop_reason: str = ""
    tool_calls: int = 0
    tokens: int = 0
    latency_s: float = 0.0


# ---- assembly ---------------------------------------------------------------


def _status_from(verdict: str, sar_file: bool, escalate: bool) -> str:
    if verdict == "legitimate":
        return "closed_legitimate"
    if escalate:
        return "escalated"
    if verdict == "fraud":
        return "closed_fraud"
    return "open"


def _evidence_json(ledger: EvidenceLedger) -> list[EvidenceEntry]:
    """Return the analyst-readable subset of the ledger for the answer file.

    - Every graph-sourced item (installed queries, ring evidence, detectors,
      policy-fallback rows) is kept.
    - Alert-model items are kept only when the absolute coefficient (weight
      encoded in the ``ref`` string like ``alert_model:NAME  (weight ±X.XXX)``)
      is ≥ 0.5. Everything else (low-weight features, intercept, raw
      diagnostics) belongs in the cases_meta sidecar, not the analyst view.
    """
    import re as _re
    W_RE = _re.compile(r"weight\s*([+\-]?\d+(?:\.\d+)?)")
    out: list[EvidenceEntry] = []
    for e in ledger.items:
        ref = e.ref or ""
        is_alert_model = ref.startswith("alert_model:")
        if is_alert_model:
            m = W_RE.search(ref)
            weight = float(m.group(1)) if m else 0.0
            if abs(weight) < 0.5:
                continue  # relegated to model_diagnostics sidecar
        out.append(EvidenceEntry(
            claim=e.claim, source=e.source, ref=e.ref,
            entity_ids=[str(x) for x in e.entity_ids],
        ))
    return out


def _model_diagnostics(ledger: EvidenceLedger) -> list[dict]:
    """Return every alert_model ledger item as a diagnostics sidecar row.

    Written to cases_meta/<case_id>.json — NOT into the answer file.
    Each row: {name, weight, direction, channel, claim}.
    """
    import re as _re
    W_RE = _re.compile(r"weight\s*([+\-]?\d+(?:\.\d+)?)")
    N_RE = _re.compile(r"alert_model:([^\s]+)")
    out: list[dict] = []
    for e in ledger.items:
        if not (e.ref or "").startswith("alert_model:"):
            continue
        m_w = W_RE.search(e.ref)
        m_n = N_RE.search(e.ref)
        out.append({
            "feature":   m_n.group(1) if m_n else "",
            "weight":    float(m_w.group(1)) if m_w else 0.0,
            "channel":   e.channel,
            "direction": e.direction,
            "claim":     e.claim,
        })
    return out


def _earliest_txn_id(txn_ids: list[str]) -> str:
    """Return the earliest-by-ts txn from the affected list (by TransactionID
    lookup in ``txn_features``). Falls back to the first entry."""
    from sentinel.data.features import connect
    if not txn_ids:
        return ""
    try:
        clean = [int(str(t).lstrip("T")) for t in txn_ids if str(t).lstrip("T").isdigit()]
        if not clean:
            return str(txn_ids[0])
        con = connect()
        placeholders = ",".join("?" * len(clean))
        r = con.execute(
            f"SELECT TransactionID FROM txn_features "
            f"WHERE TransactionID IN ({placeholders}) ORDER BY ts ASC LIMIT 1",
            clean,
        ).fetchone()
        return str(r[0]) if r else str(txn_ids[0])
    except Exception:  # noqa: BLE001
        return str(txn_ids[0])


def _summary_activity_dates(ledger: EvidenceLedger, opened_at: str) -> list[str]:
    """Extract first and last date from device_link.first_seen_on_card fields."""
    dates: list[str] = []
    for e in ledger.items:
        if e.device_link and e.device_link.first_seen_on_card:
            dates.append(e.device_link.first_seen_on_card[:10])
    if not dates:
        return [opened_at[:10], opened_at[:10]]
    return [min(dates), max(dates)]


def build_answer_file(state: dict) -> dict:
    """Turn the LangGraph state into the answer JSON dict.

    Returns a plain dict (JSON-ready), not the pydantic model, so downstream
    invariant checks and JSON serialisation are cheap.
    """
    from sentinel.agent.state import Telemetry

    tel: Telemetry = state.get("telemetry") or Telemetry()
    decision: PolicyDecision = state["policy_decision"]
    ledger: EvidenceLedger = state["ledger"]
    row = state.get("txn_row", {})
    verdict = state["verdict"]
    p = state["fraud_probability"]

    # -- initial actions: re-run policy engine with the SAME derivations as
    # the agent used at node_decide, but with customer_response and its VOI-
    # induced posterior stripped. This gives "what would we have done BEFORE
    # asking?".
    from sentinel.policy.policy_engine import (
        PolicyInput, add_block_all_cards_if_permitted, decide as policy_decide,
    )
    from sentinel.agent.posterior import score_ledger, verdict_for
    tags = state.get("_tags", set())

    # Rebuild a ledger without the customer_response evidence.
    initial_items = [e for e in ledger.items if e.source != "customer"]
    initial_ledger = EvidenceLedger(items=list(initial_items))
    s = score_ledger(initial_ledger, prior_log_odds=state.get("prior_log_odds", 0.0))
    p_initial = s["fraud_probability"]
    verdict_initial = verdict_for(
        p_initial, n_independent_channels=len(initial_ledger.channels()))

    # shared_element uses the same graph-signals-driven rule as node_decide.
    shared_element = state.get("shared_element")

    pin_initial = PolicyInput(
        verdict=verdict_initial,                                       # type: ignore
        fraud_probability=p_initial,
        exposure_usd=float(row.get("TransactionAmt", 0) or 0),
        pattern=state.get("pattern", "none"),                          # type: ignore
        signal_channels=frozenset(initial_ledger.channels()),
        shared_element=shared_element,                                 # type: ignore
        customer_response=None,                                        # type: ignore
        is_dispute=(state.get("trigger_type") == "customer_report"),
        is_recurring_match=("is_recurring_match" in tags),
        # Use the real DISTINCT card-tuple count set on state by node_decide.
        prior_confirmed_fraud_on_two_cards=(state.get("r10_distinct_prior_cards", 0) >= 2),
        credentials_confirmed_compromised=False,
        undocumented_coordinated=("undocumented_coordinated" in tags),
        testing_sequence_fires=("testing_sequence_fires" in tags),
        testing_sequence_over_100_cleared=False,
        trigger_type=state.get("trigger_type"),
        channel=row.get("channel"),
        id_15_new=(row.get("id_15") == "New"),
    )
    initial_decision = policy_decide(pin_initial)
    initial_actions = add_block_all_cards_if_permitted(
        initial_decision.actions, pin_initial)

    # -- evidence requests
    ev_requests: list[EvidenceRequest] = []
    if state.get("customer_response"):
        response_str = {
            "denied":    "Customer states they did not make this purchase.",
            "confirmed": "Customer states they made this purchase.",
            "no_reply":  "Customer did not respond within 24h.",
        }.get(state["customer_response"], state["customer_response"])
        ev_requests.append(EvidenceRequest(
            type="customer_validation", asked_after_step=6,
            assumed_response=response_str,
        ))

    # -- pattern_description (only for 'undocumented'). I9: verdict=legitimate
    # forces pattern to "none".
    pattern = state.get("pattern", "none")
    if verdict == "legitimate":
        pattern = "none"
    pattern_desc = ""
    if pattern == "undocumented":
        # Prefer LLM-generated prose from node_explain if available; else
        # concatenate ledger claims.
        pattern_desc = state.get("pattern_description_prose") or ""
        if not pattern_desc:
            undoc_evs = [e for e in ledger.items if "undocumented" in e.ref.lower()
                         or e.channel in ("device", "sequence")]
            pattern_desc = " ".join(e.claim for e in undoc_evs[:2]) or \
                "Coordinated pattern not fitting the five documented archetypes."

    # -- affected txns + exposure (from node_assemble_episode).
    affected: list[str] = list(state.get("affected_txn_ids") or [])
    exposure_usd = float(state.get("exposure_usd", 0.0) or 0.0)

    # -- connected cards / devices (I13-consistent; see compute_connected_cards)
    connected_cards = compute_connected_cards(state)
    connected_devices: list[str] = []
    dp = row.get("device_profile")
    if dp:
        connected_devices.append(str(dp))

    # -- SAR block
    sar_file = decision.sar_should_file
    # No-SAR reason: cite §3a with the numbers so it's clear why not.
    if not sar_file:
        shared_elem = state.get("shared_element")
        undoc = pattern == "undocumented" or "undocumented_coordinated" in (state.get("_tags") or set())
        no_sar_reason = (
            f"§3a: SAR not required — "
            f"exposure ${exposure_usd:.2f} vs $1,000 threshold ("
            f"{'above' if exposure_usd > 1000 else 'below'}), "
            f"shared_element = {shared_elem or 'none'}, "
            f"pattern is {'undocumented' if undoc else 'documented / none'}. "
            "None of the §3a trigger conditions holds."
        )
    else:
        no_sar_reason = ""
    sar = SAR(
        file=sar_file,
        reason=(next((a.reason for a in decision.actions if a.action == "FILE_REPORT"), "")
                if sar_file else no_sar_reason),
        narrative=state.get("sar_narrative") or "" if sar_file else "",
        subjects=([state["customer_id"], state["card_id"], state["txn_id"]]
                  if sar_file else []),
        total_amount_usd=(exposure_usd if sar_file else 0.0),
        activity_dates=(_summary_activity_dates(ledger, state["opened_at"])
                        if sar_file else []),
    )

    prior_cases = compute_similar_prior_cases(state.get("memory_hits") or [])

    # -- nba what_changed. Invariant I4: no ev-requests ⇒ final == initial
    # AND what_changed == "nothing". Initial rebuilt from the customer-
    # response-stripped ledger; scrub any "customer denied/confirmed"
    # language from initial reasons and re-guard BLOCK_CARD (initial only
    # if p_initial ≥ 0.85 and ≥2 channels).
    def _scrub_initial_reasons(actions: list) -> list:
        """Initial reasons never reference the customer response — the initial
        recommendation is pre-response by definition.

        BLOCK_CARD in initial gets a specific pre-response reason so a
        reader can tell it wasn't triggered by any customer signal.
        Anything else that leaks 'denied' / 'confirmed' / 'no reply' /
        'confirmed fraud' language gets a generic pre-response reason.
        """
        from sentinel.policy.policy_engine import Action as _A
        out = []
        LEAK_PHRASES = (
            "customer denied", "customer confirmed", "customer_response",
            "customer said", "customer reply", "denied — block + reissue",
            "no customer reply", "no_reply", "confirmed fraud",
            "confirmed as fraud", "customer confirms",
            "confirmed by customer", "denied by customer",
        )
        for a in actions:
            low = a.reason.lower()
            leaks = any(p in low for p in LEAK_PHRASES)
            reason = a.reason
            if leaks or a.action == "BLOCK_CARD":
                # BLOCK_CARD in initial only survives _guard_initial_block_card
                # when p_initial ≥ 0.85 AND ≥2 independent channels — encode
                # that in the reason so readers see the justification.
                if a.action == "BLOCK_CARD":
                    reason = ("§6: posterior ≥ 0.85 on ≥2 independent "
                              "channels before customer contact")
                else:
                    reason = ("R1/§6: pre-response initial recommendation "
                              "(no customer signal has been consumed yet)")
            out.append(_A(action=a.action, route=a.route, reason=reason))
        return out

    def _guard_initial_block_card(actions: list) -> list:
        """Drop BLOCK_CARD from initial unless p_initial ≥ 0.85 AND ≥2 channels."""
        n_channels = len({e.channel for e in initial_ledger.items
                          if abs(e.log_lr) > 1e-9})
        if p_initial >= 0.85 and n_channels >= 2:
            return actions
        return [a for a in actions if a.action != "BLOCK_CARD"]

    initial_actions = _scrub_initial_reasons(_guard_initial_block_card(initial_actions))

    initial_names = [a.action for a in initial_actions]
    final_names   = [a.action for a in decision.actions]

    def _what_changed(initial_ns: list, final_ns: list) -> str:
        added   = [x for x in final_ns   if x not in initial_ns]
        removed = [x for x in initial_ns if x not in final_ns]
        if not added and not removed:
            return "nothing"
        parts = []
        if added:   parts.append("added: " + ", ".join(added))
        if removed: parts.append("removed: " + ", ".join(removed))
        return "; ".join(parts)

    if not ev_requests:
        what_changed = "nothing"
        final_actions_effective = list(decision.actions)
        initial_actions = _scrub_initial_reasons(_guard_initial_block_card(
            list(decision.actions)))
    else:
        final_actions_effective = list(decision.actions)
        # P5v5-4: CREATE_CASE retained in final whenever evidence_requests
        # is non-empty — even if the response-driven branch (R3, R7 confirmed)
        # closes the case, we keep the record of the ask.
        if not any(a.action == "CREATE_CASE" for a in final_actions_effective):
            from sentinel.policy.policy_engine import _mk
            final_actions_effective.append(_mk(
                "CREATE_CASE",
                "§3a: evidence_requests non-empty — retain case record"))
        what_changed = _what_changed(
            [a.action for a in initial_actions],
            [a.action for a in final_actions_effective])

    # Re-route BLOCK_CARD by the FINAL exposure_usd (§2: > $2,500 → L2).
    # `decide()` used the flagged-txn amount, but node_assemble_episode may
    # have expanded the episode past that. Rebuild every BLOCK_CARD's route
    # against the final exposure_usd.
    from sentinel.policy.policy_engine import route_for as _route_for, Action as _Action
    def _reroute(actions: list) -> list:
        out = []
        for a in actions:
            if a.action == "BLOCK_CARD":
                r = _route_for("BLOCK_CARD", exposure_usd=exposure_usd)
                out.append(_Action(action="BLOCK_CARD", route=r, reason=a.reason))
            else:
                out.append(a)
        return out
    initial_actions = _reroute(initial_actions)
    final_actions_effective = _reroute(final_actions_effective)

    answer = AnswerFile(
        case_id=state["case_id"],
        case=Case(
            status=_status_from(verdict, sar_file, decision.escalate),
            verdict=verdict, fraud_probability=round(p, 4),
            pattern=pattern, pattern_description=pattern_desc,
            affected_txn_ids=affected,
            first_suspicious_txn_id=_earliest_txn_id(affected) if affected else "",
            connected_card_ids=connected_cards,
            connected_device_profiles=connected_devices,
            exposure_usd=round(exposure_usd, 2),
            evidence=_evidence_json(ledger),
            similar_prior_cases=prior_cases,
            summary=state.get("explanation", ""),
            written_to_graph=(state.get("case_vertex_id") is not None),
            graph_case_id=state.get("case_vertex_id") or "",
        ),
        evidence_requests=ev_requests,
        next_best_actions=NextBestActions(
            initial=[ActionEntry(**a.as_dict()) for a in initial_actions],
            final=[ActionEntry(**a.as_dict()) for a in final_actions_effective],
            what_changed=what_changed,
        ),
        sar=sar,
        stop_reason=_compose_stop_reason(state),
        tool_calls=tel.tool_calls,
        tokens=tel.tokens_input + tel.tokens_output,
        latency_s=round(tel.latency_seconds, 3),
    )
    return answer.model_dump()


def _compose_stop_reason(state: dict) -> str:
    """Include any failed detector / tool in the stop_reason so downstream
    graders see WHY the case ended where it did.
    """
    parts: list[str] = []
    if state.get("customer_response"):
        parts.append(f"Customer response ({state['customer_response']}) settled the verdict.")
    else:
        parts.append("No customer response was required; posterior converged.")
    if state.get("_incoherent_fraud_downgraded"):
        parts.append("Verdict was downgraded to 'uncertain' because pattern=none (coherence rule).")
    notes = state.get("critic_notes") or []
    if notes:
        top = [n.get("concern", "") for n in notes[:3] if n.get("concern")]
        if top:
            parts.append("Critic concerns (annotation only): " + "; ".join(top))
    errs = state.get("errors") or []
    if errs:
        parts.append("Errors during investigation: " + "; ".join(errs[:5]))
    return " ".join(parts)


def write_answer_file(state: dict, out_dir: Path | None = None) -> Path:
    """Serialise + write ``cases/<case_id>.json`` and the diagnostics sidecar.

    The answer file itself carries only analyst-readable content. Every
    low-weight alert-model item and pre-clamp probability trace lands in
    ``cases_meta/<case_id>.json`` — never in the answer file schema.
    """
    out_dir = out_dir or (REPO_ROOT / "cases")
    out_dir.mkdir(parents=True, exist_ok=True)
    answer = build_answer_file(state)
    path = out_dir / f"{answer['case_id']}.json"
    path.write_text(json.dumps(answer, indent=2), encoding="utf-8")

    # Diagnostics sidecar — everything an engineer would want that the
    # analyst UI doesn't need.
    meta_dir = out_dir.parent / "cases_meta" \
               if out_dir.name in ("cases", "cases_extra") \
               else out_dir / "cases_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    ledger = state.get("ledger")
    meta = {
        "case_id": answer["case_id"],
        "p_initial": state.get("p_initial"),
        "p_final":   answer["case"]["fraud_probability"],
        "verdict":   answer["case"]["verdict"],
        "pattern":   answer["case"]["pattern"],
        "shared_element": state.get("shared_element"),
        "alert_model_features_fired": state.get("alert_model_features_fired") or [],
        "model_diagnostics": _model_diagnostics(ledger) if ledger is not None else [],
        "llm_provider": state.get("llm_provider"),
        "llm_model":    state.get("llm_model"),
        "tool_calls":   answer.get("tool_calls"),
        "latency_s":    answer.get("latency_s"),
    }
    (meta_dir / f"{answer['case_id']}.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )
    return path
