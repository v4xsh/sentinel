"""Hybrid retrieval — structural + semantic, with as_of time-gating.

Every function takes ``as_of: datetime`` and returns only ClosedCases whose
``closed_at < as_of`` and SentinelCases whose ``opened_at < as_of``. This is
critical for backtests: at investigation time the agent must not see cases
that closed AFTER the alert it's investigating.

Retrieval sources:
  - **Structural**: ``closed_cases_touching(card, device, region, window)`` —
    the installed GSQL query. Returns ClosedCases attached to the case's card,
    device, or region.
  - **Semantic**: TigerVector ``search_top_k_similarity`` over
    ``ClosedCase.notes_embedding`` (and, once memory is populated,
    ``SentinelCase.summary_embedding``).
  - **Policy / RegDoc**: ``PolicyChunk.embedding`` + ``RegDocChunk.embedding``
    for grounding the SAR narrative writer in Phase 5.

Fusion: reciprocal rank fusion (RRF) — de-duped and sorted by fused score.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sentinel.graph.client import TGClient
from sentinel.graph.token import export_to_env

logger = logging.getLogger(__name__)


@dataclass
class RetrievalHit:
    """One retrieved item — a ClosedCase, SentinelCase, or doc chunk."""

    id: str                              # e.g. "CC-2649", "CASE-2016-1187", "POL:policy:003"
    kind: str                            # "closed_case" | "sentinel_case" | "policy" | "regdoc"
    score: float                         # fused score (higher is better)
    source: str                          # "structural" | "semantic" | "fused"
    attrs: dict[str, Any] = field(default_factory=dict)


# ---- helpers ------------------------------------------------------------


def _as_iso(dt: datetime | str) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ---- structural retrieval ----------------------------------------------


def _vertex_exists(tg: TGClient, vtype: str, vid: str) -> bool:
    """Cheap presence check via a single restpp GET."""
    try:
        r = tg.restpp_get(f"graph/FraudGraph/vertices/{vtype}/{vid}")
    except Exception:  # noqa: BLE001
        return False
    return not r.get("error") and bool(r.get("results"))


def structural_closed_cases(
    tg: TGClient,
    *,
    card_id: str,
    device_profile: str | None,
    addr1: str | None,
    window_start: datetime | str,
    window_end: datetime | str,
    as_of: datetime | str,
) -> list[RetrievalHit]:
    """Call the installed ``closed_cases_touching`` query and time-gate."""
    # Call the query directly — TG returns the union of the three legs; if
    # a device/region isn't a real vertex the query simply returns nothing
    # for that leg (with a "Failed to convert user vertex id" logged, which
    # we tolerate). Skipping the two prior _vertex_exists GETs saves ~1s /
    # case for backtest (C6v2-L).
    hits: list[RetrievalHit] = []
    params: dict[str, Any] = {
        "p_card":         {"id": card_id},
        "p_window_start": _as_iso(window_start),
        "p_window_end":   _as_iso(window_end),
        # Sentinels: reuse card_id for absent device/region legs.
        "p_device":       {"id": device_profile or card_id},
        "p_region":       {"id": str(addr1) if addr1 else card_id},
    }
    import os as _os
    use_mcp = _os.getenv("SENTINEL_GRAPH_VIA_MCP", "1").strip().lower() in ("1", "true", "yes")
    if use_mcp:
        from sentinel.graph.mcp_client import sync_run_installed_query
        try:
            r = sync_run_installed_query("FraudGraph", "closed_cases_touching", params)
        except Exception as e:  # noqa: BLE001
            r = {"error": True, "message": str(e)[:200], "results": []}
    else:
        r = tg.run_query("FraudGraph", "closed_cases_touching", params)
    if r.get("error"):
        logger.warning("closed_cases_touching error: %s", r.get("message"))
        return hits

    seen: set[str] = set()
    cutoff = as_of if isinstance(as_of, datetime) else _parse_ts(as_of)

    # SetAccum<VERTEX<ClosedCase>> serializes each item as a dict carrying
    # both ``v_id`` and ``attributes`` — read them directly instead of one
    # RESTPP GET per candidate (10× fewer round-trips for top_k=10).
    for row in r["results"]:
        cases = row.get("cases") or []
        if not isinstance(cases, list):
            continue
        for cc in cases:
            if isinstance(cc, dict):
                cid = cc.get("v_id") or cc.get("attributes", {}).get("case_id")
                attrs = cc.get("attributes") or {}
            else:
                cid = str(cc); attrs = {"case_id": str(cc)}
            if not cid or cid in seen:
                continue
            seen.add(cid)
            closed_at = attrs.get("closed_at", "")
            try:
                closed_dt = _parse_ts(closed_at) if closed_at else None
            except ValueError:
                closed_dt = None
            if closed_dt is not None and closed_dt >= cutoff:
                continue
            hits.append(RetrievalHit(
                id=cid, kind="closed_case", score=1.0,
                source="structural", attrs=attrs,
            ))
    return hits


# ---- semantic retrieval via MCP ----------------------------------------


async def _semantic_top_k(
    vertex_type: str,
    vector_attribute: str,
    query_vector: list[float],
    top_k: int,
    return_vectors: bool = False,
) -> list[dict]:
    """One MCP round-trip. Returns the raw list of {v_id, v_type, attributes} rows."""
    export_to_env()
    from sentinel.graph.mcp_client import open_session

    async with open_session() as sess:
        resp = await sess.call_tool(
            "tigergraph__search_top_k_similarity",
            {
                "vertex_type":       vertex_type,
                "vector_attribute":  vector_attribute,
                "query_vector":      query_vector,
                "top_k":             top_k,
                "return_vectors":    return_vectors,
                "graph_name":        "FraudGraph",
            },
        )

    text = "\n".join(getattr(c, "text", str(c)) for c in resp.content)
    # The tool wraps its response as ```json ... ``` and then repeats
    # summary/data blocks. Use JSONDecoder.raw_decode on the first ``{``
    # we find, ignoring trailing content.
    body = text
    if "```json" in body:
        body = body.split("```json", 1)[1].lstrip("` \n")
    else:
        body = body[body.find("{"):]
    try:
        payload, _ = json.JSONDecoder().raw_decode(body)
    except Exception as e:  # noqa: BLE001
        logger.warning("semantic parse failed: %s", e)
        return []
    if not payload.get("success"):
        logger.warning("semantic search failed: %s", payload.get("summary"))
        return []
    result = payload.get("data", {}).get("result", [])
    # result is often a list of one item with a "v" list of vertices.
    out: list[dict] = []
    for r in result:
        if isinstance(r, dict) and "v" in r:
            out.extend(r["v"])
        elif isinstance(r, dict):
            out.append(r)
    return out


def semantic_closed_cases_rest(
    tg: TGClient,
    query_vector: list[float],
    *,
    as_of: datetime | str,
    top_k: int = 15,
) -> list[RetrievalHit]:
    """REST fast-path — invokes the installed ``vector_search_cc`` query.

    ~1.4s vs ~30s via MCP. Preferred when a TGClient is already open.
    """
    import os as _os
    use_mcp = _os.getenv("SENTINEL_GRAPH_VIA_MCP", "1").strip().lower() in ("1", "true", "yes")
    if use_mcp:
        from sentinel.graph.mcp_client import sync_run_installed_query
        try:
            r = sync_run_installed_query("FraudGraph", "vector_search_cc",
                                          {"p_query_vector": query_vector,
                                           "p_top_k": top_k * 3})
        except Exception as e:  # noqa: BLE001
            r = {"error": True, "message": str(e)[:200], "results": []}
    else:
        r = tg.run_query("FraudGraph", "vector_search_cc", {
            "p_query_vector": query_vector,
            "p_top_k": top_k * 3,       # ask for extra so time-gate has room
        })
    if r.get("error"):
        return []
    cutoff = as_of if isinstance(as_of, datetime) else _parse_ts(as_of)
    hits: list[RetrievalHit] = []
    for row in r.get("results", []):
        cases = row.get("Cases") or []
        for i, cc in enumerate(cases):
            attrs = cc.get("attributes", {})
            cid = attrs.get("case_id") or cc.get("v_id")
            closed_at = attrs.get("closed_at", "")
            try:
                dt = _parse_ts(closed_at) if closed_at else None
            except ValueError:
                dt = None
            if dt is not None and dt >= cutoff:
                continue
            hits.append(RetrievalHit(
                id=cid, kind="closed_case",
                score=1.0 / (i + 1),
                source="semantic",
                attrs=attrs,
            ))
            if len(hits) >= top_k:
                break
    return hits


def semantic_closed_cases(
    query_vector: list[float],
    *,
    as_of: datetime | str,
    top_k: int = 15,
) -> list[RetrievalHit]:
    """Top-k similar ClosedCases, time-gated by closed_at < as_of. MCP path.
    Kept for backwards-compat; prefer ``semantic_closed_cases_rest``."""
    raw = asyncio.run(_semantic_top_k(
        "ClosedCase", "notes_embedding", query_vector, top_k=top_k,
    ))
    cutoff = as_of if isinstance(as_of, datetime) else _parse_ts(as_of)
    hits: list[RetrievalHit] = []
    for i, row in enumerate(raw):
        attrs = row.get("attributes", {})
        cid = attrs.get("case_id") or row.get("v_id")
        closed_at = attrs.get("closed_at", "")
        try:
            closed_dt = _parse_ts(closed_at) if closed_at else None
        except ValueError:
            closed_dt = None
        if closed_dt is not None and closed_dt >= cutoff:
            continue
        # Score ~ 1/(rank+1) as a naive similarity proxy — the tool returns
        # results in cosine-order; explicit scores are not exposed.
        hits.append(RetrievalHit(
            id=cid, kind="closed_case",
            score=1.0 / (i + 1),
            source="semantic",
            attrs=attrs,
        ))
        if len(hits) >= top_k:
            break
    return hits


def semantic_sentinel_cases(
    query_vector: list[float],
    *,
    as_of: datetime | str,
    top_k: int = 15,
) -> list[RetrievalHit]:
    """Top-k similar SentinelCases (memory), time-gated by opened_at < as_of."""
    raw = asyncio.run(_semantic_top_k(
        "SentinelCase", "summary_embedding", query_vector, top_k=top_k,
    ))
    cutoff = as_of if isinstance(as_of, datetime) else _parse_ts(as_of)
    hits: list[RetrievalHit] = []
    for i, row in enumerate(raw):
        attrs = row.get("attributes", {})
        cid = attrs.get("case_id") or row.get("v_id")
        opened_at = attrs.get("opened_at", "")
        try:
            opened_dt = _parse_ts(opened_at) if opened_at else None
        except ValueError:
            opened_dt = None
        if opened_dt is not None and opened_dt >= cutoff:
            continue
        hits.append(RetrievalHit(
            id=cid, kind="sentinel_case",
            score=1.0 / (i + 1),
            source="semantic",
            attrs=attrs,
        ))
        if len(hits) >= top_k:
            break
    return hits


def semantic_policy(query_vector: list[float], *, top_k: int = 8) -> list[RetrievalHit]:
    """Top-k policy + reg-doc chunks. No time-gating (documents are static)."""
    hits: list[RetrievalHit] = []
    for vt, kind, attr in [
        ("PolicyChunk", "policy", "embedding"),
        ("RegDocChunk", "regdoc", "embedding"),
    ]:
        raw = asyncio.run(_semantic_top_k(vt, attr, query_vector, top_k=top_k))
        for i, row in enumerate(raw):
            attrs = row.get("attributes", {})
            hits.append(RetrievalHit(
                id=attrs.get("chunk_id") or row.get("v_id"),
                kind=kind,
                score=1.0 / (i + 1),
                source="semantic",
                attrs=attrs,
            ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


# ---- fusion ------------------------------------------------------------


def _rrf(hit_lists: list[list[RetrievalHit]], k: int = 60) -> list[RetrievalHit]:
    """Reciprocal rank fusion: each hit at rank r in list L contributes 1/(k+r)."""
    scores: dict[str, float] = {}
    payload: dict[str, RetrievalHit] = {}
    for hits in hit_lists:
        for rank, h in enumerate(hits):
            if not h.id:
                continue
            scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (k + rank + 1)
            payload.setdefault(h.id, h)
    fused: list[RetrievalHit] = []
    for hid, sc in sorted(scores.items(), key=lambda x: -x[1]):
        base = payload[hid]
        fused.append(RetrievalHit(
            id=base.id, kind=base.kind, score=sc,
            source="fused", attrs=base.attrs,
        ))
    return fused


def hybrid_retrieve(
    tg: TGClient,
    *,
    query_vector: list[float],
    card_id: str,
    device_profile: str | None,
    addr1: str | None,
    window_start: datetime | str,
    window_end: datetime | str,
    as_of: datetime | str,
    top_k: int = 10,
    skip_sentinel_cases: bool = False,
) -> list[RetrievalHit]:
    """Full hybrid retrieval — structural + semantic ClosedCases + SentinelCases.

    The two MCP-backed semantic calls run concurrently via asyncio.gather —
    each round-trip is ~20-25s, so serial cost was 40-50s; concurrent is
    ~25s. Structural query is done in the main thread first.

    ``skip_sentinel_cases``: pass True on the first case of the run when
    the memory graph is known to be empty (nothing has been written yet).
    """
    struct = structural_closed_cases(
        tg,
        card_id=card_id,
        device_profile=device_profile,
        addr1=addr1,
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
    )

    # Fast-path: use the installed vector_search_cc query over REST.
    sem_cc = semantic_closed_cases_rest(tg, query_vector, as_of=as_of, top_k=top_k)

    # SentinelCase memory: MCP is still the only tool for now. Skip on
    # cold-start when the memory graph is known-empty.
    sem_sc: list[RetrievalHit] = []
    if not skip_sentinel_cases:
        try:
            sc_raw = asyncio.run(_semantic_top_k(
                "SentinelCase", "summary_embedding", query_vector, top_k=top_k))
            cutoff = as_of if isinstance(as_of, datetime) else _parse_ts(as_of)
            sem_sc = _semantic_rows_to_hits(sc_raw, "sentinel_case", "opened_at", cutoff)
        except Exception as e:  # noqa: BLE001
            logger.info("SentinelCase MCP search failed: %s", e)

    fused = _rrf([struct, sem_cc, sem_sc])
    return fused[:top_k]


def _semantic_rows_to_hits(raw: list, kind: str, date_field: str, cutoff: datetime) -> list[RetrievalHit]:
    hits: list[RetrievalHit] = []
    for i, row in enumerate(raw or []):
        attrs = row.get("attributes", {}) if isinstance(row, dict) else {}
        cid = attrs.get("case_id") or (row.get("v_id") if isinstance(row, dict) else None)
        if not cid:
            continue
        dt_val = attrs.get(date_field, "")
        try:
            dt_parsed = _parse_ts(dt_val) if dt_val else None
        except ValueError:
            dt_parsed = None
        if dt_parsed is not None and dt_parsed >= cutoff:
            continue
        hits.append(RetrievalHit(
            id=cid, kind=kind, score=1.0 / (i + 1),
            source="semantic", attrs=attrs,
        ))
    return hits
