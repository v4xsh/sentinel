"""Point-in-time (as-of) feature engineering — hybrid DuckDB + Python.

Every card-relative signal is computed from *only* the transactions on that
card with ``ts`` strictly earlier than the current row. **No reference to the
fraud/cleared labels.** This eliminates the label leakage of the old
``card_baseline_clean``-driven features, which excluded confirmed-fraud txns
and therefore gave ``is_new_device_for_card`` an impossibly-high LR.

Why hybrid: ``LIST(DISTINCT x) OVER (ROWS UNBOUNDED PRECEDING AND 1 PRECEDING)``
is O(n²) in DuckDB (recomputes distinct per frame). For prior-set signals we
process each card incrementally in Python — O(n_txn) total. Cheap window
functions (COUNT, MEDIAN over RANGE, ntile) stay in DuckDB.
"""

from __future__ import annotations

import logging
from collections import Counter

import duckdb
import numpy as np

from sentinel.config import PARQUET, RAW

logger = logging.getLogger(__name__)


TX = f"read_parquet('{PARQUET / 'transactions.parquet'}')"
CC_CSV = RAW / "closed_cases_history.csv"


def _build_base(con: duckdb.DuckDBPyConnection) -> None:
    """Join txn + identity, derive tuple columns and device_profile."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE _tx_base AS
        SELECT
          t.TransactionID,
          t.ts,
          t.channel,
          t.ProductCD,
          t.addr1::VARCHAR AS addr1,
          t.P_emaildomain,
          t.R_emaildomain,
          t.TransactionAmt,
          t.risk_score,
          t.customer_id,
          COALESCE(t.card2::VARCHAR,'_') AS c2,
          COALESCE(t.card3::VARCHAR,'_') AS c3,
          COALESCE(t.card4::VARCHAR,'_') AS c4,
          COALESCE(t.card5::VARCHAR,'_') AS c5,
          COALESCE(t.card6::VARCHAR,'_') AS c6,
          COALESCE(t.M1::VARCHAR,'') AS M1, COALESCE(t.M2::VARCHAR,'') AS M2,
          COALESCE(t.M3::VARCHAR,'') AS M3, COALESCE(t.M4,'') AS M4,
          COALESCE(t.M5::VARCHAR,'') AS M5, COALESCE(t.M6::VARCHAR,'') AS M6,
          COALESCE(t.M7::VARCHAR,'') AS M7, COALESCE(t.M8::VARCHAR,'') AS M8,
          COALESCE(t.M9::VARCHAR,'') AS M9,
          i.id_15, i.id_23,
          CASE WHEN t.channel = 'online'
               THEN COALESCE(i.DeviceInfo,'-')||' | '||COALESCE(i.id_30,'-')||' | '||
                    COALESCE(i.id_31,'-')||' | '||COALESCE(i.id_33,'-')
               ELSE NULL END AS device_profile,
          CASE WHEN i.id_23 IS NULL THEN FALSE
               WHEN i.id_23 LIKE 'IP_PROXY:%' THEN TRUE
               ELSE FALSE END AS proxy_flag,
          EPOCH(t.ts) AS ts_epoch
        FROM {TX} t
        LEFT JOIN read_parquet('{PARQUET / "identity.parquet"}') i
          ON i.TransactionID = t.TransactionID
        """
    )


