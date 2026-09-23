"""Resume-regen script: only re-run HHG cases 13–20 (the tail that was
in progress when TG hibernated) and EXTRA cases 13–15.

The first 12 HHG and first 12 EXTRA answer files already reflect the
current code (strict peer definition + citation guard + read-only DB
+ analyst-only evidence + ring_wcc wiring). This script fills in the
rest so we can R1-guard the whole 20+15 without paying the ~90 min to
re-run everything.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.pop("SENTINEL_LLM_DISABLED", None)
os.environ.pop("SENTINEL_WRITE_MEMORY_DISABLED", None)
os.environ.setdefault("SENTINEL_GRAPH_VIA_MCP", "1")

from sentinel.agent.graph import run_plain
from sentinel.agent.state import Telemetry
from sentinel.output.answer_file import build_answer_file, write_answer_file
from sentinel.policy.invariants import check_invariants

TARGETS_HHG = {f"HHG-{i:03d}" for i in range(13, 21)}


def main() -> int:
    with open(REPO / "data" / "raw" / "case_pack.csv") as f:
        alerts = [r for r in csv.DictReader(f) if r["case_id"] in TARGETS_HHG]
    print(f"Resuming: {len(alerts)} HHG cases from {sorted(TARGETS_HHG)}\n",
          flush=True)
    for a in alerts:
        cid = a["case_id"]
        print(f"[{cid}] starting...", flush=True)
        state = {
            "case_id": cid, "txn_id": a["flagged_txn_id"],
            "card_id": a["card_id"], "customer_id": a["customer_id"],
            "opened_at": a["opened_at"], "trigger_type": a["trigger_type"],
            "trigger_text": a["trigger_text"], "prior_log_odds": 0.0,
            "telemetry": Telemetry(),
        }
        t0 = time.time()
        try:
            state = run_plain(state)
        except Exception as e:
            print(f"[{cid}] EXCEPTION: {e}", flush=True)
            continue
        ans = build_answer_file(state)
        write_answer_file(state)
        v = check_invariants(ans)
        codes = [x.invariant for x in v]
        print(f"[{cid}] {time.time()-t0:.1f}s verdict={ans['case']['verdict']} "
              f"p={ans['case']['fraud_probability']:.3f} "
              f"pattern={ans['case']['pattern']} sar={ans['sar']['file']} "
              f"invariants={'OK' if not codes else codes}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
