# Calibration report (class-balanced)
- Labelled txns: 590,742  (fraud=14,055, non-fraud=576,687)
- Scoring prior: p=0.5 (logit 0) — see DECISIONS.md.
- Train / holdout split: 80 / 20 stratified per class, seed=20260920.

## Metrics on holdout

| metric | raw | isotonic-cal |
|---|---:|---:|
| balanced Brier    | 0.1598 | 0.1553 |
| unweighted Brier  | 0.1812 | 0.1449 |
| AUC               | 0.8478 | 0.8485 |

## Reliability (isotonic, class-balanced weights)

| decile | predicted | observed(weighted) | n |
|---|---:|---:|---:|
| 1 | 0.041 | 0.057 | 5,425 |
| 2 | 0.060 | 0.068 | 11,203 |
| 3 | 0.117 | 0.128 | 12,337 |
| 4 | 0.173 | 0.146 | 16,092 |
| 5 | 0.210 | 0.237 | 6,796 |
| 6 | 0.225 | 0.211 | 10,514 |
| 7 | 0.323 | 0.331 | 15,516 |
| 8 | 0.368 | 0.404 | 9,540 |
| 9 | 0.511 | 0.512 | 12,757 |
| 10 | 0.831 | 0.829 | 17,969 |

Plots: `backtest/calibration.png` (isotonic, class-balanced) and `backtest/calibration_raw.png` (LR-sigmoid raw).
