# 20-case benchmark — results

Every case ran end-to-end through the LangGraph agent with all graph tool calls dispatched through the shared TigerGraph MCP session (see `docs/MCP_TRANSCRIPT_HHG-014.md` for the captured tool-call log). Alert-model coefficients from `sentinel/evidence/alert_model.json` (L2 logistic, 5-fold CV **AUC 0.9465, Brier 0.0927**). **Out-of-time evaluation** — model refit on Jul–Sep cases only, evaluated on all Oct+ closed cases it never saw (n=1,372: 1,228 fraud + 144 cleared) — reproduces the CV estimate: **AUC 0.9374, Brier 0.0829** (`python scripts/oot_eval.py`, report at `backtest/oot/OOT_REPORT.md`). Each SentinelCase was written back to the graph via the `write_case` installed query.

**Oracle-mode caveat.** The 150-case oracle backtest hits 1.000 verdict accuracy *by construction*: `sentinel.backtest.run::_oracle_response` derives `customer_response` from the historical `actions_taken` label, which then determines the verdict via §6. Oracle accuracy is a policy-engine correctness check, not a model accuracy claim. The honest end-to-end evaluations are the simulated backtest (τ=0.30, verdict acc **0.833**) and the OOT AUC above.

## Summary

- 12 fraud, 8 legitimate, 0 uncertain
- 4 SAR filings
- All 20 answer files pass invariants I1–I13

**Ask-first cases** (non-empty `evidence_requests` with initial ≠ final — the R1/§6 verify → response → re-decide branch): HHG-001, HHG-002, HHG-004, HHG-006, HHG-007, HHG-009, HHG-011, HHG-012, HHG-014, HHG-017. These are the cases the demo video opens on: the agent decides to ask the customer, the response drives §6 to a settled verdict, and the action list changes accordingly.

## Case table

