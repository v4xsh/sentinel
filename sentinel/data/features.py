"""DuckDB feature store — Phase 1.

Materializes into ``sentinel.duckdb``:

    fraud_txn_ids     — TransactionIDs inside confirmed_fraud closed cases' txn_ids
    card_map          — (customer_id, c2..c6) -> rank-E card_id string
    tx_enriched       — one row per Transaction: identity joined, device_profile
                        computed, is_new_device_for_card, proxy_flag, is_out_of_home_region
    card_baseline_raw   — per-card baseline, using ALL txns (may include fraud episodes)
    card_baseline_clean — per-card baseline, EXCLUDING any txn in fraud_txn_ids

Card resolution is always by tuple ``(customer_id, c2,c3,c4,c5,c6)``.
The ``-K<rank>`` string is a derived, human-readable alias — never a join key.
See ``resolve_card_by_txn()`` and ``emit_card_id()`` for canonical helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import duckdb

from sentinel.config import DUCKDB_PATH, PARQUET, RAW


# ---------- constants -----------------------------------------------------------


TX_PARQUET = PARQUET / "transactions.parquet"
ID_PARQUET = PARQUET / "identity.parquet"
CC_CSV = RAW / "closed_cases_history.csv"
CP_CSV = RAW / "case_pack.csv"


@dataclass(frozen=True)
class CardTuple:
    """Canonical card identity: (customer_id, c2, c3, c4, c5, c6).

    Missing components are the placeholder ``"_"`` (matches ``COALESCE(x::VARCHAR, '_')``).
    """

    customer_id: str
    c2: str
    c3: str
    c4: str
    c5: str
    c6: str

    def sql_where(self, tx_alias: str = "t") -> str:
        return (
            f"{tx_alias}.customer_id = '{self.customer_id}' "
            f"AND COALESCE({tx_alias}.card2::VARCHAR,'_') = '{self.c2}' "
            f"AND COALESCE({tx_alias}.card3::VARCHAR,'_') = '{self.c3}' "
            f"AND COALESCE({tx_alias}.card4::VARCHAR,'_') = '{self.c4}' "
            f"AND COALESCE({tx_alias}.card5::VARCHAR,'_') = '{self.c5}' "
            f"AND COALESCE({tx_alias}.card6::VARCHAR,'_') = '{self.c6}'"
        )


# ---------- feature-store construction ------------------------------------------


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(DUCKDB_PATH))
    con.execute("PRAGMA threads=8")
    con.execute("PRAGMA memory_limit='6GB'")
    return con


def build_fraud_txn_ids(con: duckdb.DuckDBPyConnection) -> int:
    """Expand ``closed_cases_history.txn_ids`` -> per-row TransactionIDs."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE fraud_txn_ids AS
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
    return con.execute("SELECT COUNT(*) FROM fraud_txn_ids").fetchone()[0]


def build_card_map(con: duckdb.DuckDBPyConnection) -> int:
    """(customer_id, tuple) -> rank-E card_id (first_ts DESC, n DESC)."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE card_map AS
        WITH t AS (
          SELECT
            customer_id,
            COALESCE(card2::VARCHAR,'_') AS c2,
            COALESCE(card3::VARCHAR,'_') AS c3,
            COALESCE(card4::VARCHAR,'_') AS c4,
            COALESCE(card5::VARCHAR,'_') AS c5,
            COALESCE(card6::VARCHAR,'_') AS c6,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts,
            COUNT(*) AS n
          FROM read_parquet('{TX_PARQUET}')
          GROUP BY 1,2,3,4,5,6
        )
        SELECT customer_id, c2, c3, c4, c5, c6, first_ts, last_ts, n,
               ROW_NUMBER() OVER (PARTITION BY customer_id
                                  ORDER BY first_ts DESC, n DESC) AS k,
               customer_id || '-K' ||
                 CAST(ROW_NUMBER() OVER (
                   PARTITION BY customer_id ORDER BY first_ts DESC, n DESC
                 ) AS VARCHAR) AS card_id
        FROM t
        """
    )
    return con.execute("SELECT COUNT(*) FROM card_map").fetchone()[0]


