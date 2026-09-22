"""Evidence data classes + the ledger.

Every piece of evidence carries:
  - a natural-language claim
  - a source (graph | document | customer | external)
  - a ref (which query / doc / evidence-request produced it)
  - the entities it rests on
  - a channel (device | region | sequence | amount | history | memory |
    customer | identity_flags | policy)
  - a log_lr (float; may be 0 for informational evidence)
  - optional device_link fields (id_15, id_23, first_seen_on_card,
    activity_window_overlap) — required for device/family-shared items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

EvidenceSource  = Literal["graph", "document", "customer", "external"]
EvidenceChannel = Literal[
    "device", "region", "sequence", "amount", "history",
    "memory", "customer", "identity_flags", "policy",
]


@dataclass
class DeviceLink:
    """Details of a device / device-family link on this card.

    Populated when the evidence carries a device signal (whether the card's
    own device or a shared one). ``activity_window_overlap`` is 'in',
    'before', 'after' or 'none' relative to the linked activity's window
    (e.g. the U1 ring's Aug 27 – Sep 4 window for HHG-011).
    """
    id_15: Optional[str] = None                     # "New" / "Found" / None
    id_23: Optional[str] = None                     # "IP_PROXY:*" / None
    first_seen_on_card: Optional[str] = None        # ISO ts
    activity_window_overlap: str = "none"           # in | before | after | none


@dataclass
class Evidence:
    claim: str
    source: EvidenceSource
    ref: str
    entity_ids: list[str] = field(default_factory=list)
    channel: EvidenceChannel = "history"
    log_lr: float = 0.0
    direction: str = ""                              # "for" | "against" | "neutral"
    device_link: Optional[DeviceLink] = None

    def to_answer_dict(self) -> dict:
        """Shape written to the answer JSON's ``case.evidence`` list."""
        return {
            "claim": self.claim,
            "source": self.source,
            "ref": self.ref,
            "entity_ids": self.entity_ids,
        }


@dataclass
class EvidenceLedger:
    """Ordered list of Evidence items + convenience accessors."""
    items: list[Evidence] = field(default_factory=list)

    def add(self, e: Evidence) -> None:
        self.items.append(e)

    def add_many(self, es: list[Evidence]) -> None:
        self.items.extend(es)

    def channels(self) -> set[str]:
        """Distinct evidence channels with non-trivial log_lr."""
        return {e.channel for e in self.items if abs(e.log_lr) > 1e-9}

    def log_odds_delta(self) -> float:
        """Sum of log-LR contributions from all evidence items."""
        return sum(e.log_lr for e in self.items)

    def to_answer_list(self) -> list[dict]:
        return [e.to_answer_dict() for e in self.items]