def _build_windows(con: duckdb.DuckDBPyConnection) -> None:
    """Cheap window aggregates (COUNT, MEDIAN over RANGE, ntile)."""
    con.execute(
        """
        CREATE OR REPLACE TABLE _tx_windows AS
        SELECT
          b.*,
          COUNT(*) OVER w_all AS n_prior_txns,
          COUNT(*) OVER w_90d AS n_prior_txns_90d,
          MEDIAN(b.TransactionAmt) OVER w_90d AS prior_median_90d,
          COUNT(*) FILTER (WHERE b.channel = 'online') OVER w_48h AS n_online_last_48h,
          COUNT(*) FILTER (WHERE b.channel = 'in_person') OVER w_24h AS n_in_person_last_24h,
          COUNT(*) FILTER (WHERE b.channel = 'online') OVER w_24h AS n_online_last_24h
        FROM _tx_base b
        WINDOW
          w_all AS (
            PARTITION BY b.customer_id, b.c2, b.c3, b.c4, b.c5, b.c6
            ORDER BY b.ts_epoch, b.TransactionID
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
          ),
          w_90d AS (
            PARTITION BY b.customer_id, b.c2, b.c3, b.c4, b.c5, b.c6
            ORDER BY b.ts_epoch
            RANGE BETWEEN 7776000 PRECEDING AND 1 PRECEDING
          ),
          w_48h AS (
            PARTITION BY b.customer_id, b.c2, b.c3, b.c4, b.c5, b.c6
            ORDER BY b.ts_epoch
            RANGE BETWEEN 172800 PRECEDING AND 1 PRECEDING
          ),
          w_24h AS (
            PARTITION BY b.customer_id, b.c2, b.c3, b.c4, b.c5, b.c6
            ORDER BY b.ts_epoch
            RANGE BETWEEN 86400 PRECEDING AND 1 PRECEDING
          )
        """
    )

    # Rolling MAD over 90d.
    con.execute(
        """
        CREATE OR REPLACE TABLE _tx_mad AS
        SELECT p.*,
          MEDIAN(ABS(p.TransactionAmt - p.prior_median_90d)) OVER w_90d AS prior_mad_90d
        FROM _tx_windows p
        WINDOW w_90d AS (
          PARTITION BY p.customer_id, p.c2, p.c3, p.c4, p.c5, p.c6
          ORDER BY p.ts_epoch
          RANGE BETWEEN 7776000 PRECEDING AND 1 PRECEDING
        )
        """
    )

    # Risk deciles.
    con.execute(
        """
        CREATE OR REPLACE TABLE _risk_deciles AS
        SELECT TransactionID, ntile(10) OVER (ORDER BY risk_score) AS risk_score_decile
        FROM _tx_base
        """
    )


def _compute_prior_sets_python(con: duckdb.DuckDBPyConnection) -> None:
    """Per-card incremental scan over (ts, TransactionID) → prior-set signals."""
    df = con.execute(
        """
        SELECT
          TransactionID, customer_id, c2, c3, c4, c5, c6,
          ts_epoch, channel, ProductCD, addr1,
          P_emaildomain, device_profile
        FROM _tx_base
        ORDER BY customer_id, c2, c3, c4, c5, c6, ts_epoch, TransactionID
        """
    ).fetch_arrow_table()

    logger.info("  … scanning %d rows in Python for prior-set signals", df.num_rows)

    tid       = df["TransactionID"].to_pylist()
    cust      = df["customer_id"].to_pylist()
    c2        = df["c2"].to_pylist()
    c3        = df["c3"].to_pylist()
    c4        = df["c4"].to_pylist()
    c5        = df["c5"].to_pylist()
    c6        = df["c6"].to_pylist()
    channel   = df["channel"].to_pylist()
    prod      = df["ProductCD"].to_pylist()
    addr1     = df["addr1"].to_pylist()
    p_email   = df["P_emaildomain"].to_pylist()
    device    = df["device_profile"].to_pylist()

    n = df.num_rows

    out_new_device = [None] * n
    out_unseen_prod = [None] * n
    out_unseen_pemail = [None] * n
    out_home_addr = [None] * n
    out_out_of_home = [None] * n

    # State — reset per card boundary.
    prev_key = None
    devices: set[str] = set()
    products: set[str] = set()
    p_emails: set[str] = set()
    inperson_addr_counts: Counter = Counter()

    for i in range(n):
        key = (cust[i], c2[i], c3[i], c4[i], c5[i], c6[i])
        if key != prev_key:
            devices = set()
            products = set()
            p_emails = set()
            inperson_addr_counts = Counter()
            n_prior_this_card = 0
            prev_key = key

        n_prior_this_card = len(products) + (len(devices) - len(devices & {None}))  # rough
        # Actual n_prior_txns comes from the DuckDB window; here we only need
        # "is this the first / very-early txn on the card" — use inperson count sum
        # + a running counter.
        # Fire signals only if we've seen >=5 prior txns on this card.

        # Compute output BEFORE updating the state.
        # is_new_device_for_card
        if channel[i] == "online" and device[i] is not None:
            if device[i] in devices:
                out_new_device[i] = False
            else:
                out_new_device[i] = True
        # unseen_productcd
        if prod[i] is not None:
            out_unseen_prod[i] = (prod[i] not in products)
        # unseen_p_emaildomain
        if channel[i] == "online" and p_email[i] is not None:
            out_unseen_pemail[i] = (p_email[i] not in p_emails)
        # home_addr1_asof + out_of_home_region_asof
        if inperson_addr_counts:
            home = inperson_addr_counts.most_common(1)[0][0]
            out_home_addr[i] = home
            if channel[i] == "in_person" and addr1[i] is not None:
                out_out_of_home[i] = (addr1[i] != home)

        # Update state with THIS txn.
        if device[i] is not None:
            devices.add(device[i])
        if prod[i] is not None:
            products.add(prod[i])
        if p_email[i] is not None:
            p_emails.add(p_email[i])
        if channel[i] == "in_person" and addr1[i] is not None:
            inperson_addr_counts[addr1[i]] += 1

    # Register as a Python-side arrow table then load into DuckDB.
    import pyarrow as pa

    pit = pa.table({
        "TransactionID": tid,
        "is_new_device_for_card_raw": out_new_device,
        "unseen_productcd_raw":       out_unseen_prod,
        "unseen_p_emaildomain_raw":   out_unseen_pemail,
        "home_addr1_asof":            out_home_addr,
        "out_of_home_region_asof_raw": out_out_of_home,
    })
    con.register("_pit_python", pit)
    con.execute("CREATE OR REPLACE TABLE _tx_pit AS SELECT * FROM _pit_python")


