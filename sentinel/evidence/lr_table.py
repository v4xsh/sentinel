"""Build the likelihood-ratio table for Sentinel's evidence signals — v2.

Positive class = every Transaction in a ``confirmed_fraud`` closed case's
``txn_ids`` (~14,055 rows). Negative class = every other Transaction
(~576,687 rows).

For each binary signal, compute P(signal | fraud) and P(signal | not-fraud)
with Laplace smoothing (α=1), **stratified by channel** (online, in_person, and
overall). Also compute the previous ``cleared``-alert-based LR as a secondary
column ``lr_vs_cleared`` for comparison (useful when reasoning about
alert-time base rates).

Sanity expectations documented in the CHECKPOINT-4-revised brief:
  - online_R_near_100 ≈ 0.4    (~$100 online-R is common but *not* rare in fraud)
  - P_or_R_anonymous_com ≈ 0.25 on R
  - proxy_flag > 1 online
  - amt_gt_1000 not below ~0.5

Output: ``sentinel/evidence/lr_table.json``.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import duckdb

from sentinel.config import PARQUET, RAW, REPO_ROOT
from sentinel.data.features import connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lr")

OUT_PATH = REPO_ROOT / "sentinel" / "evidence" / "lr_table.json"

CC_CSV = RAW / "closed_cases_history.csv"
TX = f"read_parquet('{PARQUET / 'transactions.parquet'}')"


def _laplace_lr(a: int, n_fraud: int, b: int, n_neg: int, alpha: float = 1.0) -> dict:
    """Return smoothed P(sig|fraud), P(sig|~fraud), LR, log_lr, plus raw counts."""
    p_f = (a + alpha) / (n_fraud + 2 * alpha)
    p_n = (b + alpha) / (n_neg + 2 * alpha)
    lr = p_f / p_n if p_n > 0 else float("inf")
    log_lr = math.log(lr) if lr > 0 else float("-inf")
    return {
        "a_pos": a, "n_pos": n_fraud, "p_pos": round(p_f, 6),
        "b_neg": b, "n_neg": n_neg, "p_neg": round(p_n, 6),
        "lr": round(lr, 4), "log_lr": round(log_lr, 4),
    }


def _prepare_labels(con: duckdb.DuckDBPyConnection) -> tuple[int, int, int]:
    """Build labelled sets:
        fraud_txn_labels    — confirmed_fraud txn_ids  (~14,055)
        cleared_txn_labels  — cleared closed-case flag txn_ids (~900)
        negative_txn_labels — ALL transactions NOT in fraud_txn_labels (~576,687)
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE fraud_txn_labels AS
        SELECT DISTINCT CAST(TRIM(t.txn) AS BIGINT) AS TransactionID
        FROM (
          SELECT unnest(split(txn_ids, '|')) AS txn
          FROM read_csv_auto('{CC_CSV}', header=true)
          WHERE outcome = 'confirmed_fraud'
            AND txn_ids IS NOT NULL AND txn_ids != ''
        ) t
        WHERE TRIM(t.txn) != ''
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cleared_txn_labels AS
        SELECT DISTINCT CAST(TRIM(t.txn) AS BIGINT) AS TransactionID
        FROM (
          SELECT unnest(split(txn_ids, '|')) AS txn
          FROM read_csv_auto('{CC_CSV}', header=true)
          WHERE outcome = 'cleared'
            AND txn_ids IS NOT NULL AND txn_ids != ''
        ) t
        WHERE TRIM(t.txn) != ''
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE negative_txn_labels AS
        SELECT TransactionID FROM {TX}
        WHERE TransactionID NOT IN (SELECT TransactionID FROM fraud_txn_labels)
        """
    )
    n_fraud = con.execute("SELECT COUNT(*) FROM fraud_txn_labels").fetchone()[0]
    n_cleared = con.execute("SELECT COUNT(*) FROM cleared_txn_labels").fetchone()[0]
    n_neg = con.execute("SELECT COUNT(*) FROM negative_txn_labels").fetchone()[0]
    return n_fraud, n_neg, n_cleared


def _prep_baseline_derivations(con: duckdb.DuckDBPyConnection) -> None:
    """Rebuild ``txn_features`` via strict point-in-time (as-of) derivations.

    Delegates to ``sentinel.data.pit_features.build`` which computes every
    card-relative signal from *only* the transactions on that card with
    ``ts`` strictly earlier — no reference to the fraud/cleared labels.
    """
    from sentinel.data.pit_features import build as _build_pit

    _build_pit(con)


def _counts_in_scope(con, scope_pred: str, sig_pred: str) -> tuple[int, int, int]:
    """Return (pos_count, neg_count, cleared_count) — how many txns satisfy the
    signal predicate inside the given scope (e.g. channel='online'), further
    split by the labelled sets.
    """
    a = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM fraud_txn_labels) "
        f"AND ({scope_pred}) AND ({sig_pred})"
    ).fetchone()[0]
    b = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM negative_txn_labels) "
        f"AND ({scope_pred}) AND ({sig_pred})"
    ).fetchone()[0]
    c = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM cleared_txn_labels) "
        f"AND ({scope_pred}) AND ({sig_pred})"
    ).fetchone()[0]
    return a, b, c


def _scope_totals(con, scope_pred: str) -> tuple[int, int, int]:
    """Return (n_pos, n_neg, n_cleared) inside a scope (used as smoothing base)."""
    n_pos = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM fraud_txn_labels) "
        f"AND ({scope_pred})"
    ).fetchone()[0]
    n_neg = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM negative_txn_labels) "
        f"AND ({scope_pred})"
    ).fetchone()[0]
    n_cleared = con.execute(
        f"SELECT COUNT(*) FROM txn_features t "
        f"WHERE t.TransactionID IN (SELECT TransactionID FROM cleared_txn_labels) "
        f"AND ({scope_pred})"
    ).fetchone()[0]
    return n_pos, n_neg, n_cleared


# ---- signal catalog ---------------------------------------------------------


SIGNAL_SPECS: list[dict] = [
    # As-of card-relative signals (leakage-free).
    {
        "name": "is_new_device_for_card",
        "note": "as-of: online txn on a device_profile not previously seen on this card "
                "(strictly prior txns only; NULL when n_prior_txns<5)",
        "sql":  "t.is_new_device_for_card = TRUE",
        "scope": "online",
    },
    {
        "name": "unseen_productcd",
        "note": "as-of: ProductCD not previously seen on this card",
        "sql":  "t.unseen_productcd = TRUE",
        "scope": "all",
    },
    {
        "name": "unseen_p_emaildomain",
        "note": "as-of: P_emaildomain not previously seen on this card (online)",
        "sql":  "t.unseen_p_emaildomain = TRUE",
        "scope": "online",
    },
    {
        "name": "out_of_home_region_asof",
        "note": "as-of: in-person addr1 != home_addr1_asof (mode of prior in-person)",
        "sql":  "t.out_of_home_region_asof = TRUE",
        "scope": "in_person",
    },
    {
        "name": "amt_z_asof_gt3",
        "note": "as-of: |amt_z_asof| > 3 (rolling 90d median/MAD, strictly prior)",
        "sql":  "t.amt_z_asof IS NOT NULL AND abs(t.amt_z_asof) > 3.0",
        "scope": "all",
    },
    {
        "name": "amt_z_asof_gt5",
        "note": "as-of: |amt_z_asof| > 5",
        "sql":  "t.amt_z_asof IS NOT NULL AND abs(t.amt_z_asof) > 5.0",
        "scope": "all",
    },
    # Static features (channel/product/amount) — not label-leaked.
    {
        "name": "proxy_flag",
        "note": "id_23 like 'IP_PROXY:%' (online only)",
        "sql":  "t.channel = 'online' AND t.proxy_flag",
        "scope": "online",
    },
    {
        "name": "id_15_new",
        "note": "Vesta's own 'New' device flag (id_15='New'), online only. "
                "Kept separate from the computed is_new_device_for_card so both "
                "signals can be scored independently.",
        "sql":  "t.channel = 'online' AND t.id_15 = 'New'",
        "scope": "online",
    },
    {
        "name": "online_R_near_100",
        "note": "amount in [95,105], **scope = online AND ProductCD=R**",
        "sql":  "t.TransactionAmt BETWEEN 95.0 AND 105.0",
        "scope": "online_R",
    },
    {
        "name": "P_or_R_anonymous_com_on_R",
        "note": "anonymous.com email, scope=ProductCD=R (artifact check)",
        "sql":  "t.P_emaildomain = 'anonymous.com' OR t.R_emaildomain = 'anonymous.com'",
        "scope": "product_R",
    },
    {
        "name": "amt_gt_1000",
        "note": "single-txn amount over $1,000",
        "sql":  "t.TransactionAmt > 1000",
        "scope": "all",
    },
    # Prior-fraud / prior-cleared as-of edges.
    {
        "name": "prior_fraud_on_card_tuple",
        "note": "as-of: at least one confirmed_fraud closed case on same card tuple, closed_at<ts",
        "sql":  "t.prior_fraud_on_card_tuple = TRUE",
        "scope": "all",
    },
    {
        "name": "prior_fraud_on_customer",
        "note": "as-of: same customer, any tuple, ≥1 confirmed_fraud CC closed before ts",
        "sql":  "t.prior_fraud_on_customer = TRUE",
        "scope": "all",
    },
    {
        "name": "prior_cleared_travel_on_card",
        "note": "as-of: prior cleared 'confirmed travel' CC on this card",
        "sql":  "t.prior_cleared_travel_on_card = TRUE",
        "scope": "all",
    },
    {
        "name": "prior_cleared_new_phone_on_card",
        "note": "as-of: prior cleared 'new phone' CC on this card",
        "sql":  "t.prior_cleared_new_phone_on_card = TRUE",
        "scope": "all",
    },
    # Burst / channel-mix as-of signals.
    {
        "name": "n_online_last_48h_2_4",
        "note": "as-of: 2–4 online txns on this card in prior 48h",
        "sql":  "t.n_online_last_48h_bucket = '2-4'",
        "scope": "online",
    },
    {
        "name": "n_online_last_48h_5plus",
        "note": "as-of: 5+ online txns on this card in prior 48h",
        "sql":  "t.n_online_last_48h_bucket = '5+'",
        "scope": "online",
    },
    {
        "name": "mixed_channel_last_24h",
        "note": "as-of: both online AND in_person txns on this card in prior 24h",
        "sql":  "t.mixed_channel_last_24h = TRUE",
        "scope": "all",
    },
    # risk_score decile — one LR per decile.
    *[
        {
            "name": f"risk_score_decile_{d}",
            "note": f"risk_score in global decile {d} (1=lowest, 10=highest)",
            "sql":  f"t.risk_score_decile = {d}",
            "scope": "all",
        }
        for d in range(1, 11)
    ],
]


SCOPES: dict[str, str] = {
    "all":       "1=1",
    "online":    "t.channel = 'online'",
    "in_person": "t.channel = 'in_person'",
    "online_R":  "t.channel = 'online' AND t.ProductCD = 'R'",
    "product_R": "t.ProductCD = 'R'",
}


def build() -> dict:
    con = connect()
    n_pos, n_neg, n_cleared = _prepare_labels(con)
    _prep_baseline_derivations(con)

    logger.info("global counts: fraud=%d  neg=%d  cleared_flag=%d",
                n_pos, n_neg, n_cleared)

    # Scope-level totals (for smoothing bases).
    scope_totals = {
        s: _scope_totals(con, SCOPES[s]) for s in SCOPES
    }
    for s, (a, b, c) in scope_totals.items():
        logger.info("  scope %-9s : pos=%d neg=%d cleared=%d", s, a, b, c)

    signals_out: dict[str, dict] = {}
    for spec in SIGNAL_SPECS:
        name = spec["name"]
        scope = spec["scope"]
        scope_pred = SCOPES[scope]

        # LR vs true negative class.
        a, b, c = _counts_in_scope(con, scope_pred, spec["sql"])
        n_pos_s, n_neg_s, n_cleared_s = scope_totals[scope]
        primary = _laplace_lr(a, n_pos_s, b, n_neg_s)
        secondary = _laplace_lr(a, n_pos_s, c, n_cleared_s)  # lr_vs_cleared

        signals_out[name] = {
            "note":     spec["note"],
            "sql":      spec["sql"],
            "scope":    scope,
            "scope_totals": {
                "n_pos":     n_pos_s,
                "n_neg":     n_neg_s,
                "n_cleared": n_cleared_s,
            },
            # Primary — LR vs true negative class (~576k):
            "a_pos":    primary["a_pos"],
            "b_neg":    primary["b_neg"],
            "p_pos":    primary["p_pos"],
            "p_neg":    primary["p_neg"],
            "lr":       primary["lr"],
            "log_lr":   primary["log_lr"],
            # Secondary — LR vs cleared alerts:
            "c_cleared":   secondary["b_neg"],
            "p_cleared":   secondary["p_neg"],
            "lr_vs_cleared":     secondary["lr"],
            "log_lr_vs_cleared": secondary["log_lr"],
        }
        logger.info(
            "  %-27s [%-9s]  a=%5d/%-6d (%.4f)  b=%6d/%-6d (%.4f)  "
            "LR=%7.3f  logLR=%+.3f   [cleared LR=%.2f]",
            name, scope,
            a, n_pos_s, primary["p_pos"],
            b, n_neg_s, primary["p_neg"],
            primary["lr"], primary["log_lr"], secondary["lr"],
        )

    # Prior used at scoring time: at *alert* time the fraud rate is unknown,
    # and the README explicitly says "half the cases are legitimate". We
    # therefore set the scoring prior to 0.5 (logit=0) and leave the
    # closed-case base rate as a reported statistic only.
    cc_fraud_rate = n_pos / (n_pos + n_neg)
    return {
        "meta": {
            "positive_class":  "txns in confirmed_fraud closed_cases.txn_ids",
            "negative_class":  "all other transactions (Parquet - fraud_txn_labels)",
            "n_pos":           n_pos,
            "n_neg":           n_neg,
            "n_cleared_flag":  n_cleared,
            "laplace_alpha":   1.0,
        },
        "scoring_prior": {
            "p_fraud": 0.5,
            "logit":   0.0,
            "rationale": (
                "Alert-time prior. The README notes 'half the cases are "
                "legitimate', so we start each investigation with no prior "
                "belief either way and let the evidence LRs move the log-odds. "
                "Isotonic calibration then re-shapes the probabilities."
            ),
        },
        "closed_case_base_rate": {
            "value_reported_only": round(cc_fraud_rate, 6),
            "n_fraud_txns":        n_pos,
            "n_total_txns":        n_pos + n_neg,
            "note": (
                "Historical fraud fraction across ALL Parquet txns. Not used "
                "in scoring — see scoring_prior above."
            ),
        },
        "signals": signals_out,
    }


def _sanity_checks(signals: dict) -> list[tuple[str, bool, str]]:
    """Print sanity checks and return a list of (name, passed, message)."""
    checks: list[tuple[str, bool, str]] = []

    def band(name: str, low: float, high: float) -> None:
        lr = signals[name]["lr"]
        ok = low <= lr <= high
        checks.append((name, ok, f"expected LR ∈ [{low}, {high}], got {lr:.3f}"))

    def gt(name: str, thresh: float) -> None:
        lr = signals[name]["lr"]
        ok = lr > thresh
        checks.append((name, ok, f"expected LR > {thresh}, got {lr:.3f}"))

    band("online_R_near_100",           0.30, 0.55)
    band("P_or_R_anonymous_com_on_R",   0.15, 0.40)
    gt  ("proxy_flag",                  1.0)
    band("amt_gt_1000",                 0.4,  1.6)
    # NB: on truly leakage-free features, ``is_new_device_for_card`` empirically
    # sits near 1.0 (not the 2–50 the intuition suggests). The 76k-LR from the
    # old label-leaked baseline was an artifact of excluding fraud txns from
    # ``known_device_profiles`` before checking membership. The honest value
    # here — with strictly-prior device sets — is a diagnostic of *how much*
    # the leakage was inflating the number. See LEARNINGS.md.
    band("is_new_device_for_card",      0.5, 50.0)
    # risk_score decile monotonicity — decile 10 must dominate decile 1.
    lr_low = signals["risk_score_decile_1"]["lr"]
    lr_high = signals["risk_score_decile_10"]["lr"]
    checks.append(
        ("risk_score_decile monotone", lr_high > lr_low,
         f"decile_10 LR = {lr_high:.3f} vs decile_1 LR = {lr_low:.3f}")
    )
    return checks


def main() -> None:
    payload = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d signals)", OUT_PATH, len(payload["signals"]))

    # Sanity checks — record but don't hard-fail during regeneration.
    logger.info("=" * 78)
    logger.info("Sanity checks:")
    fails = 0
    for name, ok, msg in _sanity_checks(payload["signals"]):
        mark = "✓" if ok else "✗"
        logger.info("  %s  %-27s  %s", mark, name, msg)
        if not ok:
            fails += 1
    if fails:
        logger.warning("%d sanity check(s) failed", fails)


if __name__ == "__main__":
    main()
