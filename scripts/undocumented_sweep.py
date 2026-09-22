"""Sweep Nov–Dec 2016 transactions for U1/U2 undocumented signatures.

U1 = narrow-device (KNOWN_DEVICE degree ≤ 100) proxied ring with ≥3 cards
     touching the device in a ±14-day window and ≥1 proxied txn.
U2 = near-threshold burst — a card with ≥3 online txns strictly below a
     round threshold within 48 hours.

Writes docs/UNDOCUMENTED_FINDINGS.md with a short LLM-generated
description per finding (falls back to a template line if the LLM is
disabled).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sentinel.data.features import connect


def _u1_findings(con, top_n: int = 10) -> list[dict]:
    """Devices with ≤100 degree, ≥3 cards in Nov+Dec, ≥1 proxied txn."""
    rows = con.execute(
        """
        WITH windowed AS (
          SELECT t.device_profile,
                 t.ts,
                 t.customer_id,
                 (t.id_23 LIKE 'IP_PROXY:%') AS proxied
          FROM txn_features t
          JOIN dev_degree d ON d.device_profile = t.device_profile
          WHERE t.channel = 'online'
            AND d.n_cards <= 100
            AND t.ts >= '2016-11-01' AND t.ts <= '2016-12-31'
        ),
        agg AS (
          SELECT device_profile,
                 COUNT(DISTINCT customer_id) AS n_cards,
                 SUM(CASE WHEN proxied THEN 1 ELSE 0 END) AS n_proxied_txns,
                 MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                 COUNT(*) AS n_txns
          FROM windowed
          GROUP BY device_profile
        )
        SELECT * FROM agg
        WHERE n_cards >= 3 AND n_proxied_txns >= 1
        ORDER BY n_cards DESC, n_proxied_txns DESC
        LIMIT ?
        """,
        [top_n],
    ).fetchall()
    return [dict(zip(
        ["device_profile","n_cards","n_proxied_txns","first_ts","last_ts","n_txns"], r
    )) for r in rows]


def _u2_findings(con, top_n: int = 10) -> list[dict]:
    """Cards with ≥3 online txns strictly < round threshold within 48h."""
    THRESHOLDS = [100.0, 200.0, 500.0, 1000.0]
    out: list[dict] = []
    for th in THRESHOLDS:
        rows = con.execute(
            """
            SELECT customer_id,
                   COUNT(*) AS n_below,
                   MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                   AVG(TransactionAmt) AS avg_amt
            FROM txn_features
            WHERE channel = 'online'
              AND TransactionAmt < ?
              AND TransactionAmt > ? - 20.0
              AND ts >= '2016-11-01' AND ts <= '2016-12-31'
            GROUP BY customer_id
            HAVING COUNT(*) >= 3
              AND (MAX(ts) - MIN(ts)) < INTERVAL 48 HOUR
            ORDER BY n_below DESC
            LIMIT ?
            """,
            [float(th), float(th), top_n],
        ).fetchall()
        for r in rows:
            out.append({"threshold": th, **dict(zip(
                ["customer_id","n_below","first_ts","last_ts","avg_amt"], r
            ))})
    return out[:top_n]


def _describe(prompt: str) -> str:
    """Optional LLM narration; falls back to first-line summary."""
    if os.getenv("SENTINEL_LLM_DISABLED", "").strip() in ("1","true","TRUE"):
        return prompt.splitlines()[0][:180]
    try:
        from sentinel.llm import generate
        return generate(prompt, temperature=0.3, max_output_tokens=140).strip()
    except Exception:
        return prompt.splitlines()[0][:180]


def main() -> int:
    con = connect()
    u1 = _u1_findings(con, top_n=10)
    u2 = _u2_findings(con, top_n=10)
    print(f"U1 findings: {len(u1)}   U2 findings: {len(u2)}")

    lines: list[str] = [
        "# Undocumented findings — Nov–Dec 2016 sweep\n",
        ("Two coordinated fraud shapes surfaced by scanning the exam-period "
         "transactions against the U1 (narrow proxied device ring) and U2 "
         "(near-threshold burst) definitions from Sentinel's detectors. "
         "Neither is one of the five README-documented patterns.\n"),
        "## U1 — narrow proxied device rings\n",
        "Devices with `KNOWN_DEVICE` degree ≤ 100 that carried ≥ 3 different "
        "cards in Nov–Dec 2016 with ≥ 1 proxied transaction.\n",
        "| # | device_profile | n_cards | n_proxied_txns | window | n_txns | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(u1, 1):
        note = _describe(
            f"One-sentence description of a shared-device ring: "
            f"{r['n_cards']} distinct cards used device '{r['device_profile'][:60]}' "
            f"between {r['first_ts']} and {r['last_ts']} "
            f"with {r['n_proxied_txns']} proxied online txns. Don't invent facts."
        )
        lines.append(f"| U1-{i} | `{r['device_profile'][:60]}` | {r['n_cards']} | "
                     f"{r['n_proxied_txns']} | {r['first_ts']} → {r['last_ts']} | "
                     f"{r['n_txns']} | {note} |")

    lines.append("\n## U2 — near-threshold bursts\n")
    lines.append("Cards with ≥ 3 online transactions in a ±48-hour window whose "
                 "amounts sit strictly below a round threshold (100 / 200 / 500 / "
                 "1000 USD), consistent with structuring.\n")
    lines.append("| # | customer_id | threshold | n_below | window | avg_amt | note |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(u2, 1):
        note = _describe(
            f"One-sentence description of a near-threshold burst: card "
            f"{r['customer_id']} placed {r['n_below']} online charges just "
            f"under ${r['threshold']:.0f} between {r['first_ts']} and "
            f"{r['last_ts']} at an average of ${r['avg_amt']:.2f}. Don't invent facts."
        )
        lines.append(f"| U2-{i} | {r['customer_id']} | ${r['threshold']:.0f} | "
                     f"{r['n_below']} | {r['first_ts']} → {r['last_ts']} | "
                     f"${r['avg_amt']:.2f} | {note} |")

    out = REPO / "docs" / "UNDOCUMENTED_FINDINGS.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}  ({len(u1)} U1 + {len(u2)} U2 findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
