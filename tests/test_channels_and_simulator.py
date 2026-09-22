"""Tests for feature→channel mapping + simulator behaviour.

  1. HHG-014's evidence carries ≥2 independent channels (needed for the
     two-channel verdict gate).
  2. On a healthy graph run the simulator never returns ``no_reply`` —
     that response is reserved for the graph-unreachable branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_hhg014_ledger_has_multi_channels():
    """Load the persisted HHG-014 answer and count distinct channels in its
    evidence list. Should be ≥ 2. The alert_model itself doesn't stamp channel
    on the JSON evidence entries (only source+ref+claim survive), so we
    approximate by inspecting the ref prefix + claim keywords used by the
    frozen mapping."""
    p = REPO / "cases" / "HHG-014.json"
    if not p.exists():
        pytest.skip("HHG-014.json not written yet")
    d = json.loads(p.read_text())
    channels: set[str] = set()
    for e in d["case"]["evidence"]:
        r = (e.get("ref") or "")
        if r.startswith("alert_model:"):
            name = r.split(":", 1)[1]
            from sentinel.agent.graph import FEATURE_CHANNEL
            channels.add(FEATURE_CHANNEL.get(name, "history"))
        elif "ring_components" in r or "device" in r.lower():
            channels.add("device")
        elif "region" in r.lower():
            channels.add("region")
        elif "recurring" in r.lower() or "amount" in r.lower() or "amt" in r.lower():
            channels.add("amount")
    assert len(channels) >= 2, f"HHG-014 has only {channels}"


def test_simulator_no_no_reply_on_healthy():
    """On a state with no ``errors`` recorded, ``_pick_response_by_rule``
    always returns one of {denied, confirmed} — never no_reply."""
    from sentinel.agent.graph import _pick_response_by_rule
    for p_init in (0.1, 0.4, 0.6, 0.9):
        for tags in [set(), {"legit_trip_hint"}, {"testing_sequence_fires"}]:
            state = {
                "p_initial": p_init, "fraud_probability": p_init,
                "_tags": tags, "graph_signals": {"ring_components": {}},
                "ledger": _fake_ledger(),
            }
            resp, _ = _pick_response_by_rule(state)
            assert resp in ("denied", "confirmed"), \
                f"simulator returned {resp!r} on healthy p_init={p_init}, tags={tags}"


def _fake_ledger():
    from sentinel.evidence.ledger import EvidenceLedger
    return EvidenceLedger()