def build_tx_enriched(con: duckdb.DuckDBPyConnection) -> int:
    """Per-txn enrichment: device_profile, is_new_device_for_card, proxy_flag, region, etc."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE tx_enriched AS
        WITH base AS (
          SELECT
            t.TransactionID,
            t.customer_id,
            t.ts,
            t.channel,
            t.ProductCD,
            t.TransactionAmt,
            t.risk_score,
            t.card1, t.card2, t.card3, t.card4, t.card5, t.card6,
            COALESCE(t.card2::VARCHAR,'_') AS c2,
            COALESCE(t.card3::VARCHAR,'_') AS c3,
            COALESCE(t.card4::VARCHAR,'_') AS c4,
            COALESCE(t.card5::VARCHAR,'_') AS c5,
            COALESCE(t.card6::VARCHAR,'_') AS c6,
            t.addr1, t.addr2, t.dist1, t.dist2,
            t.P_emaildomain, t.R_emaildomain,
            t.M1, t.M2, t.M3, t.M4, t.M5, t.M6, t.M7, t.M8, t.M9,
            i.id_15, i.id_23, i.id_28, i.id_30, i.id_31, i.id_32, i.id_33, i.id_34,
            i.DeviceType, i.DeviceInfo
          FROM read_parquet('{TX_PARQUET}') t
          LEFT JOIN read_parquet('{ID_PARQUET}') i
            ON i.TransactionID = t.TransactionID
        )
        SELECT
          base.*,
          CASE WHEN base.channel = 'online'
               THEN
                 COALESCE(base.DeviceInfo, '-') || ' | ' ||
                 COALESCE(base.id_30,      '-') || ' | ' ||
                 COALESCE(base.id_31,      '-') || ' | ' ||
                 COALESCE(base.id_33,      '-')
               ELSE NULL
          END AS device_profile,
          CASE WHEN base.id_23 IS NULL THEN FALSE
               WHEN base.id_23 LIKE 'IP_PROXY:%' THEN TRUE
               ELSE FALSE
          END AS proxy_flag,
          -- fraud flag: is this txn inside a confirmed_fraud closed case?
          CASE WHEN base.TransactionID IN (SELECT TransactionID FROM fraud_txn_ids)
               THEN TRUE ELSE FALSE
          END AS in_fraud_episode
        FROM base
        """
    )
    return con.execute("SELECT COUNT(*) FROM tx_enriched").fetchone()[0]


