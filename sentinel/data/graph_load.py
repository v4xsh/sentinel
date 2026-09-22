"""Emit the reduced CSVs that populate FraudGraph.

Materialized into ``data/graph_load/``:

  customers.csv        Customer vertices
  payment_cards.csv    PaymentCard vertices  (id = tuple hash)
  transactions.csv     Transaction vertices (25 attrs)
  device_profiles.csv  DeviceProfile vertices (unique by DeviceInfo|OS|browser|screen)
  email_domains.csv    EmailDomain vertices (unique P/R domains)
  billing_regions.csv  BillingRegion vertices (unique addr1)
  product_codes.csv    ProductCode vertices (5 static codes)
  closed_cases.csv     ClosedCase vertices + edge lists to expand

  edges_owns.csv       Customer -> PaymentCard
  edges_made.csv       PaymentCard -> Transaction
  edges_from_device.csv       Transaction -> DeviceProfile
  edges_purchaser_email.csv   Transaction -> EmailDomain
  edges_recipient_email.csv   Transaction -> EmailDomain
  edges_billed_in.csv         Transaction -> BillingRegion
  edges_in_product.csv        Transaction -> ProductCode
  edges_next.csv              Transaction -> Transaction (LAG within card_tuple)
  edges_home_region.csv       PaymentCard -> BillingRegion (mode addr1 in-person)
  edges_known_device.csv      PaymentCard -> DeviceProfile (first-seen + count)

  edges_cc_involves.csv       ClosedCase -> Transaction
  edges_cc_on_card.csv        ClosedCase -> PaymentCard   (by tuple)
  edges_cc_on_customer.csv    ClosedCase -> Customer
  edges_cc_connected_to.csv   ClosedCase -> PaymentCard   (from connected_card_ids)
  edges_cc_on_device.csv      ClosedCase -> DeviceProfile (parsed from notes)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import duckdb

from sentinel.config import GRAPH_LOAD, PARQUET, RAW
from sentinel.data.features import connect

logger = logging.getLogger(__name__)
GRAPH_LOAD.mkdir(parents=True, exist_ok=True)

TX = f"read_parquet('{PARQUET / 'transactions.parquet'}')"
ID = f"read_parquet('{PARQUET / 'identity.parquet'}')"
CC = f"read_csv_auto('{RAW / 'closed_cases_history.csv'}', header=true)"


def _card_tuple_id_expr(alias: str = "t") -> str:
    """SQL expression that builds Card.id as customer_id|c2|c3|c4|c5|c6."""
    return (
        f"{alias}.customer_id || '|' || "
        f"COALESCE({alias}.card2::VARCHAR,'_') || '|' || "
        f"COALESCE({alias}.card3::VARCHAR,'_') || '|' || "
        f"COALESCE({alias}.card4::VARCHAR,'_') || '|' || "
        f"COALESCE({alias}.card5::VARCHAR,'_') || '|' || "
        f"COALESCE({alias}.card6::VARCHAR,'_')"
    )


def emit_customers(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "customers.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            customer_id AS id,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts,
            COUNT(DISTINCT ({_card_tuple_id_expr()})) AS n_cards
          FROM {TX} t
          GROUP BY 1
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_payment_cards(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "payment_cards.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            {_card_tuple_id_expr()} AS id,
            t.customer_id,
            cm.card_id AS card_id_display,
            ANY_VALUE(t.card1) AS card1,
            ANY_VALUE(COALESCE(t.card2::VARCHAR,'_')) AS card2,
            ANY_VALUE(COALESCE(t.card3::VARCHAR,'_')) AS card3,
            ANY_VALUE(COALESCE(t.card4::VARCHAR,'_')) AS card4,
            ANY_VALUE(COALESCE(t.card5::VARCHAR,'_')) AS card5,
            ANY_VALUE(COALESCE(t.card6::VARCHAR,'_')) AS card6,
            MIN(t.ts) AS first_ts,
            MAX(t.ts) AS last_ts,
            COUNT(*) AS n_txns
          FROM {TX} t
          LEFT JOIN card_map cm
            ON cm.customer_id = t.customer_id
           AND cm.c2 = COALESCE(t.card2::VARCHAR,'_')
           AND cm.c3 = COALESCE(t.card3::VARCHAR,'_')
           AND cm.c4 = COALESCE(t.card4::VARCHAR,'_')
           AND cm.c5 = COALESCE(t.card5::VARCHAR,'_')
           AND cm.c6 = COALESCE(t.card6::VARCHAR,'_')
          GROUP BY 1,2,3
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_transactions(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "transactions.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            'T' || CAST(t.TransactionID AS VARCHAR) AS id,
            t.TransactionID AS txn_int,
            t.ts,
            t.TransactionAmt AS amt,
            COALESCE(t.ProductCD,'') AS ProductCD,
            t.channel,
            t.risk_score,
            COALESCE(t.addr1::VARCHAR,'') AS addr1,
            COALESCE(t.addr2::VARCHAR,'') AS addr2,
            COALESCE(t.dist1,-1) AS dist1,
            COALESCE(t.dist2,-1) AS dist2,
            COALESCE(t.P_emaildomain,'') AS P_emaildomain,
            COALESCE(t.R_emaildomain,'') AS R_emaildomain,
            COALESCE(t.M1::VARCHAR,'') M1, COALESCE(t.M2::VARCHAR,'') M2, COALESCE(t.M3::VARCHAR,'') M3,
            COALESCE(t.M4::VARCHAR,'') M4, COALESCE(t.M5::VARCHAR,'') M5, COALESCE(t.M6::VARCHAR,'') M6,
            COALESCE(t.M7::VARCHAR,'') M7, COALESCE(t.M8::VARCHAR,'') M8, COALESCE(t.M9::VARCHAR,'') M9,
            COALESCE(i.id_15,'') AS id_15,
            COALESCE(i.id_23,'') AS id_23,
            CASE WHEN t.channel = 'online'
                 THEN COALESCE(i.DeviceInfo,'-') || ' | ' ||
                      COALESCE(i.id_30,      '-') || ' | ' ||
                      COALESCE(i.id_31,      '-') || ' | ' ||
                      COALESCE(i.id_33,      '-')
                 ELSE ''
            END AS device_profile
          FROM {TX} t
          LEFT JOIN {ID} i ON i.TransactionID = t.TransactionID
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',', QUOTE '"');
        """
    )
    return out


