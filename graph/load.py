"""Load reduced CSVs from ``data/graph_load/`` into FraudGraph via chunked
HTTP POST to ``/restpp/ddl/FraudGraph``.

Two modes:
    ``--sample 10000``   load only the first 10k rows of transactions.csv (and
                          the corresponding subset of dependent edge files)
    ``--full``           load everything.

Progress is logged after every chunk. Final counts are verified against Parquet.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

from sentinel.config import GRAPH_LOAD, PARQUET, TG_GRAPHNAME
from sentinel.graph.client import TGClient
from sentinel.data.features import connect as duck_connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("load")


# Each entry: (job_name, csv_file, filename_var, expected_count_from_parquet)
JOBS: list[tuple[str, str, str]] = [
    ("load_customers",       "customers.csv",        "f"),
    ("load_payment_cards",   "payment_cards.csv",    "f"),
    ("load_device_profiles", "device_profiles.csv",  "f"),
    ("load_email_domains",   "email_domains.csv",    "f"),
    ("load_billing_regions", "billing_regions.csv",  "f"),
    ("load_product_codes",   "product_codes.csv",    "f"),
    ("load_closed_cases",    "closed_cases.csv",     "f"),
    ("load_transactions",    "transactions.csv",     "f"),
    ("load_owns",            "edges_owns.csv",       "f"),
    ("load_made",            "edges_made.csv",       "f"),
    ("load_from_device",     "edges_from_device.csv","f"),
    ("load_purchaser_email", "edges_purchaser_email.csv", "f"),
    ("load_recipient_email", "edges_recipient_email.csv", "f"),
    ("load_billed_in",       "edges_billed_in.csv",  "f"),
    ("load_in_product",      "edges_in_product.csv", "f"),
    ("load_next",            "edges_next.csv",       "f"),
    ("load_home_region",     "edges_home_region.csv","f"),
    ("load_known_device",    "edges_known_device.csv","f"),
    ("load_cc_involves",     "edges_cc_involves.csv","f"),
    ("load_cc_on_card",      "edges_cc_on_card.csv", "f"),
    ("load_cc_on_customer",  "edges_cc_on_customer.csv","f"),
    ("load_cc_connected_to", "edges_cc_connected_to.csv","f"),
    ("load_cc_on_device",    "edges_cc_on_device.csv","f"),
]


def _iter_lines(path: Path):
    """Yield (header, rest_iterator) so we can prepend the header to each chunk."""
    with open(path, "rb") as f:
        header = f.readline().rstrip(b"\n").rstrip(b"\r")
        for line in f:
            yield line.rstrip(b"\n").rstrip(b"\r")
    return header


def _load_file(
    tg: TGClient,
    graph: str,
    job: str,
    csv_path: Path,
    filename_var: str,
    chunk_rows: int,
    max_rows: int | None,
) -> dict:
    """Stream a CSV file into a loading job, chunk_rows at a time.

    IMPORTANT: TG's ``/restpp/ddl`` doesn't honor HEADER=true, so we skip the
    header line here (never send it) — otherwise the header becomes a data row
    and can create ghost vertices with column names as IDs.
    """
    def line_iter():
        n_yielded = 0
        with open(csv_path, "rb") as fh:
            fh.readline()  # skip header — never sent
            for raw in fh:
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                yield line
                n_yielded += 1
                if max_rows is not None and n_yielded >= max_rows:
                    return

    sent = 0
    total_stats: dict = {}
    t0 = time.time()

    def on_progress(sent_rows: int, res: dict) -> None:
        stats = res.get("results", [{}])[0].get("statistics", {}) if isinstance(res, dict) else {}
        vc = stats.get("vertex", [])
        ec = stats.get("edge", [])
        v_ok = sum(x.get("validObject", 0) for x in vc)
        e_ok = sum(x.get("validObject", 0) for x in ec)
        rate = sent_rows / max(time.time() - t0, 0.001)
        logger.info(
            "  chunk ok: sent=%d  vertices+=%d  edges+=%d  (%.0f rows/s)",
            sent_rows, v_ok, e_ok, rate,
        )
        # accumulate
        for x in vc:
            k = f"vertex:{x['typeName']}"
            total_stats[k] = total_stats.get(k, 0) + x.get("validObject", 0)
        for x in ec:
            k = f"edge:{x['typeName']}"
            total_stats[k] = total_stats.get(k, 0) + x.get("validObject", 0)

    results = tg.run_loading_job_from_iter(
        graph=graph,
        job=job,
        filename_var=filename_var,
        lines=line_iter(),
        chunk_rows=chunk_rows,
        header=None,  # DDL endpoint ignores HEADER="true"; we already stripped
        on_progress=on_progress,
    )
    return {"chunks": len(results), "stats": total_stats}


def load_all(
    *,
    sample_rows: int | None = None,
    only: list[str] | None = None,
    chunk_rows: int = 25_000,
) -> None:
    tg = TGClient()
    graph = TG_GRAPHNAME
    grand: dict[str, dict] = {}
    for job, csv_name, fname in JOBS:
        if only and job not in only:
            continue
        csv_path = GRAPH_LOAD / csv_name
        if not csv_path.exists():
            logger.warning("skip %s: %s does not exist", job, csv_path)
            continue

        # Only apply the sample cap to transactions + txn-dependent edges.
        max_rows = None
        if sample_rows is not None:
            # For sample mode, cap the *transactions* rows and let every other file
            # load in full — vertex catalogs (customers, cards, devices, emails,
            # regions, products, closed_cases) are small; edges to the loaded
            # transactions will simply skip rows whose endpoints don't exist.
            if csv_name == "transactions.csv":
                max_rows = sample_rows

        logger.info("→ %s  (%s)  chunk=%d  max=%s",
                    job, csv_name, chunk_rows, max_rows or "all")
        result = _load_file(tg, graph, job, csv_path, fname, chunk_rows, max_rows)
        grand[job] = result
        logger.info("  done: %d chunks, cumulative stats=%s", result["chunks"], result["stats"])

    logger.info("=" * 78)
    logger.info("Grand totals:")
    for job, res in grand.items():
        logger.info("  %-30s chunks=%d  %s", job, res["chunks"], res["stats"])


def verify_counts(*, sample_rows: int | None = None) -> None:
    """Compare TigerGraph counts to Parquet + graph_load counts."""
    tg = TGClient()
    graph = TG_GRAPHNAME
    con = duck_connect()

    logger.info("=" * 78)
    logger.info("Verify vertex counts:")
    vt_expected = {
        "Customer":      con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % (GRAPH_LOAD/"customers.csv")).fetchone()[0],
        "PaymentCard":   con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % (GRAPH_LOAD/"payment_cards.csv")).fetchone()[0],
        "DeviceProfile": con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % (GRAPH_LOAD/"device_profiles.csv")).fetchone()[0],
        "EmailDomain":   con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % (GRAPH_LOAD/"email_domains.csv")).fetchone()[0],
        "BillingRegion": con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % (GRAPH_LOAD/"billing_regions.csv")).fetchone()[0],
        "ProductCode":   con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % (GRAPH_LOAD/"product_codes.csv")).fetchone()[0],
        "ClosedCase":    con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % (GRAPH_LOAD/"closed_cases.csv")).fetchone()[0],
    }
    tx_expected = sample_rows if sample_rows else 590_742
    vt_expected["Transaction"] = tx_expected

    for vt, exp in vt_expected.items():
        try:
            got = tg.get_graph_vertex_count(graph, vt)
        except Exception as e:
            got = f"ERROR: {e}"
        logger.info("  %-16s  parquet=%-8s  graph=%s", vt, exp, got)

    logger.info("Verify edge counts:")
    et_files = {
        "OWNS":              GRAPH_LOAD/"edges_owns.csv",
        "MADE":              GRAPH_LOAD/"edges_made.csv",
        "FROM_DEVICE":       GRAPH_LOAD/"edges_from_device.csv",
        "PURCHASER_EMAIL":   GRAPH_LOAD/"edges_purchaser_email.csv",
        "RECIPIENT_EMAIL":   GRAPH_LOAD/"edges_recipient_email.csv",
        "BILLED_IN":         GRAPH_LOAD/"edges_billed_in.csv",
        "IN_PRODUCT":        GRAPH_LOAD/"edges_in_product.csv",
        "NEXT":              GRAPH_LOAD/"edges_next.csv",
        "HOME_REGION":       GRAPH_LOAD/"edges_home_region.csv",
        "KNOWN_DEVICE":      GRAPH_LOAD/"edges_known_device.csv",
        "CC_INVOLVES":       GRAPH_LOAD/"edges_cc_involves.csv",
        "CC_ON_CARD":        GRAPH_LOAD/"edges_cc_on_card.csv",
        "CC_ON_CUSTOMER":    GRAPH_LOAD/"edges_cc_on_customer.csv",
        "CC_CONNECTED_TO":   GRAPH_LOAD/"edges_cc_connected_to.csv",
        "CC_ON_DEVICE":      GRAPH_LOAD/"edges_cc_on_device.csv",
    }
    for et, path in et_files.items():
        exp = con.execute("SELECT COUNT(*) FROM read_csv_auto('%s')" % path).fetchone()[0]
        try:
            got = tg.get_graph_edge_count(graph, et)
        except Exception as e:
            got = f"ERROR: {e}"
        logger.info("  %-18s  csv=%-8s  graph=%s", et, exp, got)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None,
                    help="load only first N transactions (and dependent edges)")
    ap.add_argument("--full", action="store_true", help="alias for no --sample")
    ap.add_argument("--only", nargs="*", help="only run these job names")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip loading, just report counts")
    ap.add_argument("--chunk", type=int, default=25_000)
    args = ap.parse_args()

    if not args.verify_only:
        load_all(sample_rows=args.sample, only=args.only, chunk_rows=args.chunk)
    verify_counts(sample_rows=args.sample if not args.full else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
