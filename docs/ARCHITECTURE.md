# Sentinel — Architecture Deep Dive

## 1. Data plane

| Store | Purpose |
|---|---|
| **TigerGraph FraudGraph** | Card / device / region / txn / closed-case graph + `notes_embedding` HNSW index (384-dim, cosine). Written to by the agent (`SentinelCase` vertices). |
| **DuckDB** (`data/parquet/sentinel.duckdb`) | Feature store: point-in-time `txn_features`, `card_map`, `cc_join`, `dev_degree`. Used by detectors + the alert model. |
| **BAAI/bge-small-en-v1.5** (local, CPU) | Sentence-transformers for embedding case notes + queries. Deterministic across runs. |
| **`sentinel/evidence/alert_model.json`** | Fitted L2-logistic weights + intercept + 5-fold CV metrics. |

## 2. Agent (LangGraph)

Nodes in order:
1. `trigger` — hydrate the txn row from `txn_features`.
2. `open_case` — seed the ledger.
3. `gather_baseline` — one call fans out **8 TG queries in parallel**.
   Routing is env-gated. `SENTINEL_GRAPH_VIA_MCP=1` is the **library
   default** — every unset caller (investigation path, `run_all_20.py`,
   `run_case.py`, ad-hoc REPL) dispatches through the `tigergraph-mcp`
   stdio server via `sentinel.graph.mcp_client.mcp_run_installed_query`
   (single long-lived session, shared across the process). The 150-case
   backtest explicitly sets `SENTINEL_GRAPH_VIA_MCP=0` in
   `sentinel/backtest/run.py` to force the `asyncio.gather` REST fast-path
   over `httpx.AsyncClient` and avoid serialising 150 cases through one
   stdio subprocess. Both paths
   share the same eight-query set:  `ring_components`,
   `near_threshold_burst`, `testing_sequence`, `burst_48h`,
   `recurring_match`, `device_neighbors`, `region_history`,
   `region_cluster`. Total wall-clock ≈ 1.5-2 s (vs 24 s sequential).
4. `run_detectors` — 12 detector functions read the row + `graph_signals`
   and emit `pattern` labels + typology tags. Evidence contributions are
   zeroed (alert_model owns posterior).
5. `score_alert_model` — extracts a 45-dim binary/one-hot feature vector,
   scores via `sigmoid(intercept_adj + Σ coef·x)`. Each firing feature
   becomes a ledger Evidence item with `log_lr = coef`.
6. `memory_retrieve` — REST vector-search (`vector_search_cc`) for
   ClosedCases; MCP semantic search for SentinelCases (skipped on
   first-case cold-start). All time-gated by `as_of`.
7. `assess` — de-dup ledger, sum log-LRs (cap ±4), sigmoid → posterior.
   Verdict per §6: **denied⇒fraud, confirmed⇒legitimate, no_reply⇒uncertain;
   otherwise 2-channel + 0.85/0.15**.
8. `voi_plan` + `simulate_response` + `reassess` — only fires when the
   customer hasn't already responded (customer_report trigger short-circuits
   through denial). Simulator: `denied` iff `p_initial ≥ τ`, else
   `confirmed`; overrides: ring T3+/testing → denied, legit-archetype →
   confirmed; `no_reply` only when the graph is unreachable.
9. `critic` — HHG-011-style adversarial check on shared-device evidence
   (`activity_window_overlap`, `first_seen_on_card`, id_15/id_23).
10. `decide` — deterministic policy engine (`sentinel/policy/policy_engine.py`).
    Guarantees `BLOCK_CARD` on any fraud verdict except R7. R10 gated on
    ≥2 DISTINCT prior fraud card tuples for the customer.
11. `assemble_episode` — for fraud verdicts, expands `affected_txn_ids`
    to the card's ±48h txns sharing the fraud signature (same new device,
    same out-of-home region, or online burst). Records Jaccard before/after.
12. `explain` — Gemini generates the natural-language summary + (if SAR)
    the FinCEN narrative. Falls through to Groq on daily-quota 429.
13. `write_memory` — persists a `SentinelCase` vertex with all edges via
    the `write_case` installed query. Skipped when
    `SENTINEL_WRITE_MEMORY_DISABLED=1`.
14. `emit` — assembles the pydantic answer file and validates I1-I13.

## 3. Policy engine

- 14 canonical actions with routes fixed by §2.
- Rules R1-R10 in a single dispatch (`_decide_impl`) with a post-process
  `_ensure_block_card_on_fraud` guarantee.
