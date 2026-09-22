"""LangGraph state machine — the Sentinel investigation flow.

Nodes:
  1. trigger            — parse the alert, hydrate the txn row
  2. open_case          — build the case skeleton
  3. gather_baseline    — DuckDB baseline + graph structural signals
  4. run_detectors      — pattern detectors → evidence ledger
  5. memory_retrieve    — hybrid retrieval (structural + semantic, as-of gated)
  6. assess             — sum log-LRs → calibrated posterior → verdict
  7. uncertainty_gate   — decide whether to VOI-plan or emit
  8. voi_plan           — evidence-conditioned simulator + VOI decision
  9. request_evidence   — record the request in the case
  10. simulate_response — pick a synthetic customer response (for offline runs)
  11. reassess          — apply the response, recompute posterior
  12. critic            — adversarial review + device-link checks
  13. decide            — policy engine (rules R1..R10 + §6 SAR)
  14. explain           — Gemini writes the analyst summary
  15. write_memory      — persist SentinelCase to TigerGraph
  16. emit              — pydantic AnswerFile + telemetry

The LLM never chooses actions, routes, or rule citations — those come from
the policy engine (`sentinel.policy`). The critic can only add concerns.
"""

from __future__ import annotations

import json
import logging
import os as _os
import time
from datetime import datetime
from typing import Any, Optional

from sentinel.agent.critic import run_critic
from sentinel.agent.explain import write_summary, write_sar
from sentinel.agent.posterior import score_ledger, verdict_for
from sentinel.agent.state import AgentState, Telemetry
from sentinel.agent.voi import apply_customer_response, voi_plan
from sentinel.data.features import connect
from sentinel.detectors import DetectorContext, run_all_detectors
from sentinel.evidence.ledger import Evidence, EvidenceLedger
from sentinel.policy.invariants import check_invariants
from sentinel.policy.policy_engine import (
    Action,
    PolicyDecision,
    PolicyInput,
    add_block_all_cards_if_permitted,
    decide,
)

logger = logging.getLogger(__name__)


# ---------- shared helpers ----------------------------------------------------


def _time(node_name: str, state: AgentState):
    """Context manager that records node duration into telemetry."""
    class _Ctx:
        def __enter__(self):
            self.t0 = time.time()
            return self
        def __exit__(self, *args):
            tel: Telemetry = state.setdefault("telemetry", Telemetry())
            tel.record_node(node_name, time.time() - self.t0)
    return _Ctx()


# ---------- nodes -------------------------------------------------------------


def node_trigger(state: AgentState) -> AgentState:
    """Hydrate the alert with the txn's feature row."""
    with _time("trigger", state):
        con = connect()
        row = con.execute(
            "SELECT * FROM txn_features WHERE TransactionID = ?",
            [int(state["txn_id"])],
        ).fetchdf()
        if row.empty:
            state.setdefault("errors", []).append(
                f"txn {state['txn_id']} not in txn_features")
            return state
        state["txn_row"] = row.iloc[0].to_dict()
    return state


def node_open_case(state: AgentState) -> AgentState:
    """Set the case_id and ledger seed."""
    with _time("open_case", state):
        state.setdefault("ledger", EvidenceLedger())
    return state


def _apply_customer_report_denial_if_no_r7(state: AgentState) -> None:
    """P5v4-3: on customer_report triggers the trigger text IS a denial.

    Apply it AFTER detectors have run so we know whether R7 fires. If R7
    (recurring_match + dispute) fires, don't apply the denial — the
    simulator will decide from legit-archetype tags. Otherwise apply the
    denial evidence up-front so R2 fires cleanly.
    """
    if state.get("trigger_type") != "customer_report":
        return
    if state.get("customer_response") is not None:
        return
    tags = state.get("_tags", set())
    if "is_recurring_match" in tags:
        # R7 will fire — let the simulator decide.
        return
    apply_customer_response(state["ledger"], "denied")
    state["customer_response"] = "denied"
    state["_denial_from_trigger"] = True


def node_gather_baseline(state: AgentState) -> AgentState:
    """Compute a compact baseline summary + populate graph_signals."""
    with _time("gather_baseline", state):
        row = state.get("txn_row", {})
        state["baseline"] = {
            "n_prior_txns": row.get("n_prior_txns"),
            "n_prior_txns_90d": row.get("n_prior_txns_90d"),
            "prior_median_90d": row.get("prior_median_90d"),
            "prior_mad_90d": row.get("prior_mad_90d"),
            "amt_z_asof": row.get("amt_z_asof"),
            "home_addr1_asof": row.get("home_addr1_asof"),
            "is_new_device_for_card": row.get("is_new_device_for_card"),
            "unseen_productcd": row.get("unseen_productcd"),
            "prior_fraud_on_card_tuple": row.get("prior_fraud_on_card_tuple"),
            "prior_fraud_on_customer": row.get("prior_fraud_on_customer"),
            "risk_score": row.get("risk_score"),
            "risk_score_decile": row.get("risk_score_decile"),
        }
        state.setdefault("graph_signals", {})
        _fetch_graph_signals(state)
    return state


def _lookup_device_degree(device_profile: str | None) -> int:
    """Return the KNOWN_DEVICE degree (n_cards) for ``device_profile`` from DuckDB."""
    if not device_profile:
        return 0
    try:
        con = connect()
        r = con.execute(
            "SELECT n_cards FROM dev_degree WHERE device_profile = ?",
            [device_profile],
        ).fetchone()
        return int(r[0]) if r else 0
    except Exception:  # noqa: BLE001
        return 0


