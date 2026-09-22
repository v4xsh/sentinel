"""Sentinel backtest runner (C6v2 protocol).

Sampling: 75 confirmed_fraud stratified by pattern + 75 cleared stratified
by archetype (travel / new_phone / big_purchase, inferred from
analyst_notes). Trigger = risk_score. Flagged = ``first_fraud_txn_id`` for
confirmed_fraud, else the cleared case's first txn.

Modes
-----
``--mode oracle`` — customer_response derived from ``actions_taken``:
    - ``BLOCK_CARD`` in actions_taken → denied
    - ``CLOSE_NO_FRAUD`` in actions_taken → confirmed
    - else → no_reply

``--mode simulated`` — our simulator:
    denied iff p_initial ≥ τ (tuned to max response accuracy);
    confirmed otherwise. no_reply only when the graph is unreachable
    (raised as an error during memory_retrieve or _fetch_graph_signals).
    Overrides:
      * legit-archetype detector fired (recurring, trip, new phone, big
        purchase) → confirmed regardless.
      * ring T3+ OR testing_sequence fired → denied regardless.

Metrics reported: verdict accuracy, pattern confusion matrix, SAR agreement
with report_filed, exposure MAE, affected-txn Jaccard (before & after
signature expansion), Brier/AUC/reliability of p_initial, simulator
response accuracy vs oracle (in simulated mode).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

from sentinel.agent.graph import run_plain
from sentinel.agent.state import Telemetry
from sentinel.config import REPO_ROOT
from sentinel.output.answer_file import build_answer_file

logger = logging.getLogger(__name__)

CC_CSV = REPO_ROOT / "data" / "raw" / "closed_cases_history.csv"


# ---- archetype inference for cleared cases ---------------------------------


TRAVEL_RE      = re.compile(r"\btravel(?:led|ling)?\b|\btrip\b", re.I)
NEW_PHONE_RE   = re.compile(r"\b(new phone|new device|got a new)\b", re.I)
BIG_PURCHASE_RE = re.compile(r"\b(large|big|expensive) (?:purchase|transaction)\b", re.I)


def _cleared_archetype(row: dict) -> str:
    notes = row.get("analyst_notes", "") or ""
    if TRAVEL_RE.search(notes):
        return "travel"
    if NEW_PHONE_RE.search(notes):
        return "new_phone"
    if BIG_PURCHASE_RE.search(notes):
        return "big_purchase"
    return "other"


# ---- sampling --------------------------------------------------------------


def _load_closed_cases() -> list[dict]:
    with open(CC_CSV) as f:
        return list(csv.DictReader(f))


def _sample_split(rows: list[dict], n_fraud: int, n_cleared: int, seed: int) -> list[dict]:
    """C6v2 sample: n_fraud stratified by pattern + n_cleared stratified by archetype."""
    rng = random.Random(seed)
    frauds  = [r for r in rows if r["outcome"] == "confirmed_fraud"]
    cleared = [r for r in rows if r["outcome"] == "cleared"]

    # Stratify frauds by pattern
    by_pat: dict[str, list[dict]] = {}
    for r in frauds:
        by_pat.setdefault(r.get("pattern") or "none", []).append(r)
    per_pat = max(1, n_fraud // len(by_pat))
    out: list[dict] = []
    for k, pool in by_pat.items():
        out.extend(rng.sample(pool, min(per_pat, len(pool))))
    # Top up.
    remaining = [r for r in frauds if r not in out]
    while len(out) < n_fraud and remaining:
        pick = rng.choice(remaining); out.append(pick); remaining.remove(pick)
    fraud_sample = out[:n_fraud]

    # Stratify cleared by archetype.
    by_arch: dict[str, list[dict]] = {}
    for r in cleared:
        by_arch.setdefault(_cleared_archetype(r), []).append(r)
    per_arch = max(1, n_cleared // max(1, len(by_arch)))
    out2: list[dict] = []
    for k, pool in by_arch.items():
        out2.extend(rng.sample(pool, min(per_arch, len(pool))))
    remaining = [r for r in cleared if r not in out2]
    while len(out2) < n_cleared and remaining:
        pick = rng.choice(remaining); out2.append(pick); remaining.remove(pick)
    cleared_sample = out2[:n_cleared]

    combined = fraud_sample + cleared_sample
    rng.shuffle(combined)
    return combined


# ---- oracle / simulator ---------------------------------------------------


def _oracle_response(row: dict) -> str:
    """derive customer_response from actions_taken in the ground truth."""
    acts = (row.get("actions_taken") or "").upper()
    if "BLOCK_CARD" in acts:
        return "denied"
    if "CLOSE_NO_FRAUD" in acts:
        return "confirmed"
    return "no_reply"


def _first_txn(cc: dict) -> str | None:
    if cc.get("first_fraud_txn_id"):
        return cc["first_fraud_txn_id"].strip()
    ids = (cc.get("txn_ids") or "").strip()
    if ids:
        return ids.split("|")[0].strip()
    return None


# ---- single-case runner ---------------------------------------------------


LEGIT_TAGS = {"is_recurring_match", "legit_recurring_r7_hint",
              "legit_trip_hint", "legit_new_phone_hint", "legit_big_purchase_hint"}


def _sim_response(state: dict, tau: float) -> tuple[str, str]:
    """Simulator: response based on tau threshold + overrides. Returns (response, rule)."""
    tags = state.get("_tags", set())
    # Override 1: ring T3+ or testing sequence → denied.
    graph = state.get("graph_signals", {})
    ring = graph.get("ring_components") or {}
    ring_deg_ok = 0 < int(ring.get("device_degree") or 0) <= 100
    ring_hits = (int(ring.get("n_cards", 0) or 0) - 1) >= 2
    ring_cc   = int(ring.get("n_other_cards_with_fraud_cc", 0) or 0) >= 2
    ring_t3   = ring_deg_ok and ring_hits and (
        int(ring.get("n_other_new_or_proxied_in_window", 0) or 0) >= 2
        or ring_cc
    )
    if ring_t3 or "testing_sequence_fires" in tags:
        return "denied", "override:ring-T3+/testing_sequence"
    # Override 2: legit-archetype → confirmed.
    if tags & LEGIT_TAGS:
        return "confirmed", f"override:legit-archetype {sorted(tags & LEGIT_TAGS)}"
    # Threshold rule on p_initial.
    p_init = state.get("p_initial", state.get("fraud_probability", 0.5))
    if p_init >= tau:
        return "denied", f"τ-rule: p_initial={p_init:.3f} ≥ τ={tau:.3f}"
    return "confirmed", f"τ-rule: p_initial={p_init:.3f} < τ={tau:.3f}"


def _run_one_dry(cc: dict) -> dict:
    """Run the agent WITHOUT injecting a customer response, so we can measure
    p_initial and, in simulated mode, apply the simulator rule externally.
    """
    txn_id = _first_txn(cc)
    if not txn_id:
        return {"case_id": cc["case_id"], "skipped": "no_txn"}
    state: dict[str, Any] = {
        "case_id":      cc["case_id"],
        "txn_id":       txn_id,
        "card_id":      cc["card_id"] or "",
        "customer_id":  cc["customer_id"],
        "opened_at":    cc["opened_at"],
        "trigger_type": "risk_score",     # C6v2 protocol
        "trigger_text": f"Historical replay of {cc['case_id']} @ {cc['opened_at']}.",
        "prior_log_odds": 0.0,
        "telemetry": Telemetry(),
    }
    t0 = time.time()
    try:
        state = run_plain(state)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent failed on %s: %s", cc["case_id"], e)
        return {"case_id": cc["case_id"], "error": str(e)}
    state["telemetry"].latency_seconds = time.time() - t0
    return {"state": state, "cc": cc}


def _rerun_with_response(cc: dict, response: str) -> dict:
    """Re-run the agent with a pre-set customer_response to force the R2/R3 branch."""
    txn_id = _first_txn(cc)
    state: dict[str, Any] = {
        "case_id":       cc["case_id"],
        "txn_id":        txn_id,
        "card_id":       cc["card_id"] or "",
        "customer_id":   cc["customer_id"],
        "opened_at":     cc["opened_at"],
        "trigger_type":  "risk_score",
        "trigger_text":  f"Historical replay of {cc['case_id']} @ {cc['opened_at']}.",
        "prior_log_odds": 0.0,
        "customer_response": response,
        "telemetry": Telemetry(),
    }
    t0 = time.time()
    state = run_plain(state)
    state["telemetry"].latency_seconds = time.time() - t0
    return state


def _finalise(state: dict, cc: dict, mode: str, response: str,
              sim_rule: str = "") -> dict:
    """Build the answer JSON + per-case backtest record."""
    answer = build_answer_file(state)
    out_dir = REPO_ROOT / "backtest" / "cases"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{cc['case_id']}.json").write_text(json.dumps(answer, indent=2))
    ep_before = list(state.get("episode_before_signature") or [])
    ep_after  = list(state.get("episode_after_signature") or [])
    return {
        "case_id":             cc["case_id"],
        "opened_at":           cc["opened_at"],
        "txn_id":              _first_txn(cc),
        "mode":                mode,
        "assumed_response":    response,
        "simulator_rule":      sim_rule,
        "gt_outcome":          cc["outcome"],
        "gt_pattern":          cc["pattern"],
        "gt_txn_ids":          (cc.get("txn_ids") or "").split("|"),
        "gt_exposure":         float(cc.get("exposure_usd") or 0.0),
        "gt_report":           (cc.get("report_filed") or "").strip().lower() in ("yes","true"),
        "gt_actions":          (cc.get("actions_taken") or "").split("|"),
        "gt_cleared_archetype": _cleared_archetype(cc) if cc["outcome"] == "cleared" else "",
        "p_initial":           state.get("p_initial"),
        "predicted_verdict":   answer["case"]["verdict"],
        "predicted_p":         answer["case"]["fraud_probability"],
        "predicted_pattern":   answer["case"]["pattern"],
        "predicted_txn_ids":   answer["case"]["affected_txn_ids"],
        "predicted_exposure":  answer["case"]["exposure_usd"],
        "predicted_sar":       answer["sar"]["file"],
        "episode_before_signature": ep_before,
        "episode_after_signature":  ep_after,
        "tool_calls":          answer["tool_calls"],
        "latency_s":           answer["latency_s"],
    }


# ---- driver ---------------------------------------------------------------


def run_backtest(n: int, stratify: str, seed: int, out_dir: str,
                 mode: str = "simulated", tau: float | None = None,
                 n_fraud: int | None = None, n_cleared: int | None = None,
                 tune_frac: float = 0.5, oot: bool = False) -> int:
    """C6v2 driver.

    ``mode`` = ``oracle`` or ``simulated``. If simulated and ``tau`` is None,
    we tune τ on the p_initial curve to maximise response accuracy against
    the oracle.

    Sample: default 75 fraud + 75 cleared (150 total). Override via
    ``n_fraud`` / ``n_cleared`` (or the ``--n`` split.)

    ``tune_frac`` — fraction of the sample used to tune τ; the rest is the
    held-out eval set. Default 0.5 → 75 tune + 75 eval. Metrics report both
    halves separately; τ is only tuned on the tune half so eval numbers are
    honest.

    ``oot`` — out-of-time evaluation. Tune set is drawn from opened_at ∈
    Jul–Sep 2016; eval set from Oct 2016 onwards. Overrides random sampling.
    Reports the eval-half AUC as the OOT AUC.
    """
    os.environ.setdefault("SENTINEL_LLM_DISABLED", "1")
    os.environ.setdefault("SENTINEL_WRITE_MEMORY_DISABLED", "1")
    # Backtests force the REST fast-path (MCP is default in the library but
    # would serialise the 150+ cases through a single stdio subprocess).
    os.environ["SENTINEL_GRAPH_VIA_MCP"] = "0"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if n_fraud is None: n_fraud = n // 2
    if n_cleared is None: n_cleared = n - n_fraud

    rows = _load_closed_cases()
    if oot:
        tune_rows = [r for r in rows if r.get("opened_at", "")[:7] in
                     ("2016-07", "2016-08", "2016-09")]
        eval_rows = [r for r in rows if r.get("opened_at", "")[:7] in
                     ("2016-10", "2016-11")]
        n_tune = max(2, int(round(n * tune_frac)))
        n_eval = max(2, n - n_tune)
        sample_tune = _sample_split(tune_rows, n_fraud=n_tune // 2,
                                    n_cleared=n_tune - n_tune // 2, seed=seed)
        sample_eval = _sample_split(eval_rows, n_fraud=n_eval // 2,
                                    n_cleared=n_eval - n_eval // 2, seed=seed + 1)
        sample = sample_tune + sample_eval
        tune_idx = set(range(len(sample_tune)))
        logger.info("OOT: tune=%d (Jul–Sep) + eval=%d (Oct+) = %d total",
                    len(sample_tune), len(sample_eval), len(sample))
    else:
        sample = _sample_split(rows, n_fraud=n_fraud, n_cleared=n_cleared, seed=seed)
        cut = max(1, int(round(len(sample) * tune_frac)))
        tune_idx = set(range(cut))
        logger.info("sampled %d cases (%d fraud + %d cleared) from %d closed cases; "
                    "tune=%d, eval=%d",
                    len(sample), n_fraud, n_cleared, len(rows),
                    len(tune_idx), len(sample) - len(tune_idx))

    out_root = Path(out_dir)
    if not out_root.is_absolute():
        out_root = REPO_ROOT / out_dir
    out_root.mkdir(parents=True, exist_ok=True)
    predictions_path = out_root / f"predictions_{mode}_n{len(sample)}.jsonl"

    # -- dry run to obtain p_initial for every case --
    dries: list[dict] = []
    with open(predictions_path.with_suffix(".dry.jsonl"), "w") as f:
        for i, cc in enumerate(sample, 1):
            dr = _run_one_dry(cc)
            dries.append({"cc": cc, "state": dr.get("state") or {}, "err": dr.get("error")})
            f.write(json.dumps({
                "case_id": cc["case_id"],
                "gt_outcome": cc["outcome"],
                "p_initial": (dr.get("state") or {}).get("p_initial"),
                "error": dr.get("error"),
            }) + "\n")
            f.flush()
            p = (dr.get("state") or {}).get("p_initial")
            print(f"[dry {i}/{len(sample)}] {cc['case_id']}  "
                  f"gt={cc['outcome']}  p_initial={p if p is None else f'{p:.3f}'}",
                  flush=True)

    # -- τ tuning in simulated mode: tune on the tune subset only --
    if mode == "simulated" and tau is None:
        tune_dries = [d for i, d in enumerate(dries) if i in tune_idx]
        tau_curve = _tau_curve(tune_dries)
        tau = _pick_best_tau(tau_curve)
        (out_root / "tau_curve.json").write_text(json.dumps(tau_curve, indent=2))
        logger.info("τ tuned to %.3f on %d tune-set dries (curve saved to tau_curve.json)",
                    tau, len(tune_dries))
    if mode == "simulated":
        print(f"\nUsing τ = {tau:.3f} (tuned on {len(tune_idx)} tune cases)\n")

    # -- final pass with customer_response set --
    with open(predictions_path, "w") as f:
        for i, entry in enumerate(dries, 1):
            cc = entry["cc"]; state_dry = entry["state"]
            split = "tune" if (i - 1) in tune_idx else "eval"
            if entry.get("err") or not state_dry:
                f.write(json.dumps({"case_id": cc["case_id"], "split": split,
                                     "error": entry.get("err")}) + "\n")
                continue
            if mode == "oracle":
                response = _oracle_response(cc)
                sim_rule = f"oracle: actions_taken → {response}"
            else:
                if state_dry.get("errors"):
                    response = "no_reply"
                    sim_rule = "graph unreachable → no_reply"
                else:
                    response, sim_rule = _sim_response(state_dry, tau)
            state = _rerun_with_response(cc, response)
            rec = _finalise(state, cc, mode=mode, response=response, sim_rule=sim_rule)
            rec["tau"] = tau if mode == "simulated" else None
            rec["split"] = split
            f.write(json.dumps(rec) + "\n"); f.flush()
            print(f"[{mode} {i}/{len(dries)} {split}] {cc['case_id']}  "
                  f"gt={cc['outcome']}/{cc['pattern']}  "
                  f"pred={rec['predicted_verdict']}/{rec['predicted_pattern']}  "
                  f"resp={response}", flush=True)

    print(f"\nWrote {predictions_path}")
    from sentinel.backtest.metrics import compute_and_report_metrics_v2
    compute_and_report_metrics_v2(predictions_path, out_dir=out_root, mode=mode,
                                   tau=tau, dries=dries, oot=oot)
    return 0


# ---- τ tuning helpers ------------------------------------------------------


def _tau_curve(dries: list[dict]) -> list[dict]:
    """For each τ, compute simulator response accuracy vs oracle.

    Response accuracy is measured over the intersection of {oracle response
    is denied/confirmed} — no_reply oracles are excluded.
    """
    pairs: list[tuple[float, str]] = []
    for d in dries:
        cc = d["cc"]
        state = d.get("state") or {}
        if not state:
            continue
        p_init = state.get("p_initial")
        if p_init is None:
            continue
        oracle = _oracle_response(cc)
        if oracle == "no_reply":
            continue
        pairs.append((float(p_init), oracle))

    if not pairs:
        return []
    curve = []
    for tau in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
                0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]:
        correct = 0
        for p, oracle in pairs:
            pred = "denied" if p >= tau else "confirmed"
            if pred == oracle:
                correct += 1
        curve.append({
            "tau":       round(tau, 3),
            "correct":   correct,
            "total":     len(pairs),
            "accuracy":  round(correct / len(pairs), 4),
        })
    return curve


def _pick_best_tau(curve: list[dict]) -> float:
    if not curve:
        return 0.5
    return max(curve, key=lambda r: r["accuracy"])["tau"]
