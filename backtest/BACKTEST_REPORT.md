# CHECKPOINT 6 (v3) — combined report

**Alert model** (L2 logistic, class-balanced): 5-fold CV **AUC = 0.9465 ± 0.0046**, **Brier = 0.0927**  (5565 closed cases: 4665 confirmed_fraud + 900 cleared).

## Mode comparison

| Metric | Oracle | Simulated (τ=0.300) |
|---|---|---|
| Verdict accuracy (all)          | **1.000** | **0.833** |
| Uncertain rate                   | 0/150 | 0/150 |
| SAR precision / recall           | 0.875 / 0.412 (TP=7, FP=1, FN=10) | 0.600 / 0.353 (TP=6, FP=4, FN=11) |
| Exposure MAE                     | $442.50 | $479.08 |
| Jaccard before signature exp     | 0.284 | 0.357 |
| Jaccard after signature exp      | **0.357** | **0.420** |

## Pattern confusion (oracle — the ceiling)

| gt \ pred | card_testing | card_not_present_fraud | card_not_present_new_device | out_of_region_use | account_takeover | undocumented | none |
|---|---|---|---|---|---|---|---|
| card_testing | 7 | 1 | 1 | 0 | 1 | 2 | 0 |
| card_not_present_fraud | 0 | 6 | 6 | 0 | 0 | 1 | 0 |
| card_not_present_new_device | 0 | 2 | 13 | 0 | 0 | 0 | 0 |
| out_of_region_use | 0 | 0 | 0 | 12 | 1 | 0 | 0 |
| account_takeover | 0 | 0 | 0 | 0 | 6 | 0 | 7 |
| undocumented | 0 | 3 | 1 | 0 | 0 | 5 | 0 |
| none | 0 | 0 | 0 | 0 | 0 | 0 | 75 |

## τ tuning curve

| τ | correct/total | accuracy |
|---|---|---|
| 0.05 | 112/150 | 0.747 |
| 0.10 | 125/150 | 0.833 |
| 0.15 | 126/150 | 0.840 |
| 0.20 | 125/150 | 0.833 |
| 0.25 | 126/150 | 0.840 |
| 0.30 | 136/150 | 0.907 |
| 0.35 | 135/150 | 0.900 |
| 0.40 | 134/150 | 0.893 |
| 0.45 | 133/150 | 0.887 |
| 0.50 | 131/150 | 0.873 |
| 0.55 | 130/150 | 0.867 |
| 0.60 | 126/150 | 0.840 |
| 0.65 | 124/150 | 0.827 |
| 0.70 | 119/150 | 0.793 |
| 0.75 | 118/150 | 0.787 |
| 0.80 | 115/150 | 0.767 |
| 0.85 | 115/150 | 0.767 |

τ = **0.300** (max accuracy vs oracle response).

## Simulator override frequency

- `τ-rule`: 102
- `ring-T3+/testing`: 38
- `legit-archetype`: 10

## Top 10 misclassifications (simulated mode)

| # | case_id | gt outcome/pattern | pred verdict/pattern | p_init | resp | rule |
|---|---|---|---|---|---|---|
| 1 | CC-0587 | cleared/none | fraud/card_not_present_new_device | 0.016 | denied | override:ring-T3+/testing_sequence |
| 2 | CC-1430 | confirmed_fraud/card_not_present_new_device | legitimate/none | 0.016 | confirmed | τ-rule: p_initial=0.016 < τ=0.300 |
| 3 | CC-0952 | cleared/none | fraud/card_not_present_new_device | 0.016 | denied | override:ring-T3+/testing_sequence |
| 4 | CC-3882 | confirmed_fraud/card_not_present_new_device | legitimate/none | 0.058 | confirmed | τ-rule: p_initial=0.058 < τ=0.300 |
| 5 | CC-0600 | cleared/none | fraud/card_not_present_new_device | 0.003 | denied | override:ring-T3+/testing_sequence |
| 6 | CC-0647 | cleared/none | fraud/undocumented | 0.016 | denied | override:ring-T3+/testing_sequence |
| 7 | CC-3612 | cleared/none | fraud/card_not_present_new_device | 0.016 | denied | override:ring-T3+/testing_sequence |
| 8 | CC-3907 | confirmed_fraud/undocumented | legitimate/none | 0.002 | confirmed | τ-rule: p_initial=0.002 < τ=0.300 |
| 9 | CC-0924 | cleared/none | fraud/card_not_present_new_device | 0.012 | denied | override:ring-T3+/testing_sequence |
| 10 | CC-1060 | cleared/none | fraud/undocumented | 0.003 | denied | override:ring-T3+/testing_sequence |

