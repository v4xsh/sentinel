# Undocumented findings — Nov–Dec 2016 sweep

Two coordinated fraud shapes surfaced by scanning the exam-period transactions against the U1 (narrow proxied device ring) and U2 (near-threshold burst) definitions from Sentinel's detectors. Neither is one of the five README-documented patterns.

## U1 — narrow proxied device rings

Devices with `KNOWN_DEVICE` degree ≤ 100 that carried ≥ 3 different cards in Nov–Dec 2016 with ≥ 1 proxied transaction.

| # | device_profile | n_cards | n_proxied_txns | window | n_txns | note |
|---|---|---|---|---|---|---|
| U1-1 | `iOS Device | iOS 11.3.0 | mobile safari generic | 2436x1125` | 76 | 16 | 2016-11-01 05:13:19 → 2016-12-10 00:56:26 | 126 | One-sentence description of a shared-device ring: 76 distinct cards used device 'iOS Device | iOS 11.3.0 | mobile safari generic | 2436x1125' between 2016-11-01 05:13:19 and 2016-1 |
| U1-2 | `iOS Device | iOS 11.3.0 | mobile safari 11.0 | 2048x1536` | 72 | 1 | 2016-12-05 18:23:57 → 2016-12-30 20:51:55 | 115 | One-sentence description of a shared-device ring: 72 distinct cards used device 'iOS Device | iOS 11.3.0 | mobile safari 11.0 | 2048x1536' between 2016-12-05 18:23:57 and 2016-12-3 |
| U1-3 | `Windows | Windows 10 | chrome 65.0 | 1600x900` | 56 | 1 | 2016-11-01 14:24:20 → 2016-12-18 21:01:31 | 83 | One-sentence description of a shared-device ring: 56 distinct cards used device 'Windows | Windows 10 | chrome 65.0 | 1600x900' between 2016-11-01 14:24:20 and 2016-12-18 21:01:31  |
| U1-4 | `Windows | Windows 7 | chrome 65.0 | 1366x768` | 55 | 4 | 2016-11-01 03:23:53 → 2016-12-27 11:42:10 | 82 | One-sentence description of a shared-device ring: 55 distinct cards used device 'Windows | Windows 7 | chrome 65.0 | 1366x768' between 2016-11-01 03:23:53 and 2016-12-27 11:42:10 w |
| U1-5 | `Windows | Windows 10 | chrome 66.0 | 1600x900` | 55 | 1 | 2016-12-05 17:45:53 → 2016-12-30 11:32:25 | 76 | One-sentence description of a shared-device ring: 55 distinct cards used device 'Windows | Windows 10 | chrome 66.0 | 1600x900' between 2016-12-05 17:45:53 and 2016-12-30 11:32:25  |
| U1-6 | `Windows | Windows 7 | chrome generic | 1920x1080` | 54 | 12 | 2016-11-01 19:48:04 → 2016-12-08 00:45:47 | 71 | One-sentence description of a shared-device ring: 54 distinct cards used device 'Windows | Windows 7 | chrome generic | 1920x1080' between 2016-11-01 19:48:04 and 2016-12-08 00:45: |
| U1-7 | `iOS Device | iOS 11.3.0 | mobile safari 11.0 | 1136x640` | 49 | 7 | 2016-12-08 00:26:17 → 2016-12-30 03:33:04 | 84 | One-sentence description of a shared-device ring: 49 distinct cards used device 'iOS Device | iOS 11.3.0 | mobile safari 11.0 | 1136x640' between 2016-12-08 00:26:17 and 2016-12-30 |
| U1-8 | `iOS Device | iOS 11.3.0 | mobile safari generic | 2048x1536` | 48 | 5 | 2016-11-05 02:37:49 → 2016-12-09 17:24:50 | 75 | One-sentence description of a shared-device ring: 48 distinct cards used device 'iOS Device | iOS 11.3.0 | mobile safari generic | 2048x1536' between 2016-11-05 02:37:49 and 2016-1 |
| U1-9 | `Windows | Windows 7 | chrome 66.0 | 1366x768` | 47 | 3 | 2016-12-06 13:47:32 → 2016-12-30 15:47:33 | 63 | One-sentence description of a shared-device ring: 47 distinct cards used device 'Windows | Windows 7 | chrome 66.0 | 1366x768' between 2016-12-06 13:47:32 and 2016-12-30 15:47:33 w |
| U1-10 | `Windows | Windows 7 | chrome 66.0 | 1600x900` | 45 | 1 | 2016-12-07 17:17:12 → 2016-12-30 13:59:37 | 56 | One-sentence description of a shared-device ring: 45 distinct cards used device 'Windows | Windows 7 | chrome 66.0 | 1600x900' between 2016-12-07 17:17:12 and 2016-12-30 13:59:37 w |

