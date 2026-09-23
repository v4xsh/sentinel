# Sentinel — Submission summary

**Hacker House Goa 2026 × TigerGraph.** One-page reviewer's guide.

## What it is

A LangGraph agent that reads a card-transaction graph and, for each
flagged alert, produces a verdict, a fraud-pattern label, the actions
the bank should take with §2 approval routes, and a FinCEN-shaped SAR
narrative when policy calls for one. The LLM writes prose; a
deterministic policy engine decides actions.

## What's in the repo

| Path | Contents |
|---|---|
| `sentinel/` | agent, detectors, policy engine, alert model, retrieval |
| `sentinel/policy/` | 14 canonical actions + R1–R10 + I1–I13 invariants |
| `graph/queries/` | 18 installed GSQL queries (incl. `ring_wcc`, HNSW `vector_search_cc`, `write_case`) |
| `cases/` | 20 answer JSONs (I1–I13 pass) |
| `cases_extra/` | 15 monitoring-mode answer JSONs from the Nov–Dec sweep |
| `backtest/BACKTEST_REPORT.md` | 150-case simulated backtest (75 tune / 75 held-out eval), τ curve, reliability |
| `ui/` | FastAPI + d3 SPA — 10 views incl. what-if, memory, monitor, findings |
| `docs/` | this file + ARCHITECTURE + BENCHMARK_RESULTS + DECISIONS + LEARNINGS + BLOG_DRAFT + SOCIAL_POST + MCP_TRANSCRIPT + UNDOCUMENTED_FINDINGS + DEMO_SCRIPT |
| `tests/` | 129 tests (I1–I13, 16 query contracts, SAR encoding replay, initial-reason sweep, ask-first) |

## Headline numbers

| Metric | Value |
|---|---|
| Answer files passing I1–I13 | **20 / 20** |
| Verdict split | **12 fraud / 8 legitimate / 0 uncertain** |
| SARs | 4: **HHG-004, HHG-006, HHG-011, HHG-014** |
| Alert model 5-fold CV | **AUC 0.9465 ± 0.0046,  Brier 0.0927** |
| Simulated backtest (n=150, τ=0.30) | **verdict acc 0.833** |
| Oracle backtest (n=150) | **verdict acc 1.000** — by construction; `customer_response` is derived from the historical `actions_taken` label. Use the simulated number and the OOT AUC as the honest end-to-end evaluations. |
| OOT AUC (train Jul–Sep 2016, test Oct+; n=75 held-out eval) | **AUC 0.9431, Brier 0.0804** |
| §3a policy encoding: SAR-decision replay | **4,665 / 4,665** confirmed-fraud rows |
| SentinelCase vertices in graph | **35** (20 benchmark + 15 monitoring) |
| Installed GSQL queries | **18** |
| Latency (LLM on, shared MCP session) | **~5 s / case** |
| pytest | **129 / 129** offline |

## Three things to look at first

1. **`cases/HHG-014.json`** — the undocumented-ring flagship. Analyst
   flagged, T4 device signal fires shared_element=device, §3a filed a
   SAR, R6 monitors the 25 connected cards from the ring. Pattern
   `undocumented` requires a prose `pattern_description`; ours cites
   only whitelisted case-IDs (I12).

2. **`cases/HHG-003.json`** — R7 no-block on a customer dispute of a
   recurring charge. Naïve system would block; Sentinel looks up the
   card's recurring pattern (`recurring_match` GSQL), sees 7 prior hits
   at that ProductCD/amount, and lands legitimate at p=0.15. No SAR,
   card stays active.

3. **`docs/MCP_TRANSCRIPT_HHG-014.md`** — every graph tool call for one
   investigation, captured from the live MCP stdio session
   (`sentinel/graph/mcp_client.py::sync_run_installed_query`, a
   background asyncio loop in a daemon thread shared across the whole
   process).

## How to run it

```bash
cp .env.example .env             # fill TG_* + GOOGLE_API_KEY (up to _3)
pip install -e .
python graph/load.py             # one-time load
python graph/install.py          # installs 18 queries
python -m sentinel.evidence.alert_model    # fits L2 logistic
python scripts/run_all_20.py     # 20 cases → cases/*.json (MCP path)
python -m sentinel backtest --n 150 --tune-frac 0.5 --mode simulated --oot
./run_ui.sh                      # http://localhost:8000
```

## Status

- Code, tests, cases, monitoring runs, backtest, docs — all in the repo
  at this URL.
- Demo video and blog publication follow on submission day.
- Repo layout, architecture, and per-case rationale for judges:
  `docs/ARCHITECTURE.md`, `docs/BENCHMARK_RESULTS.md`.
