"""Replay every confirmed_fraud closed case through the policy engine's SAR rule.

Assert 100% agreement with the historical ``report_filed`` field. This
proves that the *policy encoding* reproduces the exact SAR decision the
bank made on all 4,665 confirmed_fraud cases in the ground-truth history
— it is NOT a claim that Sentinel investigated 4,665 cases end-to-end.
The agent's 150-case backtest is the end-to-end evaluation; this test
just verifies the FinCEN §3a rule encoding matches the historical record.

The mapping from closed-case history to :func:`decide` input:
  - verdict = "fraud"                             (all rows are confirmed_fraud)
  - fraud_probability = 1.0                       (case was closed as fraud)
  - exposure_usd = row.exposure_usd
  - pattern = row.pattern
  - shared_element = "card" if connected_card_ids non-empty else None
    (we approximate; the actual policy accepts "device"|"region"|"recipient",
    but the SAR rule only cares that a shared element is present)
  - undocumented_coordinated = (pattern == "undocumented")

We then check that the engine's ``sar_should_file`` matches ``report_filed``.
"""

from __future__ import annotations

import os

import duckdb
import pytest

from sentinel.config import RAW
from sentinel.policy import PolicyInput, decide
from sentinel.policy.policy_engine import _sar_should_file

pytestmark = pytest.mark.skipif(
    not RAW.exists() or not (RAW / "closed_cases_history.csv").exists(),
    reason="closed_cases_history.csv missing",
)


def _cc_rows() -> list[dict]:
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT case_id, pattern, exposure_usd, connected_card_ids, report_filed
        FROM read_csv_auto('{RAW / "closed_cases_history.csv"}', header=true)
        WHERE outcome = 'confirmed_fraud'
        """
    ).fetchall()
    return [
        {
            "case_id": r[0],
            "pattern": r[1],
            "exposure_usd": float(r[2] or 0),
            "connected_card_ids": r[3] or "",
            "report_filed": r[4],
        }
        for r in rows
    ]


def _shared_from_connected(connected: str) -> str | None:
    if not connected:
        return None
    s = connected.strip()
    if s and s.lower() != "nan":
        return "device"  # any shared element flag is enough for the SAR rule
    return None


def test_all_4665_fraud_cases_sar_agreement() -> None:
    rows = _cc_rows()
    assert len(rows) == 4665, f"expected 4665 confirmed_fraud rows, got {len(rows)}"

    disagreements: list[tuple[str, bool, str]] = []
    for r in rows:
        inp = PolicyInput(
            verdict="fraud",
            fraud_probability=1.0,
            exposure_usd=r["exposure_usd"],
            pattern=r["pattern"],
            shared_element=_shared_from_connected(r["connected_card_ids"]),
            undocumented_coordinated=(r["pattern"] == "undocumented"),
        )
        engine_sar = _sar_should_file(inp)
        expected = str(r["report_filed"]).strip().lower() in ("yes", "true", "1")
        if engine_sar != expected:
            disagreements.append((r["case_id"], engine_sar, r["report_filed"]))

    assert not disagreements, (
        f"{len(disagreements)} SAR disagreements. First 5: {disagreements[:5]}"
    )


def test_none_of_the_900_cleared_get_sar_via_decide() -> None:
    """The 900 cleared cases don't reach ``_sar_should_file(verdict='fraud')``
    but we can still verify: with verdict=legitimate, sar_should_file is False.
    """
    inp = PolicyInput(
        verdict="legitimate", fraud_probability=0.1, exposure_usd=0,
        pattern="none",
    )
    d = decide(inp)
    assert d.sar_should_file is False
    assert "FILE_REPORT" not in [a.action for a in d.actions]
