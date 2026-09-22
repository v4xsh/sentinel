"""Install q18_ring_wcc into FraudGraph and smoke-test it against one card.

Idempotent — safe to re-run. Fails loudly if TG is unreachable or the
GSQL install returns anything that looks like an error.
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

from sentinel.agent.id_resolver import card_tuple_id
from sentinel.config import REPO_ROOT, TG_GRAPHNAME
from sentinel.graph.client import TGClient

QUERY_PATH = REPO_ROOT / "graph" / "queries" / "q18_ring_wcc.gsql"


def install(cli: TGClient) -> None:
    src = QUERY_PATH.read_text()
    print(f"→ Installing {QUERY_PATH.name} ({len(src)} bytes)…")
    ok, out = cli.gsql_ok(src, graph=TG_GRAPHNAME)
    if not ok:
        print(out); sys.exit(f"GSQL install FAILED: {QUERY_PATH.name}")
    print("✓ installed OK\n")


def smoke(cli: TGClient, seed_card: str) -> None:
    # 60-day window ending at ClosedCase 2016-11-01 (dataset tail).
    end = datetime(2016, 11, 1)
    start = end - timedelta(days=60)
    tuple_id = card_tuple_id(seed_card)
    body = {
        "p_card": tuple_id,
        "p_window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "p_window_end":   end.strftime("%Y-%m-%d %H:%M:%S"),
        "p_degree_cap":   100,
        "p_max_iter":     8,
    }
    print(f"→ ring_wcc({seed_card} → {tuple_id[:40]}…, "
          f"window=[{body['p_window_start']}, {body['p_window_end']}])")
    r = cli.restpp_get(f"query/{TG_GRAPHNAME}/ring_wcc", params=body)
    if not isinstance(r, dict) or "results" not in r:
        print(f"raw response: {r}"); sys.exit("no results field")
    result = {}
    for row in r["results"]:
        result.update(row)
    # Trim card/device lists for readability.
    printable = {k: (v if not isinstance(v, list) or len(v) <= 5
                       else f"[{len(v)} items]") for k, v in result.items()}
    print(textwrap.indent(json.dumps(printable, indent=2, default=str), "  "))


if __name__ == "__main__":
    seed = sys.argv[1] if len(sys.argv) > 1 else "C00259-K1"
    cli = TGClient()
    try:
        install(cli)
        smoke(cli, seed)
    finally:
        cli.close()