## U2 — near-threshold bursts

Cards with ≥ 3 online transactions in a ±48-hour window whose amounts sit strictly below a round threshold (100 / 200 / 500 / 1000 USD), consistent with structuring.

| # | customer_id | threshold | n_below | window | avg_amt | note |
|---|---|---|---|---|---|---|
| U2-1 | C02740 | $100 | 8 | 2016-11-17 13:23:37 → 2016-11-17 14:23:09 | $99.94 | One-sentence description of a near-threshold burst: card C02740 placed 8 online charges just under $100 between 2016-11-17 13:23:37 and 2016-11-17 14:23:09 at an average of $99.94. |
| U2-2 | C13134 | $100 | 4 | 2016-12-22 20:40:54 → 2016-12-22 21:20:52 | $99.95 | One-sentence description of a near-threshold burst: card C13134 placed 4 online charges just under $100 between 2016-12-22 20:40:54 and 2016-12-22 21:20:52 at an average of $99.95. |
| U2-3 | C00215 | $100 | 4 | 2016-12-12 08:42:04 → 2016-12-12 11:20:20 | $82.65 | One-sentence description of a near-threshold burst: card C00215 placed 4 online charges just under $100 between 2016-12-12 08:42:04 and 2016-12-12 11:20:20 at an average of $82.65. |
| U2-4 | C09787 | $100 | 4 | 2016-12-07 01:34:24 → 2016-12-07 02:10:06 | $94.80 | One-sentence description of a near-threshold burst: card C09787 placed 4 online charges just under $100 between 2016-12-07 01:34:24 and 2016-12-07 02:10:06 at an average of $94.80. |
| U2-5 | C11678 | $100 | 4 | 2016-11-30 18:33:01 → 2016-12-01 17:56:47 | $99.95 | One-sentence description of a near-threshold burst: card C11678 placed 4 online charges just under $100 between 2016-11-30 18:33:01 and 2016-12-01 17:56:47 at an average of $99.95. |
| U2-6 | C06533 | $100 | 4 | 2016-11-20 21:03:23 → 2016-11-22 05:07:22 | $80.22 | One-sentence description of a near-threshold burst: card C06533 placed 4 online charges just under $100 between 2016-11-20 21:03:23 and 2016-11-22 05:07:22 at an average of $80.22. |
| U2-7 | C10295 | $100 | 3 | 2016-12-24 18:33:53 → 2016-12-24 18:46:04 | $99.94 | One-sentence description of a near-threshold burst: card C10295 placed 3 online charges just under $100 between 2016-12-24 18:33:53 and 2016-12-24 18:46:04 at an average of $99.94. |
| U2-8 | C03144 | $100 | 3 | 2016-12-30 21:59:00 → 2016-12-30 22:04:13 | $85.81 | One-sentence description of a near-threshold burst: card C03144 placed 3 online charges just under $100 between 2016-12-30 21:59:00 and 2016-12-30 22:04:13 at an average of $85.81. |
| U2-9 | C00466 | $100 | 3 | 2016-11-25 00:37:56 → 2016-11-25 00:50:15 | $86.51 | One-sentence description of a near-threshold burst: card C00466 placed 3 online charges just under $100 between 2016-11-25 00:37:56 and 2016-11-25 00:50:15 at an average of $86.51. |
| U2-10 | C00895 | $100 | 3 | 2016-12-25 16:13:36 → 2016-12-26 18:30:28 | $85.01 | One-sentence description of a near-threshold burst: card C00895 placed 3 online charges just under $100 between 2016-12-25 16:13:36 and 2016-12-26 18:30:28 at an average of $85.01. |