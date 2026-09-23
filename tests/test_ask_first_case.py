"""Acceptance item: at least one benchmark case must be an ask-first case.

An ask-first case is one where ``evidence_requests`` is non-empty AND the
final action list is *different* from the initial action list — i.e. the
agent decided to ask the customer, the customer answered, and the
response changed what the bank does. This is the case the demo video
opens on; if there aren't any, the whole simulate/verify branch of the
agent went unused on the benchmark, which is a story problem before
it's a code problem.

Also asserts sar.file ⇔ FILE_REPORT in final on every case (I1
re-assertion, catches any drift the sweep test misses).
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _ask_first(a: dict) -> bool:
    if not (a.get("evidence_requests") or []):
        return False
    nba = a["next_best_actions"]
    ini = [x["action"] for x in nba["initial"]]
    fin = [x["action"] for x in nba["final"]]
    return ini != fin


def test_at_least_one_ask_first_benchmark_case():
    d = REPO / "cases"
    files = sorted(d.glob("HHG-*.json"))
    assert files, "no benchmark answer files present"
    ask_first = []
    for p in files:
        a = json.loads(p.read_text())
        if _ask_first(a):
            ask_first.append(p.name.replace(".json", ""))
    assert ask_first, (
        "No benchmark case has non-empty evidence_requests with initial ≠ final. "
        "The demo video opens on such a case; regen must produce at least one."
    )


def test_i1_sar_iff_file_report_across_all_cases():
    for dirname in ("cases", "cases_extra"):
        d = REPO / dirname
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            if not p.name.startswith(("HHG-", "EXTRA-")):
                continue
            a = json.loads(p.read_text())
            sar_file = bool((a.get("sar") or {}).get("file"))
            final_names = [x["action"] for x in a["next_best_actions"]["final"]]
            has_report = "FILE_REPORT" in final_names
            assert sar_file == has_report, (
                f"{p.name}: sar.file={sar_file} but FILE_REPORT-in-final="
                f"{has_report}. I1 violated."
            )
