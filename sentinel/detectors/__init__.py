"""Pattern detectors.

Each detector is a pure function of a :class:`DetectorContext` and returns a
:class:`DetectorResult` = (pattern_label | None, list[Evidence]).

Design rules per Phase-5 spec:
  - The pattern label alone does NOT add log-odds. It is a textual conclusion
    used to populate ``case.pattern`` and to guide policy-engine behaviour.
  - For conjunctive detectors (e.g. CNP burst = unseen_product AND
    2-4 online in 48h AND amt_z>3), the joint LR is estimated from closed
    cases in ``sentinel.evidence.conjunctive_lr`` — we don't sum component LRs.
  - Graph-derived signals with a hand-set LR (no historical prevalence data
    or too-rare-to-estimate) mark the ``ref`` string with ``"prior: hand-set"``
    so the ledger writer can flag it in the evidence list.
"""

from sentinel.detectors.base import (  # noqa: F401
    DetectorContext, DetectorResult,
)
from sentinel.detectors.pattern_detectors import (  # noqa: F401
    detect_card_testing,
    detect_cnp,
    detect_cnp_new_device,
    detect_out_of_region,
    detect_account_takeover,
    detect_undocumented_proxy_ring,
    detect_undocumented_threshold_burst,
    detect_legit_trip,
    detect_legit_new_phone,
    detect_legit_big_purchase,
    detect_legit_recurring_r7,
    run_all_detectors,
)
