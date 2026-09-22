# Building Sentinel: an agentic fraud investigator on TigerGraph

*A hackathon retrospective from Hacker House Goa 2026 × TigerGraph.*

---

## The problem the bank actually has

Six months of Vesta card transactions, all 393 original columns kept.
Every transaction now carries a risk score from the bank's model. Cards,
devices, billing regions, customers, and 5,565 previously-closed cases
live in a graph. Out of that graph fall 20 alerts a month. A human has
to say, for each: is this fraud, what kind of fraud, how far it goes,
and what to do next.

The obvious thing to build is a rules engine that reads the alert and
outputs actions. The trap: the rules-engine's inputs are exactly the
same signals the risk score already conditioned on, so the "reasoning"
mostly restates why the alert fired. We wanted something that reads the
graph the way an analyst does — pulls prior cases, checks the device's
history, looks at other cards on the same device, and only *then* forms
an opinion — and produces evidence-linked prose an analyst can defend
to a regulator.

## The shape of the agent

Sentinel is a LangGraph state machine. Fifteen nodes. Every node either
reads from TigerGraph or reads from a local DuckDB feature store; the
LLM is called in exactly two places — the analyst-summary node and the
SAR-narrative node — and only after all the graph work is done. The LLM
never decides an action, never picks a rule citation, never assigns a
verdict. It writes prose grounded in the evidence ledger the earlier
nodes produced.

That split is the whole safety story. If Gemini hallucinates, the worst
that happens is a badly-worded summary; the actions are already
committed by a deterministic policy engine (README §1-§6, R1-R10)
before the summary node runs.

## Three "obvious" signals that weren't

We spent a whole checkpoint on evidence weighting before we understood
why the first three passes over-called fraud on every case. Here's what
we learned:

**Risk-score decile.** In the labelled data, decile 10 is 6.5× as likely
to be fraud as decile 1. Tempting to use as a strong LR. Tempting, but
wrong. The alert *is* a decile-10 event. Using its own decile as evidence
double-counts the fact of being alerted. Fix: for `trigger_type=risk_score`
the decile contributes zero log-LR; for `customer_report` and
`analyst_request` we use the alert-conditional LR (fraud vs cleared, not
fraud vs everything) clipped to ±0.7. Documented in `docs/DECISIONS.md`.

**Device is-new.** Vesta's `id_15='New'` looks like a red flag. It
isn't. Roughly half the cleared cases in the history are cardholders
who traveled or got a new phone — first thing you'd expect to trip the
"new device" flag. Empirical LR: 0.671. Slightly *against* fraud.
Trying to hand-pick coefficients gave us the wrong sign. Fitting a
logistic regression on all 5,565 closed cases at once (fraud vs cleared)
gave us the actual coefficient: **-4.93**. The fitted model's job isn't
to detect fraud in the wild; it's to distinguish fraud alerts from
cleared alerts, which is a much easier problem *and* the actually
useful one.

**Big-purchase.** `TransactionAmt > $1,000` → LR 0.54. Against fraud.
Fraudulent charges cluster around $30-100 online, not $1,500 hotels.
We added a `legit_big_purchase` archetype detector with strict
conditions (prior big txn on card, non-new device, non-new phone) so
the simulator can override to "customer confirmed" on those.

## Verdict logic, ratified in blood

Five attempts:

1. Sum the log-LRs, sigmoid, verdict at 0.5. This called everything fraud.
2. Add a two-channel gate: legit iff `p ≤ 0.15 AND channels ≥ 2`, fraud
   iff `p ≥ 0.85 AND channels ≥ 2`. This made everything uncertain —
   the sum-of-log-LRs rarely cleared 0.85.
3. Fit an L2 logistic to the 5,565 closed cases, use its coefficients
   as log-LRs, apply the two-channel gate anyway. 98% uncertain.
4. Realise the verdict logic itself was broken: a customer *response*
   should override the p, not gate it. The whole point of asking is to
   settle the case. **denied ⇒ fraud, confirmed ⇒ legitimate, no_reply ⇒
   uncertain**; the p is scored on the calibrated posterior including
   the response evidence, and the two-channel gate applies only when
   there is no response. This is C6v3.
5. Oracle mode (use `actions_taken` from the ClosedCase to derive the
   response) hits 100% verdict accuracy on 150 cases. Simulated mode
   with τ=0.30 hits 83.3%. That's the shape we wanted from the start.

## What TigerGraph is doing under the hood

The graph has one central story: connected structure the flat CSV can't
express. Same device across multiple cards. Same billing region across
customers. Same closed case attached to a device, a region, a card
tuple. Every "similar to" and "shares X with" question is a graph
traversal in the agent.

Native features we lean on:
- **HNSW cosine index on `ClosedCase.notes_embedding`**. We embed all
  5,565 case notes with `BAAI/bge-small-en-v1.5` (384-dim, CPU, local).
  Semantic retrieval at query time is ~1.4 s over a REST-wrapped
  installed query (`vector_search_cc`) — the MCP path was 30 s per call
  because of how the tool serialises the result.
