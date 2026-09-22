"""Backtest metrics + reliability plot.

Ingest ``predictions_n<N>.jsonl`` and compute:

  * verdict accuracy (mapping cleared→legitimate, confirmed_fraud→fraud)
  * pattern confusion matrix
  * SAR agreement with ``report_filed``
  * exposure MAE
  * affected-txn Jaccard
  * Brier score against ground-truth fraud indicator
  * reliability curve (saved as PNG)
  * mean tool_calls / latency

Writes a markdown report to ``backtest/BACKTEST_REPORT.md``.
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


VERDICT_TO_GT = {
    "confirmed_fraud": "fraud",
    "cleared": "legitimate",
}


def _load(preds_path: Path) -> list[dict]:
    with open(preds_path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _verdict_accuracy(rows: list[dict]) -> dict:
    """Predicted vs ground-truth. Treat 'uncertain' as neither."""
    n_total = 0; n_correct = 0; n_uncertain = 0
    for r in rows:
        gt = VERDICT_TO_GT.get(r.get("gt_outcome", ""))
        pv = r.get("predicted_verdict")
        if not gt or pv is None:
            continue
        n_total += 1
        if pv == "uncertain":
            n_uncertain += 1
            continue
        if pv == gt:
            n_correct += 1
    return {
        "n": n_total, "correct": n_correct, "uncertain": n_uncertain,
        "acc_excl_uncertain": (n_correct / (n_total - n_uncertain)
                                if n_total > n_uncertain else float("nan")),
        "acc_all": n_correct / n_total if n_total else float("nan"),
    }


def _pattern_confusion(rows: list[dict]) -> dict:
    """Cross-tab of predicted_pattern vs gt_pattern."""
    labels = ["card_testing", "card_not_present_fraud",
              "card_not_present_new_device", "out_of_region_use",
              "account_takeover", "undocumented", "none"]
    matrix = {gt: Counter() for gt in labels}
    for r in rows:
        gt = r.get("gt_pattern", "none")
        pv = r.get("predicted_pattern", "none")
        if gt not in matrix:
            matrix[gt] = Counter()
        matrix[gt][pv] += 1
    return {"labels": labels, "matrix": {k: dict(v) for k, v in matrix.items()}}


def _sar_agreement(rows: list[dict]) -> dict:
    """Agreement between predicted_sar and ground-truth report_filed."""
    tp = fp = fn = tn = 0
    for r in rows:
        gt = bool(r.get("gt_report"))
        pv = bool(r.get("predicted_sar"))
        if pv and gt: tp += 1
        elif pv and not gt: fp += 1
        elif not pv and gt: fn += 1
        else: tn += 1
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec  = tp / (tp + fn) if tp + fn else float("nan")
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": prec, "recall": rec}


def _exposure_mae(rows: list[dict]) -> dict:
    diffs: list[float] = []
    for r in rows:
        gt = float(r.get("gt_exposure", 0) or 0)
        pv = float(r.get("predicted_exposure", 0) or 0)
        diffs.append(abs(gt - pv))
    return {"n": len(diffs), "mae": (sum(diffs) / len(diffs)) if diffs else float("nan"),
            "max_abs_err": max(diffs) if diffs else 0.0}


def _txn_jaccard(rows: list[dict]) -> dict:
    j: list[float] = []
    for r in rows:
        gt = {str(x) for x in (r.get("gt_txn_ids") or []) if x}
        pv = {str(x) for x in (r.get("predicted_txn_ids") or []) if x}
        if not gt and not pv:
            j.append(1.0)
            continue
        u = gt | pv
        if not u:
            continue
        j.append(len(gt & pv) / len(u))
    return {"n": len(j), "mean": (sum(j) / len(j)) if j else float("nan")}


def _brier(rows: list[dict]) -> float:
    """Brier over ground-truth binary fraud."""
    accum = 0.0; n = 0
    for r in rows:
        gt = VERDICT_TO_GT.get(r.get("gt_outcome"))
        p  = float(r.get("predicted_p", 0.5) or 0.5)
        if gt is None:
            continue
        y = 1.0 if gt == "fraud" else 0.0
        accum += (p - y) ** 2
        n += 1
    return accum / n if n else float("nan")


def _reliability(rows: list[dict], n_bins: int = 10) -> list[tuple]:
    """Return [(bin_center, avg_p, avg_y, count)] bins."""
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for r in rows:
        gt = VERDICT_TO_GT.get(r.get("gt_outcome"))
        if gt is None:
            continue
        p = float(r.get("predicted_p", 0.5))
        y = 1.0 if gt == "fraud" else 0.0
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append((p, y))
    out: list[tuple] = []
    for i, b in enumerate(bins):
        if not b:
            continue
        avg_p = sum(x for x, _ in b) / len(b)
        avg_y = sum(y for _, y in b) / len(b)
        out.append(((i + 0.5) / n_bins, avg_p, avg_y, len(b)))
    return out


def _mean_tool_calls_and_latency(rows: list[dict]) -> dict:
    tc = [r.get("tool_calls", 0) or 0 for r in rows]
    lt = [r.get("latency_s", 0) or 0 for r in rows]
    return {
        "mean_tool_calls": (sum(tc) / len(tc)) if tc else float("nan"),
        "mean_latency_s":  (sum(lt) / len(lt)) if lt else float("nan"),
    }


def _plot_reliability(reliability: list[tuple], out_path: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.info("matplotlib not available — skipping reliability plot")
        return
    if not reliability:
        return
    xs = [avg_p for _, avg_p, _, _ in reliability]
    ys = [avg_y for _, _, avg_y, _ in reliability]
    sz = [max(4, min(200, cnt * 2)) for _, _, _, cnt in reliability]
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "k--", alpha=0.4, label="perfect")
    ax.scatter(xs, ys, s=sz, alpha=0.7, label="reliability")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.set_xlabel("Mean predicted probability (bin)")
    ax.set_ylabel("Empirical fraud rate")
    ax.set_title("Sentinel reliability curve")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def compute_and_report_metrics(preds_path: Path, out_dir: Path) -> None:
    """Read predictions, compute metrics, write a markdown report + reliability png."""
    rows = _load(preds_path)
    rows = [r for r in rows if "predicted_verdict" in r]     # drop skipped/errored
    logger.info("evaluating %d valid predictions", len(rows))

    ver = _verdict_accuracy(rows)
    pat = _pattern_confusion(rows)
    sar = _sar_agreement(rows)
    exp = _exposure_mae(rows)
    jac = _txn_jaccard(rows)
    brier = _brier(rows)
    reliability = _reliability(rows, n_bins=10)
    tc = _mean_tool_calls_and_latency(rows)

    reliability_png = out_dir / "reliability.png"
    _plot_reliability(reliability, reliability_png)

    md = out_dir / "BACKTEST_REPORT.md"
    lines: list[str] = []
    lines.append(f"# Backtest report ({len(rows)} cases)\n")
    lines.append("## Verdict accuracy\n")
    lines.append(f"- n = {ver['n']}, correct = {ver['correct']}, uncertain = {ver['uncertain']}")
    lines.append(f"- Accuracy (all)       : **{ver['acc_all']:.3f}**")
    lines.append(f"- Accuracy (excl uncr.) : **{ver['acc_excl_uncertain']:.3f}**\n")

    lines.append("## Pattern confusion matrix\n")
    lines.append("Rows = ground truth pattern; columns = predicted pattern.\n")
    header = "| gt \\ pred | " + " | ".join(pat["labels"]) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(pat["labels"]) + 1))
    for gt in pat["labels"]:
        row = pat["matrix"].get(gt, {})
        cells = [str(row.get(pv, 0)) for pv in pat["labels"]]
        lines.append("| " + gt + " | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## SAR vs report_filed\n")
    lines.append(f"- TP={sar['tp']}, FP={sar['fp']}, FN={sar['fn']}, TN={sar['tn']}")
    lines.append(f"- Precision = {sar['precision']:.3f}, Recall = {sar['recall']:.3f}\n")

    lines.append("## Exposure MAE\n")
    lines.append(f"- MAE = ${exp['mae']:.2f}   max |err| = ${exp['max_abs_err']:.2f}\n")

    lines.append("## Affected-txn Jaccard\n")
    lines.append(f"- Mean Jaccard = {jac['mean']:.3f} (n={jac['n']})\n")

    lines.append("## Brier score\n")
    lines.append(f"- Brier = **{brier:.4f}** (0=perfect, 0.25=uninformative)\n")

    lines.append("## Reliability bins\n")
    lines.append("| bin_center | avg_p | avg_y (empirical fraud rate) | n |")
    lines.append("|---|---|---|---|")
    for center, ap, ay, cnt in reliability:
        lines.append(f"| {center:.2f} | {ap:.3f} | {ay:.3f} | {cnt} |")
    lines.append(f"\n![reliability]({reliability_png.name})\n")

    lines.append("## Runtime\n")
    lines.append(f"- Mean tool_calls per case = {tc['mean_tool_calls']:.2f}")
    lines.append(f"- Mean latency per case = {tc['mean_latency_s']:.2f}s\n")

    md.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s", md)
    print(f"\nWrote {md}")


# ---- C6v2 metrics ---------------------------------------------------------


def _auc(rows: list[dict]) -> float:
    """AUC of predicted_p vs binary ground-truth fraud."""
    pairs = []
    for r in rows:
        gt = VERDICT_TO_GT.get(r.get("gt_outcome", ""))
        p  = r.get("p_initial", r.get("predicted_p"))
        if gt is None or p is None:
            continue
        pairs.append((float(p), 1 if gt == "fraud" else 0))
    if not pairs:
        return float("nan")
    n_pos = sum(y for _, y in pairs)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    pairs.sort(key=lambda x: x[0])
    # Rank-based AUC (Mann-Whitney U).
    rank_sum = 0.0
    for i, (_, y) in enumerate(pairs, 1):
        if y == 1:
            rank_sum += i
    auc = (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return auc


def _jaccard_ep_before_after(rows: list[dict]) -> tuple[float, float]:
    """Compute Jaccard using episode_before_signature vs episode_after_signature
    against gt_txn_ids."""
    j_before = []; j_after = []
    for r in rows:
        gt = {str(x) for x in (r.get("gt_txn_ids") or []) if x}
        ebef = {str(x) for x in (r.get("episode_before_signature") or []) if x}
        eaft = {str(x) for x in (r.get("episode_after_signature") or r.get("predicted_txn_ids") or []) if x}
        if not gt and not ebef and not eaft:
            j_before.append(1.0); j_after.append(1.0)
            continue
        if gt or ebef:
            u = gt | ebef
            j_before.append(len(gt & ebef) / len(u) if u else 0.0)
        if gt or eaft:
            u = gt | eaft
            j_after.append(len(gt & eaft) / len(u) if u else 0.0)
    def _mean(xs): return sum(xs)/len(xs) if xs else float("nan")
    return _mean(j_before), _mean(j_after)


def _simulator_vs_oracle(rows: list[dict]) -> dict:
    """Compare simulator's assumed_response with the oracle-derived one."""
    from sentinel.backtest.run import _oracle_response
    n = correct = 0
    conf: dict[str, int] = {}
    for r in rows:
        # reconstruct a cc-like row for _oracle_response
        cc_like = {
            "actions_taken": "|".join(r.get("gt_actions") or []),
            "outcome": r.get("gt_outcome"),
        }
        oracle = _oracle_response(cc_like)
        sim = r.get("assumed_response") or "no_reply"
        if oracle in ("denied", "confirmed"):
            n += 1
            if sim == oracle:
                correct += 1
        conf[f"{oracle}|{sim}"] = conf.get(f"{oracle}|{sim}", 0) + 1
    return {"n": n, "correct": correct,
            "accuracy": correct/n if n else float("nan"),
            "confusion": conf}


