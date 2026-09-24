# Learnings

Surprises and gotchas discovered while building Sentinel. Feeds the blog draft.

## Phase 0

### `card_id` = `<customer_id>-K<rank>` is derived, and the ranking rule was non-obvious
`card1` is 1:1 with `customer_id` (both cardinality 13,553), so the `-K1`/`-K2`
suffix must partition transactions of a customer by *another* column combination.
It turns out to be the distinct tuple `(card2, card3, card4, card5, card6)`,
ranked per customer by **`ROW_NUMBER() OVER (ORDER BY first-seen ts DESC, txn count DESC)`**.
The most-recently-first-seen card gets `K1`.

Two other rankings I tried each scored 10–11 / 20 on the case pack. The winning
rule scores **20 / 20 on the case pack** and **4,356 / 4,665 on the fraud
history** (rate 93.4%). The exact mismatch count is **309** on the fraud
history (previously I wrote "20" — that was the `LIMIT 20` in the diagnostic).

Of the 309 mismatches, the "customer acquired a new card after the case was
closed and it shifted the K-rank" theory only explains **153**. The other **156**
are true rule disagreements — even scoped to `first_ts ≤ closed_at`, our
derivation disagrees with the historical string. I probed 7 alternative
rankings (`first_ts ASC/DESC`, `last_ts ASC/DESC`, `n DESC`, hybrids); none beats
rule E's 4356/4665, and no single deterministic rule reproduces history
100%. The historical `-Kn` was probably assigned at investigation time from
whatever tuples the analyst was working with, with occasional human
inconsistency.

**Engineering conclusion**: never join on the `-Kn` string. Always join
`closed_cases_history.csv` → `first_fraud_txn_id` → `Transaction` → tuple.
When we emit answer JSON, we use the case pack's stated `card_id` verbatim
(rule E derives it correctly anyway) — that's what the grader compares against.
The 309 legacy mismatches only affect similar-prior-cases retrieval when the
retriever incorrectly keys on the string suffix.

### 21 customers have all-null `(card2..card6)` tuples
Their `card_map` entry has all placeholder `'_'` values. Real fraud detection
against them is likely brittle (no card4/card6 = no network/type info). Note
this in evidence when it comes up.

### Case-pack `opened_at` ≠ txn `ts`
The alert timestamps in `case_pack.csv` lag the underlying `ts` by 2–6 h and
the gap is not fixed. `opened_at` = when the alert fired; `ts` = when the
customer transacted. Investigations should key off `ts` for temporal windows,
not `opened_at`.

### `anonymous.com` is a product artifact, **NOT** a fraud signal
Naïve intuition ("anonymous email → suspicious") is dead wrong on this dataset.
Computed likelihood ratios `P(P_or_R=anonymous.com | in-fraud-episode) / P(anon | not-fraud)`:

| segment | P(anon \| fraud) | P(anon \| ~fraud) | **LR** |
|---|---|---|---|
| all online | 6.6% | 17.2% | **0.38** |
| online AND ProductCD=R | 8.9% | **35.9%** | **0.25** |
| online AND ProductCD=C | 5.6% | 8.7% | **0.64** |
| online AND ProductCD=H | 9.6% | 14.4% | **0.67** |

In every online segment, `anonymous.com` is more common among **not-fraud** txns
than fraud txns. It is evidence *against* fraud, if anything. Vesta's
`P_emaildomain`/`R_emaildomain` fields are placeholders for product families
where email isn't captured (particularly `R`, where ~29% of all txns have
`R_emaildomain=anonymous.com`), so the naïve "anon = bad" heuristic is a
product-encoding artifact.

**Consequence:** three benchmark cases (HHG-010, HHG-015, HHG-017) all carry
`P_emaildomain=R_emaildomain=anonymous.com`. The detector must NOT penalize
them for this — the LR is 0.25 on R-product, ~0.6 on H/C. The evidence ledger
must use the true LR (an argument *against*), not the naïve one.

## Phase 1

### 154 cards have every transaction inside a confirmed-fraud episode
`card_baseline_clean` has 14,739 rows vs 14,893 for `card_baseline_raw`. The 154
missing cards had *all* of their transactions inside one or more confirmed-fraud
closed cases — they never carried a legitimate transaction. Likely fresh burner
cards used only by rings. Their `card_baseline_clean` is NULL by design, and
detectors that need a baseline must fall back to `card_baseline_raw` (or,
better, treat those cards as strong ring-membership signals in themselves —
one txn wholly inside a fraud episode across the entire dataset is a big red
flag).

**Distribution of n_txns** for these 154 cards: min=1, p25=1, median=1, p75=2,
p95=4.3, max=9, mean=1.63. **111/154 (72%) are single-transaction throwaways**;
the remaining 43 have 2–9 txns, and every one of them starts and ends within
21 days (most within a single day). Textbook burner-card signature.

### `~$100` on online-R alone is evidence AGAINST fraud (LR ≈ 0.42)
The observation in CHECKPOINT-0 that HHG-005 ($100.07), HHG-017 ($100.09),
HHG-019 ($99.92) are all online-R ~$100 seemed suspicious. But
`P(amt ∈ [95,105] | fraud)` = **13.4%** vs `P(amt ∈ [95,105] | ~fraud)` =
**31.9%** on online-R. **LR = 0.42.** The amount range is nearly identical for
[98,102], [99,101], [99.5,100.5], [95,105] etc. — no sub-band bumps.

