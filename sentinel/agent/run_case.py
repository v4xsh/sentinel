"""Run a single case end-to-end and pretty-print the result.

Usage:
    python -m sentinel.agent.run_case HHG-014
    python -m sentinel.agent.run_case HHG-014 HHG-003 HHG-007 HHG-010 HHG-012 HHG-017
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time

# Investigation path defaults to the TigerGraph MCP stdio server.
os.environ.setdefault("SENTINEL_GRAPH_VIA_MCP", "1")

from sentinel.agent.graph import run_plain
from sentinel.agent.state import Telemetry
from sentinel.config import REPO_ROOT
from sentinel.evidence.ledger import EvidenceLedger
from sentinel.output.answer_file import build_answer_file, write_answer_file


CASE_PACK = REPO_ROOT / "data" / "raw" / "case_pack.csv"


def load_alerts() -> dict[str, dict]:
    with open(CASE_PACK) as f:
        return {r["case_id"]: r for r in csv.DictReader(f)}


def run_one(case_id: str, alerts: dict[str, dict]) -> dict:
    a = alerts[case_id]
    state = {
        "case_id":      a["case_id"],
        "txn_id":       a["flagged_txn_id"],
        "card_id":      a["card_id"],
        "customer_id":  a["customer_id"],
        "opened_at":    a["opened_at"],
        "trigger_type": a["trigger_type"],
        "trigger_text": a["trigger_text"],
        "prior_log_odds": 0.0,
        "telemetry": Telemetry(),
    }
    t0 = time.time()
    state = run_plain(state)
    state["telemetry"].latency_seconds = time.time() - t0
    return state


def _print_ledger(ledger: EvidenceLedger) -> None:
    print("  ledger:")
    for e in ledger.items:
        dl = ""
        if e.device_link:
            dl = (f"  [id_15={e.device_link.id_15} id_23={e.device_link.id_23} "
                  f"first_seen={e.device_link.first_seen_on_card} "
                  f"overlap={e.device_link.activity_window_overlap}]")
        print(f"    - [{e.channel:<15}] log_lr={e.log_lr:+7.3f} {e.direction:<7} "
              f"ref={e.ref}{dl}")
        print(f"      claim: {e.claim[:180]}")
        # If this is a device-ring evidence with tier LRs attached, print them.
        tiers = getattr(e, "device_tiers_report", None)
        if tiers:
            print(f"      device_tiers: T1={tiers.get('T1')}  T2={tiers.get('T2')}  "
                  f"T3={tiers.get('T3')}  used={tiers.get('used')}")


LEGIT_TAGS = {
    "is_recurring_match", "legit_recurring_r7_hint",
    "legit_trip_hint", "legit_new_phone_hint", "legit_big_purchase_hint",
}


def _summary_block(state: dict) -> str:
    tel = state["telemetry"]
    decision = state["policy_decision"]
    detectors_fired: list[str] = []
    for r in state.get("detector_results", []) or []:
        if r.is_hit():
            detectors_fired.append(
                f"{r.pattern or 'noop'}({len(r.evidence)} ev)"
            )
    tags = state.get("_tags", set())
    legit_tags = sorted(tags & LEGIT_TAGS)
    lines = [
        f"=== {state['case_id']} ===",
        f"  trigger  : {state['trigger_type']}",
        f"  txn      : {state['txn_id']} (${state['txn_row'].get('TransactionAmt', 0):.2f})",
        f"  detectors: {', '.join(detectors_fired) or '(none fired)'}",
        f"  legit-arch tags: {legit_tags or '(none)'}",
        f"  simulator: {state.get('simulator_rule','not_run')}",
        f"  r10-priors: distinct_prior_cards={state.get('r10_distinct_prior_cards',0)}",
        f"  pattern  : {state.get('pattern','none')}"
        + ("  (best-match, low confidence)" if state.get("pattern_low_confidence") else ""),
        f"  posterior: p_initial={state.get('p_initial', 0):.3f} → "
        f"p_final={state['fraud_probability']:.3f}  log_odds={state.get('log_odds',0):+.3f}"
        + ("  (CAPPED)" if state.get("log_odds_capped") else "")
        + (f"  uncapped={state.get('log_odds_uncapped',0):+.3f}"),
        f"  verdict  : {state['verdict']}"
        + ("  (coherence-downgraded)" if state.get("_incoherent_fraud_downgraded") else ""),
        f"  affected : {state.get('affected_txn_ids', [])}",
        f"  exposure : ${state.get('exposure_usd', 0):.2f}",
        f"  shared   : {state.get('shared_element','—')}",
        f"  cust-resp: {state.get('customer_response','—')}",
        f"  case-vid : {state.get('case_vertex_id','—')}",
        f"  telemetry: tool_calls={tel.tool_calls}  "
        f"tokens={tel.tokens_input + tel.tokens_output}  "
        f"latency={tel.latency_seconds:.2f}s",
        f"  llm      : provider={state.get('llm_provider','?')}  model={state.get('llm_model','?')}",
    ]
    lines.append("  timings  :")
    for step, secs in tel.node_timings.items():
        lines.append(f"    {step:<20} {secs:7.2f}s")

    lines.append("  actions  :")
    for act in decision.actions:
        lines.append(f"    - {act.action:<26} {act.route:<4} {act.reason}")
    if state.get("critic_notes"):
        lines.append("  critic-notes:")
        for c in state["critic_notes"]:
            lines.append(f"    - [{c.get('severity','?')}] {c['concern']}")
    if state.get("errors"):
        lines.append("  errors   :")
        for err in state["errors"]:
            lines.append(f"    - {err}")
    lines.append(f"  sar.file: {state['policy_decision'].sar_should_file}")
    return "\n".join(lines)


def main() -> None:
    ids = sys.argv[1:] or ["HHG-014", "HHG-003", "HHG-007", "HHG-010", "HHG-012", "HHG-017"]
    alerts = load_alerts()
    all_fraud = True
    for cid in ids:
        if cid not in alerts:
            print(f"[skip] {cid} not in case_pack")
            continue
        state = run_one(cid, alerts)
        state["answer"] = build_answer_file(state)
        path = write_answer_file(state)
        print(_summary_block(state))
        _print_ledger(state["ledger"])
        print(f"  wrote {path}\n")
        if state["verdict"] != "fraud":
            all_fraud = False
    if all_fraud and len(ids) > 1:
        print("### ALL cases still returned verdict=fraud — halting per user directive.")


if __name__ == "__main__":
    main()
