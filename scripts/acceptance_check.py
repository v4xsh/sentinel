"""Full acceptance checklist from PATH_TO_70.md + the two judge notes.

Prints PASS/FAIL for each check; exits 0 iff every check passes.
"""

from __future__ import annotations

import glob
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _ok(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}]  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def check_1_pytest() -> bool:
    r = subprocess.run(["python", "-m", "pytest", str(REPO / "tests"), "-q",
                         "--tb=no"], capture_output=True, text=True)
    ok = r.returncode == 0
    tail = r.stdout.strip().split("\n")[-1]
    return _ok("pytest", ok, tail)


def check_2_verdict_distribution() -> bool:
    files = sorted(glob.glob(str(REPO / "cases" / "HHG-*.json")))
    v = Counter(json.load(open(f))["case"]["verdict"] for f in files)
    ok = v == Counter({"fraud": 12, "legitimate": 8})
    return _ok("verdict split 12/8/0", ok, f"got {dict(v)}")


def check_3_sar_set() -> bool:
    files = sorted(glob.glob(str(REPO / "cases" / "HHG-*.json")))
    sars = sorted(f.split("/")[-1].replace(".json", "")
                  for f in files if json.load(open(f))["sar"]["file"])
    target = ["HHG-004", "HHG-006", "HHG-011", "HHG-014"]
    ok = sars == target
    return _ok(f"SAR set = {target}", ok, f"got {sars}")


def check_4_i1_sweep() -> bool:
    """I1 re-assertion — sar.file ⇔ FILE_REPORT-in-final across all files."""
    bad = []
    for dirname in ("cases", "cases_extra"):
        for f in sorted(glob.glob(str(REPO / dirname / "*.json"))):
            n = Path(f).name
            if not (n.startswith("HHG-") or n.startswith("EXTRA-")):
                continue
            a = json.load(open(f))
            sar = bool((a.get("sar") or {}).get("file"))
            names = [x["action"] for x in a["next_best_actions"]["final"]]
            if sar != ("FILE_REPORT" in names):
                bad.append(n)
    return _ok("I1 sar.file ⇔ FILE_REPORT (all 35 files)", not bad,
                f"violations: {bad}" if bad else "35 / 35 consistent")


def check_5_i12_hallucinations() -> bool:
    """No invented case-IDs in prose across all 35 files."""
    CC = re.compile(r"\bCC-\d{4,}\b")
    CASE = re.compile(r"\bCASE-[A-Z0-9]+-\d+\b")
    bad = []
    for dirname in ("cases", "cases_extra"):
        for f in sorted(glob.glob(str(REPO / dirname / "*.json"))):
            n = Path(f).name
            if not (n.startswith("HHG-") or n.startswith("EXTRA-")):
                continue
            a = json.load(open(f))
            c = a["case"]
            allowed = {a["case_id"]} | set(c.get("similar_prior_cases") or [])
            for field in ("summary", "pattern_description"):
                for cc in set(CC.findall(c.get(field, "") or "")):
                    if cc not in allowed:
                        bad.append((n, field, cc))
                for cs in set(CASE.findall(c.get(field, "") or "")):
                    if cs not in allowed:
                        bad.append((n, field, cs))
            nar = (a.get("sar") or {}).get("narrative", "") or ""
            for cc in set(CC.findall(nar)):
                if cc not in allowed:
                    bad.append((n, "sar.narrative", cc))
    return _ok("I12 no invented case-IDs (all prose fields)", not bad,
                f"hallucinations: {bad[:5]}" if bad else "0 hallucinations")


