# Architecture Decision Log

Each entry: date, decision, rationale, alternatives considered.

## 2026-09-20 — Repo skeleton and data location

- Data files (`transactions.csv`, `identity.csv`, `closed_cases_history.csv`, `case_pack.csv`,
  `README.md`) moved from repo root into `data/raw/`. `.gitignore` keeps the two large CSVs
  (`transactions.csv`, `identity.csv`) and derived Parquet/graph_load out of git; `README.md`,
  `case_pack.csv`, and `closed_cases_history.csv` are committed as read-only reference data.
- Alternative: keep files at repo root. Rejected because the brief mandates `data/raw/` and
  code should not have to know about repo-root special-cases.

## 2026-09-20 — TigerVector confirmed on Savanna 4.2.5

Round-trip verified via a smoke script (upsert 12-dim vector, query top-k):

1. `add_vector_attribute(ClosedCase, smoke_emb, dim=8, metric=COSINE)` — succeeds.
2. `upsert_vectors` with rows `{"vertex_id": "<id>", "vector": [...]}` — 3/3 upserted.
3. `search_top_k_similarity` returns CC-2985 (0.95 cos-sim) before CC-2971
   (0.0 cos-sim) when queried with `[1,0,...,0]`. Ordering is correct.
4. `fetch_vector(vertex_ids=[...])` returns stored embeddings.
5. `list_vector_attributes` shows `{vertex_type, vector_name, dimension, index_type: HNSW, data_type: FLOAT, metric: COSINE}`.
6. `drop_vector_attribute` cleanly removes.

Quirks / gotchas:
- The **scope of `add_vector_attribute` is `"global"`** — the attribute lives
  on the vertex type at global scope, not per-graph. So even though we passed
  `graph_name="FraudGraph"`, it's visible workspace-wide. Re-add fails with
  "The vector name … conflict" until you `drop_vector_attribute`.
- The Phase-3 embedding builder must be idempotent: try `drop_vector_attribute`
  first (ignoring not-found) before `add_vector_attribute`.
- Upsert row shape is `{"vertex_id": str, "vector": list[float]}` — NOT
  `{"id": ...}` or attribute-keyed. `fetch_vector` takes `vertex_ids` (plural).
- Index type is **HNSW**; data type is **FLOAT (single precision)**. Metric
  options include COSINE, EUCLIDEAN, L2 — COSINE is what we want for embeddings.
- Dimension: 8 confirmed working. TG documentation supports up to 32,768;
  Gemini `text-embedding-004`/`gemini-embedding-001` typically emit 768 or
  3072-dim vectors — both well within limits.
- The HNSW index is built asynchronously; wait for
  `get_vector_index_status` → `"Ready"` before calling
  `search_top_k_similarity`. Empirically ready in <5s for 3 vectors.

## 2026-09-20 — MCP auth working via TG_JWT_TOKEN

Confirmed with pyTigerGraph 2.0.4 (latest as of this session) that
`conn.getToken(secret)` still fails with "User authentication failed" on
TigerGraph 4.2.5. The library still targets the legacy REST++
`/requesttoken` route; on 4.x the token flow moved to `/gsql/v1/tokens`.

`tigergraph-mcp` 1.0.3, however, accepts a pre-minted JWT via `TG_JWT_TOKEN`
and its `ConnectionManager` uses it as a bearer for both REST++ and GSQL
routes. So the wiring is:

    sentinel/graph/token.py   -- mints/refreshes JWT via /gsql/v1/tokens
    sentinel/graph/token.export_to_env()  -- pushes it as TG_JWT_TOKEN
    tigergraph-mcp                        -- reads TG_JWT_TOKEN + TG_HOST +
                                             TG_TGCLOUD + TG_RESTPP_PORT/
                                             TG_GS_PORT and just works.

Note: with `TG_JWT_TOKEN` in the env, do NOT also set `TG_GRAPHNAME` to a
graph that doesn't exist yet; the ConnectionManager validates by echoing,
which is fine, but a stale graphname will cause graph-scoped tools to 404.
Clear `TG_GRAPHNAME` before invoking global tools like `list_graphs` and
`get_global_schema`; set it once `FraudGraph` is created.

