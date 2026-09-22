"""Fitted L2-regularized logistic model over closed cases.

Trains on all 5,565 ClosedCases (4,665 confirmed_fraud + 900 cleared),
using each case's flagged transaction and as-of features from
``txn_features``. Features are the same binary/one-hot signals surfaced
in the current LR table plus device-tier x degree bucket flags and
graph-derived cluster indicators.

At scoring time, each feature that fires becomes a ledger Evidence with
``log_lr = coefficient``.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np

from sentinel.config import REPO_ROOT

logger = logging.getLogger(__name__)

MODEL_PATH = REPO_ROOT / "sentinel" / "evidence" / "alert_model.json"


# ---- Feature schema ---------------------------------------------------------


BINARY_FEATURES = [
    # Vesta's own flags
    "id_15_new",                       # id_15 == 'New'
    "proxy_flag",                      # id_23 LIKE 'IP_PROXY:%'
    # Computed / as-of
    "is_new_device_for_card",
    "unseen_productcd",
    "unseen_p_emaildomain",
    "out_of_home_region_asof",
    "had_prior_txn_in_region",         # this address seen on this card before
    "prior_cleared_travel_on_card",
    "prior_cleared_new_phone_on_card",
    "prior_fraud_on_card_tuple",
    "prior_fraud_on_customer",
    "mixed_channel_last_24h",
    "recurring_ge3",                   # ≥3 prior at same (ProductCD, amount)
    # amt_z magnitude buckets
    "amt_z_gt_3",
    "amt_z_gt_2",
]
BUCKET_FEATURES = [
    "n_online_48h_2_4",
    "n_online_48h_ge_5",
]
DECILE_FEATURES = [f"risk_decile_{d}" for d in range(1, 11)]
CHANNEL_FEATURES = ["channel_online", "channel_in_person"]

# Device tier × degree bucket (12 columns)
TIER_BUCKETS = []
for tier in ("T1", "T2", "T3", "T4"):
    for bucket in ("le5", "6-20", "21-100"):
        TIER_BUCKETS.append(f"{tier}_{bucket}")

# Graph-derived
GRAPH_FEATURES = [
    "device_neighbors_ge2",
    "region_cluster_ge2",
    "recipient_cluster_ge2",
    "testing_sequence_fires",
    "near_threshold_burst",
]

ALL_FEATURES: list[str] = (
    BINARY_FEATURES + BUCKET_FEATURES + DECILE_FEATURES + CHANNEL_FEATURES
    + TIER_BUCKETS + GRAPH_FEATURES
)


# ---- Feature extraction (at scoring time) -----------------------------------


def extract_features_at_txn(
    txn_row: dict, graph_signals: dict | None, con: duckdb.DuckDBPyConnection,
) -> dict[str, int]:
    """Extract the feature vector for one flagged transaction, as-of ts."""
    r = txn_row
    graph_signals = graph_signals or {}
    feat: dict[str, int] = {name: 0 for name in ALL_FEATURES}

    # Binary flags from the txn row
    feat["id_15_new"]                   = int(r.get("id_15") == "New")
    feat["proxy_flag"]                  = int(bool(r.get("proxy_flag")))
    feat["is_new_device_for_card"]      = int(r.get("is_new_device_for_card") is True)
    feat["unseen_productcd"]            = int(r.get("unseen_productcd") is True)
    feat["unseen_p_emaildomain"]        = int(r.get("unseen_p_emaildomain") is True)
    feat["out_of_home_region_asof"]     = int(r.get("out_of_home_region_asof") is True)
    feat["prior_cleared_travel_on_card"]   = int(r.get("prior_cleared_travel_on_card") is True)
    feat["prior_cleared_new_phone_on_card"] = int(r.get("prior_cleared_new_phone_on_card") is True)
    feat["prior_fraud_on_card_tuple"]   = int(r.get("prior_fraud_on_card_tuple") is True)
    feat["prior_fraud_on_customer"]     = int(r.get("prior_fraud_on_customer") is True)
    feat["mixed_channel_last_24h"]      = int(r.get("mixed_channel_last_24h") is True)

    # had_prior_txn_in_region ← graph_signals.region_history
    rh = graph_signals.get("region_history") or {}
    feat["had_prior_txn_in_region"]     = int(bool(rh.get("had_prior_txn_in_region")))

    # amt_z buckets
    z = r.get("amt_z_asof")
    if z is not None:
        feat["amt_z_gt_2"] = int(abs(z) > 2.0)
        feat["amt_z_gt_3"] = int(abs(z) > 3.0)

    # n_online_48h buckets
    b = r.get("n_online_last_48h_bucket")
    feat["n_online_48h_2_4"]  = int(b == "2-4")
    feat["n_online_48h_ge_5"] = int(b == "5+")

    # recurring ≥3 hits — compute on the fly
    prod = r.get("ProductCD"); amt = r.get("TransactionAmt")
    if prod is not None and amt is not None:
        try:
            row = con.execute(
                """
                SELECT COUNT(*) FROM tx_enriched te
                WHERE te.customer_id = ?
                  AND te.ProductCD = ?
                  AND ABS(te.TransactionAmt - ?) < 0.02
                  AND te.ts < ?
                """,
                [r.get("customer_id"), prod, float(amt), r.get("ts")],
            ).fetchone()
            feat["recurring_ge3"] = int(int(row[0] or 0) >= 3)
        except Exception:
            pass

    # Risk-score decile one-hot
    d = r.get("risk_score_decile")
    if d is not None:
        try:
            feat[f"risk_decile_{int(d)}"] = 1
        except (ValueError, KeyError):
            pass

    # Channel one-hot
    ch = r.get("channel")
    if ch == "online":
        feat["channel_online"] = 1
    elif ch == "in_person":
        feat["channel_in_person"] = 1

    # Device tier × degree bucket (from ring_components signal)
    ring = graph_signals.get("ring_components") or {}
    deg = int(ring.get("device_degree", 0) or 0)
    bucket = "le5" if 0 < deg <= 5 else ("6-20" if 6 <= deg <= 20 else ("21-100" if 21 <= deg <= 100 else None))
    has_cc  = bool(ring.get("has_confirmed_fraud_cc"))
    proxied = bool(r.get("proxy_flag"))
    n_np    = int(ring.get("n_other_new_or_proxied_in_window", 0) or 0)
    n_cust  = int(ring.get("n_other_cards_with_fraud_cc", 0) or 0)
    if bucket:
        if has_cc:
            feat[f"T1_{bucket}"] = 1
            if proxied:
                feat[f"T2_{bucket}"] = 1
                if n_np >= 2:
                    feat[f"T3_{bucket}"] = 1
            if n_cust >= 2:
                feat[f"T4_{bucket}"] = 1

    # Graph clusters
    dn = graph_signals.get("device_neighbors") or {}
    feat["device_neighbors_ge2"] = int(int(dn.get("n_other_cards", 0) or 0) >= 2)
    rc = graph_signals.get("region_cluster") or {}
    feat["region_cluster_ge2"]   = int(int(rc.get("n_other_cards", 0) or 0) >= 2)
    rec = graph_signals.get("recipient_email_cluster") or {}
    feat["recipient_cluster_ge2"] = int(int(rec.get("n_other_cards", 0) or 0) >= 2)
    ts = graph_signals.get("testing_sequence") or {}
    feat["testing_sequence_fires"] = int(bool(ts.get("fires")))
    ntb = graph_signals.get("near_threshold_burst") or {}
    feat["near_threshold_burst"]  = int(bool(ntb.get("fires")))
    return feat


# ---- Training helpers -------------------------------------------------------


def _cc_flagged_txn_ids(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int, str]]:
    """Return (case_id, TransactionID, outcome) rows for each ClosedCase."""
    # For confirmed_fraud we have ftxn in cc_join. For cleared we take txn_ids[0]
    # from the CSV directly. Load the CSV so we can pick the split.
    csv_path = REPO_ROOT / "data" / "raw" / "closed_cases_history.csv"
    import csv as _csv
    out: list[tuple[str, int, str]] = []
    with open(csv_path) as f:
        for r in _csv.DictReader(f):
            first_txn = r.get("first_fraud_txn_id") or (r.get("txn_ids") or "").split("|")[0]
            first_txn = (first_txn or "").strip()
            if not first_txn:
                continue
            try:
                out.append((r["case_id"], int(first_txn), r["outcome"]))
            except ValueError:
                continue
    return out


def _build_feature_matrix(con: duckdb.DuckDBPyConnection) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Load features for every ClosedCase; return (X, y, case_ids)."""
    cases = _cc_flagged_txn_ids(con)
    logger.info("training feature matrix over %d closed cases", len(cases))
    Xs: list[dict[str, int]] = []
    ys: list[int] = []
    ids: list[str] = []
    for case_id, txn_id, outcome in cases:
        row = con.execute(
            "SELECT * FROM txn_features WHERE TransactionID = ?", [txn_id],
        ).fetchdf()
        if row.empty:
            continue
        r = row.iloc[0].to_dict()
        feat = extract_features_at_txn(r, graph_signals=None, con=con)
        Xs.append(feat)
        ys.append(1 if outcome == "confirmed_fraud" else 0)
        ids.append(case_id)
    X = np.array([[x[k] for k in ALL_FEATURES] for x in Xs], dtype=np.float32)
    y = np.array(ys, dtype=np.int8)
    return X, y, ids