def check_6_initial_reasons_clean() -> bool:
    LEAK = re.compile(
        r"customer\s+(denied|confirmed|reply|said|confirms)"
        r"|no[_\s-]reply"
        r"|confirmed[_\s-]?(fraud|as[_\s-]fraud)"
        r"|denied\s+—\s+block",
        re.I,
    )
    bad = []
    for dirname in ("cases", "cases_extra"):
        for f in sorted(glob.glob(str(REPO / dirname / "*.json"))):
            n = Path(f).name
            if not (n.startswith("HHG-") or n.startswith("EXTRA-")):
                continue
            a = json.load(open(f))
            for act in a["next_best_actions"]["initial"]:
                r = act.get("reason", "") or ""
                if LEAK.search(r):
                    bad.append((n, act["action"], r[:80]))
    return _ok("no initial reason references a customer response",
                not bad, f"leaks: {bad[:3]}" if bad else "clean")


def check_7_fallback_wording() -> bool:
    """No answer file carries the old 'Fallback pattern X assigned' claim."""
    bad = []
    for dirname in ("cases", "cases_extra"):
        for f in sorted(glob.glob(str(REPO / dirname / "*.json"))):
            n = Path(f).name
            if not (n.startswith("HHG-") or n.startswith("EXTRA-")):
                continue
            txt = Path(f).read_text()
            if "Fallback pattern" in txt or "fallback_pattern" in txt:
                bad.append(n)
    return _ok("fallback wording gone", not bad,
                f"still present in: {bad}" if bad else "clean")


def check_8_ask_first_case() -> bool:
    """At least one HHG case has non-empty evidence_requests + initial ≠ final."""
    files = sorted(glob.glob(str(REPO / "cases" / "HHG-*.json")))
    hits = []
    for f in files:
        a = json.load(open(f))
        if not (a.get("evidence_requests") or []):
            continue
        ini = [x["action"] for x in a["next_best_actions"]["initial"]]
        fin = [x["action"] for x in a["next_best_actions"]["final"]]
        if ini != fin:
            hits.append(Path(f).name.replace(".json", ""))
    return _ok(f"ask-first case present (initial ≠ final)", len(hits) >= 1,
                f"{len(hits)} cases: {hits[:5]}")


def check_9_sentinelcase_count() -> bool:
    """35 SentinelCase vertices live in the graph."""
    try:
        sys.path.insert(0, str(REPO))
        from sentinel.graph.client import TGClient
        cli = TGClient()
        try:
            r = cli.restpp_get("graph/FraudGraph/vertices/SentinelCase",
                                params={"limit": 200})
            ids = [x["v_id"] for x in r.get("results", []) if isinstance(x, dict)]
            strays = [i for i in ids
                      if not (i.startswith("CASE-HHG-") or i.startswith("CASE-EXTRA-"))]
            ok = len(ids) == 35 and not strays
            return _ok("SentinelCase live count = 35 (no strays)", ok,
                        f"count={len(ids)}, strays={strays[:5]}")
        finally:
            cli.close()
    except Exception as e:
        return _ok("SentinelCase live count = 35 (no strays)",
                    False, f"TG unreachable: {e}")


def check_10_oot_number_present() -> bool:
    for f in ("README.md", "docs/SUBMISSION.md", "docs/BENCHMARK_RESULTS.md",
              "docs/BLOG_DRAFT.md"):
        txt = (REPO / f).read_text()
        if "0.9431" not in txt:
            return _ok(f"OOT AUC 0.9431 in {f}", False, "not found")
    return _ok("OOT AUC 0.9431 in all four docs", True, "README + SUBMISSION + BENCHMARK + BLOG")


def main() -> int:
    print("═══ Sentinel acceptance checklist ═══\n")
    checks = [
        check_1_pytest,
        check_2_verdict_distribution,
        check_3_sar_set,
        check_4_i1_sweep,
        check_5_i12_hallucinations,
        check_6_initial_reasons_clean,
        check_7_fallback_wording,
        check_8_ask_first_case,
        check_9_sentinelcase_count,
        check_10_oot_number_present,
    ]
    results = [f() for f in checks]
    passed = sum(results); total = len(results)
    print(f"\n═══ {passed} / {total} checks passed ═══")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