- R10 gate uses `sentinel/agent/r10.py` — counts DISTINCT card tuples with
  `outcome=confirmed_fraud AND closed_at < opened_at`, +1 if the current
  card's verdict is fraud.
- Invariants I1-I13 in `sentinel/policy/invariants.py`, enforced on every
  answer file:
  1. `sar.file` ⇔ `FILE_REPORT` in final actions.
  2. `verdict=legitimate` ⇒ empty affected_txn_ids, 0 exposure, no SAR.
  3. `pattern=undocumented` ⇔ non-empty `pattern_description`.
  4. `evidence_requests=[]` ⇒ `final == initial` and `what_changed=nothing`.
  5. `exposure_usd == round(sum(|amt|) over affected_txn_ids, 2)`.
  6. Every ID exists in the dataset.
  7. Every action's route matches §2 exactly (BLOCK_CARD re-routed by
     final exposure).
  8. Every action's reason cites Rn or §Nx.
  9. `verdict=legitimate` ⇒ `pattern=none`.
  10. `BLOCK_ALL_CARDS` allowed only when `verdict=fraud` and never
      alongside `CLOSE_NO_FRAUD` (R10 surface guard).
  11. `verdict=fraud ⇒ fraud_probability ≥ 0.5` and
      `verdict=legitimate ⇒ fraud_probability ≤ 0.5` (probability agrees
      with settled verdict; clamped in `node_assess`/`node_reassess`).

## 4. Alert model

L2-regularized logistic regression, class-balanced, trained on all 5,565
ClosedCases (4,665 confirmed_fraud + 900 cleared):

- **45 features**: 15 binary signals (id_15='New', proxy, unseen product /
  email, out_of_home_region, prior_fraud_on_{card_tuple,customer},
  prior_cleared_travel/new_phone, mixed_channel_last_24h, recurring ≥ 3),
  10 risk_score decile one-hot, 2 channel one-hot, 12 device tier × degree
  bucket flags (T1..T4 × [≤5, 6-20, 21-100]), 5 graph flags
  (device_neighbors ≥ 2, region_cluster ≥ 2, recipient_cluster ≥ 2,
  testing_sequence_fires, near_threshold_burst).
- **5-fold CV**: AUC 0.9465 ± 0.0046, Brier 0.0927.
- **Intercept rebasing**: trained intercept encodes the 0.838 prevalence
  bias in the ClosedCase pool; we subtract `logit(0.838)` so `no evidence
  → p = 0.5`.
- Top coefficients (sorted by |coef|): id_15_new (-4.93), risk_decile_10
  (-4.54), channel_online (+2.72), amt_z_gt_3 (-2.60), risk_decile_9
  (+1.64), proxy_flag (-1.59). Negative coefficients on "flag" features
  reflect that alerted-but-cleared cases *also* carry them (the model
  learns to separate fraud from cleared, not fraud from all txns).

## 5. UI

FastAPI backend (`ui/backend/main.py`) with 9 endpoints. Static SPA
frontend (`ui/frontend/{index.html,styles.css,app.js}`) with 9 views:
case list, detail, evidence + posterior, timeline, actions + what-if,
SAR, d3 force-layout graph, backtest (reliability plot inline), memory
(live SentinelCase count from TG).

The what-if endpoint (`POST /api/whatif/{case_id}`) re-runs the policy
engine with a swapped customer response so investigators can preview
what the actions would have been if the customer had said something else.

## 6. Latency budget (per case, warm caches)

| Stage | Time |
|---|---|
| Trigger + hydrate | 20 ms |
| Parallel graph_signals (8 queries) | 1.5-2 s |
| Detectors + alert_model | < 100 ms |
| Memory retrieve (REST vector search) | 1-2 s |
| Assess + policy + assemble_episode | ~50 ms |
| Explain (Gemini) | 2-6 s |
| Write_case | 500 ms |
| **Total** | **~5-10 s (LLM on), ~2.5 s (LLM off)** |

## 7. Tests

- 89 offline unit tests (policy, features, calibration, R10, invariants
  I1–I11 including the dedicated I10/I11 suite, BLOCK_CARD-on-fraud,
  ring components, SAR replay, channels/simulator).
- 16 query-contract tests hitting every one of the 18 installed GSQL
  queries with the exact param dicts the agent uses.
- Total suite: **129 tests** (`python -m pytest tests/`).
