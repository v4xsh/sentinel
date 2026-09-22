# Sentinel — Social posts

## X / Twitter thread

**1/** We built Sentinel for @TigerGraphDB × Hacker House Goa 2026: an
agent that reads six months of Vesta card txns as a graph and decides
whether each of 20 flagged cases is fraud, what kind, and what to do
next — with evidence, actions with approval routes, and (when the policy
calls for it) a full SAR.

**2/** Every fact the LLM cites comes from a graph query or a fitted
model. The LLM writes prose. The policy engine decides actions. Two
different jobs — and only one of them can hallucinate, so we contained
it.

**3/** TigerGraph MCP + REST + a native `vectorSearch()` installed
query. HNSW cosine over 5,565 closed-case notes (`BAAI/bge-small-en-v1.5`
embeddings). 8 GSQL queries in parallel via asyncio.gather ⇒ 1.5 s per
case, down from 25 s sequential.

**4/** The alert model is an L2 logistic over 45 as-of features on all
5,565 closed cases. 5-fold CV: **AUC 0.9465, Brier 0.09**. Fraud vs
cleared, not fraud vs everything, so the coefficients actually surprise
you (id_15='New' is *slightly against* fraud — half the cleared cases
are new-phone travellers).

**5/** Backtest, 150 stratified cases (75 tune / 75 held-out eval):
- Oracle (using each case's actions_taken): **100% verdict accuracy**.
- Simulated (τ=0.30, tuned on tune half): **83.3%**.
Both modes: 20/20 benchmark answer files pass all I1–I11 invariants
(SAR ⇔ FILE_REPORT, probability agrees with the settled verdict, etc.).

**6/** FinCEN §3a policy encoding reproduces the historical SAR decision
on **4,665 / 4,665** confirmed-fraud cases (rule-encoding test, see
`tests/test_sar_replay.py`).

**7/** UI: FastAPI backend + vanilla-JS + d3 SPA. Nine views: case list,
detail, evidence + posterior, timeline, initial-vs-final actions with
route badges, SAR narrative, force-layout graph neighbourhood, backtest
report inline (reliability plot + confusion matrix), memory (live
SentinelCase count from TG).

**8/** What-if endpoint: `POST /api/whatif/{case_id}` re-runs the
policy engine with a swapped customer response. Investigators can
preview what would have happened if the customer had said otherwise —
deterministic, sub-10ms, no LLM.

**9/** Everything land-writes: each investigation becomes a
`SentinelCase` vertex with edges to card, customer, txns, devices,
regions, similar-prior ClosedCases. 35 vertices in the graph (20 benchmark + 15 monitoring) after
the benchmark set. HHG-014 replayed found `CASE-HHG-014` in memory —
the loop closes.

**10/** Repo, demo script, architecture writeup, backtest report, and a
1,500-word blog post at [link]. Thanks @TigerGraphDB for the workspace
and the dataset. #GraphRAG #Fraud

## LinkedIn version

**Sentinel: an agentic fraud investigator on TigerGraph**

Submission to Hacker House Goa 2026 × TigerGraph. Sentinel is an agent
that reads a card-transaction graph and produces, for each flagged
alert: a verdict (fraud / legitimate / uncertain), a fraud pattern
label, the actions the bank should take with approval routes, and a
Suspicious Activity Report when the policy calls for one.

**The design principle**: the LLM writes prose but never decides. Every
action the agent recommends comes from a deterministic policy engine
implementing the 10 rules the bank's fraud team wrote. Every claim in
the prose has a graph query or a fitted-model coefficient behind it.
Two different jobs, and only one of them can hallucinate.

**Under the hood**:
- TigerGraph FraudGraph with 7 vertex types, HNSW cosine index over
  5,565 closed-case notes, 18 installed GSQL queries. 8 queries dispatched
  in parallel per case via `asyncio.gather` on `httpx.AsyncClient` — 15×
  speedup over sequential.
- L2-logistic alert model trained on all 5,565 closed cases at once.
  5-fold CV AUC 0.9465, Brier 0.09. 45 features spanning binary flags,
  risk-decile one-hots, channel, and 12 device-tier × degree-bucket
  interactions.
- LangGraph state machine with a critic, VOI planner, deterministic
  simulator, and a memory write-back node that persists every case as a
  `SentinelCase` vertex with edges.

**Results on the 20-case benchmark**:
- 20/20 answer files pass invariants I1–I11 (SAR ⇔ FILE_REPORT, verdict
  ⇔ pattern, probability agrees with settled verdict, IDs valid,
  actions cite rules, etc.)
- **12 fraud, 8 legitimate, 0 uncertain** finals. 4 SARs filed.
- HHG-014 undocumented-ring reasoning ends with `MONITOR_CONNECTED_CARDS`
  covering the shared-device blast radius.
- HHG-003 R7 (customer disputes recurring charge) correctly does NOT
  block the card.

**Results on the 150-case backtest**:
- 75-case tune half → τ tuned; 75-case held-out eval half reported
  separately (optionally out-of-time: tune from Jul–Sep, eval Oct+).
- Oracle mode: 100% verdict accuracy (n=150, excl. uncertain 1.000).
- Simulated mode (τ=0.30 tuned): 83.3%.
- FinCEN §3a policy encoding replay: **4,665 / 4,665** historical SAR
  decisions reproduced (rule-encoding test).
- Latency: 2.5 s per case with LLM off (REST fast-path), ~5 s with LLM
  on (shared MCP session).

**UI**: FastAPI + vanilla JS + d3, one-command launcher (`./run_ui.sh`).
Nine views, including a live graph neighbourhood from `card_window +
device_neighbors`, a what-if toggle that re-runs the policy engine with
a swapped customer response, and the full backtest report with a
reliability plot rendered inline.

Repo: [link] · Demo: [link] · Blog: [link]

Thanks to @TigerGraphDB for the workspace, the dataset, and the MCP
stack. #Fraud #GraphDatabases #TigerGraph
