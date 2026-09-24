# Sentinel — 3-5 Minute Demo Script

**Pre-flight (~2 min before recording)**
```bash
# 1. Wake Savanna (workspace hibernates after inactivity — first hit warms it)
set -a && source .env && set +a       # load TG_HOST from .env
curl -s "$TG_HOST/api/ping"
# 2. Start the UI
./run_ui.sh                       # → http://localhost:8000
# 3. Warm the live ask-first case in the background
python -m sentinel.agent.run_case HHG-001 &
# 4. LLM probe (should print "gemini/keyN OK")
python -c "from sentinel.llm import generate; print(generate('reply OK', max_output_tokens=8))"
# 5. Close all other browser tabs; give focus to the demo tab
# 6. Browser zoom 110% so screen recording reads clearly
```

Have `backtest/BACKTEST_REPORT.md` open in a second tab.

---

## Scene 1 — Sentinel's job (0:00–0:20)

**Say**: "The bank flags a transaction. Someone has to decide: block or
not, file a SAR or not, and how far the fraud goes. That's what Sentinel
does — it reads the graph, the customer's history, and any similar past
cases, and produces a case, a SAR when needed, and the actions with the
approval routes."

**Show**: Cases list. 20 cases → **12 fraud / 8 legitimate / 0 uncertain**, **4 SARs** (HHG-004, HHG-006, HHG-011, HHG-014).

---

## Scene 2 — HHG-014 — the undocumented ring (0:20–1:20)

**Click** HHG-014 in the list.

**Say**: "Analyst flagged this — 'several cards this month share an
unusual device profile'. Sentinel closed it as **fraud, pattern =
undocumented, p_final = 0.86, exposure $187.33, SAR filed**. The
device-tier T4 signal (a narrow shared device with a prior
confirmed-fraud ClosedCase on it) fired shared_element=device, so §3a
triggers a SAR and R6 fires MONITOR_CONNECTED_CARDS."

**Click** the **Evidence** tab.

**Say**: "The ledger carries the graph-sourced items — ring_components,
ring_wcc (the BFS-fixpoint over the card-device projection),
device_neighbors, proxy_flag, and the alert model's channel and device
signals. The posterior sat above the §6 two-channel threshold before
we even asked the customer."

**Click** the **SAR** tab. Show the FinCEN narrative and §3a reason.

**Click** the **Graph** tab.

**Say**: "d3 force layout of the neighbourhood. The case in the middle,
the shared device it lives on, and (when the ring is broader) the peer
cards that touched that device."

**Click** **Actions** tab.

**Say**: "Initial actions were STEP_UP_AUTH, CREATE_CASE, FILE_REPORT,
ESCALATE_TO_ANALYST, MONITOR_CONNECTED_CARDS — all pre-response, gated
by §6 (posterior ≥ 0.85 on ≥ 2 independent channels). Customer denied
on the simulator's τ-rule, so the final adds BLOCK_CARD. FILE_REPORT
and MONITOR_CONNECTED_CARDS stay."

---

## Scene 3 — HHG-003 — R7 no-block (1:20–2:10)

**Click** HHG-003.

**Say**: "Customer message says 'I never made this $49 charge'. Naïve
system would block the card. Sentinel looks up the card's recurring hits
at that ProductCD/amount — 7 prior hits, cadence CV 0.63. That's R7:
**dispute against a recurring charge**."

**Click** **Actions**.

**Say**: "R7 branch is triggered specifically. Actions are CREATE_CASE,
VERIFY_WITH_CUSTOMER, WARN_CUSTOMER — **no BLOCK_CARD**. Simulator ran
because legit-archetype fired, response = confirmed, verdict lands
legitimate at p 0.15. No SAR, card stays active."

**Click** **Evidence** tab and read out the recurring-match evidence
line.

---

## Scene 4 — What-if toggle (2:10–2:50)

Still on HHG-003, **click** **Actions** tab.

**Say**: "Investigators need to preview what would have happened if the
customer had denied. Sentinel's what-if row hits `POST /api/whatif/HHG-003`,
which re-runs the policy engine with the swapped response — no LLM, no
graph calls, deterministic in <10 ms."

**Click** **If customer denied**.

**Show** JSON output: actions flip to `BLOCK_CARD` (routed L1 because
exposure < $2,500) + `CREATE_CASE` per R2.

---

## Scene 5 — HHG-001 — VERIFY → BLOCK (2:50–3:20)

**Click** HHG-001.

**Say**: "Risk-score alert, out_of_region_use, p_final = 0.96, exposure
$77.07. Initial actions were VERIFY_WITH_CUSTOMER + CREATE_CASE —
pre-response, since §6's two-channel-≥0.85 gate didn't fire on the
alerted txn alone. Simulator's τ-rule denied on the assumed response,
so final is BLOCK_CARD + CREATE_CASE, BLOCK_CARD routed L1 (exposure
below $2,500). Every final action carries its route (auto / L1 / L2
per §2) and the rule citation (R1, R4, R6...)."

---

## Scene 6 — Backtest (3:20–4:00)

**Click** **Backtest** in the top nav.

**Say**: "150 closed cases stratified by pattern/archetype. Alert model
5-fold CV: **AUC 0.9465, Brier 0.09**. Out-of-time: train on Jul–Sep
2016, evaluate on 75 held-out Oct+ cases the model never saw —
**AUC 0.9431, Brier 0.08**. The model doesn't overfit the training
window. Simulated backtest at τ=0.30 hits **83.3%** verdict accuracy.
The reliability plot is right here."

**Scroll** into the report — show the pattern confusion matrix and the
top-10 misclassifications with ledgers.

**Say the caveats out loud**: "Oracle mode hits 100% by construction —
customer_response is derived from the historical actions_taken label,
so oracle accuracy is a policy-engine correctness check, not a model
claim."

---

## Scene 7 — Memory write-back (4:00–4:40)

**Click** **Memory** in the top nav.

**Say**: "Every SentinelCase we write goes into the graph as a vertex
with edges to card, customer, transactions, devices, regions, and the
similar ClosedCases it retrieved. That's the memory the *next*
investigation reads — 35 SentinelCase vertices (20 benchmark + 15
monitoring) right now."

**Say (optional)**: "Re-run HHG-014 and it now finds `CASE-HHG-014` in
the retrieval — proof of the write-back closing the loop."

---

## Scene 8 — Close (4:40–5:00)

**Say**: "Everything that touches the graph is a real installed GSQL
query or a TigerVector search — Sentinel does not embed the raw txn
table. Everything the LLM says has evidence behind it. All 20 answer
files pass I1–I13 invariants including SAR ⇔ FILE_REPORT, probability
agrees with the settled verdict, no invented case-IDs in prose, and
connected_card_ids ⇒ §3a + R6. Our FinCEN §3a rule encoding reproduces
the historical SAR decision on 4,665 / 4,665 confirmed-fraud cases."

**End on**: the cases list with all 20 badges visible.
