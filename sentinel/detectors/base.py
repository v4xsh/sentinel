"""Detector base classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sentinel.evidence.ledger import Evidence


@dataclass
class DetectorContext:
    """Everything a detector needs from the DuckDB feature store + graph."""
    case_id: str
    txn_id: str                       # e.g. "T3478561"
    txn_row: dict                     # a row from txn_features / tx_enriched
    card_tuple_id: str                # "C13487|555.0|150.0|..."
    customer_id: str
    opened_at: str                    # ISO
    trigger_type: str
    trigger_text: str
    lr_table: dict                    # sentinel/evidence/lr_table.json contents
    # Prior context lookups (populated by the agent's gather_baseline node):
    baseline_summary: dict = field(default_factory=dict)   # from card_window
    memory_hits: list = field(default_factory=list)        # RetrievalHit list
    graph_signals: dict = field(default_factory=dict)      # e.g. ring_components result
    # DuckDB connection (shared) — detectors may need to run cheap queries.
    con: Optional[object] = None


@dataclass
class DetectorResult:
    pattern: Optional[str]                     # README enum or None
    affected_txn_ids: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    # Detector-specific bit set to flag pattern-specific fields on the case.
    tags: set[str] = field(default_factory=set)

    def is_hit(self) -> bool:
        return self.pattern is not None or bool(self.evidence)