def _assemble(con: duckdb.DuckDBPyConnection) -> None:
    """Join everything and apply the n_prior_txns < 5 NULL guard."""
    con.execute(
        """
        CREATE OR REPLACE TABLE txn_features AS
        SELECT
          m.TransactionID, m.ts, m.channel, m.ProductCD, m.addr1,
          m.P_emaildomain, m.R_emaildomain, m.TransactionAmt, m.risk_score,
          m.customer_id, m.c2, m.c3, m.c4, m.c5, m.c6,
          m.M1, m.M2, m.M3, m.M4, m.M5, m.M6, m.M7, m.M8, m.M9,
          m.id_15, m.id_23, m.device_profile, m.proxy_flag,
          m.n_prior_txns, m.n_prior_txns_90d,
          m.prior_median_90d, m.prior_mad_90d,
          m.n_online_last_48h, m.n_in_person_last_24h, m.n_online_last_24h,
          rd.risk_score_decile,
          p.home_addr1_asof,
          CASE WHEN m.n_prior_txns < 5 THEN NULL
               ELSE p.is_new_device_for_card_raw
          END AS is_new_device_for_card,
          CASE WHEN m.n_prior_txns < 5 THEN NULL
               ELSE p.unseen_productcd_raw
          END AS unseen_productcd,
          CASE WHEN m.n_prior_txns < 5 THEN NULL
               ELSE p.unseen_p_emaildomain_raw
          END AS unseen_p_emaildomain,
          CASE WHEN m.n_prior_txns < 5 THEN NULL
               ELSE p.out_of_home_region_asof_raw
          END AS out_of_home_region_asof,
          CASE
            WHEN m.n_prior_txns_90d < 5 THEN NULL
            WHEN m.prior_mad_90d IS NULL OR m.prior_mad_90d = 0 THEN NULL
            ELSE (m.TransactionAmt - m.prior_median_90d) / (1.4826 * m.prior_mad_90d)
          END AS amt_z_asof,
          (m.n_online_last_24h > 0 AND m.n_in_person_last_24h > 0)
            AS mixed_channel_last_24h,
          CASE
            WHEN m.n_online_last_48h >= 5 THEN '5+'
            WHEN m.n_online_last_48h >= 2 THEN '2-4'
            WHEN m.n_online_last_48h = 1  THEN '1'
            ELSE '0'
          END AS n_online_last_48h_bucket
        FROM _tx_mad m
        LEFT JOIN _tx_pit  p  ON p.TransactionID = m.TransactionID
        LEFT JOIN _risk_deciles rd ON rd.TransactionID = m.TransactionID
        """
    )