def _device_prior_fraud_cc_asof(device_profile: str | None, as_of: str) -> tuple[bool, int]:
    """(has_prior_fraud_cc, n_prior_fraud_cc) — closed_at < as_of."""
    if not device_profile:
        return False, 0
    try:
        con = connect()
        r = con.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT DISTINCT cc.case_id
              FROM tx_enriched t
              JOIN cc_join cc ON cc.ftxn = t.TransactionID
              WHERE t.device_profile = ?
                AND cc.outcome = 'confirmed_fraud'
                AND cc.closed_at < ?
            )
            """,
            [device_profile, as_of],
        ).fetchone()
        n = int(r[0] or 0) if r else 0
        return (n > 0, n)
    except Exception:  # noqa: BLE001
        return False, 0


def _n_other_cards_new_or_proxied_on_device(
    device_profile: str | None, customer_id: str, as_of: str, window_days: int = 14,
) -> int:
    """Count of DISTINCT other customers with id_15='New' or IP_PROXY:* txns
    on this device in ±window_days of as_of.
    """
    if not device_profile:
        return 0
    try:
        con = connect()
        r = con.execute(
            """
            SELECT COUNT(DISTINCT customer_id)
            FROM tx_enriched
            WHERE device_profile = ?
              AND customer_id <> ?
              AND ts BETWEEN (CAST(? AS TIMESTAMP) - INTERVAL 14 DAY)
                         AND (CAST(? AS TIMESTAMP) + INTERVAL 14 DAY)
              AND (id_15 = 'New' OR id_23 LIKE 'IP_PROXY:%')
            """,
            [device_profile, customer_id, as_of, as_of],
        ).fetchone()
        return int(r[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


def _n_other_cards_with_fraud_cc_on_device(
    device_profile: str | None, customer_id: str, as_of: str,
) -> int:
    """Count of DISTINCT other customers whose txns on this device have an
    attached confirmed-fraud ClosedCase closed before as_of.
    """
    if not device_profile:
        return 0
    try:
        con = connect()
        r = con.execute(
            """
            SELECT COUNT(DISTINCT t.customer_id)
            FROM tx_enriched t
            JOIN cc_join cc ON cc.ftxn = t.TransactionID
            WHERE t.device_profile = ?
              AND t.customer_id <> ?
              AND cc.outcome = 'confirmed_fraud'
              AND cc.closed_at < ?
            """,
            [device_profile, customer_id, as_of],
        ).fetchone()
        return int(r[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


def _record_error(state: AgentState, source: str, err: str) -> None:
    """Append a structured error entry and log it."""
    state.setdefault("errors", []).append(f"{source}: {err}")
    logger.warning("%s: %s", source, err)


def _run_query(tg, graph: str, name: str, params: dict, state: AgentState) -> dict:
    """Wrap TGClient.run_query, recording HTTP + TG errors to state.errors."""
    tel: Telemetry = state.setdefault("telemetry", Telemetry())
    try:
        r = tg.run_query(graph, name, params)
    except Exception as e:  # noqa: BLE001
        _record_error(state, f"query:{name}", f"http/exception: {e}")
        return {"error": True, "message": str(e), "results": []}
    tel.tool_calls += 1
    if isinstance(r, dict) and r.get("error"):
        _record_error(state, f"query:{name}", r.get("message", "unknown TG error"))
    return r


def _fetch_graph_signals(state: AgentState) -> None:
    """Fire up to eight independent TG queries CONCURRENTLY over one
    httpx.AsyncClient. Each failure is recorded to ``state.errors``.

    Queries: ring_components, near_threshold_burst, recurring_match,
    device_neighbors, region_history, region_cluster, testing_sequence,
    burst_48h. All are independent — no cross-query dependencies.
    """
    import asyncio
    from datetime import datetime, timedelta
    from sentinel.agent.id_resolver import card_tuple_id
    from sentinel.config import TG_GRAPHNAME, TG_HOST
    from sentinel.graph.token import get_jwt

    as_of = datetime.strptime(state["opened_at"], "%Y-%m-%d %H:%M:%S")
    window_start = as_of - timedelta(days=14)
    card_pk = card_tuple_id(state["card_id"])
    row = state.get("txn_row", {})
    tel: Telemetry = state.setdefault("telemetry", Telemetry())

    ws = window_start.strftime("%Y-%m-%d %H:%M:%S")
    we = as_of.strftime("%Y-%m-%d %H:%M:%S")
    device_profile = row.get("device_profile")
    addr1 = row.get("addr1")
    prod = row.get("ProductCD")
    amt = row.get("TransactionAmt")

    # Build the concurrent request set.
    jobs: list[tuple[str, dict]] = []
    jobs.append(("ring_components", {
        "p_card": {"id": card_pk}, "p_window_start": ws, "p_window_end": we,
        "p_degree_cap": 100, "p_expand_family": True,
    }))
    jobs.append(("near_threshold_burst", {
        "p_card": {"id": card_pk}, "p_window_start": ws, "p_window_end": we,
        "p_threshold": 500.0, "p_min_count": 3,
    }))
    jobs.append(("testing_sequence", {
        "p_card": {"id": card_pk}, "p_window_start": ws, "p_window_end": we,
    }))
    jobs.append(("burst_48h", {
        "p_card": {"id": card_pk},
        "p_window_start": (as_of - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S"),
        "p_window_end":   we,
    }))
    if prod and amt is not None:
        jobs.append(("recurring_match", {
            "p_card": {"id": card_pk}, "p_product_cd": str(prod),
            "p_amount": float(amt), "p_tolerance_cents": 0.02, "p_min_days_apart": 20,
        }))
    if device_profile:
        jobs.append(("device_neighbors", {
            "p_device": {"id": device_profile},
            "p_window_start": ws, "p_window_end": we, "p_degree_cap": 100,
        }))
    if addr1 is not None:
        jobs.append(("region_history", {
            "p_card": {"id": card_pk}, "p_region": {"id": str(addr1)},
            "p_window_start": ws, "p_window_end": we,
        }))
        jobs.append(("region_cluster", {
            "p_region": {"id": str(addr1)}, "p_window_start": ws, "p_window_end": we,
        }))

    use_mcp = _os.getenv("SENTINEL_GRAPH_VIA_MCP", "1").strip().lower() in ("1", "true", "yes")

    async def _run_all_rest():
        import httpx
        headers = {"Authorization": f"Bearer {get_jwt()}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=90.0, headers=headers) as client:
            async def _one(name: str, params: dict) -> tuple[str, dict]:
                url = f"{TG_HOST}/restpp/query/{TG_GRAPHNAME}/{name}"
                try:
                    resp = await client.post(url, json=params)
                    resp.raise_for_status()
                    body = resp.json()
                except Exception as e:  # noqa: BLE001
                    return name, {"error": True, "message": str(e), "results": []}
                return name, body
            return await asyncio.gather(*(_one(n, p) for n, p in jobs))

    def _run_all_mcp_shared():
        """Uses the shared MCP session (one subprocess for the whole process)."""
        from sentinel.graph.mcp_client import sync_run_installed_query
        # Sync submit each query to the shared loop; loop runs them
        # sequentially — subprocess stdio is single-lane anyway.
        out: list[tuple[str, dict]] = []
        for name, params in jobs:
            try:
                body = sync_run_installed_query(TG_GRAPHNAME, name, params)
            except Exception as e:  # noqa: BLE001
                body = {"error": True, "message": str(e)[:200], "results": []}
            out.append((name, body))
        return out

    results = _run_all_mcp_shared() if use_mcp else asyncio.run(_run_all_rest())
    tel.tool_calls += len(results)
    by_name: dict[str, dict] = {}
    for name, body in results:
        if isinstance(body, dict) and body.get("error"):
            _record_error(state, f"query:{name}", str(body.get("message", "?"))[:200])
        by_name[name] = body

    # ---- ring_components → tier context ----
    ring_body = by_name.get("ring_components", {})
    if not ring_body.get("error"):
        ring: dict = {}
        for it in ring_body.get("results", []):
            ring.update({k: v for k, v in it.items() if not k.startswith("@")})
        n_cards = int(ring.get("n_cards", 0) or 0)
        device_degree = _lookup_device_degree(device_profile)
        dp = device_profile
        has_cc, n_cc = _device_prior_fraud_cc_asof(dp, state["opened_at"])
        n_other_new_proxied = _n_other_cards_new_or_proxied_on_device(
            dp, state["customer_id"], state["opened_at"], window_days=14)
        n_other_fraud_cc = _n_other_cards_with_fraud_cc_on_device(
            dp, state["customer_id"], state["opened_at"])
        state["graph_signals"]["ring_components"] = {
            "n_cards": n_cards,
            "n_proxied_cards": int(ring.get("n_proxied_cards", 0) or 0),
            "n_new_device_cards": int(ring.get("n_new_device_cards", 0) or 0),
            "@@blast_radius": float(ring.get("ring_exposure_usd", 0) or 0),
            "cards": [c["v_id"] if isinstance(c, dict) else c
                      for c in (ring.get("cards") or [])],
            "n_ring_txns": int(ring.get("n_ring_txns", 0) or 0),
            "device_degree": device_degree,
            "has_confirmed_fraud_cc": has_cc,
            "n_prior_fraud_cc_on_device": n_cc,
            "n_other_new_or_proxied_in_window": n_other_new_proxied,
            "n_other_cards_with_fraud_cc": n_other_fraud_cc,
        }

    # ---- near_threshold_burst ----
    b = by_name.get("near_threshold_burst", {})
    if not b.get("error"):
        n = 0; fires = False
        for it in b.get("results", []):
            n = int(it.get("n", n) or n)
            fires = bool(it.get("fires", fires))
        if fires and n >= 3:
            state["graph_signals"]["near_threshold_burst"] = {
                "fires": True, "n": n, "p_threshold": 500.0,
            }

    # ---- testing_sequence ----
    b = by_name.get("testing_sequence", {})
    if not b.get("error"):
        n_small = n_larger = 0; fires = False
        small_txns: list[str] = []; larger_txns: list[str] = []
        for it in b.get("results", []):
            n_small  = int(it.get("@@n_small",  it.get("n_small",  n_small))  or n_small)
            n_larger = int(it.get("@@n_larger", it.get("n_larger", n_larger)) or n_larger)
            for t in (it.get("@@small") or it.get("small") or []):
                small_txns.append(t["v_id"] if isinstance(t, dict) else str(t))
            for t in (it.get("@@larger") or it.get("larger") or []):
                larger_txns.append(t["v_id"] if isinstance(t, dict) else str(t))
        fires = n_small >= 3
        if fires:
            state["graph_signals"]["testing_sequence"] = {
                "fires": True, "n_small": n_small, "n_larger": n_larger,
                "small_txns": small_txns[:20], "larger_txns": larger_txns[:20],
            }

    # ---- burst_48h ----
    b = by_name.get("burst_48h", {})
    if not b.get("error"):
        n_online = 0; txns: list[str] = []
        for it in b.get("results", []):
            n_online = int(it.get("@@n_online", it.get("n_online", n_online)) or n_online)
            for t in (it.get("@@txns") or it.get("txns") or []):
                txns.append(t["v_id"] if isinstance(t, dict) else str(t))
        state["graph_signals"]["burst_48h"] = {
            "n_online": n_online, "txns": txns[:20], "fires": n_online >= 2,
        }

    # ---- recurring_match ----
    b = by_name.get("recurring_match")
    if b and not b.get("error"):
        for it in b.get("results", []):
            n = int(it.get("n", 0) or 0)
            fires = bool(it.get("fires", False))
            if fires and n >= 3:
                state["graph_signals"]["recurring_match"] = {
                    "fires": True, "n": n,
                    "cadence_cv": float(it.get("cadence_cv", 0) or 0),
                    "n_distinct_addr1": int(it.get("n_distinct_addr1", 0) or 0),
                }

    # ---- device_neighbors ----
    b = by_name.get("device_neighbors")
    if b and not b.get("error"):
        other_cards: list[str] = []
        for it in b.get("results", []):
            for c in (it.get("cards") or it.get("neighbors") or it.get("card_ids") or []):
                cid = c["v_id"] if isinstance(c, dict) else c
                if cid and cid != card_pk:
                    other_cards.append(cid)
        if other_cards:
            state["graph_signals"]["device_neighbors"] = {
                "n_other_cards": len(set(other_cards)),
                "cards": list(set(other_cards))[:20],
            }

    # ---- region_history ----
    b = by_name.get("region_history")
    if b and not b.get("error"):
        n_in_region_ever = 0; n_home_in_window = 0
        for it in b.get("results", []):
            n_in_region_ever = int(it.get("@@n_in_region_ever", n_in_region_ever) or n_in_region_ever)
            n_home_in_window = int(it.get("@@n_in_home_in_window", n_home_in_window) or n_home_in_window)
        state["graph_signals"]["region_history"] = {
            "had_prior_txn_in_region": n_in_region_ever > 0,
            "n_home_txns_in_window": n_home_in_window,
        }

    # ---- region_cluster ----
    b = by_name.get("region_cluster")
    if b and not b.get("error"):
        other_cards: list[str] = []
        for it in b.get("results", []):
            for c in (it.get("cards") or it.get("card_ids") or []):
                cid = c["v_id"] if isinstance(c, dict) else c
                if cid and cid != card_pk:
                    other_cards.append(cid)
        if other_cards:
            state["graph_signals"]["region_cluster"] = {
                "n_other_cards": len(set(other_cards)),
                "cards": list(set(other_cards))[:20],
            }


def node_run_detectors(state: AgentState) -> AgentState:
    """Run every detector, absorb their evidence, pick a pattern label."""
    with _time("run_detectors", state):
        from sentinel.config import REPO_ROOT
        lr_table = json.loads(
            (REPO_ROOT / "sentinel" / "evidence" / "lr_table.json").read_text())
        con = connect()
        row = state.get("txn_row", {})
        ctx = DetectorContext(
            case_id=state["case_id"], txn_id=state["txn_id"],
            txn_row=row,
            card_tuple_id=state.get("card_id", ""),
            customer_id=state.get("customer_id", ""),
            opened_at=state.get("opened_at", ""),
            trigger_type=state.get("trigger_type", ""),
            trigger_text=state.get("trigger_text", ""),
            lr_table=lr_table,
            baseline_summary=state.get("baseline", {}),
            memory_hits=state.get("memory_hits", []),
            graph_signals=state.get("graph_signals", {}),
            con=con,
        )
        results = run_all_detectors(ctx)
        state["detector_results"] = results

        ledger = state.setdefault("ledger", EvidenceLedger())
        # C6v3-M: detector Evidence items are ANNOTATIONS only — their log_lr
        # is zeroed so the fitted alert_model owns the posterior. The detector
        # still supplies pattern label + tags.
        for r in results:
            for e in r.evidence:
                e.log_lr = 0.0
                ledger.add(e)

        # Pattern selection: strongest fired README pattern wins.
        # If undocumented fires, it dominates. Otherwise, adopt the pattern
        # of the detector whose evidence contributed the most positive log_lr.
        pat = "none"
        undoc = next((r for r in results if r.pattern == "undocumented"), None)
        if undoc:
            pat = "undocumented"
        else:
            named = [r for r in results if r.pattern and r.pattern not in ("none", None)]
            if named:
                named.sort(key=lambda r: sum(max(e.log_lr, 0) for e in r.evidence),
                           reverse=True)
                pat = named[0].pattern or "none"
        state["pattern"] = pat

        # Merge detector tags → policy bits.
        tags = set().union(*(r.tags for r in results))
        state["_tags"] = tags

        # P5v4-3/5: customer_report trigger implies denial unless R7 fires.
        _apply_customer_report_denial_if_no_r7(state)
    return state


_SENTINEL_MEMORY_HAS_DATA: bool = False


FEATURE_CHANNEL = {
    # identity flags
    "id_15_new": "identity_flags", "proxy_flag": "identity_flags",
    "mixed_channel_last_24h": "identity_flags",
    "is_new_device_for_card": "device",
    # region
    "out_of_home_region_asof": "region", "had_prior_txn_in_region": "region",
    "prior_cleared_travel_on_card": "region", "region_cluster_ge2": "region",
    # amount
    "amt_z_gt_2": "amount", "amt_z_gt_3": "amount",
    "unseen_productcd": "amount", "unseen_p_emaildomain": "amount",
    # sequence / burst
    "n_online_48h_2_4": "sequence", "n_online_48h_ge_5": "sequence",
    "testing_sequence_fires": "sequence", "near_threshold_burst": "sequence",
    "recurring_ge3": "amount",
    # history (prior cases + risk decile)
    "prior_fraud_on_card_tuple": "history", "prior_fraud_on_customer": "history",
    "prior_cleared_new_phone_on_card": "history",
    "channel_online": "history", "channel_in_person": "history",
    # device tiers → device
    "T1_le5": "device", "T1_6-20": "device", "T1_21-100": "device",
    "T2_le5": "device", "T2_6-20": "device", "T2_21-100": "device",
    "T3_le5": "device", "T3_6-20": "device", "T3_21-100": "device",
    "T4_le5": "device", "T4_6-20": "device", "T4_21-100": "device",
    "device_neighbors_ge2": "device", "recipient_cluster_ge2": "amount",
}
# All risk_decile_* → history
for _i in range(1, 11):
    FEATURE_CHANNEL[f"risk_decile_{_i}"] = "history"


def node_score_alert_model(state: AgentState) -> AgentState:
    """C6v3-M: extract features + score with the fitted L2 logistic model.

    Adds one Evidence per firing feature with ``log_lr = coefficient``.
    Each feature is mapped to its real channel (device / region / sequence
    / amount / history / identity_flags) so the two-channel gate works.
    """
    from sentinel.evidence.alert_model import (
        extract_features_at_txn, load_model, score,
    )
    with _time("alert_model", state):
        try:
            model = load_model()
        except Exception as e:  # noqa: BLE001
            _record_error(state, "alert_model:load", str(e))
            return state
        row = state.get("txn_row", {})
        try:
            feats = extract_features_at_txn(
                row, state.get("graph_signals", {}), connect(),
            )
        except Exception as e:  # noqa: BLE001
            _record_error(state, "alert_model:extract", str(e))
            return state
        p_init, fired = score(feats, model)
        state["p_initial"] = p_init
        state["alert_model_features_fired"] = fired
        from sentinel.evidence.readable import sentence_for
        ledger = state.setdefault("ledger", EvidenceLedger())
        # Prior: model intercept re-shifted so "no evidence" gives p = 0.5,
        # merged into one baseline line.
        intercept_adj = float(model.get("intercept_adjusted", 0.0))
        ledger.add(Evidence(
            claim="Alert model baseline (fitted on 5,565 historical cases).",
            source="graph",
            ref=f"alert_model:__intercept__  (weight {intercept_adj:+.3f})",
            entity_ids=[state["txn_id"]],
            channel="history",
            log_lr=intercept_adj,
            direction="for" if intercept_adj > 0 else "against",
        ))
        # Drop small-effect features so the ledger reads cleanly.
        for name, coef in fired:
            if abs(coef) < 0.05:
                continue
            ledger.add(Evidence(
                claim=sentence_for(name, float(coef)),
                source="graph",
                ref=f"alert_model:{name}  (weight {coef:+.3f})",
                entity_ids=[state["txn_id"]],
                channel=FEATURE_CHANNEL.get(name, "history"),
                log_lr=float(coef),
                direction="for" if coef > 0 else "against",
            ))
    return state


def node_memory_retrieve(state: AgentState) -> AgentState:
    """Hybrid retrieval (as-of gated). Records failures to state.errors."""
    global _SENTINEL_MEMORY_HAS_DATA
    with _time("memory_retrieve", state):
        try:
            from datetime import timedelta
            from sentinel.agent.id_resolver import card_tuple_id
            from sentinel.graph.client import TGClient
            from sentinel.llm import embed_one
            from sentinel.retrieval.hybrid import hybrid_retrieve

            row = state.get("txn_row", {})
            as_of = datetime.strptime(state["opened_at"], "%Y-%m-%d %H:%M:%S")
            q = f"{state['trigger_text']} pattern={state.get('pattern','?')}"
            qv = embed_one(q)
            tg = TGClient()
            window_start = as_of - timedelta(days=14)
            hits = hybrid_retrieve(
                tg,
                query_vector=qv,
                card_id=card_tuple_id(state.get("card_id", "")),
                device_profile=row.get("device_profile"),
                addr1=str(row.get("addr1")) if row.get("addr1") is not None else None,
                window_start=window_start,
                window_end=as_of,
                as_of=as_of,
                top_k=10,
                skip_sentinel_cases=not _SENTINEL_MEMORY_HAS_DATA,
            )
            state["memory_hits"] = hits
            tel: Telemetry = state.setdefault("telemetry", Telemetry())
            tel.tool_calls += (2 if not _SENTINEL_MEMORY_HAS_DATA else 3)
        except Exception as e:  # noqa: BLE001
            _record_error(state, "memory_retrieve", str(e))
            state["memory_hits"] = []
    return state


def node_assess(state: AgentState) -> AgentState:
    """Compute posterior + verdict, after ledger de-duplication."""
    with _time("assess", state):
        from sentinel.agent.normalize import deduplicate

        ledger = state["ledger"]
        deduplicate(ledger)
        prior = state.get("prior_log_odds", 0.0)
        s = score_ledger(ledger, prior_log_odds=prior)
        state["log_odds"] = s["log_odds"]
        state["log_odds_uncapped"] = s["log_odds_uncapped"]
        state["log_odds_capped"] = s["capped"]
        p_new = s["fraud_probability"]
        # Prefer alert_model's p_initial if it's been computed.
        if "p_initial" not in state:
            state["p_initial"] = p_new
        # I11: probability must agree with a settled customer_response.
        resp = state.get("customer_response")
        # Snapshot the pre-response posterior into the ledger when the
        # response was set upstream (e.g. customer_report trigger denial)
        # and we haven't already snapshot'd it. reassess handles the
        # simulator-set-response case.
        if (resp is not None
                and state.get("p_initial") is not None
                and not state.get("_pre_response_snapshotted")):
            state["ledger"].add(Evidence(
                claim=f"Posterior before customer contact: {state['p_initial']:.3f}.",
                source="graph",
                ref="alert_model:__pre_response_p__",
                entity_ids=[state["txn_id"]],
                channel="history",
                log_lr=0.0,
                direction="neutral",
            ))
            state["_pre_response_snapshotted"] = True
        if resp == "denied":
            p_new = max(p_new, 0.85)
        elif resp == "confirmed":
            p_new = min(p_new, 0.15)
        state["fraud_probability"] = p_new
        state["verdict"] = verdict_for(
            p_new,
            n_independent_channels=len(state["ledger"].channels()),
            customer_response=resp,
        )
    return state


def node_voi_plan(state: AgentState) -> AgentState:
    with _time("voi_plan", state):
        exposure = float(state.get("txn_row", {}).get("TransactionAmt", 0) or 0)
        plan = voi_plan(
            state["ledger"],
            current_p=state["fraud_probability"],
            prior_log_odds=state.get("prior_log_odds", 0.0),
            exposure_usd=exposure,
        )
        state["voi_plan"] = {
            "should_ask": plan.should_ask,
            "reason": plan.reason,
            "expected_p_fraud": plan.expected_p_fraud,
            "expected_abs_shift": plan.expected_abs_shift,
            "worlds": [
                {"response": w.response, "p_fraud": w.fraud_probability,
                 "p_world": w.p_of_world}
                for w in plan.worlds
            ],
        }
        state["simulator_paths"] = state["voi_plan"]["worlds"]
    return state


LEGIT_ARCHETYPE_TAGS = {
    "is_recurring_match", "legit_recurring_r7_hint",
    "legit_trip_hint", "legit_new_phone_hint", "legit_big_purchase_hint",
}

# Channels that count toward "non-risk, non-prior-fraud" evidence for the
# simulator rule. Excludes 'history' (which carries the risk-score decile and
# prior-fraud-on-card signals) and 'memory' (retrieval hints).
_SIM_EVIDENCE_CHANNELS = {"device", "region", "sequence", "amount",
                          "identity_flags", "customer"}


def _simulator_should_run(state: AgentState) -> bool:
    """Simulator runs iff VERIFY_WITH_CUSTOMER or STEP_UP_AUTH would appear
    in the *initial* action list (i.e., before any customer response)."""
    from sentinel.policy.policy_engine import PolicyInput, decide as policy_decide

    row = state.get("txn_row", {})
    exposure = float(row.get("TransactionAmt", 0) or 0)
    tags = state.get("_tags", set())
    pin = PolicyInput(
        verdict=state["verdict"],           # type: ignore
        fraud_probability=state["fraud_probability"],
        exposure_usd=exposure,
        pattern=state.get("pattern", "none"),  # type: ignore
        signal_channels=frozenset(state["ledger"].channels()),
        shared_element=_shared_element_from_signals(state),   # type: ignore
        customer_response=None,             # type: ignore
        is_dispute=(state.get("trigger_type") == "customer_report"),
        is_recurring_match=("is_recurring_match" in tags),
        trigger_type=state.get("trigger_type"),
        channel=row.get("channel"),
        id_15_new=(row.get("id_15") == "New"),
    )
    initial = policy_decide(pin)
    names = {a.action for a in initial.actions}
    return "VERIFY_WITH_CUSTOMER" in names or "STEP_UP_AUTH" in names


SIM_TAU_DEFAULT = 0.30    # tuned in BACKTEST_REPORT (max response accuracy)


def _pick_response_by_rule(state: AgentState) -> tuple[str, str]:
    """Return (response, rule_used). The C6v3-final rule:

      1. Ring T3+ / testing_sequence fires  →  denied.
      2. Legit-archetype detector fired     →  confirmed.
      3. p_initial ≥ τ                      →  denied.
      4. Otherwise                           →  confirmed.

    no_reply is reserved for the graph-unreachable branch (handled in the
    caller). This method never returns no_reply.
    """
    tags = state.get("_tags", set())

    # Override 1: ring T3+ / testing sequence → denied.
    ring = (state.get("graph_signals") or {}).get("ring_components") or {}
    dev_deg  = int(ring.get("device_degree") or 0)
    n_cards  = int(ring.get("n_cards", 0) or 0)
    n_new_pr = int(ring.get("n_other_new_or_proxied_in_window", 0) or 0)
    n_fraudCC = int(ring.get("n_other_cards_with_fraud_cc", 0) or 0)
    ring_t3_plus = (0 < dev_deg <= 100 and (n_cards - 1) >= 2
                    and (n_new_pr >= 2 or n_fraudCC >= 2))
    if ring_t3_plus or "testing_sequence_fires" in tags:
        return "denied", "override:ring-T3+/testing_sequence → denied"

    # Override 2: legit-archetype → confirmed.
    if tags & LEGIT_ARCHETYPE_TAGS:
        return "confirmed", (
            f"override:legit-archetype {sorted(tags & LEGIT_ARCHETYPE_TAGS)} → confirmed"
        )

    # Base rule: denied iff p_initial ≥ τ.
    p_init = state.get("p_initial", state.get("fraud_probability", 0.5))
    tau = SIM_TAU_DEFAULT
    if p_init >= tau:
        return "denied", f"τ-rule: p_initial={p_init:.3f} ≥ τ={tau:.3f} → denied"
    return "confirmed", f"τ-rule: p_initial={p_init:.3f} < τ={tau:.3f} → confirmed"


def node_simulate_response(state: AgentState) -> AgentState:
    """Offline mode: pick a customer response by the deterministic rule
    described in CHECKPOINT 5 (final). Sets ``simulator_rule`` on state.
    """
    with _time("simulate_response", state):
        if state.get("customer_response") is not None:
            return state
        if not _simulator_should_run(state):
            state["simulator_rule"] = "not_run: no VERIFY/STEP_UP in initial actions"
            return state
        # no_reply only when the graph was unreachable during this run.
        errs = state.get("errors") or []
        graph_unreachable = any(
            ("http/exception" in e) or ("query:" in e and "502" in e)
            for e in errs
        )
        if graph_unreachable:
            state["customer_response"] = "no_reply"
            state["simulator_rule"] = "graph unreachable → no_reply → R4"
            apply_customer_response(state["ledger"], "no_reply")
            return state
        response, rule = _pick_response_by_rule(state)
        state["customer_response"] = response
        state["simulator_rule"] = rule
        apply_customer_response(state["ledger"], response)
    return state


def node_reassess(state: AgentState) -> AgentState:
    """Recompute posterior after the customer response was added.

    Enforce probability↔verdict agreement (I11): denied ⇒ p = max(p, 0.85),
    confirmed ⇒ p = min(p, 0.15). Record the pre-response p in a ledger
    Evidence item so the analyst can see what we thought before contact.
    """
    with _time("reassess", state):
        # Snapshot the pre-response posterior into the ledger (once).
        p_pre = state.get("p_initial")
        if (p_pre is not None
                and state.get("customer_response") is not None
                and not state.get("_pre_response_snapshotted")):
            state["ledger"].add(Evidence(
                claim=f"Posterior before customer contact: {p_pre:.3f}.",
                source="graph",
                ref="alert_model:__pre_response_p__",
                entity_ids=[state["txn_id"]],
                channel="history",
                log_lr=0.0,
                direction="neutral",
            ))
            state["_pre_response_snapshotted"] = True
        s = score_ledger(state["ledger"], prior_log_odds=state.get("prior_log_odds", 0.0))
        p_new = s["fraud_probability"]
        resp = state.get("customer_response")
        if resp == "denied":
            p_new = max(p_new, 0.85)
        elif resp == "confirmed":
            p_new = min(p_new, 0.15)
        state["log_odds"] = s["log_odds"]
        state["log_odds_uncapped"] = s["log_odds_uncapped"]
        state["log_odds_capped"] = s["capped"]
        state["fraud_probability"] = p_new
        state["verdict"] = verdict_for(
            p_new,
            n_independent_channels=len(state["ledger"].channels()),
            customer_response=resp,
        )
    return state


def node_assemble_episode(state: AgentState) -> AgentState:
    """C6v2-EPI: signature-matched ±48h episode assembly for fraud verdicts.

    Includes:
      * the flagged txn (unless verdict=legitimate).
      * detector-contributed ``affected_txn_ids``.
      * card's txns within ±48h of flag ts matching the fraud signature:
          - same new device / proxied txn if the flag was new-device-triggered
          - same addr1 if the flag is out-of-home-region
          - same online burst if pattern is card_testing / cnp*

    Reports ``jaccard_before / jaccard_after`` on state for backtest use.
    Exposure = sum of |TransactionAmt| over the deduped set from txn_features.
    """
    with _time("assemble_episode", state):
        if state.get("verdict") == "legitimate":
            state["affected_txn_ids"] = []
            state["exposure_usd"] = 0.0
            state["episode_before_signature"] = set()
            state["episode_after_signature"] = set()
            return state

        # Baseline (before signature expansion): flagged + detectors.
        base_ids: set[str] = set()
        if state.get("verdict") in ("fraud", "uncertain"):
            base_ids.add(str(state["txn_id"]))
        for r in state.get("detector_results", []) or []:
            for tid in getattr(r, "affected_txn_ids", []) or []:
                if tid:
                    base_ids.add(str(tid))

        txn_ids = set(base_ids)

        # Ring pattern → expand via ring devices.
        ring = state.get("graph_signals", {}).get("ring_components") or {}
        if ring and ring.get("n_cards", 0) >= 2:
            try:
                _add_ring_txns(state, txn_ids)
            except Exception as e:  # noqa: BLE001
                _record_error(state, "assemble_episode:ring_txns", str(e))

        # Fraud-only signature expansion (±48h).
        if state.get("verdict") == "fraud":
            try:
                _add_signature_matched_txns(state, txn_ids)
            except Exception as e:  # noqa: BLE001
                _record_error(state, "assemble_episode:sig", str(e))
        state["episode_before_signature"] = set(base_ids)
        state["episode_after_signature"] = set(txn_ids)

        # Compute exposure from txn_features.
        con = connect()
        clean_ids: list[int] = []
        for t in txn_ids:
            try:
                clean_ids.append(int(str(t).lstrip("T")))
            except ValueError:
                continue
        if not clean_ids:
            state["affected_txn_ids"] = list(txn_ids)
            state["exposure_usd"] = float(state.get("txn_row", {}).get("TransactionAmt", 0) or 0)
            return state
        placeholders = ",".join("?" * len(clean_ids))
        rows = con.execute(
            f"SELECT TransactionID, TransactionAmt FROM txn_features "
            f"WHERE TransactionID IN ({placeholders})",
            clean_ids,
        ).fetchall()
        exposure = round(sum(abs(float(r[1] or 0)) for r in rows), 2)
        state["affected_txn_ids"] = [str(r[0]) for r in rows]
        state["exposure_usd"] = exposure
    return state


def _add_signature_matched_txns(state: AgentState, txn_ids: set[str]) -> None:
    """Add card's txns within ±48h of the flag that share the fraud signature.

    Signature dimensions:
      - same device_profile if flag was new-device or ring-based
      - same addr1 if pattern is out_of_region_use
      - online burst — pattern is card_testing / cnp* → add other ±48h online txns
    """
    from datetime import timedelta
    row = state.get("txn_row", {})
    pat = state.get("pattern", "none")
    con = connect()
    predicates: list[str] = []
    params: list = [state["customer_id"], row.get("ts"), row.get("ts")]
    dp   = row.get("device_profile")
    addr = row.get("addr1")

    if pat in ("card_not_present_new_device", "undocumented", "account_takeover") and dp:
        predicates.append("device_profile = ?")
        params.append(dp)
    if pat == "out_of_region_use" and addr is not None:
        predicates.append("CAST(addr1 AS VARCHAR) = ?")
        params.append(str(addr))
    if pat in ("card_testing", "card_not_present_fraud"):
        predicates.append("channel = 'online'")

    if not predicates:
        return
    where_sig = " OR ".join(f"({p})" for p in predicates)
    q = f"""
        SELECT TransactionID
        FROM txn_features
        WHERE customer_id = ?
          AND ts BETWEEN (CAST(? AS TIMESTAMP) - INTERVAL 48 HOUR)
                     AND (CAST(? AS TIMESTAMP) + INTERVAL 48 HOUR)
          AND ({where_sig})
    """
    try:
        for r in con.execute(q, params).fetchall():
            txn_ids.add(str(r[0]))
    except Exception:  # noqa: BLE001
        pass


def _add_ring_txns(state: AgentState, txn_ids: set[str]) -> None:
    """Pull this card's txns on ring devices within the window.

    Uses the txn_features table + card_map to avoid another network call.
    """
    from datetime import timedelta
    row = state.get("txn_row", {})
    device = row.get("device_profile")
    if not device:
        return
    as_of = datetime.strptime(state["opened_at"], "%Y-%m-%d %H:%M:%S")
    window_start = as_of - timedelta(days=14)
    con = connect()
    rows = con.execute(
        """
        SELECT t.TransactionID
        FROM txn_features t
        WHERE t.customer_id = ?
          AND t.device_profile = ?
          AND t.ts >= ? AND t.ts <= ?
        """,
        [state["customer_id"], device,
         window_start.strftime("%Y-%m-%d %H:%M:%S"),
         as_of.strftime("%Y-%m-%d %H:%M:%S")],
    ).fetchall()
    for r in rows:
        txn_ids.add(str(r[0]))


def node_critic(state: AgentState) -> AgentState:
    with _time("critic", state):
        # LLM is off by default here to keep runs deterministic; the
        # deterministic pass still catches the HHG-011 template violations.
        # LLM critic is env-gated (SENTINEL_LLM_CRITIC=1). Its concerns are
        # annotation-only — they never change verdict, policy, or actions.
        use_llm = _os.getenv("SENTINEL_LLM_CRITIC", "").strip() in ("1", "true", "TRUE")
        state["critic_notes"] = run_critic(
            state["case_id"], state["ledger"], use_llm=use_llm)
    return state


def _shared_element_from_signals(state: AgentState) -> Optional[str]:
    """shared_element via the same helper the answer file uses so I13
    stays consistent — see ``compute_shared_element`` for the rules.

    The rest of the function (below) is kept as a legacy fallback but
    should never fire in practice: ``compute_shared_element`` is a
    strict superset of the older device-tier and cluster checks.
    """
    from sentinel.output.answer_file import compute_shared_element
    se = compute_shared_element(state)
    if se is not None:
        return se
    row = state.get("txn_row", {})
    if not row.get("device_profile"):
        # No device — device-based shared_element cannot be attributed.
        rc = (state.get("graph_signals") or {}).get("region_cluster") or {}
        if int(rc.get("n_other_cards", 0) or 0) >= 2:
            return "region"
        rec = (state.get("graph_signals") or {}).get("recipient_email_cluster") or {}
        if int(rec.get("n_other_cards", 0) or 0) >= 2:
            return "recipient"
        return None

    gs = state.get("graph_signals", {}) or {}
    ring = gs.get("ring_components") or {}
    dev_degree      = int(ring.get("device_degree", 0) or 0)
    has_cc          = bool(ring.get("has_confirmed_fraud_cc"))
    n_other_fraud   = int(ring.get("n_other_cards_with_fraud_cc", 0) or 0)
    proxy_flag      = bool(row.get("proxy_flag"))

    if dev_degree <= 100:
        # T2: device has fraud CC AND this txn is proxied.
        if has_cc and proxy_flag:
            return "device"
        # OR: ≥2 in-window other cards with attached fraud cases.
        if n_other_fraud >= 2:
            return "device"
        # OR: any device-neighbours cluster with ≥2 other cards in-window.
        dn = gs.get("device_neighbors") or {}
        if int(dn.get("n_other_cards", 0) or 0) >= 2:
            return "device"
        # OR: ring_components saw ≥2 other cards on a bounded device.
        if int(ring.get("n_cards", 0) or 0) - 1 >= 2:
            return "device"

    rc = gs.get("region_cluster") or {}
    if int(rc.get("n_other_cards", 0) or 0) >= 2:
        return "region"

    rec = gs.get("recipient_email_cluster") or {}
    if int(rec.get("n_other_cards", 0) or 0) >= 2:
        return "recipient"
    return None


def _apply_coherence_rule(state: AgentState) -> None:
    """C6v2-PAT: pattern is assigned whenever a typology detector matched;
    only verdict==legitimate forces pattern=none (enforced in answer_file).
    We no longer downgrade fraud→uncertain when pattern is 'none'.
    """
    return


def node_decide(state: AgentState) -> AgentState:
    """Policy engine. Deterministic; LLM never touches this."""
    with _time("decide", state):
        row = state.get("txn_row", {})
        exposure = float(row.get("TransactionAmt", 0) or 0)
        tags = state.get("_tags", set())

        _apply_coherence_rule(state)
        shared_element = _shared_element_from_signals(state)

        # R10: gate on ≥2 DISTINCT card tuples with prior confirmed_fraud on
        # this customer (plus this card if current verdict is fraud).
        from sentinel.agent.r10 import prior_confirmed_fraud_on_two_cards
        r10_gate, r10_count, r10_cards = prior_confirmed_fraud_on_two_cards(
            state["customer_id"], state["opened_at"],
            current_card_id=state["card_id"],
            current_verdict=state["verdict"],
        )
        state["r10_distinct_prior_cards"] = r10_count
        state["r10_prior_card_ids"] = r10_cards

        pin = PolicyInput(
            verdict=state["verdict"],           # type: ignore
            fraud_probability=state["fraud_probability"],
            exposure_usd=exposure,
            pattern=state.get("pattern", "none"),  # type: ignore
            signal_channels=frozenset(state["ledger"].channels()),
            shared_element=shared_element,          # type: ignore
            customer_response=state.get("customer_response"),  # type: ignore
            is_dispute=(state.get("trigger_type") == "customer_report"),
            is_recurring_match=("is_recurring_match" in tags),
            prior_confirmed_fraud_on_two_cards=r10_gate,
            credentials_confirmed_compromised=False,
            undocumented_coordinated=("undocumented_coordinated" in tags),
            testing_sequence_fires=("testing_sequence_fires" in tags),
            testing_sequence_over_100_cleared=False,
            trigger_type=state.get("trigger_type"),
            channel=row.get("channel"),
            id_15_new=(row.get("id_15") == "New"),
        )
        decision = decide(pin, evidence_requests_present=bool(state.get("customer_response")))
        # R10 gate: add BLOCK_ALL_CARDS only if permitted AND we have a
        # named pattern. The customer-denial path on a pattern-less alert
        # (P5F-6) must never escalate all cards — it's already handled by
        # BLOCK_CARD on this card alone.
        if pin.pattern not in (None, "none"):
            decision = PolicyDecision(
                actions=add_block_all_cards_if_permitted(decision.actions, pin),
                case_should_open=decision.case_should_open,
                sar_should_file=decision.sar_should_file,
                escalate=decision.escalate,
            )
        state["policy_decision"] = decision
        state["shared_element"] = shared_element

        # C6v3-PF: whenever verdict=fraud and no detector matched, fall back
        # to a rule-based pattern selection so the answer never carries
        # `verdict=fraud pattern=none`. Mark the ledger explicitly.
        if state.get("verdict") == "fraud" and state.get("pattern") in (None, "none"):
            best_match = _best_match_pattern(state)
            if best_match and best_match != "none":
                state["pattern_fallback_assigned"] = True
                state["pattern"] = best_match
                state["ledger"].add(Evidence(
                    claim=(
                        "Pattern assigned by channel rule: online activity "
                        "inconsistent with the cardholder's history and no "
                        "more specific typology matched (README pattern 2)."
                    ),
                    source="graph",
                    ref="c6v3-pf:channel_rule",
                    entity_ids=[state["txn_id"]],
                    channel="policy",
                    log_lr=0.0,
                    direction="neutral",
                ))
    return state


def _best_match_pattern(state: AgentState) -> str:
    """C6v3-PF: pattern fallback for fraud verdicts when no detector matched.

    Rules (in order):
      1. In-person AND ``addr1 != home_addr1_asof`` → ``out_of_region_use``.
      2. Both channels active on the card within ±48h of the flag →
         ``account_takeover``.
      3. Online AND id_15='New' / is_new_device_for_card=True →
         ``card_not_present_new_device``.
      4. Online → ``card_not_present_fraud``.
      5. Else → ``none``.
    """
    row = state.get("txn_row", {})
    ch = row.get("channel")
    addr1 = row.get("addr1")
    home  = row.get("home_addr1_asof")

    # Both-channel presence in ±48h
    both_channel = False
    try:
        con = connect()
        r = con.execute(
            """
            SELECT
              SUM(CASE WHEN channel='online'    THEN 1 ELSE 0 END) AS n_online,
              SUM(CASE WHEN channel='in_person' THEN 1 ELSE 0 END) AS n_inperson
            FROM txn_features
            WHERE customer_id = ?
              AND ts BETWEEN (CAST(? AS TIMESTAMP) - INTERVAL 48 HOUR)
                         AND (CAST(? AS TIMESTAMP) + INTERVAL 48 HOUR)
            """,
            [state["customer_id"], row.get("ts"), row.get("ts")],
        ).fetchone()
        n_on, n_ip = int(r[0] or 0), int(r[1] or 0)
        both_channel = (n_on >= 1 and n_ip >= 1)
    except Exception:  # noqa: BLE001
        pass

    if ch == "in_person" and addr1 is not None and home is not None and str(addr1) != str(home):
        return "out_of_region_use"
    if both_channel:
        return "account_takeover"
    if ch == "online" and (row.get("id_15") == "New" or row.get("is_new_device_for_card") is True):
        return "card_not_present_new_device"
    if ch == "online":
        return "card_not_present_fraud"
    return "none"


def node_explain(state: AgentState) -> AgentState:
    from sentinel.agent.explain import LLMGenerationError, LLMSkipped, write_pattern_description
    from sentinel.llm import LAST_PROVIDER, STATS as LLM_STATS
    from sentinel.output.answer_file import compute_similar_prior_cases
    with _time("explain", state):
        decision: PolicyDecision = state["policy_decision"]
        actions_json = [a.as_dict() for a in decision.actions]
        tin0, tout0 = LLM_STATS.tokens_input, LLM_STATS.tokens_output

        # GraphRAG inputs: top-3 memory hits (already retrieved by
        # node_memory_retrieve), top-3 policy chunks via the same vector
        # index (fallback: none if the query fails).
        hits = (state.get("memory_hits") or [])[:3]
        policy_hits = _retrieve_policy_chunks(state)[:3]
        # Citation whitelist for the LLM: matches what the answer file
        # will persist in `similar_prior_cases`. I12 enforces this over
        # the persisted answer text.
        similar_prior = compute_similar_prior_cases(state.get("memory_hits") or [])

        try:
            state["explanation"] = write_summary(
                state["case_id"], state["verdict"], state["fraud_probability"],
                state.get("pattern", "none"), actions_json, state["ledger"],
                memory_hits=hits, policy_hits=policy_hits,
                similar_prior_cases=similar_prior,
            )
            state["llm_provider"] = LAST_PROVIDER.get("name")
            state["llm_model"]    = LAST_PROVIDER.get("model")
        except LLMSkipped:
            state["explanation"] = ""
            state["llm_provider"] = "disabled"
            state["llm_model"] = None
        except LLMGenerationError as e:
            _record_error(state, "llm:summary", str(e))
            state["explanation"] = ""
            state["llm_provider"] = "failed"
            state["llm_model"] = None

        # Pattern description for undocumented (or fallback-labelled) patterns.
        if state.get("pattern") == "undocumented":
            try:
                state["pattern_description_prose"] = write_pattern_description(
                    state["case_id"], state["pattern"], state["ledger"],
                    memory_hits=hits,
                    similar_prior_cases=similar_prior,
                )
            except (LLMSkipped, LLMGenerationError):
                state["pattern_description_prose"] = ""

        state["sar_narrative"] = None
        if decision.sar_should_file:
            row = state.get("txn_row", {})
            try:
                state["sar_narrative"] = write_sar(
                    state["case_id"], state["verdict"],
                    float(state.get("exposure_usd", 0) or row.get("TransactionAmt", 0) or 0),
                    state.get("pattern", "none"),
                    policy_hits=policy_hits,
                    ledger=state["ledger"],
                    memory_hits=hits,
                    similar_prior_cases=similar_prior,
                )
            except LLMSkipped:
                pass
            except LLMGenerationError as e:
                _record_error(state, "llm:sar", str(e))
        tel: Telemetry = state.setdefault("telemetry", Telemetry())
        tel.tokens_input  += LLM_STATS.tokens_input  - tin0
        tel.tokens_output += LLM_STATS.tokens_output - tout0
    return state


def _retrieve_policy_chunks(state: AgentState) -> list:
    """Best-effort semantic retrieval over PolicyChunk vertices; empty on failure."""
    try:
        from sentinel.llm import embed_one
        from sentinel.retrieval.hybrid import semantic_policy
        top_evidence_text = " ".join(
            (e.claim or "") for e in (state.get("ledger") and state["ledger"].items or [])[:5]
        )[:400] or state.get("trigger_text", "") or ""
        if not top_evidence_text.strip():
            return []
        qv = embed_one(top_evidence_text)
        return semantic_policy(qv, top_k=3)
    except Exception:  # noqa: BLE001
        return []


def node_write_memory(state: AgentState) -> AgentState:
    """Persist a SentinelCase to TigerGraph via the write_case installed query.

    Best-effort — offline runs skip this cleanly. Set
    ``SENTINEL_WRITE_MEMORY_DISABLED=1`` in the env to skip write_case
    entirely (used by the backtest for latency).
    """
    import os as _os
    with _time("write_memory", state):
        if _os.getenv("SENTINEL_WRITE_MEMORY_DISABLED", "").strip() in ("1", "true", "TRUE"):
            state["case_vertex_id"] = None
            state["_write_memory_skipped"] = True
            return state
        try:
            from sentinel.agent.id_resolver import card_tuple_id, txn_vertex_id
            from sentinel.config import TG_GRAPHNAME
            from sentinel.graph.client import TGClient

            case_vid = f"CASE-{state['case_id']}"
            row = state.get("txn_row", {})
            exposure = float(row.get("TransactionAmt", 0) or 0) \
                if state["verdict"] != "legitimate" else 0.0
            device_profile = row.get("device_profile")
            addr1 = row.get("addr1")

            connected_devices = [{"id": device_profile}] if device_profile else []
            regions = ([{"id": str(addr1)}]           # BillingRegion.primary_id is string form of the addr1 float
                       if addr1 is not None else [])

            payload = {
                "p_case_id":      case_vid,
                "p_hhg_id":       state["case_id"],
                "p_card":         {"id": card_tuple_id(state["card_id"])},
                "p_customer":     {"id": state["customer_id"]},
                "p_opened_at":    state["opened_at"],
                "p_verdict":      state["verdict"],
                "p_pattern":      state.get("pattern", "none"),
                "p_fraud_probability": state["fraud_probability"],
                "p_exposure_usd":      exposure,
                "p_summary":      state.get("explanation", "")[:1500],
                "p_affected_txns":     [{"id": txn_vertex_id(state["txn_id"])}]
                                        if state["verdict"] != "legitimate" else [],
                "p_connected_cards":   [],
                "p_connected_devices": connected_devices,
                "p_regions":           regions,
                "p_similar_closed":    [
                    {"id": h.id} for h in (state.get("memory_hits") or [])
                    if getattr(h, "kind", None) == "closed_case"
                ][:10],
            }
            tg = TGClient()
            use_mcp = _os.getenv("SENTINEL_GRAPH_VIA_MCP", "1").strip().lower() in ("1", "true", "yes")
            if use_mcp:
                from sentinel.graph.mcp_client import sync_run_installed_query
                try:
                    res = sync_run_installed_query(TG_GRAPHNAME, "write_case", payload)
                except Exception as e:  # noqa: BLE001
                    res = {"error": True, "message": str(e)[:200]}
            else:
                res = tg.run_query(TG_GRAPHNAME, "write_case", payload)
            if isinstance(res, dict) and res.get("error"):
                raise RuntimeError(res.get("message", "unknown"))
            state["case_vertex_id"] = case_vid
            tel: Telemetry = state.setdefault("telemetry", Telemetry())
            tel.tool_calls += 1
            global _SENTINEL_MEMORY_HAS_DATA
            _SENTINEL_MEMORY_HAS_DATA = True
        except Exception as e:  # noqa: BLE001
            _record_error(state, "write_memory", str(e))
            state["case_vertex_id"] = None
    return state


def node_emit(state: AgentState) -> AgentState:
    """Assemble the pydantic AnswerFile and validate invariants."""
    with _time("emit", state):
        from sentinel.output.answer_file import build_answer_file
        answer = build_answer_file(state)
        violations = check_invariants(answer)
        if violations:
            state.setdefault("errors", []).extend(
                [f"{v.invariant}: {v.detail}" for v in violations])
        state["answer"] = answer
    return state


# ---------- graph assembly ----------------------------------------------------


def _uncertainty_gate(state: AgentState) -> str:
    """Route: 'voi' whenever verdict == 'uncertain' (P5v5-4).

    Even p < 0.30 or p > 0.85 outside the two-channel gate still route
    through simulate — the simulator's rule (legit-archetype vs non-risk
    evidence sum) decides. Certain verdicts (fraud/legitimate with ≥2
    channels) skip VOI.
    """
    verdict = state.get("verdict", "uncertain")
    if verdict == "uncertain":
        return "voi"
    return "critic"


def build_graph():
    """Return a compiled LangGraph. Falls back to a plain runner if
    langgraph isn't installed (tests still exercise every node).
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        return None

    g = StateGraph(dict)  # type: ignore[arg-type]
    g.add_node("trigger", node_trigger)
    g.add_node("open_case", node_open_case)
    g.add_node("gather_baseline", node_gather_baseline)
    g.add_node("run_detectors", node_run_detectors)
    g.add_node("score_alert_model", node_score_alert_model)
    g.add_node("memory_retrieve", node_memory_retrieve)
    g.add_node("assess", node_assess)
    g.add_node("voi_plan", node_voi_plan)
    g.add_node("simulate_response", node_simulate_response)
    g.add_node("reassess", node_reassess)
    g.add_node("critic", node_critic)
    g.add_node("decide", node_decide)
    g.add_node("explain", node_explain)
    g.add_node("write_memory", node_write_memory)
    g.add_node("emit", node_emit)

    g.set_entry_point("trigger")
    g.add_edge("trigger", "open_case")
    g.add_edge("open_case", "gather_baseline")
    g.add_edge("gather_baseline", "run_detectors")
    g.add_edge("run_detectors", "score_alert_model")
    g.add_edge("score_alert_model", "memory_retrieve")
    g.add_edge("memory_retrieve", "assess")
    g.add_conditional_edges("assess", _uncertainty_gate,
                            {"voi": "voi_plan", "critic": "critic"})
    g.add_edge("voi_plan", "simulate_response")
    g.add_edge("simulate_response", "reassess")
    g.add_edge("reassess", "critic")
    g.add_node("assemble_episode", node_assemble_episode)
    g.add_edge("critic", "decide")
    g.add_edge("decide", "assemble_episode")
    g.add_edge("assemble_episode", "explain")
    g.add_edge("explain", "write_memory")
    g.add_edge("write_memory", "emit")
    g.add_edge("emit", END)
    return g.compile()


# ---------- fallback runner --------------------------------------------------


def run_plain(state: AgentState) -> AgentState:
    """Deterministic single-thread runner (no LangGraph dependency).

    Same node order as ``build_graph()`` — used by tests and by the demo when
    langgraph isn't importable.
    """
    state = node_trigger(state)
    state = node_open_case(state)
    state = node_gather_baseline(state)
    state = node_run_detectors(state)
    state = node_score_alert_model(state)
    state = node_memory_retrieve(state)
    state = node_assess(state)
    if _uncertainty_gate(state) == "voi":
        state = node_voi_plan(state)
        state = node_simulate_response(state)
        state = node_reassess(state)
    state = node_critic(state)
    state = node_decide(state)
    state = node_assemble_episode(state)
    state = node_explain(state)
    state = node_write_memory(state)
    state = node_emit(state)
    return state