**Verified round-trip** via the MCP probe script:
- `tigergraph__list_graphs` → `{"graphs": ["Transaction_Fraud"], "count": 1}`.
  The `Transaction_Fraud` graph is a Savanna-provided demo (not ours) and we
  leave it untouched.
- `tigergraph__get_global_schema` → 22 KB of demo schema (Party, Merchant,
  Payment_Transaction, ...). Our `FraudGraph` will be added alongside it.

## 2026-09-20 — TigerGraph auth: JWT wrapper, not pyTigerGraph.getToken()

- Savanna runs **TigerGraph 4.2.5**; JWT is issued at `POST /gsql/v1/tokens` with
  `{secret, lifetime}` and optionally `{graph}` to graph-scope.
- pyTigerGraph 2.0.4's `getToken()` posts to the legacy `/restpp/requesttoken`
  endpoint which returns 400 on 4.x. Once a token is set manually via
  `conn.apiToken = <jwt>`, `/restpp/*` works, but `gsql()` internally re-mints
  through the legacy path and 401s.
- Decision: implement a thin **JWT-bearer client** in `sentinel/graph/client.py`
  wrapping `POST /gsql/v1/statements` and `POST /restpp/query/{graph}/{name}`.
  Reuse pyTigerGraph only for the pyTG-friendly upsert/load helpers where it
  works without token re-mint.
- `FraudGraph` doesn't exist yet — will be created by `graph/install.py` in
  Phase 2. Graph-scoped token minting deferred until then.

## 2026-09-20 — Prior-fraud signal split: card tuple vs customer

Verified for C11923, C09933, C02354 that every prior-fraud txn from their
closed-case history lands on the **same card tuple** as their case-pack card
(C11923-K2, C09933-K2, C02354-K2). Baseline `n_txns` drops equal exactly the
confirmed-fraud-on-tuple counts (226, 85, 107). So the tuple-based
`card_baseline_clean` is provably correct.

Regardless, the LR table will carry **two** signals as separate features:

- **`prior_fraud_on_card_tuple`** — same `(customer_id, c2..c6)` had ≥1 confirmed
  fraud case closed on or before the alert's `ts`. Strong signal (LR-in-Phase-4).
- **`prior_fraud_on_customer`** — same `customer_id`, any tuple, had ≥1 confirmed
  fraud case. Weaker signal; captures compromised cardholders who got a
  reissued card.

For the current 20 benchmark cases all fraud priors are on-tuple, but future
data (or a customer who acquires a fresh card mid-fraud-window) requires the
distinction. Both features go into the ledger; the policy engine reads only
their combined log-LR.

## 2026-09-20 — Point-in-time (as-of) features replace the label-leaked baseline

The Phase-1 `card_baseline_clean` (which excluded confirmed-fraud txns to
"produce a clean baseline") was the root cause of two orders of magnitude of
label leakage in the LR table: `is_new_device_for_card` came out at LR 76,750
because the very devices used in confirmed frauds had been removed from the
"known" set by construction.

Fix — `sentinel/data/pit_features.py`:

- Every card-relative signal is computed **strictly as-of** each transaction,
  using only txns on the same card with `ts` earlier than the current row.
- No reference to the fraud/cleared labels; the negative class is honest.
- Distinct-set aggregates (`prior_devices`, `prior_products`, `prior_p_emails`)
  are done via incremental Python scan (per-card group, O(n_txn) total).
  Cheap window aggregates (COUNT, MEDIAN over RANGE, ntile) stay in DuckDB.
- Signals are `NULL` (i.e. "did not fire") when `n_prior_txns < 5` — no
  spurious signal from cards without enough history.

New signals added on the same pass:

- `n_online_last_48h_bucket ∈ {'0','1','2-4','5+'}`,
  `mixed_channel_last_24h`, `risk_score_decile ∈ 1..10`.
- `prior_fraud_on_card_tuple`, `prior_fraud_on_customer`,
  `prior_cleared_travel_on_card`, `prior_cleared_new_phone_on_card`, each
  keyed on `closed_at < ts` so no future information leaks.

The rebuilt LR table has 27 signals. The leakage-free `is_new_device_for_card`
LR is ≈ 0.8 (near neutral), not 2–50 — see `docs/LEARNINGS.md` for the full
explanation of why the naïve "new-device = fraud" intuition doesn't survive
honest measurement in this dataset.