def _prior_case_edges(con: duckdb.DuckDBPyConnection) -> None:
    """Prior fraud/cleared edges keyed by (customer, tuple, closed_at<ts)."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE _cc_events AS
        SELECT
          cc.case_id, cc.customer_id, cc.outcome, cc.pattern,
          cc.closed_at::TIMESTAMP AS closed_at, cc.analyst_notes,
          COALESCE(t.card2::VARCHAR,'_') AS c2,
          COALESCE(t.card3::VARCHAR,'_') AS c3,
          COALESCE(t.card4::VARCHAR,'_') AS c4,
          COALESCE(t.card5::VARCHAR,'_') AS c5,
          COALESCE(t.card6::VARCHAR,'_') AS c6
        FROM read_csv_auto('{CC_CSV}', header=true) cc
        LEFT JOIN {TX} t ON t.TransactionID = cc.first_fraud_txn_id
        """
    )
    # Per-txn boolean flags.
    con.execute(
        """
        CREATE OR REPLACE TABLE _prior_flags AS
        SELECT
          f.TransactionID,
          EXISTS (
            SELECT 1 FROM _cc_events e
            WHERE e.outcome = 'confirmed_fraud'
              AND e.customer_id = f.customer_id
              AND e.c2=f.c2 AND e.c3=f.c3 AND e.c4=f.c4 AND e.c5=f.c5 AND e.c6=f.c6
              AND e.closed_at < f.ts
          ) AS prior_fraud_on_card_tuple,
          EXISTS (
            SELECT 1 FROM _cc_events e
            WHERE e.outcome = 'confirmed_fraud'
              AND e.customer_id = f.customer_id
              AND e.closed_at < f.ts
          ) AS prior_fraud_on_customer,
          EXISTS (
            SELECT 1 FROM _cc_events e
            WHERE e.outcome = 'cleared'
              AND e.customer_id = f.customer_id
              AND e.c2=f.c2 AND e.c3=f.c3 AND e.c4=f.c4 AND e.c5=f.c5 AND e.c6=f.c6
              AND e.closed_at < f.ts
              AND e.analyst_notes ILIKE '%confirmed travel%'
          ) AS prior_cleared_travel_on_card,
          EXISTS (
            SELECT 1 FROM _cc_events e
            WHERE e.outcome = 'cleared'
              AND e.customer_id = f.customer_id
              AND e.c2=f.c2 AND e.c3=f.c3 AND e.c4=f.c4 AND e.c5=f.c5 AND e.c6=f.c6
              AND e.closed_at < f.ts
              AND e.analyst_notes ILIKE '%new phone%'
          ) AS prior_cleared_new_phone_on_card
        FROM txn_features f
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE txn_features AS
        SELECT f.*, p.prior_fraud_on_card_tuple, p.prior_fraud_on_customer,
               p.prior_cleared_travel_on_card, p.prior_cleared_new_phone_on_card
        FROM txn_features f
        LEFT JOIN _prior_flags p USING (TransactionID)
        """
    )


def build(con: duckdb.DuckDBPyConnection) -> int:
    logger.info("point-in-time feature build starting …")
    _build_base(con)
    logger.info("  step 1/5 base done")
    _build_windows(con)
    logger.info("  step 2/5 windows done")
    _compute_prior_sets_python(con)
    logger.info("  step 3/5 python priors done")
    _assemble(con)
    logger.info("  step 4/5 assembled")
    _prior_case_edges(con)
    logger.info("  step 5/5 prior-case edges done")
    n = con.execute("SELECT COUNT(*) FROM txn_features").fetchone()[0]
    return n


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from sentinel.data.features import build_all as _phase1_build_all
    from sentinel.data.features import connect

    con = connect(read_only=False)
    _phase1_build_all(con)
    n = build(con)
    print(f"txn_features rows: {n:,}")
    # Smoke stats.
    r = con.execute(
        """
        SELECT
          SUM(CASE WHEN is_new_device_for_card THEN 1 ELSE 0 END) AS nd,
          SUM(CASE WHEN out_of_home_region_asof THEN 1 ELSE 0 END) AS ohr,
          SUM(CASE WHEN prior_fraud_on_card_tuple THEN 1 ELSE 0 END) AS pf,
          SUM(CASE WHEN prior_fraud_on_customer THEN 1 ELSE 0 END) AS pfc,
          SUM(CASE WHEN prior_cleared_travel_on_card THEN 1 ELSE 0 END) AS pct,
          SUM(CASE WHEN prior_cleared_new_phone_on_card THEN 1 ELSE 0 END) AS pcnp,
          SUM(CASE WHEN mixed_channel_last_24h THEN 1 ELSE 0 END) AS mixed,
          COUNT(*) FILTER (WHERE is_new_device_for_card IS NULL) AS n_new_null
        FROM txn_features
        """
    ).fetchone()
    print(f"is_new_device_for_card=TRUE : {r[0]:,}  (NULL: {r[7]:,})")
    print(f"out_of_home_region_asof=TRUE: {r[1]:,}")
    print(f"prior_fraud_on_card_tuple   : {r[2]:,}")
    print(f"prior_fraud_on_customer     : {r[3]:,}")
    print(f"prior_cleared_travel        : {r[4]:,}")
    print(f"prior_cleared_new_phone     : {r[5]:,}")
    print(f"mixed_channel_last_24h      : {r[6]:,}")