def _override_frequency(rows: list[dict]) -> dict:
    """Count simulator override reasons from simulator_rule strings."""
    from collections import Counter
    c = Counter()
    for r in rows:
        rule = (r.get("simulator_rule") or "").lower()
        if "override:ring" in rule or "override:testing" in rule:
            c["ring-T3+/testing"] += 1
        elif "override:legit" in rule:
            c["legit-archetype"] += 1
        elif "τ-rule" in rule:
            c["τ-rule"] += 1
        elif "no_reply" in rule:
            c["no_reply"] += 1
        elif "oracle" in rule:
            c["oracle"] += 1
        else:
            c["other"] += 1
    return dict(c)


def compute_and_report_metrics_v2(
    preds_path: Path, out_dir: Path, mode: str, tau: float | None,
    dries: list[dict] | None = None, oot: bool = False,
) -> None:
    """C6v2 report: verdict acc, pattern confusion, SAR, exposure MAE, Jaccard
    before/after signature expansion, Brier/AUC/reliability of p_initial,
    simulator response accuracy vs oracle (when applicable). Also τ curve.

    Splits by ``split`` field (``tune`` vs ``eval``): metrics reported for
    tune, eval, and combined. When ``oot=True``, the eval half is drawn from
    a later time period than the tune half, so the eval AUC is an OOT AUC.
    """
    all_rows = _load(preds_path)
    all_rows = [r for r in all_rows if "predicted_verdict" in r]
    logger.info("evaluating %d valid predictions (mode=%s)", len(all_rows), mode)

    tune_rows = [r for r in all_rows if r.get("split") == "tune"]
    eval_rows = [r for r in all_rows if r.get("split") == "eval"]
    rows = all_rows  # combined pass writes the full report body

    ver = _verdict_accuracy(rows)
    pat = _pattern_confusion(rows)
    sar = _sar_agreement(rows)
    exp = _exposure_mae(rows)
    j_before, j_after = _jaccard_ep_before_after(rows)
    brier = _brier(rows)
    auc = _auc(rows)
    tc = _mean_tool_calls_and_latency(rows)
    reliability = _reliability(rows, n_bins=10)
    _plot_reliability(reliability, out_dir / f"reliability_{mode}.png")

    sim_vs_oracle = _simulator_vs_oracle(rows) if mode == "simulated" else None

    md_path = out_dir / f"BACKTEST_REPORT_{mode}.md"
    L: list[str] = []
    L.append(f"# Backtest report — {mode} mode, n={len(rows)}\n")
    # Alert-model CV summary (if trained).
    try:
        import json as _json
        model = _json.loads(
            (Path(__file__).resolve().parents[1] / "evidence" / "alert_model.json").read_text())
        L.append(f"**Alert model** (L2 logistic): 5-fold CV **AUC = {model.get('cv_auc_mean'):.4f} "
                 f"± {model.get('cv_auc_std'):.4f}**, **Brier = {model.get('cv_brier_mean'):.4f}**")
        L.append(f"- Trained on {model.get('n_train')} closed cases "
                 f"({model.get('n_pos')} confirmed_fraud + {model.get('n_neg')} cleared)\n")
    except Exception:  # noqa: BLE001
        pass
    if mode == "simulated" and tau is not None:
        L.append(f"τ = **{tau:.3f}**  (tuned on the tune-half p_initial curve; "
                 f"see `tau_curve.json`)\n")

    # -- honest tune/eval split summary --
    if tune_rows and eval_rows:
        L.append("## Tune vs eval split\n")
        L.append(f"- Tune: **n={len(tune_rows)}** — τ tuned here")
        L.append(f"- Eval: **n={len(eval_rows)}** — held out from τ tuning"
                 + (" — **out-of-time** (later opened_at window)" if oot else ""))
        L.append("")
        L.append("| split | n | verdict acc | AUC | Brier | SAR precision | SAR recall |")
        L.append("|---|---|---|---|---|---|---|")
        for label, subset in (("tune", tune_rows), ("eval", eval_rows)):
            v = _verdict_accuracy(subset); s = _sar_agreement(subset)
            L.append(f"| {label} | {len(subset)} | {v['acc_all']:.3f} | "
                     f"{_auc(subset):.4f} | {_brier(subset):.4f} | "
                     f"{s['precision']:.3f} | {s['recall']:.3f} |")
        L.append("")
        if mode == "oracle":
            L.append("> **Note.** In oracle mode, `customer_response` is derived from "
                     "the historical `actions_taken`, which then determines the verdict "
                     "via §6 (denied⇒fraud, confirmed⇒legitimate). So verdict accuracy in "
                     "oracle mode reflects the response-derivation path, not the model. "
                     "The honest oracle-mode metrics are pattern confusion, SAR agreement, "
                     "exposure MAE, and affected-txn Jaccard.\n")

    L.append("## Verdict accuracy\n")
    L.append(f"- correct = {ver['correct']}, uncertain = {ver['uncertain']}, "
             f"n = {ver['n']}")
    L.append(f"- Accuracy (all) = **{ver['acc_all']:.3f}**,   "
             f"excl. uncertain = **{ver['acc_excl_uncertain']:.3f}**\n")

    L.append("## Pattern confusion matrix\n")
    labels = pat["labels"]
    L.append("| gt \\ pred | " + " | ".join(labels) + " |")
    L.append("|" + "---|" * (len(labels) + 1))
    for gt in labels:
        row = pat["matrix"].get(gt, {})
        L.append("| " + gt + " | " + " | ".join(str(row.get(pv, 0)) for pv in labels) + " |")
    L.append("")

    L.append("## SAR vs report_filed\n")
    L.append(f"- TP={sar['tp']}, FP={sar['fp']}, FN={sar['fn']}, TN={sar['tn']}")
    L.append(f"- Precision={sar['precision']:.3f}, Recall={sar['recall']:.3f}\n")

    L.append("## Exposure MAE\n")
    L.append(f"- MAE=${exp['mae']:.2f}  max|err|=${exp['max_abs_err']:.2f}\n")

    L.append("## Affected-txn Jaccard\n")
    L.append(f"- Before signature expansion: **{j_before:.3f}**")
    L.append(f"- After  signature expansion: **{j_after:.3f}**  (Δ = {j_after - j_before:+.3f})\n")

    L.append("## p_initial calibration\n")
    L.append(f"- Brier = **{brier:.4f}** (0=perfect, 0.25=uninformative)")
    L.append(f"- AUC   = **{auc:.4f}**\n")
    L.append("### Reliability bins")
    L.append("| bin | avg_p | empirical fraud | n |")
    L.append("|---|---|---|---|")
    for c, ap, ay, cnt in reliability:
        L.append(f"| {c:.2f} | {ap:.3f} | {ay:.3f} | {cnt} |")
    L.append(f"\n![reliability](reliability_{mode}.png)\n")

    if mode == "simulated" and sim_vs_oracle:
        L.append("## Simulator response accuracy vs oracle\n")
        L.append(f"- n (excl. no_reply oracles) = {sim_vs_oracle['n']}")
        L.append(f"- correct = {sim_vs_oracle['correct']}")
        L.append(f"- **accuracy = {sim_vs_oracle['accuracy']:.3f}**")
        L.append(f"- confusion (oracle|sim): {sim_vs_oracle['confusion']}\n")
        overrides = _override_frequency(rows)
        L.append("### Simulator override frequency")
        for k, v in overrides.items():
            L.append(f"- `{k}`: {v}")
        L.append("")

    L.append("## Runtime\n")
    L.append(f"- Mean tool_calls / case = {tc['mean_tool_calls']:.2f}")
    L.append(f"- Mean latency   / case = {tc['mean_latency_s']:.2f}s\n")

    md_path.write_text("\n".join(L), encoding="utf-8")
    logger.info("wrote %s", md_path)
    print(f"\nWrote {md_path}")
