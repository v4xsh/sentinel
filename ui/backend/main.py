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
    """Force-layout neighbourhood built live from TigerGraph.

    Given the flagged txn for ``case_id``, look up its device_profile
    (via DuckDB's txn_features), then call the installed
    ``device_neighbors`` GSQL query for the ±14-day window. Emit:

      * one **case** node (red)  — the current case
      * one **device** node (yellow) — the shared device
      * up to 40 **card** nodes (blue) — cards that transacted on that
        device in-window; cards that carry a confirmed-fraud ClosedCase
        on this device get a red outline (``has_fraud_cc: true``)
      * up to 8 **closed_case** nodes (purple) — confirmed-fraud
        ClosedCases attached to the device

    Caption fields: ``device_profile``, ``n_other_cards``, ``n_fraud_ccs``.
    """
    d = get_case(case_id)
    case_verdict = d["case"]["verdict"]

    nodes: list[dict] = [{"id": case_id, "label": case_id, "type": "case",
                           "verdict": case_verdict}]
    edges: list[dict] = []
    caption = ""
    device_profile: str | None = None

    try:
        from sentinel.data.features import connect
        from sentinel.graph.client import TGClient
        from sentinel.config import TG_GRAPHNAME
        import csv as _csv

        # 1. Flagged txn → device_profile (via txn_features).
        with open(REPO_ROOT / "data" / "raw" / "case_pack.csv") as _f:
            rec = next((r for r in _csv.DictReader(_f) if r["case_id"] == case_id), None)
        opened_at = rec["opened_at"] if rec else "2016-11-01 00:00:00"
        flagged_txn = int(rec["flagged_txn_id"]) if rec else None
        if flagged_txn is not None:
            con = connect()
            row = con.execute(
                "SELECT device_profile FROM txn_features WHERE TransactionID = ?",
                [flagged_txn],
            ).fetchone()
            device_profile = row[0] if row and row[0] else None

        if not device_profile:
            return {"case_id": case_id, "nodes": nodes, "edges": edges,
                    "caption": "no device_profile on the flagged txn "
                               "(offline / non-online alert)"}

        # 2. Live device_neighbors query.
        end = datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S")
        start = end - timedelta(days=14)
        cli = TGClient()
        try:
            # Bigger cap than the policy path uses: the graph view is analyst
            # context, not a decision signal. We want to show real device-family
            # neighbourhoods (SM-G935F etc.) that carry >100 all-time cards.
            r = cli.restpp_post(
                f"query/{TG_GRAPHNAME}/device_neighbors",
                {
                    "p_device":       {"id": device_profile},
                    "p_window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "p_window_end":   end.strftime("%Y-%m-%d %H:%M:%S"),
                    "p_degree_cap":   5000,
                },
            )
        finally:
            cli.close()

        # Merge accumulators. Fields can be bare v_id strings (SetAccum
        # of VERTEX serialised without attributes) or {v_id, attributes}
        # objects — handle both.
        merged: dict = {}
        for row in r.get("results", []):
            for k, v in row.items():
                merged[k.lstrip("@")] = v

        def _vid(x):
            return x if isinstance(x, str) else (x.get("v_id") if isinstance(x, dict) else None)
        def _attrs(x):
            return x.get("attributes", {}) if isinstance(x, dict) else {}

        peer_cards   = [c for c in (merged.get("cards_in_window") or []) if _vid(c)]
        closed_cases = [c for c in (merged.get("closed_cases_on_device") or []) if _vid(c)]

        # 3. Device node + case→device edge.
        dev_label = (device_profile or "")[:36]
        dev_nid = f"device:{device_profile[:48]}"
        nodes.append({"id": dev_nid, "label": dev_label, "type": "device"})
        edges.append({"source": case_id, "target": dev_nid, "label": "ON_DEVICE"})

        # 4. Fraud peer tuples: look up each ClosedCase's card_tuple_id from
        #    the CSV so we can outline the right cards red.
        from sentinel.agent.id_resolver import card_tuple_id as _tup_resolve
        cc_id_set = {_vid(cc) for cc in closed_cases}
        fraud_peer_tuples: set[str] = set()
        fraud_ccs: list[dict] = []       # list of (case_id, card_tuple_id, outcome)
        cc_csv = REPO_ROOT / "data" / "raw" / "closed_cases_history.csv"
        if cc_csv.exists() and cc_id_set:
            with open(cc_csv) as _f:
                for row in _csv.DictReader(_f):
                    if row["case_id"] in cc_id_set:
                        if row.get("outcome") == "confirmed_fraud":
                            # CSV stores display form (C09998-K1); resolve
                            # to the pipe-separated tuple that matches the
                            # cards_in_window vertex ids.
                            display = row.get("card_id") or ""
                            tup = _tup_resolve(display) if display else ""
                            fraud_peer_tuples.add(tup)
                            fraud_ccs.append({
                                "case_id":       row["case_id"],
                                "card_tuple_id": tup,
                                "card_display":  display,
                            })

        # 5. Card nodes (skip the seed's own tuple).
        from sentinel.agent.id_resolver import card_tuple_id as _tup
        seed_tuple = _tup(rec["card_id"]) if rec else ""
        card_added = 0
        for c in peer_cards:
            v_id = _vid(c)
            if not v_id or v_id == seed_tuple:
                continue
            attrs = _attrs(c)
            display = attrs.get("card_id_display") or (v_id.split("|")[0] + "-K?")
            has_fraud = v_id in fraud_peer_tuples
            nodes.append({
                "id":           f"card:{v_id[:48]}",
                "label":        display,
                "type":         "card",
                "has_fraud_cc": has_fraud,
            })
            edges.append({"source": dev_nid, "target": f"card:{v_id[:48]}",
                           "label": "USED"})
            card_added += 1
            if card_added >= 40:
                break

        # 6. ClosedCase nodes (purple), edge device → cc.
        for cc in fraud_ccs[:8]:
            nodes.append({
                "id":     f"cc:{cc['case_id']}",
                "label":  cc["case_id"],
                "type":   "closed_case",
            })
            edges.append({"source": dev_nid, "target": f"cc:{cc['case_id']}",
                           "label": "CC_ON_DEVICE"})

        n_fraud_ccs = len(fraud_ccs)
        n_other = card_added
        caption = (f"Shared device `{dev_label}`: {n_other} other card"
                   f"{'s' if n_other != 1 else ''} in the ±14-day window, "
                   f"{n_fraud_ccs} with a confirmed-fraud ClosedCase attached "
                   f"to this device.")
    except Exception as e:  # noqa: BLE001
        caption = f"live TG query failed: {type(e).__name__}: {str(e)[:120]}"

    return {"case_id": case_id, "nodes": nodes, "edges": edges,
            "caption": caption, "device_profile": device_profile}


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
