# Sentinel — social posts

*Hacker House Goa 2026 × TigerGraph submission.*  Repo: **https://github.com/v4xsh/sentinel**

## X / Twitter thread

**1/** We built Sentinel for @TigerGraphDB × Hacker House Goa 2026: an
agent that reads six months of card transactions as a graph, and for
each flagged alert produces a verdict, a fraud-pattern label, actions
with §2 approval routes, and (when the policy calls for it) a
FinCEN-shaped SAR.

**2/** Every fact the LLM cites is grounded in a graph query or a
fitted-model coefficient. The LLM writes prose; a deterministic policy
engine (R1–R10) decides actions. Two jobs; only one can hallucinate,
and we contained it.

**3/** TigerGraph FraudGraph + TigerVector HNSW cosine index over 5,565
closed-case `notes_embedding` vectors. 18 installed GSQL queries; 8
dispatched in parallel per case via `asyncio.gather` on
`httpx.AsyncClient` → ~2 s wall-clock, down from ~25 s sequential.

**4/** All investigation graph calls go through **tigergraph-mcp** via
a single long-lived stdio session shared across the whole process
(background asyncio loop, daemon thread).
`SENTINEL_GRAPH_VIA_MCP=1` is the library default. Full per-call MCP
transcript for one case in the repo.

**5/** Alert model: class-balanced L2 logistic over 45 as-of features
on all 5,565 closed cases. 5-fold CV **AUC 0.9465 ± 0.0046, Brier
0.0927**. Trained fraud-vs-cleared, not fraud-vs-everything — the
coefficients actually surprise you (id_15='New' → -4.93; slightly
*against* fraud, because half the cleared cases are new-phone
travellers).

**6/** 150-case backtest: 75 tune / 75 held-out eval. Simulated
(τ=0.30) → **83.3% verdict accuracy**. Oracle mode (customer_response
derived from actions_taken) → 100%, but that's a policy-engine
correctness check, not a model claim. OOT AUC (train Jul–Sep, test
Oct+) in the repo.

**7/** FinCEN §3a policy encoding replay: fed every one of the 4,665
`confirmed_fraud` rows through `_sar_should_file(...)` and every one
matched the historical `report_filed` field. **4,665 / 4,665**. Rule
encoding — the end-to-end run is the 150-case backtest.

**8/** 20-case benchmark: **12 fraud / 8 legitimate / 0 uncertain**
finals; **4 SARs** (HHG-004, HHG-006, HHG-011, HHG-014). All 20 answer
files pass invariants I1–I13 (SAR ⇔ FILE_REPORT, connected_cards ⇒ §3a
+ R6, probability agrees with settled verdict, no invented case-IDs in
prose). **113 / 113** offline pytest.

**9/** Every investigation writes back a `SentinelCase` vertex with
edges to card, customer, txns, devices, regions, similar prior
ClosedCases. Live graph has **35** SentinelCase vertices (20 benchmark
+ 15 monitoring). Replaying HHG-014 now finds `CASE-HHG-014` in the
retrieved memory — the loop closes.

**10/** Repo: **https://github.com/v4xsh/sentinel**. FastAPI + d3 SPA
with 10 views (evidence, timeline, actions with what-if toggle, SAR,
graph neighbourhood, backtest report inline, memory, monitor,
findings). Thanks @TigerGraphDB for the workspace, the dataset, and
the MCP stack. #GraphRAG #FraudDetection

---

## LinkedIn version

**Sentinel — an agentic fraud investigator on TigerGraph**

Submission to Hacker House Goa 2026 × TigerGraph. Repo:
**https://github.com/v4xsh/sentinel**

Sentinel reads a card-transaction graph and, for each flagged alert,
produces a verdict (fraud / legitimate / uncertain), a fraud-pattern
label, the actions the bank should take with §2 approval routes, and a
Suspicious Activity Report when policy calls for one.

**The design principle**: the LLM writes prose but never decides. Every
action comes from a deterministic policy engine implementing R1–R10.
Every claim in the prose has a graph query or a fitted-model
coefficient behind it. Two different jobs, and only one can
hallucinate.

**Under the hood**

- TigerGraph FraudGraph with 7 vertex types, TigerVector HNSW cosine
  index over 5,565 closed-case notes, 18 installed GSQL queries. 8
  dispatched in parallel per case via `asyncio.gather` on
  `httpx.AsyncClient` — ~2 s wall-clock vs ~25 s sequential.
- All investigation graph calls go through **tigergraph-mcp** via a
  single long-lived stdio session shared across the whole process
  (background asyncio loop, daemon thread). Set
  `SENTINEL_GRAPH_VIA_MCP=0` to force the REST fast-path (used only by
  the 150-case backtest).
- Custom GSQL algorithm `ring_wcc`: BFS-fixpoint weakly-connected
  component over the card–device projection.
- LangGraph state machine (15 nodes) with a critic, VOI planner,
  deterministic simulator (denied iff p_initial ≥ τ, with ring-T3 and
  legit-archetype overrides), memory write-back that persists every
  investigation as a `SentinelCase` vertex.

**Results on the 20-case benchmark**

- 20 / 20 answer files pass invariants I1–I13 (SAR ⇔ FILE_REPORT,
  connected_cards ⇒ §3a + R6, probability agrees with settled verdict,
  no invented case-IDs in prose).
- **12 fraud, 8 legitimate, 0 uncertain** finals. **4 SARs** —
  HHG-004 (CNP + new-device with 25 connected cards), HHG-006
  (undocumented ring), HHG-011 (card_testing with $3.9k exposure),
  HHG-014 (undocumented ring, analyst-flagged).
- HHG-003 R7 (customer disputes recurring charge) correctly does NOT
  block the card.

**Results on the 150-case backtest**

- 75-case tune / 75-case held-out eval. Alert model 5-fold CV
  **AUC 0.9465 ± 0.0046, Brier 0.0927**.
- Simulated (τ=0.30): verdict accuracy **0.833**.
- Oracle (customer_response derived from historical actions_taken):
  verdict accuracy 1.000. This is a policy-engine correctness check,
  not a model accuracy claim.
- OOT (train Jul–Sep, test Oct+): AUC number in the repo.
- FinCEN §3a policy encoding replay: **4,665 / 4,665** historical SAR
  decisions reproduced (rule test).

**UI**: FastAPI + vanilla-JS + d3, one-command launcher (`./run_ui.sh`).
10 views: cases, case detail, evidence, timeline, initial-vs-final
actions with route badges + what-if toggle, SAR narrative, d3
force-layout graph neighbourhood, backtest report with reliability plot
inline, memory (live SentinelCase count), monitor (Nov–Dec sweep),
findings (undocumented U1/U2 patterns).

Repo: **https://github.com/v4xsh/sentinel** · One-page reviewer's
summary: `docs/SUBMISSION.md`.

Thanks to @TigerGraphDB for the workspace, the dataset, and the MCP
stack. #Fraud #GraphDatabases #TigerGraph #GraphRAG
