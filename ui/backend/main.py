"""FastAPI backend for the Sentinel UI.

Serves:
  * ``/api/cases``, ``/api/cases/{id}``: cases/*.json
  * ``/api/cases/{id}/{ledger,timeline,graph}``: per-case views
  * ``/api/backtest``: latest predictions preview + BACKTEST_REPORT.md
  * ``/api/memory``: SentinelCase count from TG
  * ``/api/whatif/{id}``: re-run the policy engine with a custom response
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "cases"
BACKTEST_DIR = REPO_ROOT / "backtest"
FRONTEND_DIR = REPO_ROOT / "ui" / "frontend"

app = FastAPI(title="Sentinel UI backend", version="0.3")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _list_cases() -> list[dict]:
    out = []
    for p in sorted(CASES_DIR.glob("HHG-*.json")):
        try: d = _load_json(p)
        except Exception: continue
        c = d.get("case", {}); sar = d.get("sar", {})
        out.append({
            "case_id": d["case_id"],
            "verdict": c.get("verdict"),
            "fraud_probability": c.get("fraud_probability"),
            "pattern": c.get("pattern"),
            "status":  c.get("status"),
            "exposure_usd": c.get("exposure_usd"),
            "sar_file":  sar.get("file", False),
            "graph_case_id": c.get("graph_case_id"),
        })
    return out


# ---- API --------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "n_cases": len(list(CASES_DIR.glob("HHG-*.json")))}


@app.get("/api/cases")
def list_cases() -> list[dict]:
    return _list_cases()


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict:
    p = CASES_DIR / f"{case_id}.json"
    if not p.exists(): raise HTTPException(404)
    return _load_json(p)


@app.get("/api/cases/{case_id}/ledger")
def get_ledger(case_id: str) -> list[dict]:
    return get_case(case_id)["case"].get("evidence", [])


@app.get("/api/cases/{case_id}/timeline")
def get_timeline(case_id: str) -> dict:
    d = get_case(case_id)
    return {
        "case_id": case_id,
        "latency_s":   d.get("latency_s"),
        "tool_calls":  d.get("tool_calls"),
        "tokens":      d.get("tokens"),
        "stop_reason": d.get("stop_reason"),
    }


@app.get("/api/cases/{case_id}/graph")
def get_graph(case_id: str) -> dict:
    """Force-layout neighbourhood: card_window + device_neighbors.
    Nodes: case, card, device, connected cards. Edges: labelled.
    """
    d = get_case(case_id)
    nodes: list[dict] = []
    edges: list[dict] = []

    # Central case node
    nodes.append({"id": case_id, "label": case_id,
                  "type": "case",
                  "verdict": d["case"]["verdict"]})

    # Card + devices
    row_card = f"card:{d.get('case_id')}"
    for dp in d["case"].get("connected_device_profiles", [])[:5]:
        did = f"device:{dp[:24]}"
        nodes.append({"id": did, "label": dp[:24], "type": "device"})
        edges.append({"source": case_id, "target": did, "label": "ON_DEVICE"})
    for cid in d["case"].get("connected_card_ids", [])[:8]:
        nodes.append({"id": cid, "label": cid, "type": "card"})
        edges.append({"source": case_id, "target": cid, "label": "CONNECTED_CARD"})
    for cc in d["case"].get("similar_prior_cases", [])[:6]:
        nodes.append({"id": cc, "label": cc, "type": "closed_case"})
        edges.append({"source": case_id, "target": cc, "label": "SIMILAR_TO"})

    # Optional live enrichment via TG (best-effort).
    try:
        from sentinel.graph.client import TGClient
        from sentinel.agent.id_resolver import card_tuple_id
        card_pk = card_tuple_id(_hhg_card_id(d))
        if card_pk:
            as_of = datetime.strptime(d["case_id"].startswith("HHG") and
                                       _hhg_opened_at(d) or "2016-11-01 00:00:00",
                                       "%Y-%m-%d %H:%M:%S")
            tg = TGClient()
            r = tg.run_query("FraudGraph", "card_window", {
                "p_card": {"id": card_pk},
                "p_window_start": (as_of - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S"),
                "p_window_end":   as_of.strftime("%Y-%m-%d %H:%M:%S"),
            })
            if not r.get("error"):
                seen_devs = set()
                for it in r.get("results", []):
                    for t in (it.get("Txns") or [])[:15]:
                        dp = t.get("attributes", {}).get("device_profile", "")
                        if dp and dp not in seen_devs and dp not in nodes:
                            seen_devs.add(dp)
                            did = f"device:{dp[:24]}"
                            if not any(n["id"] == did for n in nodes):
                                nodes.append({"id": did, "label": dp[:24], "type": "device"})
                                edges.append({"source": case_id, "target": did, "label": "SEEN_ON"})
    except Exception:
        pass

    return {"case_id": case_id, "nodes": nodes, "edges": edges}


def _hhg_card_id(answer: dict) -> str | None:
    """Recover card_id from case_pack.csv."""
    import csv
    with open(REPO_ROOT / "data" / "raw" / "case_pack.csv") as f:
        for r in csv.DictReader(f):
            if r["case_id"] == answer["case_id"]:
                return r["card_id"]
    return None


def _hhg_opened_at(answer: dict) -> str | None:
    import csv
    with open(REPO_ROOT / "data" / "raw" / "case_pack.csv") as f:
        for r in csv.DictReader(f):
            if r["case_id"] == answer["case_id"]:
                return r["opened_at"]
    return None


@app.get("/api/backtest")
def backtest_summary() -> dict:
    """Latest simulated + oracle preview + BACKTEST_REPORT markdown."""
    out = {"available": False}
    for name, path in [
        ("simulated", BACKTEST_DIR / "predictions_simulated_n150.jsonl"),
        ("oracle",    BACKTEST_DIR / "predictions_oracle_n150.jsonl"),
    ]:
        if path.exists():
            rows: list[dict] = []
            with open(path) as f:
                for l in f:
                    l = l.strip()
                    if l:
                        try: rows.append(json.loads(l))
                        except Exception: pass
            out[name] = {"n": len(rows), "preview": rows[:10],
                         "path": str(path.relative_to(REPO_ROOT))}
    if any(k in out for k in ("simulated", "oracle")):
        out["available"] = True
    md = BACKTEST_DIR / "BACKTEST_REPORT.md"
    out["report"] = md.read_text() if md.exists() else ""
    out["reliability_url"] = "/backtest/reliability_simulated.png" if (BACKTEST_DIR / "reliability_simulated.png").exists() else None

    # -- Metrics box (top of the Backtest tab) --
    metrics: dict = {}
    # OOT eval from scripts/oot_eval.py
    oot_path = BACKTEST_DIR / "oot" / "oot_eval.json"
    if oot_path.exists():
        try:
            rows = json.loads(oot_path.read_text())
            n = len(rows); n_pos = sum(1 for r in rows if r.get("label") == 1)
            n_neg = n - n_pos
            # Also read AUC from OOT_REPORT.md if available; otherwise compute here.
            report_md = (BACKTEST_DIR / "oot" / "OOT_REPORT.md")
            auc = brier = None
            if report_md.exists():
                import re as _re
                txt = report_md.read_text()
                m_a = _re.search(r"AUC\s*=\s*([\d.]+)", txt)
                m_b = _re.search(r"Brier\s*=\s*([\d.]+)", txt)
                auc = float(m_a.group(1)) if m_a else None
                brier = float(m_b.group(1)) if m_b else None
            metrics["oot"] = {
                "n": n, "n_fraud": n_pos, "n_cleared": n_neg,
                "auc": auc, "brier": brier,
            }
        except Exception:
            pass
    # 5-fold CV from alert_model.json
    am_path = REPO_ROOT / "sentinel" / "evidence" / "alert_model.json"
    if am_path.exists():
        try:
            am = json.loads(am_path.read_text())
            metrics["cv"] = {
                "auc": am.get("cv_auc_mean"),
                "auc_std": am.get("cv_auc_std"),
                "brier": am.get("cv_brier_mean"),
                "n_train": am.get("n_train"),
            }
        except Exception:
            pass
    out["metrics"] = metrics
    return out


@app.get("/backtest/{fname}")
def backtest_static(fname: str):
    """Serve reliability plots + backtest report if requested by path."""
    p = BACKTEST_DIR / fname
    if not p.exists():
        raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(p)


@app.get("/api/extras")
def extras_index() -> dict:
    """Monitoring-mode alerts (cases_extra/)."""
    idx = REPO_ROOT / "cases_extra" / "_index.json"
    if not idx.exists():
        return {"available": False, "rows": []}
    rows = json.loads(idx.read_text())
    return {"available": True, "n": len(rows), "rows": rows}


@app.get("/api/extras/{case_id}")
def extras_case(case_id: str) -> dict:
    p = REPO_ROOT / "cases_extra" / f"{case_id}.json"
    if not p.exists():
        raise HTTPException(404)
    return _load_json(p)


@app.get("/api/findings")
def findings() -> dict:
    """UNDOCUMENTED_FINDINGS.md content for the Findings view."""
    p = REPO_ROOT / "docs" / "UNDOCUMENTED_FINDINGS.md"
    if not p.exists():
        return {"available": False, "error": "docs/UNDOCUMENTED_FINDINGS.md not built yet"}
    return {"available": True, "markdown": p.read_text(encoding="utf-8")}


@app.get("/api/memory")
def memory() -> dict:
    """SentinelCase count from the graph."""
    try:
        from sentinel.graph.client import TGClient
        tg = TGClient()
        n = tg.get_graph_vertex_count("FraudGraph", "SentinelCase")
        return {"sentinel_case_count": n, "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/whatif/{case_id}")
def whatif(case_id: str, body: dict) -> dict:
    """Re-run the policy engine with a swapped customer_response.

    Body:  {"customer_response": "denied|confirmed|no_reply"}

    Verdict is derived from the response per §6:
      * denied    ⇒ fraud, p = max(p, 0.85)
      * confirmed ⇒ legitimate, p = min(p, 0.15)
      * no_reply  ⇒ uncertain (posterior unchanged)

    trigger_type + is_dispute are read from data/raw/case_pack.csv;
    is_recurring_match is set when the persisted evidence carries an
    R7 `recurring_match` item (so HHG-003's dispute path still fires
    R7 no-block on a denied preview).
    """
    from sentinel.policy.policy_engine import PolicyInput, decide
    d = get_case(case_id)
    resp = body.get("customer_response")
    if resp not in ("denied", "confirmed", "no_reply", None):
        raise HTTPException(400, "customer_response must be denied|confirmed|no_reply")
    c = d["case"]

    # --- pull trigger_type + is_dispute from case_pack.csv (HHG only) ---
    import csv as _csv
    from sentinel.config import RAW as _RAW
    trigger_type = "risk_score"; is_dispute = False
    pack = _RAW / "case_pack.csv"
    if pack.exists():
        with open(pack) as _f:
            for row in _csv.DictReader(_f):
                if row.get("case_id") == case_id:
                    trigger_type = row.get("trigger_type") or "risk_score"
                    is_dispute = (trigger_type == "customer_report")
                    break

    # --- is_recurring_match from persisted evidence (R7 preservation) ---
    is_recurring_match = any(
        "recurring_match" in (e.get("ref") or "").lower()
        or "recurring" in (e.get("claim") or "").lower() and "cadence" in (e.get("claim") or "").lower()
        for e in (c.get("evidence") or [])
    )

    # --- derive verdict + p from the response per §6 ---
    p_base = float(c.get("fraud_probability", 0.5) or 0.5)
    if resp == "denied":
        verdict = "fraud";       p = max(p_base, 0.85)
    elif resp == "confirmed":
        verdict = "legitimate";  p = min(p_base, 0.15)
    elif resp == "no_reply":
        verdict = "uncertain";   p = p_base
    else:
        verdict = c.get("verdict", "uncertain"); p = p_base

    # I9: legitimate ⇒ pattern=none.
    pattern = "none" if verdict == "legitimate" else (c.get("pattern") or "none")

    pin = PolicyInput(
        verdict=verdict,                              # type: ignore
        fraud_probability=p,
        exposure_usd=float(c.get("exposure_usd", 0.0) or 0.0),
        pattern=pattern,                              # type: ignore
        shared_element=None,       # not persisted in the answer; safe default
        customer_response=resp,                       # type: ignore
        is_dispute=is_dispute,
        is_recurring_match=is_recurring_match,
        undocumented_coordinated=(pattern == "undocumented"),
        trigger_type=trigger_type,                    # type: ignore
    )
    decision = decide(pin)
    return {
        "case_id":         case_id,
        "response_used":   resp,
        "verdict":         verdict,
        "fraud_probability": round(p, 4),
        "trigger_type":    trigger_type,
        "is_recurring_match": is_recurring_match,
        "actions":         [a.as_dict() for a in decision.actions],
        "sar_should_file": decision.sar_should_file,
        "escalate":        decision.escalate,
    }


# ---- Static frontend --------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
