"""Agent state — the object every LangGraph node reads/writes.

Kept as a plain dict subclass so LangGraph's reducer semantics work cleanly.
Nodes should return a partial dict with only the keys they changed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, TypedDict

from sentinel.evidence.ledger import EvidenceLedger
from sentinel.policy.policy_engine import PolicyDecision


@dataclass
class Telemetry:
    tool_calls: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    latency_seconds: float = 0.0
    node_timings: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def record_node(self, name: str, secs: float) -> None:
        self.node_timings[name] = self.node_timings.get(name, 0.0) + secs
        self.latency_seconds += secs


class AgentState(TypedDict, total=False):
    """LangGraph state, keyed dict."""
    # ---- alert ----
    case_id: str
    txn_id: str
    card_id: str
    customer_id: str
    opened_at: str
    trigger_type: str
    trigger_text: str

    # ---- baseline / detectors ----
    txn_row: dict
    baseline: dict            # from q01_card_window etc.
    graph_signals: dict       # ring_components, near_threshold_burst, recurring_match
    detector_results: list    # list[DetectorResult]
    ledger: EvidenceLedger

    # ---- retrieval / memory ----
    memory_hits: list         # list[RetrievalHit]

    # ---- posterior ----
    prior_log_odds: float
    log_odds: float
    fraud_probability: float
    verdict: Literal["fraud", "legitimate", "uncertain"]
    pattern: str
    shared_element: Optional[str]

    # ---- customer / VOI ----
    voi_plan: dict
    customer_response: Optional[str]
    simulator_paths: list

    # ---- policy / decision ----
    policy_decision: PolicyDecision
    critic_notes: list

    # ---- output ----
    explanation: str
    sar_narrative: Optional[str]
    case_vertex_id: Optional[str]
    answer: dict

    # ---- meta ----
    telemetry: Telemetry
    errors: list