## 2026-09-20 — Scoring prior fixed at p=0.5 (logit 0)

The README's *Things to know* section states plainly: **"Half the cases are
legitimate. Many look suspicious. An agent that blocks everything scores badly."**
That's the operative belief at alert time.

We therefore fix the scoring prior at:

    p_fraud_prior = 0.5    (log-odds 0)

and let the evidence LRs move the log-odds up or down. Isotonic calibration
then re-shapes the raw probability into a calibrated one against the labelled
dataset. Two consequences:

- The closed-case base rate (14,055 fraud / 590,742 total ≈ **2.4%**) is
  written to ``lr_table.json`` under ``closed_case_base_rate`` for reporting
  only and is **not** used in the scoring path.
- Earlier attempts at trigger-type priors (0.838 global) reflected the
  historical labelled distribution, not the belief we should hold at alert
  time. They are removed from the scoring path — the calibrator learns
  whatever residual adjustment is needed.

## 2026-09-20 — Embeddings run locally on `BAAI/bge-small-en-v1.5` (CPU, 384-dim)

Gemini `gemini-embedding-001` free tier gates at **1,000 embed requests per
day** (`retryDelay: 0s` means "wait until UTC-midnight reset"). Reaching that
limit stalls Phase 3 retrieval hard and Phase 4 backtests worse: 5,565 closed
cases + repeated evidence queries would need dozens of days per run.

Switching embeddings to a local CPU model:

- **Model**: `BAAI/bge-small-en-v1.5` via ``sentence-transformers``, 384-dim,
  cosine metric, `normalize_embeddings=True`.
- **Cost / rate limit**: none. Runs entirely on the developer's box.
- **Wall time** for full-corpus embed (5,565 closed cases + 112 doc chunks,
  batch=64): **139.9 s** on Linux/Python 3.11 CPU. ~24.7 s / 1,000 texts.
- **Cache key** unchanged: `sha256("embed", model_name, dim, text)`. Since the
  model name and dim differ from the Gemini set, old cache entries stay valid
  under their old keys and don't collide.
- All four FraudGraph vector attributes are dropped and re-created at
  `dimension=384`; upsert happens after the workspace restart.

**Gemini is retained for generation only** — case summaries, SAR narratives,
undocumented-pattern descriptions, the adversarial critic. That path stays
inside a comfortable free-tier request budget because Sentinel runs 20 cases,
not 5,565 embeddings.

## 2026-09-20 — LLM provider

- Gemini (free AI Studio key) via a single `sentinel/llm.py` wrapper. `LLM_MODEL=gemini-3-flash`
  for reasoning, `LLM_MODEL_CHEAP=gemini-3.1-flash-lite` for extraction, `EMBEDDING_MODEL=gemini-embedding-001`.
- Rationale: brief section 6 supersedes earlier Anthropic assumption; free tier keeps every
  call cheap.
- No Gemini 2.5 (retired Oct 16, 2026 per brief).

## 2026-09-20 — Risk-score handling (CHECKPOINT 5 final)

The bank's `risk_score` on the flagged transaction is treated as follows:

- **`trigger_type == "risk_score"`**: the decile contributes **0 log-LR**. The
  0.5 posterior prior already conditions on being alerted, and the README
  explicitly says "above 0.7 most flagged are legitimate — never treat it as
  the answer". Using it again as evidence would double-count.
- **`trigger_type == "customer_report"` or `"analyst_request"`**: the risk
  score is independent of the trigger, so we use the *alert-conditional* LR
  — `P(decile | fraud)` vs `P(decile | cleared)` from `ClosedCase` history
  — and clip to `|log_lr| ≤ 0.7` (LR ∈ [0.5, 2.0]) so a single history-
  channel signal never drives the posterior on its own.

The alert-conditional table is stored in `sentinel/evidence/lr_table.json`
under `risk_decile_alert_conditional`. Deciles 1-9 have zero cleared cases
in the ClosedCase history, which would give unbounded LRs — the ±0.7 clip
protects against that degeneracy.

Alternative considered: keep the population-level `risk_score_decile_10` LR
of 6.5 (log +1.878). Rejected: on risk-score-triggered cases this single
signal pushes p to 0.87 with no other evidence, exactly the "blocks
everything" failure the README warns against.
