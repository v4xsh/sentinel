"""Score every labelled txn with the LR table, then isotonically calibrate.

Uses the true-negative-class labels (fraud vs everything else) and a fixed
alert-time prior of ``p=0.5`` (logit 0). Isotonic regression is fit with
**class-balanced sample weights** so the (rare) fraud class is not swamped by
the (dominant) negative class.

Outputs:
  sentinel/evidence/calibrator.json     — the fitted isotonic mapping.
  backtest/calibration.png              — the reliability curve (weighted).
  backtest/calibration_raw.png          — reliability of the LR-sigmoid raw.
  backtest/calibration_report.md        — Brier (weighted + unweighted), AUC,
                                          per-decile counts.
"""

from __future__ import annotations

import json
import logging
import math
import random
from pathlib import Path

import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from sklearn.isotonic import IsotonicRegression

from sentinel.config import PARQUET, RAW, REPO_ROOT
from sentinel.data.features import connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("calibrate")

LR_PATH   = REPO_ROOT / "sentinel" / "evidence" / "lr_table.json"
OUT_JSON  = REPO_ROOT / "sentinel" / "evidence" / "calibrator.json"
OUT_PLOT  = REPO_ROOT / "backtest" / "calibration.png"
OUT_RAW   = REPO_ROOT / "backtest" / "calibration_raw.png"
OUT_REPORT = REPO_ROOT / "backtest" / "calibration_report.md"


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _brier(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> float:
    if w is None:
        return float(np.mean((y - p) ** 2))
    return float(np.sum(w * (y - p) ** 2) / np.sum(w))


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    """Mann-Whitney U approximation of AUC."""
    idx = np.argsort(p)
    y_sorted = y[idx]
    n_pos = float((y == 1).sum())
    n_neg = float((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = np.arange(1, len(y) + 1)[y_sorted == 1]
    return (ranks.sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def load_lr_table() -> dict:
    return json.loads(LR_PATH.read_text())


def score_txns(con: duckdb.DuckDBPyConnection, lr: dict) -> None:
    """Materialize ``txn_scored(TransactionID, logit)`` where logit sums the
    log_lr contribution of every signal that fires *within its scope*."""
    from sentinel.evidence.lr_table import SCOPES  # noqa: WPS433

    parts = []
    for sig_name, meta in lr["signals"].items():
        scope_pred = SCOPES.get(meta["scope"], "1=1")
        sig_pred = meta["sql"]
        log_lr = meta["log_lr"]
        parts.append(
            f"CASE WHEN ({scope_pred}) AND ({sig_pred}) THEN {log_lr} ELSE 0.0 END"
        )

    total_expr = " + ".join(parts) if parts else "0.0"
    prior_logit = lr["scoring_prior"]["logit"]

    logger.info("scoring txn_features (prior_logit=%.3f, %d signals) …",
                prior_logit, len(lr["signals"]))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE txn_scored AS
        SELECT
          t.TransactionID,
          {prior_logit} + ({total_expr}) AS logit
        FROM txn_features t
        """
    )


def build_labels_and_scores(con: duckdb.DuckDBPyConnection) -> tuple[np.ndarray, np.ndarray]:
    """Return (y, logit) for the FULL labelled set: y=1 iff txn is in a
    confirmed_fraud closed case; y=0 for every other Parquet txn."""
    r = con.execute(
        """
        SELECT
          CASE WHEN f.TransactionID IS NOT NULL THEN 1 ELSE 0 END AS y,
          s.logit AS logit
        FROM txn_scored s
        LEFT JOIN fraud_txn_labels f USING (TransactionID)
        """
    ).fetchnumpy()
    return r["y"].astype(np.int8), r["logit"].astype(np.float64)


def _class_balanced_weights(y: np.ndarray) -> np.ndarray:
    n_pos = float((y == 1).sum())
    n_neg = float((y == 0).sum())
    n = float(len(y))
    # sklearn "balanced" recipe: w_c = n / (n_classes * count_c)
    w_pos = n / (2.0 * n_pos) if n_pos > 0 else 0.0
    w_neg = n / (2.0 * n_neg) if n_neg > 0 else 0.0
    return np.where(y == 1, w_pos, w_neg)


def fit_isotonic(p_raw: np.ndarray, y: np.ndarray, w: np.ndarray) -> IsotonicRegression:
    ir = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    ir.fit(p_raw, y, sample_weight=w)
    return ir


def reliability_plot(
    p: np.ndarray, y: np.ndarray, w: np.ndarray, title: str, out_path: Path,
) -> dict:
    """Weighted reliability curve, decile bins."""
    n = len(p)
    order = np.argsort(p)
    p_s, y_s, w_s = p[order], y[order], w[order]
    n_bins = 10
    edges = np.quantile(p_s, np.linspace(0, 1, n_bins + 1))
    edges = np.unique(edges)
    if len(edges) < 3:
        edges = np.linspace(0, 1, n_bins + 1)
    bin_ids = np.digitize(p_s, edges[1:-1])
    bin_p, bin_y, bin_n = [], [], []
    for b in range(len(edges) - 1):
        mask = bin_ids == b
        if not mask.any():
            continue
        wmask = w_s[mask]
        bin_p.append(float(np.sum(wmask * p_s[mask]) / max(np.sum(wmask), 1e-9)))
        bin_y.append(float(np.sum(wmask * y_s[mask]) / max(np.sum(wmask), 1e-9)))
        bin_n.append(int(mask.sum()))
    plt.figure(figsize=(6, 5))
    plt.plot([0, 1], [0, 1], "k--", alpha=0.5, label="perfect calibration")
    plt.plot(bin_p, bin_y, "o-", label=title)
    plt.xlabel("predicted probability")
    plt.ylabel("observed (weighted) fraud rate")
    plt.title(f"Reliability — {title}  (n={n:,})")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=110)
    plt.close()
    return {"predicted": bin_p, "observed": bin_y, "n_per_bin": bin_n}


def main() -> None:
    con = connect(read_only=False)
    lr = load_lr_table()

    # Ensure txn_features + fraud_txn_labels + negative_txn_labels exist.
    from sentinel.evidence.lr_table import _prepare_labels, _prep_baseline_derivations
    _prepare_labels(con)
    _prep_baseline_derivations(con)

    score_txns(con, lr)
    y, logit = build_labels_and_scores(con)
    p_raw = _sigmoid(logit)
    logger.info("all txns: %d (fraud=%d, non-fraud=%d)",
                len(y), int((y == 1).sum()), int((y == 0).sum()))
    logger.info("raw p distribution: mean=%.3f p05=%.3f p95=%.3f",
                p_raw.mean(), np.quantile(p_raw, 0.05), np.quantile(p_raw, 0.95))

    # Train/holdout split — stratified by class.
    rng = random.Random(20260920)
    pos_idx = np.where(y == 1)[0].tolist()
    neg_idx = np.where(y == 0)[0].tolist()
    rng.shuffle(pos_idx)
    rng.shuffle(neg_idx)
    cut_p = int(0.8 * len(pos_idx))
    cut_n = int(0.8 * len(neg_idx))
    train_idx = np.array(pos_idx[:cut_p] + neg_idx[:cut_n])
    holdout_idx = np.array(pos_idx[cut_p:] + neg_idx[cut_n:])
    logger.info("train=%d holdout=%d", len(train_idx), len(holdout_idx))

    # Class-balanced weights (train + holdout share the same recipe).
    w_train = _class_balanced_weights(y[train_idx])
    w_holdout = _class_balanced_weights(y[holdout_idx])

    ir = fit_isotonic(p_raw[train_idx], y[train_idx], w_train)
    p_cal_holdout = ir.transform(p_raw[holdout_idx])

    # Metrics on holdout.
    brier_raw_bal = _brier(y[holdout_idx].astype(float), p_raw[holdout_idx], w_holdout)
    brier_cal_bal = _brier(y[holdout_idx].astype(float), p_cal_holdout, w_holdout)
    brier_raw_uw  = _brier(y[holdout_idx].astype(float), p_raw[holdout_idx])
    brier_cal_uw  = _brier(y[holdout_idx].astype(float), p_cal_holdout)
    auc_raw = _auc(y[holdout_idx], p_raw[holdout_idx])
    auc_cal = _auc(y[holdout_idx], p_cal_holdout)
    logger.info("holdout balanced Brier: raw=%.4f  cal=%.4f", brier_raw_bal, brier_cal_bal)
    logger.info("holdout unweighted Brier: raw=%.4f  cal=%.4f", brier_raw_uw, brier_cal_uw)
    logger.info("holdout AUC:  raw=%.4f  cal=%.4f", auc_raw, auc_cal)

    stats_raw = reliability_plot(
        p_raw[holdout_idx], y[holdout_idx], w_holdout,
        title="raw (LR sum through sigmoid)",
        out_path=OUT_RAW,
    )
    stats_cal = reliability_plot(
        p_cal_holdout, y[holdout_idx], w_holdout,
        title="isotonic-calibrated (class-balanced)",
        out_path=OUT_PLOT,
    )

    # Save calibrator + metadata.
    ir_json = {
        "x_thresholds": [float(x) for x in ir.X_thresholds_],
        "y_thresholds": [float(y) for y in ir.y_thresholds_],
        "y_min": 0.0,
        "y_max": 1.0,
        "trained_on": {
            "n_train": int(len(train_idx)),
            "n_train_pos": int((y[train_idx] == 1).sum()),
            "n_train_neg": int((y[train_idx] == 0).sum()),
            "seed": 20260920,
            "sample_weight_strategy": "balanced",
        },
        "holdout": {
            "n_holdout": int(len(holdout_idx)),
            "brier_raw_balanced":     round(brier_raw_bal, 4),
            "brier_cal_balanced":     round(brier_cal_bal, 4),
            "brier_raw_unweighted":   round(brier_raw_uw, 4),
            "brier_cal_unweighted":   round(brier_cal_uw, 4),
            "auc_raw": round(auc_raw, 4),
            "auc_cal": round(auc_cal, 4),
        },
    }
    OUT_JSON.write_text(json.dumps(ir_json, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d knots)", OUT_JSON, len(ir_json["x_thresholds"]))

    lines = [
        "# Calibration report (class-balanced)\n",
        f"- Labelled txns: {len(y):,}  (fraud={int((y==1).sum()):,}, "
        f"non-fraud={int((y==0).sum()):,})\n",
        f"- Scoring prior: p=0.5 (logit 0) — see DECISIONS.md.\n",
        f"- Train / holdout split: 80 / 20 stratified per class, seed=20260920.\n",
        "\n## Metrics on holdout\n\n",
        "| metric | raw | isotonic-cal |\n|---|---:|---:|\n",
        f"| balanced Brier    | {brier_raw_bal:.4f} | {brier_cal_bal:.4f} |\n",
        f"| unweighted Brier  | {brier_raw_uw:.4f} | {brier_cal_uw:.4f} |\n",
        f"| AUC               | {auc_raw:.4f} | {auc_cal:.4f} |\n",
        "\n## Reliability (isotonic, class-balanced weights)\n\n",
        "| decile | predicted | observed(weighted) | n |\n|---|---:|---:|---:|\n",
    ]
    for i, (p, o, n) in enumerate(zip(stats_cal["predicted"], stats_cal["observed"], stats_cal["n_per_bin"]), 1):
        lines.append(f"| {i} | {p:.3f} | {o:.3f} | {n:,} |\n")
    lines.append(
        "\nPlots: `backtest/calibration.png` (isotonic, class-balanced) and "
        "`backtest/calibration_raw.png` (LR-sigmoid raw).\n"
    )
    OUT_REPORT.write_text("".join(lines), encoding="utf-8")
    logger.info("wrote %s", OUT_REPORT)


if __name__ == "__main__":
    main()
