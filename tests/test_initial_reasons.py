"""Initial action reasons must not reference the customer response.

The initial recommendation is by definition pre-response; leaking phrases
like 'customer denied' / 'confirmed' / 'no reply' / 'confirmed fraud'
in an initial reason lets the reader believe the initial call was
triggered by a customer signal — which it wasn't. Sweep every produced
answer file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

LEAK_RE = re.compile(
    r"customer\s+(denied|confirmed|reply|said|confirms)"
    r"|no[_\s-]reply"
    r"|confirmed[_\s-]?(fraud|as[_\s-]fraud)"
    r"|denied\s+—\s+block",
    re.I,
)


@pytest.mark.parametrize("dirname", ["cases", "cases_extra"])
def test_no_customer_response_in_initial_reasons(dirname: str):
    d = REPO / dirname
    if not d.exists():
        pytest.skip(f"{dirname} not present")
    files = sorted(p for p in d.glob("*.json")
                   if p.name.startswith(("HHG-", "EXTRA-")))
    if not files:
        pytest.skip(f"no answer files in {dirname}")
    violations = []
    for p in files:
        a = json.loads(p.read_text())
        for act in a["next_best_actions"]["initial"]:
            r = act.get("reason", "") or ""
            if LEAK_RE.search(r):
                violations.append(f"{p.name}: initial action {act['action']!r} "
                                  f"reason leaks response — {r[:120]!r}")
    assert not violations, "\n".join(violations)


def test_pre_response_block_card_reason_text():
    """Any initial BLOCK_CARD must cite the §6 pre-response gate literally."""
    d = REPO / "cases"
    if not d.exists():
        pytest.skip("cases/ not present")
    files = sorted(d.glob("HHG-*.json"))
    if not files:
        pytest.skip("no HHG answer files")
    violations = []
    for p in files:
        a = json.loads(p.read_text())
        for act in a["next_best_actions"]["initial"]:
            if act["action"] == "BLOCK_CARD":
                r = act.get("reason", "") or ""
                if ("posterior" not in r.lower() or
                    "independent channels" not in r.lower() or
                    "before customer contact" not in r.lower()):
                    violations.append(f"{p.name}: initial BLOCK_CARD reason "
                                      f"missing the §6-pre-response phrasing "
                                      f"— {r[:140]!r}")
    assert not violations, "\n".join(violations)
