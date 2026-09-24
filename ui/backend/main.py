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


# Hub cap matches the agent's device_neighbors call
# (sentinel/agent/graph.py — p_degree_cap=100). Above this, the device is
# treated as a common browser hub and ring signals are suppressed.
HUB_DEGREE_CAP = 100
# Cosmetic max nodes drawn — caption reports "showing K of N" when capped.
MAX_PEER_CARDS = 40
MAX_CC_NODES = 8


@app.get("/api/cases/{case_id}/graph")
def get_graph(case_id: str) -> dict:
    """Force-layout neighbourhood, live from TigerGraph.

    Given the case's flagged txn:
      1. resolve its device_profile (DuckDB txn_features);
      2. call the installed ``device_neighbors`` GSQL query with the
         SAME hub cap (100) the agent uses — if the device is a hub,
         return case + device only with an explanatory caption;
      3. otherwise draw case (red), device (yellow), peer cards (blue,
         de-duplicated), and confirmed-fraud ClosedCases attached to
         the device (purple). Cards whose tuple matches a confirmed
         fraud ClosedCase's card get a red outline.

    Caption counts always come from the SAME query that draws the
    graph, so they cannot diverge from what appears on screen.
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
        from sentinel.agent.id_resolver import card_tuple_id as _tup_resolve
        import csv as _csv

        # 1. Flagged txn → device_profile.
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
                               "(offline / non-online alert)",
                    "device_profile": None, "is_hub": False,
                    "n_cards_all_time": 0, "n_other_cards_in_window": 0,
                    "n_confirmed_fraud_ccs": 0}

        # 2. Device node up-front — every non-empty branch uses it.
        dev_label = (device_profile or "")[:36]
        dev_nid = f"device:{device_profile[:48]}"

        end = datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S")
        start = end - timedelta(days=14)

        # 3. Two calls to device_neighbors: one at cap=5000 to learn the
        #    all-time device population size (n_cards_all_time is bounded
        #    only by the cap in the query — a hub device would still
        #    report accurately here). One at cap=100 (agent parity) to
        #    know whether the ring signal fires; when the ring fires,
        #    that call is also our peer-card list.
        cli = TGClient()
        try:
            body_common = {
                "p_device":       {"id": device_profile},
                "p_window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "p_window_end":   end.strftime("%Y-%m-%d %H:%M:%S"),
            }
            r_full = cli.restpp_post(
                f"query/{TG_GRAPHNAME}/device_neighbors",
                {**body_common, "p_degree_cap": 5000},
            )
            r_agent = cli.restpp_post(
                f"query/{TG_GRAPHNAME}/device_neighbors",
                {**body_common, "p_degree_cap": HUB_DEGREE_CAP},
            )
        finally:
            cli.close()

        def _merge(resp):
            m: dict = {}
            for row in (resp.get("results") or []):
                for k, v in row.items():
                    m[k.lstrip("@")] = v
            return m

        m_full  = _merge(r_full)
        m_agent = _merge(r_agent)
        n_cards_all_time = int(m_full.get("n_cards_all_time") or 0)

        # 4. Hub case — matches agent behaviour: no ring signal.
        if n_cards_all_time > HUB_DEGREE_CAP:
            nodes.append({"id": dev_nid, "label": dev_label, "type": "device"})
            edges.append({"source": case_id, "target": dev_nid, "label": "ON_DEVICE"})
            return {
                "case_id": case_id, "nodes": nodes, "edges": edges,
                "caption": (f"Common device profile shared by "
                            f"{n_cards_all_time} cards; not treated as a "
                            f"ring signal (hub cap = {HUB_DEGREE_CAP})."),
                "device_profile":         device_profile,
                "is_hub":                 True,
                "n_cards_all_time":       n_cards_all_time,
                "n_other_cards_in_window": 0,
                "n_confirmed_fraud_ccs":  0,
            }

        # 5. Below the cap — agent path fires. Use r_agent's cards (same
        #    call agent stores in graph_signals.device_neighbors) so the
        #    counts here match the evidence exactly.
        def _vid(x):
            return x if isinstance(x, str) else (x.get("v_id") if isinstance(x, dict) else None)

        seed_tuple = _tup_resolve(rec["card_id"]) if rec else ""
        peer_tuples: list[str] = []
        seen_tuples: set[str] = set()
        for c in (m_agent.get("cards_in_window") or []):
            v = _vid(c)
            if not v or v == seed_tuple or v in seen_tuples:
                continue
            peer_tuples.append(v); seen_tuples.add(v)

        cc_ids: list[str] = []
        seen_cc: set[str] = set()
        for cc in (m_agent.get("closed_cases_on_device") or []):
            v = _vid(cc)
            if v and v not in seen_cc:
                cc_ids.append(v); seen_cc.add(v)

        # 6. Fraud peer tuples: resolve each CC's card_id (display form)
        #    from the CSV → tuple form.
        fraud_ccs: list[dict] = []
        fraud_peer_tuples: set[str] = set()
        cc_csv = REPO_ROOT / "data" / "raw" / "closed_cases_history.csv"
        if cc_csv.exists() and cc_ids:
            cc_set = set(cc_ids)
            with open(cc_csv) as _f:
                for row in _csv.DictReader(_f):
                    if row["case_id"] in cc_set and row.get("outcome") == "confirmed_fraud":
                        display = row.get("card_id") or ""
                        tup = _tup_resolve(display) if display else ""
                        fraud_peer_tuples.add(tup)
                        fraud_ccs.append({
                            "case_id":       row["case_id"],
                            "card_tuple_id": tup,
                            "card_display":  display,
                        })

        # 7. Resolve peer tuple → display card_id via DuckDB card_map.
        display_by_tuple: dict[str, str] = {}
        if peer_tuples:
            con = connect()
            placeholders = ",".join("?" * len(peer_tuples))
            rows = con.execute(
                f"""SELECT customer_id || '|' || c2 || '|' || c3 || '|' ||
                            c4 || '|' || c5 || '|' || c6 AS tup, card_id
                     FROM card_map WHERE tup IN ({placeholders})""",
                peer_tuples,
            ).fetchall()
            display_by_tuple = {t: cid for t, cid in rows}

        # 8. Emit case → device → peer-card edges (dedup by tuple).
        nodes.append({"id": dev_nid, "label": dev_label, "type": "device"})
        edges.append({"source": case_id, "target": dev_nid, "label": "ON_DEVICE"})

        n_other_full = len(peer_tuples)                 # true count from query
        drawn_peers = 0
        added_ids: set[str] = set()
        for tup in peer_tuples[:MAX_PEER_CARDS]:
            nid = f"card:{tup[:48]}"
            if nid in added_ids:                        # DOM-level dedup
                continue
            added_ids.add(nid)
            display = display_by_tuple.get(tup) or tup.split("|", 1)[0]
            nodes.append({
                "id":           nid,
                "label":        display,
                "type":         "card",
                "has_fraud_cc": tup in fraud_peer_tuples,
            })
            edges.append({"source": dev_nid, "target": nid, "label": "USED"})
            drawn_peers += 1

        # 9. ClosedCase nodes.
        for cc in fraud_ccs[:MAX_CC_NODES]:
            nid = f"cc:{cc['case_id']}"
            if nid in added_ids:
                continue
            added_ids.add(nid)
            nodes.append({"id": nid, "label": cc["case_id"], "type": "closed_case"})
            edges.append({"source": dev_nid, "target": nid, "label": "CC_ON_DEVICE"})

        # 10. Caption reads from the SAME query that drew the graph.
        #     If the case's evidence quotes a different "N other cards" number
        #     (typically from ring_components with p_expand_family=True which
        #     follows device_family, not the exact device_profile), footnote
        #     the discrepancy so the reader sees why the numbers differ.
        n_fraud_ccs = len(fraud_ccs)
        import re as _re
        ev_n_other = None
        for e in (d["case"].get("evidence") or []):
            m = _re.search(r"(\d+)\s+other\s+cards?\s+in\s+window",
                            (e.get("claim") or ""))
            if m:
                ev_n_other = int(m.group(1)); break
        pieces = [f"Shared device `{dev_label}`: {n_other_full} other card"
                  f"{'s' if n_other_full != 1 else ''} in the ±14-day window, "
                  f"{n_fraud_ccs} with a confirmed-fraud ClosedCase attached "
                  f"to this device."]
        if drawn_peers < n_other_full:
            pieces.append(f" Showing {drawn_peers} of {n_other_full} cards "
                          f"(cap for readability).")
        if ev_n_other is not None and ev_n_other != n_other_full:
            pieces.append(f" (Case evidence quotes {ev_n_other}; that number "
                          f"comes from ring_components with device_family "
                          f"expansion — a broader neighbourhood. This graph "
                          f"shows only the exact same device_profile.)")
        caption = "".join(pieces)

        return {
            "case_id": case_id, "nodes": nodes, "edges": edges,
            "caption": caption,
            "device_profile":          device_profile,
            "is_hub":                  False,
            "n_cards_all_time":        n_cards_all_time,
            "n_other_cards_in_window": n_other_full,
            "n_confirmed_fraud_ccs":   n_fraud_ccs,
        }
    except Exception as e:  # noqa: BLE001
        return {"case_id": case_id, "nodes": nodes, "edges": edges,
                "caption": f"live TG query failed: {type(e).__name__}: {str(e)[:120]}",
                "device_profile": device_profile}


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
