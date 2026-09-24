"""Honest out-of-time evaluation of the alert model.

Refits the L2 logistic on closed cases with ``closed_at < 2016-10-01`` only,
then scores every case opened on ``2016-10-01`` or later. Reports AUC,
Brier, sample sizes to backtest/oot/OOT_REPORT.md and dumps per-case
(case_id, label, p) to backtest/oot/oot_eval.json.

Same feature set + same hyperparameters as ``sentinel.evidence.alert_model
::fit_and_save``. Same DuckDB source. Only difference: the training pool is
strictly pre-October.
"""

from __future__ import annotations

import csv as _csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from sentinel.data.features import connect
from sentinel.evidence.alert_model import ALL_FEATURES, extract_features_at_txn


CUT = "2016-10-01"        # anything CLOSED before this is in-sample training
CSV = REPO / "data" / "raw" / "closed_cases_history.csv"
OUT_DIR = REPO / "backtest" / "oot"


def _load_cases():
    """(case_id, txn_id:int, outcome, opened_at, closed_at) per row."""
    with open(CSV) as f:
        for r in _csv.DictReader(f):
            first_txn = r.get("first_fraud_txn_id") or (r.get("txn_ids") or "").split("|")[0]
            first_txn = (first_txn or "").strip()
            if not first_txn:
                continue
            try:
                yield (r["case_id"], int(first_txn), r["outcome"],
                       r["opened_at"], r["closed_at"])
            except ValueError:
                continue


def _feature_row(con, txn_id: int) -> dict | None:
    row = con.execute("SELECT * FROM txn_features WHERE TransactionID = ?",
                      [txn_id]).fetchdf()
    if row.empty:
        return None
    return extract_features_at_txn(row.iloc[0].to_dict(), graph_signals=None, con=con)


def main() -> int:
    con = connect()
    train_X: list[list[float]] = []; train_y: list[int] = []
    test_X:  list[list[float]] = []; test_y:  list[int] = []
    test_meta: list[dict] = []
    skipped = 0
    for case_id, tid, outcome, opened_at, closed_at in _load_cases():
        feat = _feature_row(con, tid)
        if feat is None:
            skipped += 1; continue
        vec = [feat[k] for k in ALL_FEATURES]
        label = 1 if outcome == "confirmed_fraud" else 0
        if closed_at < CUT:
            train_X.append(vec); train_y.append(label)
        elif opened_at >= CUT:
            test_X.append(vec); test_y.append(label)
            test_meta.append({"case_id": case_id, "label": label,
                              "opened_at": opened_at})
        else:
            skipped += 1     # opened before Oct but closed after — ambiguous

    X_tr = np.asarray(train_X, dtype=np.float32); y_tr = np.asarray(train_y, dtype=np.int8)
    X_te = np.asarray(test_X,  dtype=np.float32); y_te = np.asarray(test_y,  dtype=np.int8)

    print(f"train: n={len(y_tr)}  fraud={int(y_tr.sum())}  cleared={int(len(y_tr)-y_tr.sum())}")
    print(f"eval : n={len(y_te)}  fraud={int(y_te.sum())}  cleared={int(len(y_te)-y_te.sum())}")
    print(f"skipped rows: {skipped}")

    model = LogisticRegression(penalty="l2", C=1.0, class_weight="balanced",
                                solver="liblinear", max_iter=2000)
    model.fit(X_tr, y_tr)
    p_te = model.predict_proba(X_te)[:, 1]

    auc = float(roc_auc_score(y_te, p_te))
    brier = float(np.mean((p_te - y_te) ** 2))
    print(f"\nOOT AUC   = {auc:.4f}")
    print(f"OOT Brier = {brier:.4f}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for m, p in zip(test_meta, p_te):
        rows.append({**m, "p": float(p)})
    (OUT_DIR / "oot_eval.json").write_text(json.dumps(rows, indent=2))

    n_tr_pos = int(y_tr.sum()); n_tr_neg = int(len(y_tr) - n_tr_pos)
    n_te_pos = int(y_te.sum()); n_te_neg = int(len(y_te) - n_te_pos)

    md = [
        "# Backtest report — out-of-time evaluation\n",
        "Alert model refit on closed cases with `closed_at < 2016-10-01`, "
        "scored on every case with `opened_at >= 2016-10-01`. Same features, "
        "same hyperparameters as the production 5-fold CV run — only the "
        "training pool changed.\n",
        "## Numbers\n",
        f"- Train pool (pre-Oct): **n = {len(y_tr):,}**  "
        f"(fraud = {n_tr_pos:,}, cleared = {n_tr_neg:,})",
        f"- Eval pool  (Oct+)    : **n = {len(y_te):,}**  "
        f"(fraud = {n_te_pos:,}, cleared = {n_te_neg:,})",
        "",
        f"- **AUC   = {auc:.4f}**",
        f"- **Brier = {brier:.4f}**",
        "",
        "## Reproducing",
        "```bash",
        "python scripts/oot_eval.py",
        "```",
        "Writes `backtest/oot/oot_eval.json` (per-case `case_id / label / p`) "
        "and this file.",
    ]
    (OUT_DIR / "OOT_REPORT.md").write_text("\n".join(md))
    print(f"\nWrote {OUT_DIR}/oot_eval.json + OOT_REPORT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
