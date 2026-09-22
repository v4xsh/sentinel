# Building Sentinel: an agentic fraud investigator on TigerGraph

*A retrospective from Hacker House Goa 2026 × TigerGraph.*

---

## The problem the bank actually has

Six months of Vesta card transactions, all 393 original columns kept.
Every transaction now carries a risk score from the bank's model. Cards,
devices, billing regions, customers, and 5,565 previously-closed cases
live in a graph. Out of that graph fall 20 alerts a month. A human has
to say, for each: is this fraud, what kind of fraud, how far it goes,
what to do next, and whether it warrants a Suspicious Activity Report.

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
committed by a deterministic policy engine (README §1–§6, R1–R10)
before the summary node runs. And even the prose has a guardrail: a
citation whitelist (every `CC-####` the summary is allowed to name) is
built at generation time from the case's `similar_prior_cases` list,
regexed out of the LLM output, and persisted so `sentinel/policy/
invariants.py::I12` can reject the answer file if it argues with
itself.

## How TigerGraph is used

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
  worth ~5–8 s per case.
- **`vectorSearch()`** as a top-k function inside a GSQL query. We ship
  our own `q17_vector_search_cc` so the retrieval path is all TG-native
  with no MCP dependency.
- **A hand-written weakly-connected-component algorithm** at
  `graph/queries/q18_ring_wcc.gsql`. BFS-fixpoint over the card–device
  projection containing a seed card, with a device-degree cap (default
  100) to prune hub devices. Not a fixed pattern query; an actual
  algorithm expressed in GSQL.

## MCP is the tool path

