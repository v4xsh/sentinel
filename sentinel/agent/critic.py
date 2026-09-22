"""Adversarial critic — Gemini, but with deterministic post-processing.

The critic reads the evidence ledger (+ each device link's id_15/id_23/
first_seen_on_card/activity_window_overlap) and returns a list of concerns
of the form ``{concern, targets, severity}``. It is NOT allowed to change
the fraud_probability, policy, or actions. Its only downstream effect is
that concerns appear in the case's ``critic_notes`` field and can gate
whether the agent decides to gather more evidence.

The template mirrors the README HHG-011 analysis: the critic must reason
from device_link fields before it can label a shared-device claim as
'in-window ownership'. If the device was first seen on this card *after*
the ring's activity window, the link is 'circumstantial' — a rebuttal.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sentinel.evidence.ledger import DeviceLink, EvidenceLedger
from sentinel.llm import generate

logger = logging.getLogger(__name__)


CRITIC_PROMPT = """\
You are a fraud investigator reviewing an agent's evidence ledger. Your job
is to find weaknesses — evidence that is over-weighted, mis-labelled, or
that a defense attorney could rebut. You may NOT change the fraud
probability or the policy actions; you only surface concerns.

For every device / shared-device evidence item, verify:

  1. `id_15` — Vesta's own new-device flag (values: "New", "Found", null).
  2. `id_23` — proxy label (values start with "IP_PROXY:" if any).
  3. `first_seen_on_card` — when this device first appeared on THIS card.
  4. `activity_window_overlap` — 'in' | 'before' | 'after' | 'none'
     relative to the linked activity window.

Rules (HHG-011 template):
  - If a device is shared with fraud cases but its `first_seen_on_card`
    is AFTER the fraud activity ended (activity_window_overlap='after'),
    it is a circumstantial link, not evidence of coordinated fraud.
  - If `id_15='New'` AND `id_23` starts with 'IP_PROXY', that's a strong
    ATO signal — do not dismiss.
  - If a claim cites 'shared device' without a `device_link` block, flag it
    as unfounded.

Return STRICT JSON: {{ "concerns": [{{"concern":"...","targets":[...],"severity":"low|med|high"}}] }}.
Return an empty list if the ledger looks solid.

Case ID: {case_id}
Ledger:
{ledger_json}
"""


def _ledger_dump(ledger: EvidenceLedger) -> list[dict]:
    out = []
    for e in ledger.items:
        d = {
            "claim": e.claim, "source": e.source, "ref": e.ref,
            "channel": e.channel, "log_lr": round(e.log_lr, 3),
            "direction": e.direction,
        }
        if e.device_link:
            d["device_link"] = {
                "id_15": e.device_link.id_15,
                "id_23": e.device_link.id_23,
                "first_seen_on_card": e.device_link.first_seen_on_card,
                "activity_window_overlap": e.device_link.activity_window_overlap,
            }
        out.append(d)
    return out


def run_critic(case_id: str, ledger: EvidenceLedger,
               use_llm: bool = True) -> list[dict]:
    """Return a list of concerns. Empty if the critic is satisfied."""
    concerns: list[dict] = []

    # ---- Deterministic pass: fires regardless of LLM availability. ----
    for e in ledger.items:
        if e.channel == "device":
            dl: DeviceLink | None = e.device_link
            if dl is None and "shared" in e.claim.lower():
                concerns.append({
                    "concern": "Shared-device claim without a device_link block.",
                    "targets": [e.ref],
                    "severity": "med",
                })
                continue
            if dl and dl.activity_window_overlap == "after":
                concerns.append({
                    "concern": (
                        f"Device first_seen_on_card={dl.first_seen_on_card} is "
                        "AFTER the linked activity window; classify link as "
                        "circumstantial (HHG-011 template)."
                    ),
                    "targets": [e.ref],
                    "severity": "high",
                })

    # ---- Optional LLM pass. Off by default in tests to avoid tokens. ----
    if use_llm:
        try:
            prompt = CRITIC_PROMPT.format(
                case_id=case_id,
                ledger_json=json.dumps(_ledger_dump(ledger), indent=2),
            )
            raw = generate(prompt, cheap=True, temperature=0.1,
                           max_output_tokens=600)
            # Extract JSON from the response.
            i, j = raw.find("{"), raw.rfind("}")
            if i >= 0 and j > i:
                obj = json.loads(raw[i:j + 1])
                for c in obj.get("concerns", []):
                    if isinstance(c, dict) and c.get("concern"):
                        concerns.append(c)
        except Exception as exc:  # noqa: BLE001
            logger.info("critic LLM disabled or failed: %s", exc)
    return concerns