# ---- Fit --------------------------------------------------------------------


def fit_and_save(seed: int = 42, C: float = 1.0) -> dict:
    """Fit class-balanced L2 logistic regression + 5-fold CV. Persist to disk."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold
    from sklearn.metrics import roc_auc_score

    from sentinel.data.features import connect
    con = connect()

    X, y, ids = _build_feature_matrix(con)
    logger.info("features: %d columns, %d rows (%d pos, %d neg)",
                X.shape[1], X.shape[0], int(y.sum()), int(len(y) - y.sum()))

    # 5-fold stratified CV
    aucs: list[float] = []
    briers: list[float] = []
    kf = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)
    for tr, te in kf.split(X, y):
        m = LogisticRegression(
            penalty="l2", C=C, class_weight="balanced",
            solver="liblinear", max_iter=2000,
        )
        m.fit(X[tr], y[tr])
        pv = m.predict_proba(X[te])[:, 1]
        aucs.append(roc_auc_score(y[te], pv))
        briers.append(float(np.mean((pv - y[te]) ** 2)))

    # Fit on all data
    final = LogisticRegression(
        penalty="l2", C=C, class_weight="balanced",
        solver="liblinear", max_iter=2000,
    )
    final.fit(X, y)
    coef = final.coef_[0].tolist()
    intercept = float(final.intercept_[0])
    # Adjust intercept so the model's prior corresponds to 0.5 (a well-calibrated
    # "no evidence" case). Trained intercept encodes the observed prevalence
    # (fraud/(fraud+cleared) = 4665/5565 = 0.838). Subtract logit(0.838).
    prior_logit = math.log(0.838 / (1 - 0.838))
    intercept_adj = intercept - prior_logit

    payload = {
        "features": ALL_FEATURES,
        "coef":     coef,
        "intercept": intercept,           # trained (for reference)
        "intercept_adjusted": intercept_adj,   # used at scoring time
        "prior_logit": prior_logit,
        "cv_auc_folds":   [round(a, 4) for a in aucs],
        "cv_auc_mean":    round(float(np.mean(aucs)), 4),
        "cv_auc_std":     round(float(np.std(aucs)), 4),
        "cv_brier_folds": [round(b, 4) for b in briers],
        "cv_brier_mean":  round(float(np.mean(briers)), 4),
        "n_train":        int(len(y)),
        "n_pos":          int(y.sum()),
        "n_neg":          int(len(y) - y.sum()),
        "C":              C,
    }
    MODEL_PATH.write_text(json.dumps(payload, indent=2))
    logger.info("saved %s (5-fold AUC=%.4f Brier=%.4f)",
                MODEL_PATH, payload["cv_auc_mean"], payload["cv_brier_mean"])
    return payload


# ---- Scoring ----------------------------------------------------------------


def _sigmoid(x: float) -> float:
    if x > 60:  return 1.0
    if x < -60: return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def load_model() -> dict:
    if not MODEL_PATH.exists():
        raise RuntimeError(f"alert_model not fit yet: {MODEL_PATH}. Run `python -m sentinel.evidence.alert_model`.")
    return json.loads(MODEL_PATH.read_text())


def score(features: dict[str, int], model: dict | None = None) -> tuple[float, list[tuple[str, float]]]:
    """Return (p_initial, [(feature, coef) for firing features]).

    Uses ``intercept_adjusted`` so the "no evidence" prior is 0.5.
    """
    m = model or load_model()
    coef_by_name = dict(zip(m["features"], m["coef"]))
    fired: list[tuple[str, float]] = []
    logit = float(m.get("intercept_adjusted", 0.0))
    for name, c in coef_by_name.items():
        v = int(features.get(name, 0) or 0)
        if v:
            fired.append((name, float(c)))
            logit += float(c)
    return _sigmoid(logit), fired


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    payload = fit_and_save()
    print(f"5-fold CV AUC: {payload['cv_auc_mean']:.4f} ± {payload['cv_auc_std']:.4f}")
    print(f"5-fold CV Brier: {payload['cv_brier_mean']:.4f}")
    coefs = sorted(zip(payload["features"], payload["coef"]),
                   key=lambda kv: abs(kv[1]), reverse=True)
    print(f"\nCoefficients (sorted by |coef|):")
    for name, c in coefs[:30]:
        print(f"  {name:<32s} {c:+.4f}")