This means **~$100 online-R is a *very common* legitimate transaction amount**
(subscription, standard product, etc.). Detectors must NOT flag "~$100 online-R"
as suspicious on its own. What *would* be suspicious is the *coordinated*
appearance of ~$100 online-R across multiple compromised cards in a narrow
window — that's a ring shape, not an amount shape. The Phase-2
`recipient_email_cluster` / `region_cluster` queries can find that.

### `home_addr1` is null for online-only customers
`home_addr1` is defined as the mode of `addr1` over **in-person** transactions.
HHG-011's card (C11923-K2) is 100% online (10,332 online txns, zero in-person)
so it has no home region — but it *does* have `regions_seen` populated from
online `addr1` where available. The out-of-region detector must handle
`home_addr1 IS NULL` gracefully: not a "suspicious" out-of-region signal, just
"insufficient priors".

## Phase 4 — Evidence LR & label leakage

### `is_new_device_for_card` was severely label-leaked (LR 76,750 → 0.81)

**The leak.** The first LR table used `card_baseline_clean.known_device_profiles`
to check "have we seen this device on this card before?". But `card_baseline_clean`
is **built by excluding every transaction that appears in a `confirmed_fraud`
closed case**. So the reference set of "known devices" was, by construction,
free of the exact devices used in confirmed frauds — leading to a
"known set" that was systematically missing the very devices we were testing
for. Every fraud txn was flagged `TRUE` (device "not known") because its device
had been deliberately removed from the set. The LR came out at 76,750; on a
leakage-free set it's 0.81.

**The rebuild** (`sentinel/data/pit_features.py`). Every card-relative signal
is now computed *strictly as-of* the transaction, using only prior txns on the
same card and no reference to the fraud/cleared labels:

- Per-card partition, order by (ts, TransactionID), frame
  `ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING`.
- Running distinct-sets (`prior_devices`, `prior_products`, `prior_p_emails`)
  computed incrementally in Python because DuckDB's `LIST(DISTINCT ...)`
  window aggregate is O(n²).
- Cheap aggregates (`COUNT`, `MEDIAN` over RANGE, `ntile`) stay in DuckDB
  window functions.
- `is_new_device_for_card` returns `NULL` when `n_prior_txns < 5`.

**Why the honest LR is ≈ 1 rather than 2–50** (what intuition would suggest):

1. **Fraud concentrates on high-activity cards.** C09933-K2 has 2,788 txns,
   C11923-K2 has 10,332. Their fraud events fire on devices already in the
   card's history — the criminal either shared a device with earlier legit
   use, or the criminal-owned device was seen before the earliest labelled
   fraud row. Result: **63% of confirmed_fraud online txns fire
   `is_new_device_for_card = FALSE`** (device already known).
2. **Negatives have many first-time devices.** Legitimate users routinely
   change browsers/OSes/resolutions. 36% of the negative online population
   also fires `is_new_device_for_card = TRUE`, matching the fraud rate.
3. **Even the coarser `device_family` (DeviceInfo|browser) has LR ≈ 1.06**
   for new-family-device.

So `is_new_device_for_card` **is not, in isolation, a fraud signal** in this
dataset. It becomes informative only in combination with ring-detection
context (multiple cards share an unusual device profile in a narrow window —
covered by Phase 2's `device_neighbors` and `ring_components` queries, and
Phase 3's semantic retrieval). The signal is still useful as *conjunctive*
evidence, but its base-rate LR is honest.

**Sanity check adjusted.** The `is_new_device_for_card LR ∈ [2, 50]` bound
from the earlier expectation is replaced with `[0.5, 50]` as a *diagnostic*:
it detects leakage (previous 76,750 was outside 50) but doesn't force the
signal to be strong on its own. The scoring path still keeps this signal —
its calibrated posterior contribution is small because the LR is near 1.

### Every empirical anchor in the brief holds
5,565 closed cases (4,665 confirmed / 900 cleared); the SAR rule
`report_filed == "Yes"` iff `exposure > 1000 OR connected_card_ids non-empty`
has **zero exceptions** across 4,665 fraud cases; three action combos exactly;
716 / 158 / 26 cleared archetypes; U1 (SM-G935F + anonymous proxy) hits
CC-2649 / CC-2971 / CC-2985 / CC-3035; U2 (four ~$500 within 40 min) hits
CC-3748 / CC-3841 / CC-3907 / CC-4086 / CC-4124.

## Phase 4

### `ring_wcc` at cap 100 finds the giant component, not a ring
`graph/queries/q18_ring_wcc.gsql` is a real BFS-fixpoint weakly-connected
component algorithm over the card–device projection (edges =
`KNOWN_DEVICE`). I built it hoping it would isolate the shared-device
rings that `ring_components` misses when the ring peer is one hop away
through a different KNOWN_DEVICE edge.

The smoke test told a different story. Seeded on `C00259-K1` with the
default degree cap of 100, the WCC returns **5,487 cards across 9,199
narrow devices** in 5 BFS iterations. At cap 25 it still returns
**3,831 cards**. At cap 5 it returns **1,903**. At cap 3 (very
restrictive) it still returns **1,235 cards**.

That's the giant component. In a card population of ~15,000 the
projection percolates: at any reasonable degree cap, one seed reaches
a third to half of all cards through chains of shared narrow devices.
WCC alone can't isolate a ring. A ring signal has to come from
`ring_components`' narrower filter — narrow device *plus* New/proxied
activity in-window *or* an attached confirmed-fraud ClosedCase on the
shared device. That's why `shared_element` is set by
`compute_shared_element()` on the peer filter, not on raw WCC size.

`ring_wcc` still lands in the ledger as an evidence entry (it names
the component size + exposure) — useful blast-radius context an
analyst can read, not a decision signal.
