"""Resume-regen the last 3 EXTRA cases (EXTRA-013, 014, 015)."""

from __future__ import annotations

import os, sys, time, json, csv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("SENTINEL_LLM_DISABLED", "1")
os.environ.setdefault("SENTINEL_GRAPH_VIA_MCP", "0")
os.environ.setdefault("SENTINEL_WRITE_MEMORY_DISABLED", "0")

from sentinel.agent.graph import run_plain
from sentinel.agent.state import Telemetry
from sentinel.output.answer_file import build_answer_file, write_answer_file
from sentinel.data.features import connect

# The monitor script picks the top-N by risk_score. We need the same 15,
# then keep only #13, #14, #15.
def _candidates(n: int = 30, min_score: float = 0.90) -> list[dict]:
    with open(REPO / "data" / "raw" / "case_pack.csv") as _f:
        bench = {str(r["flagged_txn_id"]) for r in csv.DictReader(_f)}
    con = connect()
    rows = con.execute(
        """
        SELECT TransactionID, ts, channel, customer_id, risk_score, TransactionAmt, addr1
        FROM txn_features
        WHERE risk_score >= ? AND ts >= '2016-11-01' AND ts <= '2016-12-31'
        ORDER BY risk_score DESC, ts DESC LIMIT ?
        """,
        [float(min_score), int(n * 3)],
    ).fetchall()
    out = []
    for tid, ts, ch, cust, rs, amt, addr in rows:
        if str(tid) in bench:
            continue
        out.append({"txn_id": str(tid), "ts": str(ts), "channel": ch,
                    "customer_id": cust, "risk_score": float(rs or 0),
                    "amount": float(amt or 0), "addr1": str(addr or "")})
        if len(out) >= n:
            break
    return out


def _card_id(customer_id: str) -> str:
    con = connect()
    r = con.execute("SELECT card_id FROM card_map WHERE customer_id = ? ORDER BY k LIMIT 1",
                     [customer_id]).fetchone()
    return r[0] if r else f"{customer_id}-K1"


def _run_one(a: dict, idx: int) -> dict:
    cid = f"EXTRA-{idx:03d}"
    print(f"[{cid}] starting txn={a['txn_id']}...", flush=True)
    state = {
        "case_id":       cid,
        "txn_id":        a["txn_id"],
        "card_id":       _card_id(a["customer_id"]),
        "customer_id":   a["customer_id"],
        "opened_at":     a["ts"],
        "trigger_type":  "risk_score",
        "trigger_text":  f"Monitoring alert @ {a['ts']} on txn {a['txn_id']} "
                          f"(risk_score={a['risk_score']:.3f}).",
        "prior_log_odds": 0.0,
        "telemetry": Telemetry(),
    }
    t0 = time.time()
    try:
        state = run_plain(state)
    except Exception as e:
        print(f"[{cid}] EXCEPTION: {e}", flush=True)
        return {"case_id": cid, "error": str(e)}
    ans = build_answer_file(state)
    write_answer_file(state, out_dir=REPO / "cases_extra")
    print(f"[{cid}] {time.time()-t0:.1f}s verdict={ans['case']['verdict']} "
          f"pattern={ans['case']['pattern']} sar={ans['sar']['file']}",
          flush=True)
    return {"case_id": cid, "txn_id": a["txn_id"],
            "verdict": ans["case"]["verdict"],
            "pattern": ans["case"]["pattern"]}


def main() -> int:
    cands = _candidates(n=15, min_score=0.90)
    if len(cands) < 15:
        print(f"ERROR: only {len(cands)} candidates found"); return 1
    for i in (13, 14, 15):
        _run_one(cands[i-1], i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
