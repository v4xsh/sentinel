"""Streaming risk_score alert monitor.

Scans the exam-period transactions (Nov + Dec 2016 in the dataset) for
high-risk-score records, deduplicates against the 20 benchmark case pack,
and runs the agent on the top-N alerts. Answer files land in
``cases_extra/`` and a summary JSON is written for the UI.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("SENTINEL_LLM_DISABLED", "1")     # LLM off for the sweep
os.environ.setdefault("SENTINEL_WRITE_MEMORY_DISABLED", "0")

from sentinel.agent.graph import run_plain
from sentinel.agent.state import Telemetry
from sentinel.output.answer_file import build_answer_file
from sentinel.data.features import connect


BENCHMARK_TXNS: set[str] = set()
with open(REPO / "data" / "raw" / "case_pack.csv") as _f:
    for _r in csv.DictReader(_f):
        BENCHMARK_TXNS.add(str(_r["flagged_txn_id"]))


def _candidate_alerts(n: int = 30, min_score: float = 0.90) -> list[dict]:
    """Pull the top-N high-risk-score November-December transactions that
    aren't already in the benchmark pack.
    """
    con = connect()
    rows = con.execute(
        """
        SELECT t.TransactionID, t.ts, t.channel, t.customer_id,
               t.risk_score, t.TransactionAmt, t.addr1
        FROM txn_features t
        WHERE t.risk_score >= ?
          AND t.ts >= '2016-11-01 00:00:00'
          AND t.ts <= '2016-12-31 23:59:59'
        ORDER BY t.risk_score DESC, t.ts DESC
        LIMIT ?
        """,
        [float(min_score), int(n * 3)],
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        tid, ts, ch, cust, rs, amt, addr = r
        if str(tid) in BENCHMARK_TXNS:
            continue
        out.append({
            "txn_id": str(tid), "ts": str(ts), "channel": ch,
            "customer_id": cust, "risk_score": float(rs or 0.0),
            "amount": float(amt or 0.0), "addr1": str(addr or ""),
        })
        if len(out) >= n:
            break
    return out


def _card_id_for_customer(customer_id: str) -> str:
    con = connect()
    r = con.execute(
        "SELECT card_id FROM card_map WHERE customer_id = ? ORDER BY k LIMIT 1",
        [customer_id],
    ).fetchone()
    return r[0] if r else f"{customer_id}-K1"


def _run_one_extra(a: dict, idx: int) -> dict:
    case_id = f"EXTRA-{idx:03d}"
    card_id = _card_id_for_customer(a["customer_id"])
    state = {
        "case_id":      case_id,
        "txn_id":       a["txn_id"],
        "card_id":      card_id,
        "customer_id":  a["customer_id"],
        "opened_at":    a["ts"],
        "trigger_type": "risk_score",
        "trigger_text": (f"Real-time model scored transaction {a['txn_id']} "
                         f"(${a['amount']:.2f}) at {a['risk_score']:.2f}. Review."),
        "prior_log_odds": 0.0,
        "telemetry": Telemetry(),
    }
    t0 = time.time()
    try:
        state = run_plain(state)
    except Exception as e:  # noqa: BLE001
        return {"case_id": case_id, "error": str(e)[:200]}
    state["telemetry"].latency_seconds = time.time() - t0
    answer = build_answer_file(state)
    return {
        "case_id":   case_id,
        "txn_id":    a["txn_id"],
        "customer_id": a["customer_id"],
        "opened_at": a["ts"],
        "risk_score": a["risk_score"],
        "verdict":   answer["case"]["verdict"],
        "p_initial": state.get("p_initial"),
        "p_final":   answer["case"]["fraud_probability"],
        "pattern":   answer["case"]["pattern"],
        "exposure":  answer["case"]["exposure_usd"],
        "sar":       answer["sar"]["file"],
        "actions":   [x["action"] for x in answer["next_best_actions"]["final"]],
        "graph_case_id": answer["case"]["graph_case_id"],
        "latency_s": answer["latency_s"],
        "answer":    answer,
    }


def sweep(n: int = 20, min_score: float = 0.90) -> None:
    out_dir = REPO / "cases_extra"
    out_dir.mkdir(parents=True, exist_ok=True)
    alerts = _candidate_alerts(n=n, min_score=min_score)
    print(f"Monitor: {len(alerts)} candidate alerts (risk_score ≥ {min_score}), "
          f"excluding the 20 benchmark txns.")
    records: list[dict] = []
    for i, a in enumerate(alerts, 1):
        r = _run_one_extra(a, i)
        # Save the answer JSON per case.
        if "answer" in r:
            (out_dir / f"{r['case_id']}.json").write_text(
                json.dumps(r["answer"], indent=2))
            del r["answer"]
        records.append(r)
        print(f"[{i}/{len(alerts)}] {r['case_id']} txn={a['txn_id']} "
              f"→ verdict={r.get('verdict','?')} pattern={r.get('pattern','?')} "
              f"sar={r.get('sar', '?')}")
    (out_dir / "_index.json").write_text(json.dumps(records, indent=2, default=str))
    n_fraud = sum(1 for r in records if r.get("verdict") == "fraud")
    n_sar   = sum(1 for r in records if r.get("sar"))
    print(f"\nSummary: {n_fraud}/{len(records)} fraud verdicts, {n_sar} SAR filings.")
    print(f"Wrote → cases_extra/*.json + cases_extra/_index.json")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    ms = float(sys.argv[2]) if len(sys.argv) > 2 else 0.90
    sweep(n=n, min_score=ms)