### Ledgers (from backtest/cases/*.json)


**CC-0587** — gt cleared/none vs pred fraud/card_not_present_new_device, p_init=0.016
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=iOS Device | iOS 11.0.3 | mobile safari 11.0 | 2208x1242
  - [graph] `conjunctive-lr:ato ±48h + mixed_channel_last_24h` — ATO shape: 1 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 1).
  - [graph] `query:ring_components  |  informational (T0)` — Device family shared with 69 other cards in the window (KNOWN_DEVICE degree=70). No prior confirmed-fraud Clos
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:risk_decile_10` — alert_model feature `risk_decile_10` fired with coefficient -4.538
  - [graph] `alert_model:channel_online` — alert_model feature `channel_online` fired with coefficient +2.715

**CC-1430** — gt confirmed_fraud/card_not_present_new_device vs pred legitimate/none, p_init=0.016
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=iOS Device | iOS 11.2.1 | mobile safari 11.0 | 2436x1125
  - [graph] `query:ring_components  |  hub_cap_100` — Device family has KNOWN_DEVICE degree=151 (>100 hub cap). Ring-component signal suppressed.
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:risk_decile_10` — alert_model feature `risk_decile_10` fired with coefficient -4.538
  - [graph] `alert_model:channel_online` — alert_model feature `channel_online` fired with coefficient +2.715

**CC-0952** — gt cleared/none vs pred fraud/card_not_present_new_device, p_init=0.016
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=rv:11.0 | Windows 7 | ie 11.0 for desktop | 1680x1050
  - [graph] `query:ring_components  |  informational (T0)` — Device family shared with 36 other cards in the window (KNOWN_DEVICE degree=37). No prior confirmed-fraud Clos
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:risk_decile_10` — alert_model feature `risk_decile_10` fired with coefficient -4.538
  - [graph] `alert_model:channel_online` — alert_model feature `channel_online` fired with coefficient +2.715

**CC-3882** — gt confirmed_fraud/card_not_present_new_device vs pred legitimate/none, p_init=0.058
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=XT1635-02 | Android 7.1.1 | chrome 55.0 for android | 64
  - [graph] `signal:prior_fraud_on_card_tuple` — This card tuple has ≥1 prior confirmed_fraud case closed before this txn.
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:is_new_device_for_card` — alert_model feature `is_new_device_for_card` fired with coefficient -0.124
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:prior_fraud_on_card_tuple` — alert_model feature `prior_fraud_on_card_tuple` fired with coefficient +0.549
  - [graph] `alert_model:prior_fraud_on_customer` — alert_model feature `prior_fraud_on_customer` fired with coefficient +0.895

**CC-0600** — gt cleared/none vs pred fraud/card_not_present_new_device, p_init=0.003
  - [graph] `conjunctive-lr:new_device + proxy` — Online txn from a device flagged New behind an anonymous/hidden proxy.  device_profile=Windows | Windows 7 | c
  - [graph] `conjunctive-lr:ato ±48h + mixed_channel_last_24h` — ATO shape: 1 online txn(s) on id_15='New' in ±48h; 1 online txn(s) via IP_PROXY:* in ±48h  (n_online in ±48h =
  - [graph] `query:ring_components  |  informational (T0)` — Device family shared with 30 other cards in the window (KNOWN_DEVICE degree=31). No prior confirmed-fraud Clos
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:proxy_flag` — alert_model feature `proxy_flag` fired with coefficient -1.590
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:risk_decile_10` — alert_model feature `risk_decile_10` fired with coefficient -4.538

**CC-0647** — gt cleared/none vs pred fraud/undocumented, p_init=0.016
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=Windows | Windows 7 | firefox 57.0 | 1366x768
  - [graph] `query:ring_components + lr_table:device_tiers.T4` — Device tier T4: KNOWN_DEVICE deg=75, 74 other cards in window, 2 confirmed-fraud ClosedCase(s) already attache
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:risk_decile_10` — alert_model feature `risk_decile_10` fired with coefficient -4.538
  - [graph] `alert_model:channel_online` — alert_model feature `channel_online` fired with coefficient +2.715
  - [graph] `alert_model:T1_21-100` — alert_model feature `T1_21-100` fired with coefficient +0.000