def _baseline_query(fraud_filter: str) -> str:
    """Return the SQL body for a card_baseline table.

    ``fraud_filter`` is either ``""`` (all txns, raw) or
    ``AND NOT in_fraud_episode`` (clean).
    """
    return f"""
    WITH filtered AS (
      SELECT * FROM tx_enriched WHERE 1=1 {fraud_filter}
    ),
    medians AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             MEDIAN(TransactionAmt) AS median_amt
      FROM filtered GROUP BY 1,2,3,4,5,6
    ),
    with_med AS (
      SELECT f.*, m.median_amt
      FROM filtered f
      JOIN medians m USING (customer_id, c2, c3, c4, c5, c6)
    ),
    inter_gaps AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             AVG(gap_h) AS avg_inter_hours
      FROM (
        SELECT customer_id, c2, c3, c4, c5, c6,
               (EPOCH(ts) - LAG(EPOCH(ts)) OVER (
                 PARTITION BY customer_id, c2, c3, c4, c5, c6 ORDER BY ts
               )) / 3600.0 AS gap_h
        FROM filtered
      )
      WHERE gap_h IS NOT NULL
      GROUP BY 1,2,3,4,5,6
    ),
    per_card AS (
      SELECT
        wm.customer_id, wm.c2, wm.c3, wm.c4, wm.c5, wm.c6,
        COUNT(*)                                                     AS n_txns,
        MIN(wm.ts)                                                   AS first_ts,
        MAX(wm.ts)                                                   AS last_ts,
        ANY_VALUE(wm.median_amt)                                     AS median_amt,
        AVG(wm.TransactionAmt)                                       AS mean_amt,
        MEDIAN(ABS(wm.TransactionAmt - wm.median_amt))               AS mad_amt,
        quantile_cont(wm.TransactionAmt, 0.95)                       AS p95_amt,
        MAX(wm.TransactionAmt)                                       AS max_amt,
        SUM(CASE WHEN wm.channel='online' THEN 1 ELSE 0 END)
          / CAST(COUNT(*) AS DOUBLE)                                 AS online_share,
        ANY_VALUE(ig.avg_inter_hours)                                AS avg_inter_hours
      FROM with_med wm
      LEFT JOIN inter_gaps ig USING (customer_id, c2, c3, c4, c5, c6)
      GROUP BY 1,2,3,4,5,6
    ),
    home_region AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             addr1 AS home_addr1, n_home
      FROM (
        SELECT customer_id, c2, c3, c4, c5, c6, addr1, COUNT(*) n_home,
               ROW_NUMBER() OVER (
                 PARTITION BY customer_id, c2, c3, c4, c5, c6
                 ORDER BY COUNT(*) DESC
               ) rk
        FROM filtered
        WHERE channel='in_person' AND addr1 IS NOT NULL
        GROUP BY 1,2,3,4,5,6,7
      ) WHERE rk = 1
    ),
    regions AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             LIST(DISTINCT addr1) AS regions_seen
      FROM filtered
      WHERE addr1 IS NOT NULL
      GROUP BY 1,2,3,4,5,6
    ),
    prods AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             LIST(DISTINCT ProductCD) AS product_codes_seen
      FROM filtered
      GROUP BY 1,2,3,4,5,6
    ),
    devices AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             LIST(DISTINCT device_profile) FILTER (WHERE device_profile IS NOT NULL)
                AS known_device_profiles
      FROM filtered
      GROUP BY 1,2,3,4,5,6
    ),
    p_emails AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             LIST(DISTINCT P_emaildomain) FILTER (WHERE P_emaildomain IS NOT NULL)
                AS known_p_emaildomains
      FROM filtered
      GROUP BY 1,2,3,4,5,6
    ),
    -- Recurring pairs: (ProductCD, amount_cents) seen >=2 times with min 20-day gap.
    recurring AS (
      SELECT customer_id, c2, c3, c4, c5, c6,
             LIST(DISTINCT (ProductCD || ':' || CAST(ROUND(TransactionAmt*100)/100 AS VARCHAR)))
                AS recurring_pairs
      FROM (
        SELECT f.customer_id, f.c2, f.c3, f.c4, f.c5, f.c6, f.ProductCD, f.TransactionAmt,
               COUNT(*) OVER (
                 PARTITION BY f.customer_id, f.c2, f.c3, f.c4, f.c5, f.c6,
                              f.ProductCD, ROUND(f.TransactionAmt*100)/100
               ) n_same,
               MAX(EPOCH(f.ts)) OVER (
                 PARTITION BY f.customer_id, f.c2, f.c3, f.c4, f.c5, f.c6,
                              f.ProductCD, ROUND(f.TransactionAmt*100)/100
               ) - MIN(EPOCH(f.ts)) OVER (
                 PARTITION BY f.customer_id, f.c2, f.c3, f.c4, f.c5, f.c6,
                              f.ProductCD, ROUND(f.TransactionAmt*100)/100
               ) span_secs
        FROM filtered f
      )
      WHERE n_same >= 2 AND span_secs >= 20 * 86400
      GROUP BY 1,2,3,4,5,6
    )
    SELECT
      p.customer_id, p.c2, p.c3, p.c4, p.c5, p.c6,
      p.n_txns, p.first_ts, p.last_ts,
      p.median_amt, p.mean_amt, p.mad_amt, p.p95_amt, p.max_amt,
      p.online_share, p.avg_inter_hours,
      hr.home_addr1, hr.n_home,
      COALESCE(rg.regions_seen, [])                    AS regions_seen,
      COALESCE(pr.product_codes_seen, [])              AS product_codes_seen,
      COALESCE(dv.known_device_profiles, [])           AS known_device_profiles,
      COALESCE(pe.known_p_emaildomains, [])            AS known_p_emaildomains,
      COALESCE(rc.recurring_pairs, [])                 AS recurring_pairs,
      cm.card_id                                        AS card_id_rankE
    FROM per_card p
    LEFT JOIN home_region hr USING (customer_id, c2, c3, c4, c5, c6)
    LEFT JOIN regions     rg USING (customer_id, c2, c3, c4, c5, c6)
    LEFT JOIN prods       pr USING (customer_id, c2, c3, c4, c5, c6)
    LEFT JOIN devices     dv USING (customer_id, c2, c3, c4, c5, c6)
    LEFT JOIN p_emails    pe USING (customer_id, c2, c3, c4, c5, c6)
    LEFT JOIN recurring   rc USING (customer_id, c2, c3, c4, c5, c6)
    LEFT JOIN card_map    cm USING (customer_id, c2, c3, c4, c5, c6)
    """


