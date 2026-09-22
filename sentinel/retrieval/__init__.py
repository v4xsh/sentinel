"""Retrieval layer: structural (graph) + semantic (TigerVector) + fused."""

from sentinel.retrieval.hybrid import (  # noqa: F401
    RetrievalHit,
    hybrid_retrieve,
    semantic_closed_cases,
    semantic_policy,
    structural_closed_cases,
)