| case | trigger | verdict | p_i → p_f | pattern | exposure | evidence request (assumed response) | initial → final actions | SAR | graph_case_id |
|---|---|---|---|---|---|---|---|---|---|
| HHG-001 | risk_score | **fraud** | 0.962 → 0.962 | out_of_region_use | $77.07 | customer_validation(Customer states they did) | `VERIFY_WITH_CUSTOMER,CREATE_CASE` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-001` |
| HHG-002 | risk_score | **fraud** | 0.909 → 0.909 | card_not_present_fraud | $292.36 | customer_validation(Customer states they did) | `VERIFY_WITH_CUSTOMER,CREATE_CASE` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-002` |
| HHG-003 | customer_report | **legitimate** | 0.150 → 0.150 | none | $0.00 | customer_validation(Customer states they mad) | `CREATE_CASE,VERIFY_WITH_CUSTOMER,WAR` → `CREATE_CASE,VERIFY_WITH_CUSTOMER,WAR` | — | `CASE-HHG-003` |
| HHG-004 | customer_report | **fraud** | 0.968 → 0.968 | card_not_present_new_device | $128.33 | customer_validation(Customer states they did) | `MONITOR_CARD,VERIFY_WITH_CUSTOMER,CR` → `BLOCK_CARD,CREATE_CASE,FILE_REPORT,M` | ✓ | `CASE-HHG-004` |
| HHG-005 | risk_score | **legitimate** | 0.020 → 0.020 | none | $0.00 | — | `CLOSE_NO_FRAUD` → `CLOSE_NO_FRAUD` | — | `CASE-HHG-005` |
| HHG-006 | customer_report | **fraud** | 0.878 → 0.878 | undocumented | $961.07 | customer_validation(Customer states they did) | `STEP_UP_AUTH,CREATE_CASE,FILE_REPORT` → `BLOCK_CARD,CREATE_CASE,FILE_REPORT,M` | ✓ | `CASE-HHG-006` |
| HHG-007 | risk_score | **fraud** | 0.965 → 0.965 | account_takeover | $111.92 | customer_validation(Customer states they did) | `VERIFY_WITH_CUSTOMER,CREATE_CASE` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-007` |
| HHG-008 | customer_report | **fraud** | 0.980 → 0.980 | card_not_present_fraud | $897.65 | customer_validation(Customer states they did) | `BLOCK_CARD,CREATE_CASE` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-008` |
| HHG-009 | customer_report | **fraud** | 0.980 → 0.980 | card_not_present_fraud | $30.02 | customer_validation(Customer states they did) | `MONITOR_CARD,VERIFY_WITH_CUSTOMER,CR` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-009` |
| HHG-010 | risk_score | **legitimate** | 0.020 → 0.020 | none | $0.00 | — | `CLOSE_NO_FRAUD` → `CLOSE_NO_FRAUD` | — | `CASE-HHG-010` |
| HHG-011 | customer_report | **fraud** | 0.850 → 0.850 | card_testing | $3,984.80 | customer_validation(Customer states they did) | `CLOSE_NO_FRAUD` → `BLOCK_CARD,CREATE_CASE,FILE_REPORT,M` | ✓ | `CASE-HHG-011` |
| HHG-012 | risk_score | **fraud** | 0.962 → 0.962 | out_of_region_use | $30.91 | customer_validation(Customer states they did) | `VERIFY_WITH_CUSTOMER,CREATE_CASE` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-012` |
| HHG-013 | risk_score | **legitimate** | 0.058 → 0.058 | none | $0.00 | — | `CLOSE_NO_FRAUD` → `CLOSE_NO_FRAUD` | — | `CASE-HHG-013` |
| HHG-014 | analyst_request | **fraud** | 0.859 → 0.859 | undocumented | $187.33 | customer_validation(Customer states they did) | `STEP_UP_AUTH,CREATE_CASE,FILE_REPORT` → `BLOCK_CARD,CREATE_CASE,FILE_REPORT,M` | ✓ | `CASE-HHG-014` |
| HHG-015 | risk_score | **legitimate** | 0.020 → 0.020 | none | $0.00 | — | `CLOSE_NO_FRAUD` → `CLOSE_NO_FRAUD` | — | `CASE-HHG-015` |
| HHG-016 | customer_report | **fraud** | 0.980 → 0.980 | card_not_present_new_device | $59.67 | customer_validation(Customer states they did) | `BLOCK_CARD,CREATE_CASE` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-016` |
| HHG-017 | risk_score | **fraud** | 0.850 → 0.850 | card_not_present_fraud | $300.14 | customer_validation(Customer states they did) | `VERIFY_WITH_CUSTOMER,CREATE_CASE` → `BLOCK_CARD,CREATE_CASE` | — | `CASE-HHG-017` |
| HHG-018 | customer_report | **legitimate** | 0.150 → 0.150 | none | $0.00 | customer_validation(Customer states they mad) | `CREATE_CASE,VERIFY_WITH_CUSTOMER,WAR` → `CREATE_CASE,VERIFY_WITH_CUSTOMER,WAR` | — | `CASE-HHG-018` |
| HHG-019 | risk_score | **legitimate** | 0.058 → 0.058 | none | $0.00 | — | `CLOSE_NO_FRAUD` → `CLOSE_NO_FRAUD` | — | `CASE-HHG-019` |
| HHG-020 | risk_score | **legitimate** | 0.070 → 0.070 | none | $0.00 | — | `CLOSE_NO_FRAUD` → `CLOSE_NO_FRAUD` | — | `CASE-HHG-020` |

## Per-case rationale

**HHG-001**: **fraud** — p=0.96. Pattern **out_of_region_use** on exposure **$77.07** (1 txns after signature expansion). Top evidence: [conjunctive-lr] In-person txn in region 444.; [lr_table] Card has 5 prior hits at this ProductCD/amount (cadence CV=0.; [signal] This card tuple has ≥1 prior confirmed_fraud case closed before this txn.. Actions routed per §2 exposure bands.
**HHG-002**: **fraud** — p=0.91. Pattern **card_not_present_fraud** on exposure **$292.36** (1 txns after signature expansion). Top evidence: [query] Device family has KNOWN_DEVICE degree=1324 (>100 hub cap).; [signal] This card tuple has ≥1 prior confirmed_fraud case closed before this txn.; [alert_model] Alert model baseline (fitted on 5,565 historical cases).. Actions routed per §2 exposure bands.
**HHG-003**: **legitimate** — p=0.15. §6 response settled the verdict. Top evidence: [conjunctive-lr] In-person txn in region 330.; [query] Card has 7 prior hits at this ProductCD/amount (cadence CV=0.; [lr_table] Model risk_score=0..
**HHG-004**: **fraud** — p=0.97. Pattern **card_not_present_new_device** on exposure **$128.33** (1 txns after signature expansion). Top evidence: [conjunctive-lr] Online txn from a device flagged New.; [query] Device family shared with 57 other cards in the window (KNOWN_DEVICE degree=6).; [lr_table] Model risk_score=0.. §3a SAR filed. Actions routed per §2 exposure bands.
**HHG-005**: **legitimate** — p=0.02. §6 response settled the verdict. Top evidence: [conjunctive-lr] Online txn from a device flagged New.; [query] Device family has KNOWN_DEVICE degree=112 (>100 hub cap).; [signal] This card tuple has ≥1 prior confirmed_fraud case closed before this txn..
**HHG-006**: **fraud** — p=0.88. Pattern **undocumented** on exposure **$961.07** (2 txns after signature expansion). Top evidence: [conjunctive-lr] Online txn from a device flagged New.; [conjunctive-lr] ATO shape: 4 online txn(s) on id_15='New' in ±48h; 2 online txn(s) via IP_PROXY:* in ±48h  (n_online in ±48h = 4).; [query] Device family has KNOWN_DEVICE degree=547 (>100 hub cap).. §3a SAR filed. Actions routed per §2 exposure bands.
**HHG-007**: **fraud** — p=0.97. Pattern **account_takeover** on exposure **$111.92** (1 txns after signature expansion). Top evidence: [conjunctive-lr] ATO shape: 2 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 2).; [lr_table] Card has 3 prior hits at this ProductCD/amount (cadence CV=0.; [signal] This card tuple has ≥1 prior confirmed_fraud case closed before this txn.. Actions routed per §2 exposure bands.
**HHG-008**: **fraud** — p=0.98. Pattern **card_not_present_fraud** on exposure **$897.65** (21 txns after signature expansion). Top evidence: [query] Device family has KNOWN_DEVICE degree=175 (>100 hub cap).; [lr_table] Model risk_score=0.; [signal] This card tuple has ≥1 prior confirmed_fraud case closed before this txn.. Actions routed per §2 exposure bands.
**HHG-009**: **fraud** — p=0.98. Pattern **card_not_present_fraud** on exposure **$30.02** (1 txns after signature expansion). Top evidence: [query] Device family has KNOWN_DEVICE degree=1324 (>100 hub cap).; [lr_table] Model risk_score=0.; [alert_model] Alert model baseline (fitted on 5,565 historical cases).. Actions routed per §2 exposure bands.
**HHG-010**: **legitimate** — p=0.02. §6 response settled the verdict. Top evidence: [conjunctive-lr] Online txn from a device flagged New.; [query] Device family has KNOWN_DEVICE degree=209 (>100 hub cap).; [alert_model] Alert model baseline (fitted on 5,565 historical cases)..
**HHG-011**: **fraud** — p=0.85. Pattern **card_testing** on exposure **$3,984.80** (84 txns after signature expansion). Top evidence: [query] Card-testing shape from testing_sequence query: 14 sub-$5 online txn(s) + 34 larger follow-up(s) in the window.; [conjunctive-lr] Online txn from a device flagged New.; [query] Device family shared with 352 other cards in the window (KNOWN_DEVICE degree=4).. §3a SAR filed. Actions routed per §2 exposure bands.
**HHG-012**: **fraud** — p=0.96. Pattern **out_of_region_use** on exposure **$30.91** (1 txns after signature expansion). Top evidence: [conjunctive-lr] In-person txn in region 494.; [lr_table] Card has 5 prior hits at this ProductCD/amount (cadence CV=1.; [signal] This card tuple has ≥1 prior confirmed_fraud case closed before this txn.. Actions routed per §2 exposure bands.
**HHG-013**: **legitimate** — p=0.06. §6 response settled the verdict. Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $5,051,414 in-window…; [conjunctive-lr] Online txn from a device flagged New.; [conjunctive-lr] ATO shape: 1 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 1)..
**HHG-014**: **fraud** — p=0.86. Pattern **undocumented** on exposure **$187.33** (2 txns after signature expansion). Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $4,405,120 in-window…; [conjunctive-lr] Online txn from a device flagged New behind an anonymous/hidden proxy.; [conjunctive-lr] ATO shape: 1 online txn(s) on id_15='New' in ±48h; 1 online txn(s) via IP_PROXY:* in ±48h  (n_online in ±48h = 1).. §3a SAR filed. Actions routed per §2 exposure bands.
**HHG-015**: **legitimate** — p=0.02. §6 response settled the verdict. Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $4,555,535 in-window…; [conjunctive-lr] Online txn from a device flagged New.; [conjunctive-lr] ATO shape: 1 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 1)..
**HHG-016**: **fraud** — p=0.98. Pattern **card_not_present_new_device** on exposure **$59.67** (1 txns after signature expansion). Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $5,004,925 in-window…; [conjunctive-lr] Online txn from a device flagged New.; [query] Device family has KNOWN_DEVICE degree=163 (>100 hub cap).. Actions routed per §2 exposure bands.
**HHG-017**: **fraud** — p=0.85. Pattern **card_not_present_fraud** on exposure **$300.14** (3 txns after signature expansion). Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $5,038,633 in-window…; [query] Device family has KNOWN_DEVICE degree=302 (>100 hub cap).; [alert_model] Alert model baseline (fitted on 5,565 historical cases).. Actions routed per §2 exposure bands.
**HHG-018**: **legitimate** — p=0.15. §6 response settled the verdict. Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $4,430,992 in-window…; [conjunctive-lr] In-person txn in region 126.; [conjunctive-lr] ATO shape: mixed-channel activity in 24h; 6 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 12)..
**HHG-019**: **legitimate** — p=0.06. §6 response settled the verdict. Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $4,652,559 in-window…; [conjunctive-lr] Online txn from a device flagged New.; [conjunctive-lr] ATO shape: 1 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 1)..
**HHG-020**: **legitimate** — p=0.07. §6 response settled the verdict. Top evidence: [query] Ring-WCC (BFS-fixpoint over the card–device projection, device-degree cap 100): component contains 5487 cards across 9199 narrow devices after 5 iterations, $4,821,624 in-window…; [conjunctive-lr] Online txn from a device flagged New.; [conjunctive-lr] ATO shape: 1 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 1)..

## SAR decisions (§3a justification)

**HHG-004** — verdict `fraud`, pattern `card_not_present_new_device`, exposure **$128.33**. Connected cards: 0. SAR narrative (excerpt): Suspicious activity involving unauthorized card-not-present transactions was identified on the account under Case ID HHG-004, resulting in a financial exposure of $128.33. The fraudulent activity was conducted online uti…

**HHG-006** — verdict `fraud`, pattern `undocumented`, exposure **$961.07**. Connected cards: 0. SAR narrative (excerpt): **Suspicious Activity Narrative (FinCEN Form 111)**

Case ID HHG-006 involves unauthorized online account access and fraudulent transactional activity totaling an exposure of $961.07. The suspicious activity was identifi…

**HHG-011** — verdict `fraud`, pattern `card_testing`, exposure **$3,984.80**. Connected cards: 0. SAR narrative (excerpt): Suspicious activity was identified under Case ID HHG-011 involving a confirmed fraud pattern of card testing on an online channel. The activity exhibits a characteristic card-testing shape consisting of 14 sub-$5 online …

**HHG-014** — verdict `fraud`, pattern `undocumented`, exposure **$187.33**. Connected cards: 0. SAR narrative (excerpt): Suspicious Activity Report Narrative:

Case ID HHG-014 involves unauthorized card-not-present activity resulting in a financial exposure of $187.33. The fraudulent transaction was executed online via an anonymous proxy u…


Every other fraud verdict stayed under §3a's threshold combination (exposure ≤ $1,000 AND no shared_element AND pattern ≠ `undocumented`).