**CC-3612** — gt cleared/none vs pred fraud/card_not_present_new_device, p_init=0.016
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=Windows | Windows 7 | firefox 58.0 | 1920x1080
  - [graph] `query:ring_components  |  informational (T0)` — Device family shared with 71 other cards in the window (KNOWN_DEVICE degree=27). No prior confirmed-fraud Clos
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:risk_decile_10` — alert_model feature `risk_decile_10` fired with coefficient -4.538
  - [graph] `alert_model:channel_online` — alert_model feature `channel_online` fired with coefficient +2.715

**CC-3907** — gt confirmed_fraud/undocumented vs pred legitimate/none, p_init=0.002
  - [graph] `conjunctive-lr:cnp_burst` — Online purchase inconsistent with the cardholder's usual pattern: ProductCD=C not previously seen on this card
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=iOS Device | iOS 11.1.2 | mobile safari 11.0 | 1334x750
  - [graph] `conjunctive-lr:ato ±48h + mixed_channel_last_24h` — ATO shape: 4 online txn(s) on id_15='New' in ±48h  (n_online in ±48h = 4).
  - [graph] `query:ring_components  |  hub_cap_100` — Device family has KNOWN_DEVICE degree=426 (>100 hub cap). Ring-component signal suppressed.
  - [graph] `query:near_threshold_burst  |  prior: hand-set` — Threshold-structured burst: 3 online txns just below $500 within the window — structuring shape.
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:unseen_productcd` — alert_model feature `unseen_productcd` fired with coefficient +0.206

**CC-0924** — gt cleared/none vs pred fraud/card_not_present_new_device, p_init=0.012
  - [graph] `conjunctive-lr:new_device` — Online txn from a device flagged New.  device_profile=MacOS | Mac OS X 10_12_6 | safari generic | 1280x800
  - [graph] `conjunctive-lr:ato ±48h + mixed_channel_last_24h` — ATO shape: mixed-channel activity in 24h; 9 online txn(s) on id_15='New' in ±48h; 1 online txn(s) via IP_PROXY
  - [graph] `query:ring_components  |  informational (T0)` — Device family shared with 402 other cards in the window (KNOWN_DEVICE degree=83). No prior confirmed-fraud Clo
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:is_new_device_for_card` — alert_model feature `is_new_device_for_card` fired with coefficient -0.124
  - [graph] `alert_model:had_prior_txn_in_region` — alert_model feature `had_prior_txn_in_region` fired with coefficient +0.000
  - [graph] `alert_model:mixed_channel_last_24h` — alert_model feature `mixed_channel_last_24h` fired with coefficient -0.220

**CC-1060** — gt cleared/none vs pred fraud/undocumented, p_init=0.003
  - [graph] `conjunctive-lr:new_device + proxy` — Online txn from a device flagged New behind an anonymous/hidden proxy.  device_profile=iOS Device | iOS 11.0.1
  - [graph] `conjunctive-lr:ato ±48h + mixed_channel_last_24h` — ATO shape: 2 online txn(s) on id_15='New' in ±48h; 1 online txn(s) via IP_PROXY:* in ±48h  (n_online in ±48h =
  - [graph] `query:ring_components + lr_table:device_tiers.T3` — Device tier T3: KNOWN_DEVICE deg=19, 93 other cards in window, 1 confirmed-fraud ClosedCase(s) already attache
  - [graph] `alert_model:__intercept__` — alert_model prior (intercept, rebased to p=0.5) = +2.646
  - [graph] `alert_model:id_15_new` — alert_model feature `id_15_new` fired with coefficient -4.932
  - [graph] `alert_model:proxy_flag` — alert_model feature `proxy_flag` fired with coefficient -1.590
  - [graph] `alert_model:is_new_device_for_card` — alert_model feature `is_new_device_for_card` fired with coefficient -0.124
  - [graph] `alert_model:unseen_productcd` — alert_model feature `unseen_productcd` fired with coefficient +0.206

## Caveats

- **Training/test overlap**: the alert model was fit on all 5,565 closed cases. The backtest samples from the SAME pool, so 150/5565 = 2.7% of the training set appears here. The reported 5-fold CV metrics (AUC 0.9465, Brier 0.0927) are the honest generalisation estimates; the in-sample verdict accuracies are optimistic.
- **Class imbalance in cleared**: ~80% of cleared cases in the history involve travel; new-phone / big-purchase are under-represented. Cleared-detector coverage of the other archetypes is therefore weaker than these numbers suggest.
- **Trigger fixed to risk_score**: real HHG cases have three trigger types (risk_score, customer_report, analyst_request) but the backtest uses risk_score for every case, which changes how the alert-conditional risk_decile feature and R7 branch behave.
- **Model frozen**: the coefficients in `sentinel/evidence/alert_model.json` are committed with hash-shaped SHA at this snapshot. Any re-fit changes the header of future reports.