- **`SetAccum<VERTEX<ClosedCase>>` on `closed_cases_touching`**. Returns
  the union of the card / device / region legs in one query. We used to
  do a follow-up `restpp GET /vertices/ClosedCase/{id}` for each result
  to enrich; skipping that (the SetAccum already returns attributes) is
  worth ~5-8 s per case.
- **`vectorSearch()`** as a top-k function inside a GSQL query. We ship
  our own `q17_vector_search_cc` so the retrieval path is all TG-native
  with no MCP dependency.

## Parallel dispatch was the big performance win

Version 1 of `_fetch_graph_signals` ran eight TG queries sequentially.
Savanna round-trip is ~3 seconds. Total wall clock: ~25 seconds per
case, before any LLM work. Just wrapping the eight independent queries
in `asyncio.gather` over one `httpx.AsyncClient` collapsed that to 1.5-2
seconds. The `write_case` write-back can be disabled via
`SENTINEL_WRITE_MEMORY_DISABLED=1` for the backtest so we don't pay 3s
per case writing memory we won't use in the same session.

## What the policy engine does

Fourteen actions. Three routes (`auto`, `L1`, `L2`) chosen by §2 of the
README's Fraud Policy. Rules R1-R10 in a single Python dispatch. A
post-processing guarantee: any fraud verdict always gets `BLOCK_CARD`
with the correct route by the *final* exposure (after `assemble_episode`
has expanded the episode to ±48h signature-matched txns) — the one
exception is R7 (customer disputes a recurring charge, verify + warn,
never block).

The verdict logic doesn't touch actions. If the customer denied, R2
fires regardless of the model's p. If the customer confirmed, R3.
`no_reply` triggers R4. The two-channel/§6 gate only decides the verdict
when the customer hasn't been asked.

Every action carries a `reason` string with a rule citation, checked by
Invariant I8. Every answer file passes I1-I11 or fails. On the 20
benchmark cases: 20 / 20.

## The 100% SAR replay

We tested Sentinel's SAR trigger against Vesta's `report_filed` field
on all 4,665 confirmed-fraud closed cases. Every case where the analyst
filed a SAR is a case where Sentinel would also file (`FILE_REPORT` in
the final action list). Zero false negatives. `tests/test_sar_replay.py`.

## The UI (60 minutes of work, once the API was there)

FastAPI + vanilla-JS + d3. Nine views: cases, detail, evidence +
posterior trajectory, timeline, initial vs final actions with route
badges, SAR narrative, force-layout graph neighbourhood, backtest
report, memory (live SentinelCase count from TG). The what-if row on
the Actions view re-runs the policy engine with a swapped response;
useful for investigators previewing "what if the customer had said
otherwise".

Everything on the client is a `fetch()` against the FastAPI backend.
There's no build step, no bundler, no npm. `run_ui.sh` is a two-line
uvicorn invocation. This felt like a good use of the time saved on the
agent side.

## What's honest about the numbers

- The alert model was fit on the full 5,565 closed cases. The backtest
  samples from that same pool. In-sample verdict accuracy is optimistic;
  the honest number is the 5-fold CV AUC (0.9465).
- Cleared cases in the history are 80% travel-related. New-phone and
  big-purchase archetypes are under-represented in both training and
  test. Our new-phone / big-purchase detectors are honest but weakly
  supported by the data.
- We used one trigger type (`risk_score`) for the backtest even though
  real HHG cases split three ways. The alert-conditional risk-decile
  feature and the R7 branch both change behaviour by trigger type, so
  in-production performance will differ from the backtest table.

## What we shipped, on time

- 105 pytest cases total (79 offline + 16 query-contract + 5 R10 + 2
  channels/simulator + 3 misc), all green.
- 20 benchmark answer files that pass every invariant. Final verdict
  split: **12 fraud / 8 legitimate / 0 uncertain**, **4 SARs** (HHG-006
  and HHG-014, both undocumented-ring cases per §3a). HHG-014 lands
  `verdict=fraud`, `pattern=undocumented`, `sar=True`, final actions
  `[BLOCK_CARD, CREATE_CASE, FILE_REPORT, MONITOR_CONNECTED_CARDS]`.
- 150-case backtest in both oracle and simulated modes with a full
  report (`BACKTEST_REPORT.md`) including τ tuning curve.
- The console at `run_ui.sh → localhost:8000`.
- SentinelCase write-back proven — 35 vertices in the graph (20 benchmark + 15 monitoring) after
  running the benchmark set once.

If you want to reproduce: clone the repo, fill in `.env` (one Google API
key or three, one TG Savanna workspace), and run `python scripts/run_all_20.py`.
For the honest numbers, `python -m sentinel backtest --n-fraud 75 --n-cleared 75
--mode simulated --seed 42`. The console shows what the LLM couldn't:
the evidence linked to every claim.

Thanks for reading. Repo at [placeholder]. Questions welcome on the
TigerGraph Discord.