def build_card_baselines(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Materialize card_baseline_raw (all) and card_baseline_clean (ex-fraud)."""
    con.execute(f"CREATE OR REPLACE TABLE card_baseline_raw AS {_baseline_query('')}")
    con.execute(
        f"CREATE OR REPLACE TABLE card_baseline_clean AS "
        f"{_baseline_query('AND NOT in_fraud_episode')}"
    )
    n_raw = con.execute("SELECT COUNT(*) FROM card_baseline_raw").fetchone()[0]
    n_cln = con.execute("SELECT COUNT(*) FROM card_baseline_clean").fetchone()[0]
    return n_raw, n_cln


def build_all(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """One-shot build for the feature store. Idempotent (CREATE OR REPLACE)."""
    counts = {}
    counts["fraud_txn_ids"] = build_fraud_txn_ids(con)
    counts["card_map"] = build_card_map(con)
    counts["tx_enriched"] = build_tx_enriched(con)
    n_raw, n_cln = build_card_baselines(con)
    counts["card_baseline_raw"] = n_raw
    counts["card_baseline_clean"] = n_cln
    return counts


# ---------- resolvers -----------------------------------------------------------


def resolve_card_by_txn(
    con: duckdb.DuckDBPyConnection, txn_id: int
) -> CardTuple | None:
    """Given a TransactionID, return the canonical CardTuple.

    This is the correct way to key a card in Sentinel — never by ``-Kn`` string.
    """
    row = con.execute(
        f"""
        SELECT customer_id,
               COALESCE(card2::VARCHAR,'_'), COALESCE(card3::VARCHAR,'_'),
               COALESCE(card4::VARCHAR,'_'), COALESCE(card5::VARCHAR,'_'),
               COALESCE(card6::VARCHAR,'_')
        FROM read_parquet('{TX_PARQUET}')
        WHERE TransactionID = {int(txn_id)}
        """
    ).fetchone()
    if row is None:
        return None
    return CardTuple(*row)


def resolve_cards_by_txns(
    con: duckdb.DuckDBPyConnection, txn_ids: Iterable[int]
) -> list[CardTuple]:
    """Batch version of resolve_card_by_txn — returns unique CardTuples."""
    ids = ",".join(str(int(x)) for x in txn_ids)
    if not ids:
        return []
    rows = con.execute(
        f"""
        SELECT DISTINCT customer_id,
               COALESCE(card2::VARCHAR,'_'), COALESCE(card3::VARCHAR,'_'),
               COALESCE(card4::VARCHAR,'_'), COALESCE(card5::VARCHAR,'_'),
               COALESCE(card6::VARCHAR,'_')
        FROM read_parquet('{TX_PARQUET}')
        WHERE TransactionID IN ({ids})
        """
    ).fetchall()
    return [CardTuple(*r) for r in rows]


@dataclass(frozen=True)
class EmittedCardId:
    """Result of :func:`emit_card_id`: the string + how it was determined."""

    card_id: str
    source: str  # "case_pack" | "closed_case" | "rule_E"


def emit_card_id(
    con: duckdb.DuckDBPyConnection,
    tup: CardTuple,
    *,
    prefer_case_pack: bool = True,
    prefer_closed_case_id: str | None = None,
) -> EmittedCardId:
    """Return a card_id string for the given card tuple.

    Preference order:
      1. If ``prefer_case_pack=True`` and any case_pack row has the same card tuple,
         return its ``card_id`` (source="case_pack").
      2. Else if ``prefer_closed_case_id`` is a specific ClosedCase whose
         ``first_fraud_txn_id`` resolves to this tuple, return that case's
         historical ``card_id`` (source="closed_case").
      3. Else fall back to the rule-E ``card_map`` (source="rule_E").

    The source is recorded so downstream code can decide whether to trust the
    string (in the answer JSON: always use case_pack when available; internally:
    join by tuple, not string).
    """
    if prefer_case_pack:
        row = con.execute(
            f"""
            SELECT c.card_id
            FROM read_csv_auto('{CP_CSV}', header=true) c
            LEFT JOIN read_parquet('{TX_PARQUET}') t
              ON t.TransactionID = c.flagged_txn_id
            WHERE t.customer_id = '{tup.customer_id}'
              AND COALESCE(t.card2::VARCHAR,'_') = '{tup.c2}'
              AND COALESCE(t.card3::VARCHAR,'_') = '{tup.c3}'
              AND COALESCE(t.card4::VARCHAR,'_') = '{tup.c4}'
              AND COALESCE(t.card5::VARCHAR,'_') = '{tup.c5}'
              AND COALESCE(t.card6::VARCHAR,'_') = '{tup.c6}'
            LIMIT 1
            """
        ).fetchone()
        if row:
            return EmittedCardId(row[0], "case_pack")

    if prefer_closed_case_id:
        row = con.execute(
            f"""
            SELECT cc.card_id
            FROM read_csv_auto('{CC_CSV}', header=true) cc
            LEFT JOIN read_parquet('{TX_PARQUET}') t
              ON t.TransactionID = cc.first_fraud_txn_id
            WHERE cc.case_id = '{prefer_closed_case_id}'
              AND t.customer_id = '{tup.customer_id}'
              AND COALESCE(t.card2::VARCHAR,'_') = '{tup.c2}'
              AND COALESCE(t.card3::VARCHAR,'_') = '{tup.c3}'
              AND COALESCE(t.card4::VARCHAR,'_') = '{tup.c4}'
              AND COALESCE(t.card5::VARCHAR,'_') = '{tup.c5}'
              AND COALESCE(t.card6::VARCHAR,'_') = '{tup.c6}'
            LIMIT 1
            """
        ).fetchone()
        if row:
            return EmittedCardId(row[0], "closed_case")

    row = con.execute(
        f"""
        SELECT card_id FROM card_map
        WHERE customer_id = '{tup.customer_id}'
          AND c2 = '{tup.c2}' AND c3 = '{tup.c3}'
          AND c4 = '{tup.c4}' AND c5 = '{tup.c5}' AND c6 = '{tup.c6}'
        """
    ).fetchone()
    if row is None:
        raise LookupError(f"CardTuple not in card_map: {tup}")
    return EmittedCardId(row[0], "rule_E")


# ---------- per-txn feature calc (against a baseline) ---------------------------


def compare_txn_to_baseline(
    con: duckdb.DuckDBPyConnection,
    txn_id: int,
    *,
    baseline: str = "clean",
) -> dict:
    """Return an at-a-glance dict comparing a transaction to its card's baseline.

    ``baseline`` is "clean" or "raw".
    """
    table = f"card_baseline_{baseline}"
    row = con.execute(
        f"""
        SELECT
          t.TransactionID, t.customer_id, t.ts, t.channel, t.ProductCD,
          t.TransactionAmt, t.risk_score,
          t.addr1, t.P_emaildomain, t.R_emaildomain,
          t.device_profile, t.proxy_flag,
          b.n_txns, b.median_amt, b.mad_amt, b.p95_amt, b.max_amt, b.online_share,
          b.home_addr1, b.regions_seen, b.product_codes_seen,
          b.known_device_profiles, b.known_p_emaildomains,
          b.card_id_rankE, cm.card_id AS card_map_id
        FROM tx_enriched t
        LEFT JOIN {table} b
          ON b.customer_id = t.customer_id
         AND b.c2 = t.c2 AND b.c3 = t.c3 AND b.c4 = t.c4
         AND b.c5 = t.c5 AND b.c6 = t.c6
        LEFT JOIN card_map cm
          ON cm.customer_id = t.customer_id
         AND cm.c2 = t.c2 AND cm.c3 = t.c3 AND cm.c4 = t.c4
         AND cm.c5 = t.c5 AND cm.c6 = t.c6
        WHERE t.TransactionID = {int(txn_id)}
        """
    ).fetchone()
    if row is None:
        raise LookupError(f"txn {txn_id} not found")
    cols = [
        "TransactionID", "customer_id", "ts", "channel", "ProductCD",
        "TransactionAmt", "risk_score", "addr1", "P_emaildomain", "R_emaildomain",
        "device_profile", "proxy_flag",
        "n_txns", "median_amt", "mad_amt", "p95_amt", "max_amt", "online_share",
        "home_addr1", "regions_seen", "product_codes_seen",
        "known_device_profiles", "known_p_emaildomains",
        "card_id_rankE", "card_map_id",
    ]
    d = dict(zip(cols, row))

    # Derived deltas.
    if d["median_amt"] is not None and d["mad_amt"] not in (None, 0):
        d["amt_z"] = (d["TransactionAmt"] - d["median_amt"]) / (1.4826 * d["mad_amt"])
    else:
        d["amt_z"] = None
    d["is_new_device_for_card"] = (
        d["channel"] == "online"
        and d["device_profile"] is not None
        and (d["known_device_profiles"] is None
             or d["device_profile"] not in (d["known_device_profiles"] or []))
    )
    d["is_out_of_home_region"] = (
        d["addr1"] is not None
        and d["home_addr1"] is not None
        and d["addr1"] != d["home_addr1"]
    )
    d["is_unseen_product"] = (
        d["ProductCD"] is not None
        and (d["product_codes_seen"] is None
             or d["ProductCD"] not in (d["product_codes_seen"] or []))
    )
    d["is_unseen_p_email"] = (
        d["channel"] == "online"
        and d["P_emaildomain"] is not None
        and (d["known_p_emaildomains"] is None
             or d["P_emaildomain"] not in (d["known_p_emaildomains"] or []))
    )
    return d


if __name__ == "__main__":  # pragma: no cover
    con = connect()
    counts = build_all(con)
    for k, v in counts.items():
        print(f"  {k:<24} {v:,}")
