# Sentinel — final social copy

Repo: **https://github.com/v4xsh/sentinel**
Post the blog + video links by replacing the markers before publishing.

---

## X / Twitter (single post, ≤ 280 chars)

Built Sentinel for @TigerGraphDB × Hacker House Goa: a LangGraph agent that reads a card-txn graph and decides fraud / SAR / next actions. LLM writes prose, policy engine decides. 12/8/0, 4 SARs, OOT AUC 0.9374. 
<!-- BLOG -->
<!-- VIDEO -->
https://github.com/v4xsh/sentinel

---

## LinkedIn

**Sentinel — an agentic fraud investigator on TigerGraph**

My submission to Hacker House Goa 2026 × TigerGraph. Repo:
**https://github.com/v4xsh/sentinel**

Sentinel reads a card-transaction graph and, for each flagged alert,
produces a verdict (fraud / legitimate / uncertain), a fraud-pattern
label, the actions the bank should take with §2 approval routes, and a
Suspicious Activity Report when policy calls for one.

**Design principle**: the LLM writes prose but never decides. Every
action comes from a deterministic policy engine implementing R1–R10.
Every claim in the prose has a graph query or a fitted-model coefficient
behind it. Thirteen invariants (I1–I13) enforce that the answer file
never argues with itself.

**Under the hood**

* TigerGraph FraudGraph with 7 vertex types, TigerVector HNSW cosine
  index over 5,565 closed-case notes, 18 installed GSQL queries. 8
  dispatched in parallel per case via `asyncio.gather` — ~2 s
  wall-clock vs ~25 s sequential.
* All investigation graph calls go through **tigergraph-mcp** via a
  single long-lived stdio session shared across the whole process.
* Custom GSQL algorithm `ring_wcc`: BFS-fixpoint weakly-connected
  component over the card–device projection.
* LangGraph state machine (15 nodes), citation-guard on every LLM
  prompt, memory write-back that persists every investigation as a
  SentinelCase vertex in the graph.

**Results**

* 20 benchmark cases: **12 fraud / 8 legitimate / 0 uncertain**, 4
  SARs (HHG-004, HHG-006, HHG-011, HHG-014). All pass invariants
  I1–I13.
* Alert model 5-fold CV: **AUC 0.9465 ± 0.0046, Brier 0.0927**.
* Out-of-time evaluation (train Jul–Sep 2016, test Oct+): **AUC
  0.9374, Brier 0.0829** on 75 held-out cases.
* Simulated backtest (τ=0.30): verdict accuracy **0.833**.
* FinCEN §3a policy encoding replay: **4,665 / 4,665** historical SAR
  decisions reproduced (rule test).
* 129/129 offline pytest.

**UI**: FastAPI + vanilla-JS + d3, one-command launcher. Ten views
including a what-if toggle that re-runs the policy engine with a
swapped customer response.

<!-- BLOG -->

<!-- VIDEO -->

Thanks to @TigerGraphDB for the workspace, the dataset, and the MCP
stack. #Fraud #GraphDatabases #TigerGraph #GraphRAG

---

## Marker legend

Replace before publishing:

* `<!-- BLOG -->` → the blog URL
* `<!-- VIDEO -->` → the demo video URL (YouTube / Loom / etc.)
