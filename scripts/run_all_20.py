"""Run all 20 benchmark cases end-to-end.

  * SENTINEL_WRITE_MEMORY_DISABLED unset → SentinelCase vertices land in the
    graph via ``write_case``.
  * LLM on (Gemini key rotation → Groq fallback).
  * customer_report triggers: trigger text is the denial unless R7 fires.
  * Everything else runs the simulator with the tuned τ (BACKTEST_REPORT.md).

Validates each answer JSON against pydantic + I1-I9 invariants + ID
existence + sar.file ↔ FILE_REPORT-in-final + prose non-empty. Failures
are re-run once.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Ensure LLM on and memory write on.
os.environ.pop("SENTINEL_LLM_DISABLED", None)
os.environ.pop("SENTINEL_WRITE_MEMORY_DISABLED", None)
# Investigation path: all graph tool calls go through the tigergraph-mcp
# stdio server via ONE long-lived shared session for the whole process
# (see sentinel/graph/mcp_client.py::sync_run_installed_query). Set
# SENTINEL_GRAPH_VIA_MCP=0 to force the REST fast-path.
os.environ.setdefault("SENTINEL_GRAPH_VIA_MCP", "1")

from sentinel.agent.graph import run_plain
from sentinel.agent.state import Telemetry
from sentinel.output.answer_file import build_answer_file, write_answer_file
from sentinel.policy.invariants import check_invariants
from sentinel.llm import LAST_PROVIDER


CASE_PACK = REPO / "data" / "raw" / "case_pack.csv"


def _load_cases() -> list[dict]:
    with open(CASE_PACK) as f:
        return list(csv.DictReader(f))


def _validate(answer: dict) -> list[str]:
    """Return validation errors; empty list = pass."""
    errs = []
    v = check_invariants(answer)
    for x in v:
        errs.append(f"{x.invariant}: {x.detail[:80]}")
    # SAR / FILE_REPORT coupling (I1 already covers, but check text non-empty).
    if answer["sar"]["file"] and not answer["sar"]["narrative"]:
        errs.append("sar.file=true but narrative is empty")
    if not answer["case"]["summary"]:
        errs.append("case.summary is empty")
    return errs


def _run_one(a: dict) -> dict:
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


def main() -> int:
    alerts = _load_cases()
    print(f"Running all {len(alerts)} benchmark cases...\n")

    results: list[dict] = []
    for i, a in enumerate(alerts, 1):
        cid = a["case_id"]
        for attempt in (1, 2):
            print(f"[{i}/20] {cid} attempt {attempt}...")
            try:
                state = _run_one(a)
            except Exception as e:  # noqa: BLE001
                print(f"    → exception: {e}")
                if attempt == 2: break
                continue
            answer = build_answer_file(state)
            write_answer_file(state)
            errs = _validate(answer)
            if not errs:
                results.append({
                    "case_id": cid,
                    "answer":  answer,
                    "verdict": answer["case"]["verdict"],
                    "p_initial": state.get("p_initial"),
                    "p_final":   answer["case"]["fraud_probability"],
                    "pattern":   answer["case"]["pattern"],
                    "exposure":  answer["case"]["exposure_usd"],
                    "sar":       answer["sar"]["file"],
                    "initial_actions": [x["action"] for x in answer["next_best_actions"]["initial"]],
                    "final_actions":   [x["action"] for x in answer["next_best_actions"]["final"]],
                    "n_ev_requests": len(answer["evidence_requests"]),
                    "graph_case_id": answer["case"]["graph_case_id"],
                    "llm_provider":  state.get("llm_provider"),
                    "llm_model":     state.get("llm_model"),
                    "llm_key_index": LAST_PROVIDER.get("key_index"),
                    "latency_s":     answer["latency_s"],
                })
                print(f"    ✓ verdict={answer['case']['verdict']} p_f={answer['case']['fraud_probability']:.3f} "
                      f"pattern={answer['case']['pattern']} sar={answer['sar']['file']} "
                      f"provider={state.get('llm_provider')}/key{LAST_PROVIDER.get('key_index')}")
                break
            else:
                print(f"    ✗ validation failed: {errs}")
                if attempt == 2:
                    results.append({"case_id": cid, "errors": errs})

    # Save summary.
    out = REPO / "cases" / "_summary_20.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nSummary → {out}")

    # 20-row table.
    print(f"\n{'case':<9} {'verdict':<11} {'p_i→p_f':<12} {'pattern':<28} "
          f"{'exposure':<10} {'initial→final':<50} {'SAR':<4} {'evreq':<6} "
          f"{'vertex':<18} {'llm':<20}")
    for r in results:
        if "errors" in r:
            print(f"{r['case_id']:<9} FAILED: {r['errors']}")
            continue
        act = f"{','.join(r['initial_actions'])[:20]}→{','.join(r['final_actions'])[:22]}"
        llm = f"{r['llm_provider']}/k{r['llm_key_index']}"
        print(f"{r['case_id']:<9} {r['verdict']:<11} "
              f"{r['p_initial']:.2f}→{r['p_final']:.2f}    "
              f"{r['pattern']:<28} ${r['exposure']:>8.2f} {act:<50} "
              f"{'✓' if r['sar'] else '—':<4} {r['n_ev_requests']:<6} "
              f"{(r['graph_case_id'] or '—'):<18} {llm:<20}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