`SENTINEL_GRAPH_VIA_MCP=1` is the library default. Every investigation
run (`run_all_20.py`, the UI's live-case runner, a REPL session)
dispatches every graph tool call through the `tigergraph-mcp` stdio
server. The naïve approach — a fresh MCP subprocess per case — was 15+
seconds per query. Our fix: a single long-lived session opened at
process start, reused across the whole run, driven by a background
asyncio event loop on a daemon thread
(`sentinel/graph/mcp_client.py::sync_run_installed_query`). Every tool
call from every case in the 20-case run goes through that one session.

The 150-case backtest is the one place we opt out (`SENTINEL_GRAPH_VIA_MCP=0`)
— pushing 150 cases sequentially through one stdio subprocess would
throttle things, so the backtest uses the REST fast-path
(`asyncio.gather` over `httpx.AsyncClient`) that hits the same installed
queries in parallel. Same eight-query set either way.

## Parallel dispatch was the big performance win

Version 1 of `_fetch_graph_signals` ran eight TG queries sequentially.
Savanna round-trip is ~500 ms. Total wall clock: ~4–5 s per case, before
any LLM work. Wrapping the eight independent queries in `asyncio.gather`
over one `httpx.AsyncClient` collapsed that to 1.5–2 seconds. The
`write_case` write-back can be disabled via
`SENTINEL_WRITE_MEMORY_DISABLED=1` for the backtest so we don't pay
per-case writing memory we won't use in the same session.

## Three "obvious" signals that weren't

We spent a whole checkpoint on evidence weighting before we understood
why the first three passes over-called fraud on every case.

**Risk-score decile.** In the labelled data, decile 10 is 6.5× as likely
to be fraud as decile 1. Tempting to use as a strong LR. Tempting, but
wrong. The alert *is* a decile-10 event. Using its own decile as
evidence double-counts the fact of being alerted. Fix: for
`trigger_type=risk_score` the decile contributes zero log-LR; for
`customer_report` and `analyst_request` we use the alert-conditional LR
(fraud vs cleared, not fraud vs everything) clipped to ±0.7.

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
Fraudulent charges cluster around $30–100 online, not $1,500 hotels.
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
   settle the case. **denied ⇒ fraud, confirmed ⇒ legitimate, no_reply
   ⇒ uncertain**; the p is scored on the calibrated posterior including
   the response evidence, and the two-channel gate applies only when
   there is no response.
5. Add clamps so probability agrees with the settled verdict
   (`I11`: fraud ⇒ p ≥ 0.85, legitimate ⇒ p ≤ 0.15 after the response
   lands). The 20 benchmark cases are now 12 fraud / 8 legitimate / 0
   uncertain finals.

## What the policy engine does

Fourteen actions. Three routes (`auto`, `L1`, `L2`) chosen by §2 of the
Fraud Policy. Rules R1–R10 in a single Python dispatch. A post-processing
guarantee: any fraud verdict always gets `BLOCK_CARD` with the correct
route by the *final* exposure (after `assemble_episode` has expanded
the episode to ±48h signature-matched txns) — the one exception is R7
(customer disputes a recurring charge, verify + warn, never block).

Thirteen invariants (I1–I13) run on every persisted answer file. A few
that mattered:

- **I8**: every action carries an Rn or §Nx citation. If the LLM
  invents an action, this fires.
- **I11**: probability agrees with the settled verdict; if the response
  path pushes the verdict to fraud but the posterior sits at 0.07, we
  clamp.
- **I12**: no invented case-IDs in the prose. The LLM is given an
  explicit whitelist, and any `CC-####` in the output that isn't in the
  whitelist causes the sentence to be dropped and the prompt re-run once
  with a strengthened instruction.
- **I13**: if the answer's `connected_card_ids` is non-empty, then
  `shared_element` must be set at decide-time, and (on a fraud verdict)
  `FILE_REPORT` + `MONITOR_CONNECTED_CARDS` must appear in the final
  action list. Otherwise the file is arguing with itself.

Every answer file passes I1–I13 or fails. On the 20 benchmark cases:
20 / 20.

## What we measured

**Alert model (offline, 5-fold CV on 5,565 closed cases)** — AUC **0.9465
± 0.0046**, Brier **0.0927**. Class-balanced L2 logistic over 45
features (binary flags, risk-decile one-hots, channel, device tier ×
degree-bucket interactions). Intercept rebased so "no evidence" gives
p = 0.5.

**150-case simulated backtest** — τ tuned on 75-case tune half, evaluated
on the held-out 75. Verdict accuracy **0.833** at τ=0.30, τ-curve saved
to the report.

**Oracle mode** — verdict accuracy **1.000** on the same 150 cases. But
oracle mode derives `customer_response` from the historical
`actions_taken` label, which then determines the verdict via §6, so this
is a policy-engine correctness check, not a model accuracy claim. We
kept it in the report as evidence the encoding is right; the honest
end-to-end number is the simulated one.

**Out-of-time evaluation** — train Jul–Sep, test Oct+. Same τ-tuning
protocol, but the eval half is genuinely held out in time. AUC number
finalises before submission and lands in this section then.

**FinCEN §3a policy encoding replay** — fed every one of the 4,665
`confirmed_fraud` rows in `closed_cases_history.csv` through the policy
engine's SAR rule (`_sar_should_file(...)`). Every case where the
analyst filed a SAR is a case where Sentinel would also file. Zero
false negatives. `tests/test_sar_replay.py` — 4,665 / 4,665. This is a
rule-encoding test, not an end-to-end run.

## The three lessons

**Leakage lurks everywhere.** The risk-score decile was the loudest
example, but every "obvious" signal from the alert row is a signal the
alert already conditioned on. The fix wasn't better weighting — it was
picking a training objective (fraud vs cleared) that already reflects
that conditioning, and letting the coefficients fall out.

**The LRs that flipped.** Three signals we walked in expecting to be
positive turned out to be against fraud: `id_15='New'`, `TransactionAmt
> $1,000`, and out-of-region use on a card with prior travel. Every one
of those is 60–70% of the legitimate-but-alerted population. The
lesson: compute LRs empirically before writing them into rules;
intuitions from "obvious" fraud shapes are alert-population intuitions,
not fraud-population intuitions.

**The response settles the verdict.** Trying to build a verdict machine
that only reads signals was a mistake. The customer has evidence we
don't: the truth of whether they made the charge. The right architecture
asks (via a VOI planner), pipes the response through §6, and lets a §6
rule (`denied ⇒ fraud`, `confirmed ⇒ legitimate`, `no_reply ⇒ uncertain
+ R4`) settle the case. The two-channel/§6 gate now fires only when the
customer hasn't been asked yet.

## What we'd improve

- **Everything in TigerGraph.** We embed case notes locally with
  BGE-small in DuckDB; the more native story would be TigerVector-native
  embedding + retrieval end-to-end. The current split is 95% TG (all
  investigation queries + HNSW retrieval) but the 5% is visible.
- **A live-labelled OOT eval loop.** The Nov–Dec sweep is our closest
  approximation, but only a real production feedback loop would give
  actually-honest OOT AUC.
- **More typologies.** The five documented patterns catch most of
  what's in the ClosedCase history, but the Nov–Dec sweep surfaced two
  more shapes (proxied device rings, near-threshold bursts —
  `docs/UNDOCUMENTED_FINDINGS.md`) that don't fit. A production system
  would upgrade these into detectors after enough evidence.
- **A critic node with teeth.** Ours flags shared-device claims
  adversarially but doesn't block the action. In production it should
  gate `BLOCK_CARD` on a second-order check.

---

**Repo layout, tests, and per-case rationale**: `docs/ARCHITECTURE.md`,
`docs/BENCHMARK_RESULTS.md`, `docs/DECISIONS.md`, `docs/LEARNINGS.md`.

**Bug reports**: open an issue at the repo.

Thanks to TigerGraph for the workspace, the dataset, and the MCP stack.
