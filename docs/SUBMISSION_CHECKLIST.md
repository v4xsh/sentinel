# Sentinel — submission checklist

*Every deliverable from the Hacker House Goa × TigerGraph brief with
its location or an explicit "user adds on submission" line.*

## Deliverables

| # | Deliverable | Location / status |
|---|---|---|
| 1 | **Repo URL** | https://github.com/v4xsh/sentinel |
| 2 | **20 answer files** (one per case pack alert, pydantic-validated) | `cases/HHG-001.json` … `cases/HHG-020.json` (20 files) |
| 3 | **Answer files pass invariants** | I1–I13 all pass on all 20 (see `tests/test_invariants_*.py`, `sentinel/policy/invariants.py`) |
| 4 | **Cases written to graph** | 35 `SentinelCase` vertices live in FraudGraph (20 benchmark + 15 monitoring) — confirmed by `scripts/acceptance_check.py::check_9` |
| 5 | **SAR files where required** | 4 filed: HHG-004, HHG-006, HHG-011, HHG-014. `sar.file=True` + `narrative` populated + `FILE_REPORT` in `next_best_actions.final` |
| 6 | **Next-best-action before / after in every file** | `next_best_actions.initial` and `next_best_actions.final` on all 20 answer files; `what_changed` records the set-diff |
| 7 | **UI** | FastAPI + d3 SPA. Launch: `./run_ui.sh` → http://localhost:8000. Screenshots: `img/{cases,hhg014_graph,backtest}.png` |
| 8 | **Backtest report** | `backtest/BACKTEST_REPORT.md` — 150 cases, 75 tune + 75 held-out eval; simulated τ=0.30 verdict acc 0.833; OOT eval AUC 0.9374, Brier 0.0829; reliability plot inline |
| 9 | **Alert-model metrics** | 5-fold CV AUC 0.9465 ± 0.0046, Brier 0.0927 (`sentinel/evidence/alert_model.json`) |
| 10 | **FinCEN §3a SAR-encoding replay** | 4,665 / 4,665 confirmed-fraud rows reproduce historical `report_filed` (`tests/test_sar_replay.py`) |
| 11 | **Monitoring mode extras** | 15 alerts in `cases_extra/EXTRA-*.json` from `python -m sentinel.monitor 15` |
| 12 | **Custom graph algorithm** | `graph/queries/q18_ring_wcc.gsql` (BFS-fixpoint WCC over card–device projection), Python wrapper at `graph/algorithms/wcc.py`, wired into the agent baseline fan-out |
| 13 | **MCP transcript** | `docs/MCP_TRANSCRIPT_HHG-014.md` — full per-call stdio session for one investigation |
| 14 | **Undocumented findings** | `docs/UNDOCUMENTED_FINDINGS.md` — U1 (proxied device rings) + U2 (near-threshold bursts) from the Nov–Dec 2016 sweep |
| 15 | **Demo video** | **user adds on submission** — replace `**Demo video:** _link added on submission_` at the top of `README.md`, and the `<!-- VIDEO -->` markers in `docs/BLOG_FINAL.md` and `docs/SOCIAL_FINAL.md` |
| 16 | **Blog post** | Text ready at `docs/BLOG_FINAL.md` (1,993 words, first-person, matches v4xsh.dev style). **User publishes and swaps `<!-- BLOG -->` markers in social copy for the live URL.** |
| 17 | **Social post** | Text ready at `docs/SOCIAL_FINAL.md` — X (246 chars) + LinkedIn. **User posts and inserts the video + blog URLs at the marker positions.** |
| 18 | **Screenshots** | `img/cases.png`, `img/hhg014_graph.png`, `img/backtest.png` (1600×900, captured via `scripts/capture_screenshots.py`) |
| 19 | **Reviewer summary** | `docs/SUBMISSION.md` — one-page reviewer's guide |
| 20 | **Architecture deep-dive** | `docs/ARCHITECTURE.md` |
| 21 | **Per-case rationale** | `docs/BENCHMARK_RESULTS.md` — regenerated from ledgers, names the 10 ask-first cases at the top |
| 22 | **Design decisions log** | `docs/DECISIONS.md` |
| 23 | **What we learned** | `docs/LEARNINGS.md` |
| 24 | **Demo script (3–5 min)** | `docs/DEMO_SCRIPT.md` with a pre-flight block |

## Headline numbers (should match everywhere)

- 20 / 20 answer files pass I1–I13
- 12 fraud / 8 legitimate / 0 uncertain (finals)
- SAR set: HHG-004, HHG-006, HHG-011, HHG-014
- Alert model 5-fold CV: AUC 0.9465, Brier 0.0927
- OOT eval (Jul–Sep tune → Oct+ eval, n=75): AUC 0.9374, Brier 0.0829
- Simulated backtest (τ=0.30): verdict acc 0.833
- Oracle backtest: verdict acc 1.000 (policy-engine correctness check)
- §3a policy encoding: 4,665 / 4,665 confirmed-fraud rows reproduced
- 129 / 129 offline pytest
- 18 installed GSQL queries
- 35 SentinelCase vertices in the graph
- 10 ask-first cases (initial ≠ final)

## Four commands the user runs before submitting

```bash
# 1. All acceptance checks (10 checks, must be 10/10)
python scripts/acceptance_check.py

# 2. Offline pytest (should be all green + a few skipped features)
pytest -q -k "not tigergraph and not query_contract and not ring_components"

# 3. Verify the 20 answer files are present
ls cases | wc -l          # → 21 (20 HHG-*.json + _summary_20.json)
ls cases/HHG-*.json | wc -l  # → 20

# 4. Clean working tree
git status                # → "nothing to commit, working tree clean"
```

## User-supplied placeholders (three)

1. `README.md` line 3 — `**Demo video:** _link added on submission_` → real video URL.
2. `docs/BLOG_FINAL.md` — the single `<!-- VIDEO -->` marker below the title → video embed / link.
3. `docs/SOCIAL_FINAL.md` — the `<!-- BLOG -->` and `<!-- VIDEO -->` markers in both X and LinkedIn variants → live URLs.

Nothing else in the repo needs manual editing before submission.