def emit_device_profiles(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "device_profiles.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            device_profile AS profile,
            ANY_VALUE(DeviceInfo) AS DeviceInfo,
            ANY_VALUE(id_30) AS os,
            ANY_VALUE(id_31) AS browser,
            ANY_VALUE(id_33) AS screen,
            ANY_VALUE(id_23) AS proxy_hint,
            COUNT(*) AS n_seen
          FROM tx_enriched
          WHERE device_profile IS NOT NULL
          GROUP BY 1
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',', QUOTE '"');
        """
    )
    return out


def emit_email_domains(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "email_domains.csv"
    con.execute(
        f"""
        COPY (
          SELECT DISTINCT domain FROM (
            SELECT P_emaildomain AS domain FROM {TX} WHERE P_emaildomain IS NOT NULL
            UNION
            SELECT R_emaildomain AS domain FROM {TX} WHERE R_emaildomain IS NOT NULL
          )
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_billing_regions(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "billing_regions.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            addr1::VARCHAR AS addr1,
            COUNT(*) AS n_seen
          FROM {TX}
          WHERE addr1 IS NOT NULL
          GROUP BY 1
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_product_codes(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "product_codes.csv"
    con.execute(
        f"""
        COPY (
          SELECT DISTINCT ProductCD AS code FROM {TX} WHERE ProductCD IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_closed_cases(con: duckdb.DuckDBPyConnection) -> Path:
    """ClosedCase vertices — the analyst_notes field is CSV-quoted."""
    out = GRAPH_LOAD / "closed_cases.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            cc.case_id,
            cc.customer_id,
            {_card_tuple_id_expr('t')} AS card_tuple_id,
            cc.card_id AS hist_card_id,
            cc.opened_at,
            cc.closed_at,
            cc.outcome,
            cc.pattern,
            CAST(cc.first_fraud_txn_id AS VARCHAR) AS first_fraud_txn_id,
            CAST(cc.n_txns AS INT) AS n_txns,
            CAST(cc.exposure_usd AS DOUBLE) AS exposure_usd,
            cc.report_filed,
            REPLACE(REPLACE(cc.analyst_notes, CHR(10), ' '), CHR(13), ' ') AS analyst_notes
          FROM {CC} cc
          LEFT JOIN {TX} t ON t.TransactionID = cc.first_fraud_txn_id
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',', QUOTE '"', ESCAPE '"');
        """
    )
    return out


# ---- edges --------------------------------------------------------------


def emit_owns(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_owns.csv"
    con.execute(
        f"""
        COPY (
          SELECT DISTINCT
            t.customer_id AS customer_id,
            {_card_tuple_id_expr('t')} AS card_id
          FROM {TX} t
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_made(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_made.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            {_card_tuple_id_expr('t')} AS card_id,
            'T' || CAST(t.TransactionID AS VARCHAR) AS txn_id
          FROM {TX} t
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_from_device(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_from_device.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            'T' || CAST(TransactionID AS VARCHAR) AS txn_id,
            device_profile AS profile
          FROM tx_enriched
          WHERE device_profile IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',', QUOTE '"');
        """
    )
    return out


def emit_purchaser_email(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_purchaser_email.csv"
    con.execute(
        f"""
        COPY (
          SELECT 'T' || CAST(TransactionID AS VARCHAR), P_emaildomain
          FROM {TX} WHERE P_emaildomain IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_recipient_email(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_recipient_email.csv"
    con.execute(
        f"""
        COPY (
          SELECT 'T' || CAST(TransactionID AS VARCHAR), R_emaildomain
          FROM {TX} WHERE R_emaildomain IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_billed_in(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_billed_in.csv"
    con.execute(
        f"""
        COPY (
          SELECT 'T' || CAST(TransactionID AS VARCHAR), addr1::VARCHAR
          FROM {TX} WHERE addr1 IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_in_product(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_in_product.csv"
    con.execute(
        f"""
        COPY (
          SELECT 'T' || CAST(TransactionID AS VARCHAR), ProductCD
          FROM {TX} WHERE ProductCD IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_next(con: duckdb.DuckDBPyConnection) -> Path:
    """NEXT edges: LAG over ts within (customer_id, tuple)."""
    out = GRAPH_LOAD / "edges_next.csv"
    con.execute(
        f"""
        COPY (
          WITH r AS (
            SELECT
              t.customer_id,
              COALESCE(t.card2::VARCHAR,'_') c2, COALESCE(t.card3::VARCHAR,'_') c3,
              COALESCE(t.card4::VARCHAR,'_') c4, COALESCE(t.card5::VARCHAR,'_') c5,
              COALESCE(t.card6::VARCHAR,'_') c6,
              t.ts,
              t.TransactionID
            FROM {TX} t
          )
          SELECT
            'T' || CAST(LAG(TransactionID) OVER (
              PARTITION BY customer_id,c2,c3,c4,c5,c6 ORDER BY ts, TransactionID
            ) AS VARCHAR) AS prev_id,
            'T' || CAST(TransactionID AS VARCHAR) AS this_id,
            CAST((EPOCH(ts) - LAG(EPOCH(ts)) OVER (
              PARTITION BY customer_id,c2,c3,c4,c5,c6 ORDER BY ts, TransactionID
            )) AS BIGINT) AS gap_secs
          FROM r
          QUALIFY LAG(TransactionID) OVER (
            PARTITION BY customer_id,c2,c3,c4,c5,c6 ORDER BY ts, TransactionID
          ) IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_home_region(con: duckdb.DuckDBPyConnection) -> Path:
    """One HOME_REGION edge per card — mode of addr1 over in-person txns."""
    out = GRAPH_LOAD / "edges_home_region.csv"
    con.execute(
        f"""
        COPY (
          WITH counts AS (
            SELECT
              {_card_tuple_id_expr('t')} AS card_id,
              addr1::VARCHAR AS addr1,
              COUNT(*) AS n
            FROM {TX} t
            WHERE channel='in_person' AND addr1 IS NOT NULL
            GROUP BY 1,2
          ),
          ranked AS (
            SELECT card_id, addr1, n,
                   ROW_NUMBER() OVER (PARTITION BY card_id ORDER BY n DESC) rk
            FROM counts
          )
          SELECT card_id, addr1, n AS n_home FROM ranked WHERE rk = 1
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_known_device(con: duckdb.DuckDBPyConnection) -> Path:
    """Card → DeviceProfile aggregation."""
    out = GRAPH_LOAD / "edges_known_device.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            t.customer_id || '|' || t.c2 || '|' || t.c3 || '|' || t.c4 || '|' || t.c5 || '|' || t.c6
              AS card_id,
            t.device_profile AS profile,
            COUNT(*) AS n_seen,
            MIN(t.ts) AS first_seen
          FROM tx_enriched t
          WHERE t.device_profile IS NOT NULL
          GROUP BY 1,2
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',', QUOTE '"');
        """
    )
    return out


# ---- closed-case edges --------------------------------------------------


def emit_cc_involves(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_cc_involves.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            cc.case_id,
            'T' || TRIM(x.txn) AS txn_id
          FROM {CC} cc,
               LATERAL (SELECT unnest(split(cc.txn_ids, '|')) AS txn) x
          WHERE cc.txn_ids IS NOT NULL AND cc.txn_ids != ''
            AND TRIM(x.txn) != ''
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_cc_on_card(con: duckdb.DuckDBPyConnection) -> Path:
    """ClosedCase -> PaymentCard by first-fraud-txn tuple (skip cleared cases).

    For cleared cases we still edge to the *customer* below; but on_card is
    by tuple so we need at least one txn to resolve it. Cleared cases have
    first_fraud_txn_id NULL, so we drop them from this edge set.
    """
    out = GRAPH_LOAD / "edges_cc_on_card.csv"
    con.execute(
        f"""
        COPY (
          SELECT
            cc.case_id,
            {_card_tuple_id_expr('t')} AS card_id
          FROM {CC} cc
          JOIN {TX} t ON t.TransactionID = cc.first_fraud_txn_id
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_cc_on_customer(con: duckdb.DuckDBPyConnection) -> Path:
    out = GRAPH_LOAD / "edges_cc_on_customer.csv"
    con.execute(
        f"""
        COPY (
          SELECT case_id, customer_id FROM {CC}
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_cc_connected_to(con: duckdb.DuckDBPyConnection) -> Path:
    """ClosedCase -> PaymentCard from connected_card_ids (K-string encoded).

    Historical -Kn strings can be mismatched by up to 309 cases (see LEARNINGS);
    we still emit these edges as-written for retrieval — but for tuple-level
    joins the primary source is CC_ON_CARD.
    """
    out = GRAPH_LOAD / "edges_cc_connected_to.csv"
    # For each connected_card_id like "C00255-K1", we need to look up its tuple.
    # We use card_map (rank-E) to translate; where the string is unrecognized
    # we skip. That means we'll miss some legacy mismatches but never emit
    # dangling edges.
    con.execute(
        f"""
        COPY (
          SELECT DISTINCT
            cc.case_id,
            cm.customer_id || '|' || cm.c2 || '|' || cm.c3 || '|' || cm.c4 || '|' || cm.c5 || '|' || cm.c6
              AS card_id
          FROM {CC} cc,
               LATERAL (SELECT unnest(split(cc.connected_card_ids, '|')) AS ccid) x
          JOIN card_map cm ON cm.card_id = TRIM(x.ccid)
          WHERE cc.connected_card_ids IS NOT NULL
            AND cc.connected_card_ids != ''
            AND TRIM(x.ccid) != ''
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',');
        """
    )
    return out


def emit_cc_on_device(con: duckdb.DuckDBPyConnection) -> Path:
    """Link ClosedCase -> DeviceProfile using the profiles observed on the
    case's own txns (cc.txn_ids -> Transaction.device_profile).

    We deliberately do NOT do free-text matching on the notes: those match too
    permissively (one "Trident/7.0 on ie 11.0 for desktop" fragment matched
    every screen resolution). The txn-based join is precise and satisfies the
    Phase-2 brief's requirement of "device-based memory retrieval structural,
    not just semantic".
    """
    out = GRAPH_LOAD / "edges_cc_on_device.csv"
    con.execute(
        f"""
        COPY (
          SELECT DISTINCT
            cc.case_id,
            e.device_profile AS profile
          FROM {CC} cc,
               LATERAL (SELECT unnest(split(cc.txn_ids, '|')) AS txn) x
          JOIN tx_enriched e ON e.TransactionID = CAST(TRIM(x.txn) AS BIGINT)
          WHERE cc.txn_ids IS NOT NULL AND cc.txn_ids != ''
            AND TRIM(x.txn) != ''
            AND e.device_profile IS NOT NULL
        ) TO '{out}' (FORMAT CSV, HEADER, DELIMITER ',', QUOTE '"');
        """
    )
    return out


# ---- driver -------------------------------------------------------------


ALL_EMITTERS: list[tuple[str, callable]] = [
    ("customers", emit_customers),
    ("payment_cards", emit_payment_cards),
    ("transactions", emit_transactions),
    ("device_profiles", emit_device_profiles),
    ("email_domains", emit_email_domains),
    ("billing_regions", emit_billing_regions),
    ("product_codes", emit_product_codes),
    ("closed_cases", emit_closed_cases),
    ("edges_owns", emit_owns),
    ("edges_made", emit_made),
    ("edges_from_device", emit_from_device),
    ("edges_purchaser_email", emit_purchaser_email),
    ("edges_recipient_email", emit_recipient_email),
    ("edges_billed_in", emit_billed_in),
    ("edges_in_product", emit_in_product),
    ("edges_next", emit_next),
    ("edges_home_region", emit_home_region),
    ("edges_known_device", emit_known_device),
    ("edges_cc_involves", emit_cc_involves),
    ("edges_cc_on_card", emit_cc_on_card),
    ("edges_cc_on_customer", emit_cc_on_customer),
    ("edges_cc_connected_to", emit_cc_connected_to),
    ("edges_cc_on_device", emit_cc_on_device),
]


def emit_all() -> dict[str, tuple[Path, int]]:
    """Emit every CSV and return {name: (path, row_count)}."""
    con = connect(read_only=False)
    # Assume the feature store is already built (features.build_all).
    result: dict[str, tuple[Path, int]] = {}
    for name, fn in ALL_EMITTERS:
        p = fn(con)
        # Count rows (excluding header).
        with open(p, "rb") as f:
            n = sum(1 for _ in f) - 1
        result[name] = (p, max(n, 0))
        logger.info("emitted %-28s rows=%d  -> %s", name, n, p.name)
    return result


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    r = emit_all()
    for k, (p, n) in r.items():
        print(f"  {k:<28} rows={n:>9,}  {p}")
