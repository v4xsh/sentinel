"""Idempotent installer for FraudGraph — schema + loading jobs + installed queries.

Uses the JWT-bearer client in ``sentinel.graph.client``. Runs GSQL over the 4.x
``/gsql/v1/statements`` endpoint. Leaves the pre-existing ``Transaction_Fraud``
demo graph on this workspace untouched.

Steps (idempotent):
  1. ``ls`` — verify workspace reachable, list existing graphs.
  2. If ``--reset``: drop FraudGraph and all Sentinel-owned global vertex/edge types.
  3. Execute ``graph/schema.gsql`` — statement-by-statement, tolerating
     "already used by another object" as a no-op.
  4. Install loading jobs from ``graph/loading_jobs.gsql``.
  5. Install every query in ``graph/queries/*.gsql``.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from sentinel.config import TG_GRAPHNAME
from sentinel.graph.client import TGClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("install")

REPO = Path(__file__).resolve().parent.parent
SCHEMA_GSQL = REPO / "graph" / "schema.gsql"
LOADING_JOBS_GSQL = REPO / "graph" / "loading_jobs.gsql"
QUERIES_DIR = REPO / "graph" / "queries"

# Every VERTEX/EDGE our schema introduces (used by --reset).
SENTINEL_VERTICES = [
    "Customer", "PaymentCard", "Transaction",
    "DeviceProfile", "EmailDomain", "BillingRegion", "ProductCode",
    "ClosedCase", "SentinelCase", "PolicyChunk", "RegDocChunk",
]
SENTINEL_EDGES = [
    "OWNS", "MADE",
    "FROM_DEVICE", "PURCHASER_EMAIL", "RECIPIENT_EMAIL", "BILLED_IN", "IN_PRODUCT",
    "NEXT",
    "HOME_REGION", "KNOWN_DEVICE",
    "CC_INVOLVES", "CC_ON_CARD", "CC_ON_CUSTOMER", "CC_CONNECTED_TO", "CC_ON_DEVICE",
    "C_INVOLVES", "C_ON_CARD", "C_ON_CUSTOMER", "C_CONNECTED_TO", "C_ON_DEVICE",
    "C_IN_REGION", "C_SIMILAR_TO_CLOSED", "C_SIMILAR_TO_CASE",
]


def _split_gsql_statements(text: str) -> list[str]:
    """Naive splitter that respects our schema's conventions.

    A statement runs until a semicolon (no semis in this file) OR a blank line
    following a line that started with ``USE`` / ``CREATE`` / ``DROP`` /
    ``ALTER``. Multi-line CREATE VERTEX blocks are held together by the parens.
    """
    # Strip comments (// ...).
    stripped = "\n".join(re.sub(r"//.*", "", ln) for ln in text.splitlines())
    # Split by top-level keyword boundaries. We assume each CREATE / DROP /
    # ALTER / USE begins at column 0.
    pattern = re.compile(r"(?m)^(?=(?:USE|CREATE|DROP|ALTER|INSTALL)\b)")
    parts = pattern.split(stripped)
    stmts = [p.strip() for p in parts if p.strip()]
    return stmts


def _run_tolerant(tg: TGClient, stmt: str, graph: str | None = None) -> tuple[bool, str]:
    """Run one statement; treat 'already used'/'already exists' as OK."""
    _ok, out = tg.gsql_ok(stmt, graph=graph)
    low = out.lower()
    if "already used by another object" in low or "already exists" in low:
        return True, out
    return _ok, out


def _reset(tg: TGClient) -> None:
    logger.warning("--reset: dropping FraudGraph + all Sentinel-owned globals.")
    # Drop FraudGraph first (frees vertex/edge references).
    _run_tolerant(tg, f"DROP GRAPH {TG_GRAPHNAME}")
    # Drop each edge, then each vertex (edges reference vertices).
    for et in SENTINEL_EDGES:
        _run_tolerant(tg, f"DROP EDGE {et}")
    for vt in SENTINEL_VERTICES:
        _run_tolerant(tg, f"DROP VERTEX {vt}")


def install(force: bool = False, reset: bool = False, schema_only: bool = False) -> int:
    tg = TGClient()

    # 1. Sanity.
    _ok, ls_before = tg.gsql_ok("ls")
    already = TG_GRAPHNAME in ls_before

    if reset:
        _reset(tg)
        already = False
    elif force and already:
        _run_tolerant(tg, f"DROP GRAPH {TG_GRAPHNAME}")
        already = False

    # 2. Schema — statement by statement.
    schema_text = SCHEMA_GSQL.read_text(encoding="utf-8")
    stmts = _split_gsql_statements(schema_text)
    logger.info("Schema has %d statements.", len(stmts))
    total_ok = 0
    total_skip = 0
    total_fail = 0
    for stmt in stmts:
        head = stmt.splitlines()[0][:80]
        ok, out = _run_tolerant(tg, stmt)
        low = out.lower()
        if "already used" in low or "already exists" in low:
            total_skip += 1
            logger.info("SKIP (exists): %s", head)
        elif ok:
            total_ok += 1
            logger.info("OK          : %s", head)
        else:
            total_fail += 1
            logger.error("FAIL        : %s", head)
            logger.error("  %s", out.strip().replace("\n", "\n  ")[:2000])
    logger.info("Schema summary: ok=%d skip=%d fail=%d", total_ok, total_skip, total_fail)
    if total_fail > 0:
        logger.error("Schema install had failures; aborting.")
        return 2

    if schema_only:
        # Print final ls.
        _ok, ls_after = tg.gsql_ok("ls")
        return 0

    # 3. Loading jobs.
    if LOADING_JOBS_GSQL.exists():
        logger.info("Installing loading jobs ...")
        jobs_text = LOADING_JOBS_GSQL.read_text()
        # Loading jobs must run inside graph scope.
        ok, out = tg.gsql_ok(jobs_text, graph=TG_GRAPHNAME)
        _log_head("loading jobs", out, 40)
    else:
        logger.info("No loading_jobs.gsql yet.")

    # 4. Queries — install one at a time.
    if QUERIES_DIR.exists():
        for q in sorted(QUERIES_DIR.glob("*.gsql")):
            logger.info("Installing query %s ...", q.name)
            ok, out = tg.gsql_ok(q.read_text(), graph=TG_GRAPHNAME)
            _log_head(f"query {q.stem}", out, 20)

    return 0


def _log_head(label: str, text: str, n: int) -> None:
    print(f"\n--- {label} ---")
    for line in text.splitlines()[:n]:
        print(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="drop FraudGraph before install (keeps global types)")
    ap.add_argument("--reset", action="store_true",
                    help="drop FraudGraph AND all Sentinel-owned global types")
    ap.add_argument("--schema-only", action="store_true")
    args = ap.parse_args()
    return install(force=args.force, reset=args.reset, schema_only=args.schema_only)


if __name__ == "__main__":
    sys.exit(main())
