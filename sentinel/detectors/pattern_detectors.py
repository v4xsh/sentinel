"""Pattern detectors — one per README typology + false-alarm archetypes.

Each detector returns a :class:`DetectorResult` = (pattern | None,
[Evidence], tags). Detectors read from ``ctx.txn_row`` (a row of
``txn_features``) and, where needed, from ``ctx.graph_signals`` /
``ctx.baseline_summary`` populated by the agent's earlier nodes.

Pattern label is a textual conclusion — it does NOT contribute log-LR.
Only the ``Evidence.log_lr`` values do.
"""

from __future__ import annotations

import logging
from typing import Optional

from sentinel.detectors.base import DetectorContext, DetectorResult
from sentinel.evidence.conjunctive_lr import joint_lr
from sentinel.evidence.ledger import DeviceLink, Evidence

logger = logging.getLogger(__name__)


# ---------- helpers -----------------------------------------------------------


def _signal_log_lr(name: str, ctx: DetectorContext) -> float:
    """Look up the base log_lr for a single signal from the LR table.

    Accepts either a DetectorContext or a raw lr_table dict as second arg.
    """
    lr_table = ctx.lr_table if hasattr(ctx, "lr_table") else ctx
    if not isinstance(lr_table, dict):
        return 0.0
    s = lr_table.get("signals", {}).get(name)
    return float(s["log_lr"]) if s else 0.0


def _device_link_from_row(row: dict, activity_window_overlap: str = "in") -> DeviceLink:
    return DeviceLink(
        id_15=row.get("id_15") or None,
        id_23=row.get("id_23") or None,
        first_seen_on_card=str(row.get("ts")) if row.get("ts") else None,
        activity_window_overlap=activity_window_overlap,
    )


# ---------- documented patterns -----------------------------------------------


def detect_card_testing(ctx: DetectorContext) -> DetectorResult:
    """R5 / README pattern 1.

    "≥3 tiny online authorizations, often <$5, followed by a larger purchase."
    C6v2: reads from the ``testing_sequence`` graph query (n_small, n_larger,
    small/larger txns) instead of the as-of prior counts. Fires whenever the
    query reports ≥3 small txns in the window (default: ± investigation
    window from ``_fetch_graph_signals``).
    """
    ts = ctx.graph_signals.get("testing_sequence") or {}
    if not ts.get("fires"):
        return DetectorResult(pattern=None)
    n_small  = int(ts.get("n_small", 0) or 0)
    n_larger = int(ts.get("n_larger", 0) or 0)
    small_txns  = list(ts.get("small_txns")  or [])
    larger_txns = list(ts.get("larger_txns") or [])

    lr = joint_lr(
        predicate_sql=(
            "t.n_online_last_48h_bucket IN ('2-4', '5+') "
            "AND t.TransactionAmt < 5"
        ),
        scope_sql="t.channel = 'online'",
        con=ctx.con,
    )
    ev = Evidence(
        claim=(
            f"Card-testing shape from testing_sequence query: {n_small} "
            f"sub-$5 online txn(s) + {n_larger} larger follow-up(s) in the "
            f"window."
        ),
        source="graph",
        ref="query:testing_sequence",
        entity_ids=[ctx.txn_id] + small_txns[:5] + larger_txns[:2],
        channel="sequence",
        log_lr=lr["log_lr"],
        direction="for" if lr["log_lr"] > 0 else "against",
    )
    affected = list(set([ctx.txn_id] + small_txns + larger_txns))
    return DetectorResult(
        pattern="card_testing",
        affected_txn_ids=affected,
        evidence=[ev],
        tags={"testing_sequence_fires"},
    )


def detect_cnp(ctx: DetectorContext) -> DetectorResult:
    """README pattern 2: CNP fraud — online, amounts/products inconsistent
    with the cardholder's history, often 2–4 in 48h.

    C6v2: uses ``burst_48h`` graph query result for the burst count (window
    is ±48h around the flag), plus row-level ``unseen_productcd`` and
    ``amt_z_asof`` for the product/amount anomaly.
    """
    row = ctx.txn_row
    if row.get("channel") != "online":
        return DetectorResult(pattern=None)

    burst_signal = ctx.graph_signals.get("burst_48h") or {}
    n_online_48h = int(burst_signal.get("n_online", 0) or 0)
    burst = n_online_48h >= 2
    unseen_prod = row.get("unseen_productcd") is True
    high_z = (row.get("amt_z_asof") is not None
              and abs(row["amt_z_asof"]) > 3.0)
    fires = unseen_prod and burst and high_z
    if not fires:
        return DetectorResult(pattern=None)

    lr = joint_lr(
        predicate_sql=(
            "t.unseen_productcd = TRUE "
            "AND t.n_online_last_48h_bucket IN ('2-4', '5+') "
            "AND t.amt_z_asof IS NOT NULL AND abs(t.amt_z_asof) > 3.0"
        ),
        scope_sql="t.channel = 'online'",
        con=ctx.con,
    )
    ev = Evidence(
        claim=(
            "Online purchase inconsistent with the cardholder's usual "
            f"pattern: ProductCD={row.get('ProductCD','?')} not previously "
            f"seen on this card, in a "
            f"{row.get('n_online_last_48h_bucket')}-txn 48h burst, "
            f"amount z-score {row['amt_z_asof']:+.2f}."
        ),
        source="graph",
        ref="conjunctive-lr:cnp_burst",
        entity_ids=[ctx.txn_id],
        channel="amount",
        log_lr=lr["log_lr"],
        direction="for" if lr["log_lr"] > 0 else "against",
    )
    return DetectorResult(pattern="card_not_present_fraud", affected_txn_ids=[ctx.txn_id],
                          evidence=[ev])


def detect_cnp_new_device(ctx: DetectorContext) -> DetectorResult:
    """README pattern 3: same as CNP but on a new device (with optional proxy)."""
    row = ctx.txn_row
    if row.get("channel") != "online":
        return DetectorResult(pattern=None)
    if not row.get("device_profile"):
        # P5v5-3: no device signal to attribute.
        return DetectorResult(pattern=None)

    is_new_device = row.get("is_new_device_for_card") is True
    id_15_new     = (row.get("id_15") == "New")
    proxy         = bool(row.get("proxy_flag"))
    if not (is_new_device or id_15_new):
        return DetectorResult(pattern=None)

    lr = joint_lr(
        predicate_sql=(
            "(t.is_new_device_for_card = TRUE OR t.id_15 = 'New')"
        ),
        scope_sql="t.channel = 'online'",
        con=ctx.con,
    )
    lr_proxy = joint_lr(
        predicate_sql=(
            "(t.is_new_device_for_card = TRUE OR t.id_15 = 'New') AND t.proxy_flag = TRUE"
        ),
        scope_sql="t.channel = 'online'",
        con=ctx.con,
    ) if proxy else None

    device_link = _device_link_from_row(row)
    ev: list[Evidence] = [
        Evidence(
            claim=(
                f"Online txn from a device flagged {'New' if id_15_new else '(prior-unseen for this card)'}"
                + (" behind an anonymous/hidden proxy." if proxy else ".")
                + f"  device_profile={row.get('device_profile', '?')}"
            ),
            source="graph",
            ref="conjunctive-lr:new_device" + (" + proxy" if proxy else ""),
            entity_ids=[ctx.txn_id],
            channel="device",
            log_lr=(lr_proxy or lr)["log_lr"],
            direction="for" if (lr_proxy or lr)["log_lr"] > 0 else "against",
            device_link=device_link,
        )
    ]
    return DetectorResult(pattern="card_not_present_new_device",
                          affected_txn_ids=[ctx.txn_id], evidence=ev)


def detect_out_of_region(ctx: DetectorContext) -> DetectorResult:
    """README pattern 4: card-present in a new billing region.

    C6v2 rules:
      * Fires whenever ``channel == 'in_person'`` and ``addr1 != home_addr1_asof``.
      * ``had_prior_txn_in_region`` and ``prior_cleared_travel_on_card`` DAMPEN
        the LR (they don't veto the label — the pattern still applies).
      * Concurrent home activity in-window (``n_home_txns_in_window > 0``)
        boosts the LR (clone-shape).
      * Tag ``legit_trip_hint`` is added when trip-shaped signals dominate,
        so the simulator can override to "confirmed".
    """
    row = ctx.txn_row
    if row.get("channel") != "in_person":
        return DetectorResult(pattern=None)
    addr1 = row.get("addr1")
    home  = row.get("home_addr1_asof")
    if addr1 is None or home is None or str(addr1) == str(home):
        return DetectorResult(pattern=None)

    lr = joint_lr(
        predicate_sql="t.out_of_home_region_asof = TRUE",
        scope_sql="t.channel = 'in_person'",
        con=ctx.con,
    )
    base_log_lr = float(lr["log_lr"])
    rh = ctx.graph_signals.get("region_history") or {}
    n_home_in_window = int(rh.get("n_home_txns_in_window", 0) or 0)
    had_prior_here   = bool(rh.get("had_prior_txn_in_region"))
    prior_cleared_travel = row.get("prior_cleared_travel_on_card") is True

    # Dampeners: multiplicative factors (never zero out).
    damp = 1.0
    tags: set[str] = set()
    if had_prior_here:
        damp *= 0.5
    if prior_cleared_travel:
        damp *= 0.35
        tags.add("legit_trip_hint")
    # Booster: concurrent home activity strengthens the log-lr (clone).
    if n_home_in_window > 0:
        damp *= 1.5

    log_lr = base_log_lr * damp
    claim_parts = [
        f"In-person txn in region {addr1} differs from home region {home}"
    ]
    if n_home_in_window > 0:
        claim_parts.append(f"card had {n_home_in_window} concurrent home-region txns in 14d (clone-shape)")
    if had_prior_here:
        claim_parts.append("card has prior activity in this region (dampener)")
    if prior_cleared_travel:
        claim_parts.append("prior cleared-travel case on this card (dampener + legit_trip_hint)")
    ev = Evidence(
        claim="; ".join(claim_parts) + f". Effective log_lr = {log_lr:+.2f} (base {base_log_lr:+.2f} × damp {damp:.2f}).",
        source="graph",
        ref="conjunctive-lr:out_of_home_region_asof + region_history:dampened",
        entity_ids=[ctx.txn_id, str(addr1)],
        channel="region",
        log_lr=log_lr,
        direction="for" if log_lr > 0 else "against",
    )
    return DetectorResult(pattern="out_of_region_use",
                          affected_txn_ids=[ctx.txn_id],
                          evidence=[ev], tags=tags)


def detect_account_takeover(ctx: DetectorContext) -> DetectorResult:
    """README pattern 5: ATO.

    C6v2 rewrite: use the CARD's online txns within ±48h of the flag —
    new device / proxy / M-flag anomalies + ``mixed_channel_last_24h`` on
    the alert's own row. The flagged txn's own device does NOT need to be
    problematic; other online txns on the card do.
    """
    row = ctx.txn_row
    mixed = row.get("mixed_channel_last_24h") is True

    con = ctx.con
    # Aggregate the card's online AND in-person txns in ±48h around the flag.
    try:
        r = con.execute(
            """
            SELECT
              SUM(CASE WHEN channel='online'    THEN 1 ELSE 0 END) AS n_online,
              SUM(CASE WHEN channel='in_person' THEN 1 ELSE 0 END) AS n_inperson,
              SUM(CASE WHEN channel='online' AND id_15='New'                THEN 1 ELSE 0 END) AS n_new_device,
              SUM(CASE WHEN channel='online' AND id_23 LIKE 'IP_PROXY:%'    THEN 1 ELSE 0 END) AS n_proxied,
              SUM(CASE WHEN channel='online' AND (M1='false' OR M2='false' OR M3='false'
                             OR M6='false' OR M7='false' OR M8='false'
                             OR M9='false')              THEN 1 ELSE 0 END) AS n_m_bad
            FROM txn_features
            WHERE customer_id = ?
              AND ts BETWEEN (CAST(? AS TIMESTAMP) - INTERVAL 48 HOUR)
                         AND (CAST(? AS TIMESTAMP) + INTERVAL 48 HOUR)
            """,
            [ctx.customer_id, row.get("ts"), row.get("ts")],
        ).fetchone()
        n_online, n_inperson, n_new_device, n_proxied, n_m_bad = (int(x or 0) for x in r)
    except Exception:  # noqa: BLE001
        n_online = n_inperson = n_new_device = n_proxied = n_m_bad = 0

    both_channel_48h = (n_online >= 1 and n_inperson >= 1)
    ato_signals = ((n_new_device >= 1) + (n_proxied >= 1) + (n_m_bad >= 1))
    if not (mixed or both_channel_48h or ato_signals >= 2):
        return DetectorResult(pattern=None)

    lr = joint_lr(
        predicate_sql=(
            "t.mixed_channel_last_24h = TRUE "
            "OR (t.id_15 = 'New' AND t.proxy_flag = TRUE)"
        ),
        scope_sql="1=1",
        con=ctx.con,
    )
    parts = []
    if mixed:
        parts.append("mixed-channel activity in 24h")
    if n_new_device >= 1:
        parts.append(f"{n_new_device} online txn(s) on id_15='New' in ±48h")
    if n_proxied >= 1:
        parts.append(f"{n_proxied} online txn(s) via IP_PROXY:* in ±48h")
    if n_m_bad >= 1:
        parts.append(f"{n_m_bad} online txn(s) with an M-flag false in ±48h")
    ev = Evidence(
        claim="ATO shape: " + "; ".join(parts) + f"  (n_online in ±48h = {n_online}).",
        source="graph",
        ref="conjunctive-lr:ato ±48h + mixed_channel_last_24h",
        entity_ids=[ctx.txn_id],
        channel="identity_flags",
        log_lr=lr["log_lr"],
        direction="for" if lr["log_lr"] > 0 else "against",
    )
    return DetectorResult(pattern="account_takeover",
                          affected_txn_ids=[ctx.txn_id], evidence=[ev])


# ---------- undocumented (U1 / U2) --------------------------------------------


def detect_undocumented_proxy_ring(ctx: DetectorContext) -> DetectorResult:
    """Tiered device-ring evidence (T1 / T2 / T3).

    Tier definitions, all computed as-of the alert's opened_at:

      * **T1**: device has ≥1 confirmed-fraud ClosedCase closed < opened_at.
      * **T2**: T1 AND this txn is proxied (id_23 starts with IP_PROXY:*).
      * **T3**: T2 AND ≥2 OTHER cards had id_15='New' or proxied txns on
                the device in ±14 days of opened_at.

    Ring detector emits the HIGHEST tier that fires, with that tier's
    empirical LR from ``lr_table.device_tiers``. Tiers are never summed.
    Hub cap: KNOWN_DEVICE degree ≤ 100 (browser-hub filter).

    Pattern = "undocumented" (U1) only when T3 fires. shared_element
    handling is done in ``_shared_element_from_signals`` — this detector
    just emits evidence.
    """
    # P5v5-3: bail cleanly when the flagged txn has no device_profile.
    row = ctx.txn_row
    if not row.get("device_profile"):
        return DetectorResult(pattern=None)

    ring = ctx.graph_signals.get("ring_components")
    if not ring:
        return DetectorResult(pattern=None)
    n_cards        = int(ring.get("n_cards", 0) or 0)
    dev_degree     = int(ring.get("device_degree", 0) or 0)
    has_cc         = bool(ring.get("has_confirmed_fraud_cc"))
    n_cc           = int(ring.get("n_prior_fraud_cc_on_device", 0) or 0)
    n_other_np     = int(ring.get("n_other_new_or_proxied_in_window", 0) or 0)
    n_distinct_cust_with_cc = int(ring.get("n_other_cards_with_fraud_cc", 0) or 0)
    proxy_flag     = bool(row.get("proxy_flag"))

    # Hub cap: browsers with >100 cards on them are not rings.
    if dev_degree > 100:
        ev = Evidence(
            claim=(
                f"Device family has KNOWN_DEVICE degree={dev_degree} (>100 "
                "hub cap). Ring-component signal suppressed."
            ),
            source="graph",
            ref="query:ring_components  |  hub_cap_100",
            entity_ids=[ctx.txn_id],
            channel="device",
            log_lr=0.0,
            direction="neutral",
            device_link=_device_link_from_row(row, "in"),
        )
        return DetectorResult(pattern=None, evidence=[ev])

    # No tier fires — no evidence.
    if not has_cc:
        # Emit informational only if there's meaningful graph structure.
        if n_cards >= 3:
            ev = Evidence(
                claim=(
                    f"Device family shared with {n_cards - 1} other cards in "
                    f"the window (KNOWN_DEVICE degree={dev_degree}). No prior "
                    "confirmed-fraud ClosedCase on the device — no tier "
                    "fires."
                ),
                source="graph",
                ref="query:ring_components  |  informational (T0)",
                entity_ids=[ctx.txn_id],
                channel="device",
                log_lr=0.0,
                direction="neutral",
                device_link=_device_link_from_row(row, "in"),
            )
            return DetectorResult(pattern=None, evidence=[ev])
        return DetectorResult(pattern=None)

    # ---- Which tier fires? Highest wins, never sum. ----
    # Precedence: T4 > T3 > T2 > T1. LR is picked from the degree-bucket
    # table (C6v2-LR): 1-5 (narrow), 6-20 (medium), 21-100 (broad).
    def _bucket(deg: int) -> str:
        if deg <= 5:      return "le5"
        if deg <= 20:     return "6-20"
        return "21-100"
    bucket = _bucket(dev_degree)
    tiers_by_deg = ctx.lr_table.get("device_tiers_by_degree", {})
    tiers = tiers_by_deg.get(bucket) or ctx.lr_table.get("device_tiers", {})
    tier_used = "T1"
    lr_info = tiers.get("T1", {"lr": 1.0, "log_lr": 0.0})
    if proxy_flag:
        tier_used = "T2"
        lr_info = tiers.get("T2", lr_info)
        if n_other_np >= 2:
            tier_used = "T3"
            lr_info = tiers.get("T3", lr_info)
    if n_distinct_cust_with_cc >= 2:
        tier_used = "T4"
        lr_info = tiers.get("T4", lr_info)
    log_lr = float(lr_info.get("log_lr", 0.0))

    claim_parts = [
        f"Device tier {tier_used}: KNOWN_DEVICE deg={dev_degree}, "
        f"{n_cards - 1} other cards in window, {n_cc} confirmed-fraud "
        f"ClosedCase(s) already attached to this device"
    ]
    if tier_used in ("T2", "T3"):
        claim_parts.append("this txn is proxied (IP_PROXY:*)")
    if tier_used == "T3":
        claim_parts.append(
            f"{n_other_np} other cards showed id_15='New' or IP_PROXY in ±14d"
        )
    claim = "; ".join(claim_parts) + f"; LR({tier_used})={lr_info.get('lr', 1.0):.3f}."

    ev = Evidence(
        claim=claim,
        source="graph",
        ref=f"query:ring_components + lr_table:device_tiers.{tier_used}",
        entity_ids=[ctx.txn_id] + list(ring.get("cards", []))[:5],
        channel="device",
        log_lr=log_lr,
        direction="for" if log_lr > 0 else "against",
        device_link=_device_link_from_row(row, "in"),
    )

    # Report all four LRs on the evidence for the run summary.
    tiers_report = {
        "bucket": bucket,
        "T1": tiers.get("T1", {}).get("lr"),
        "T2": tiers.get("T2", {}).get("lr"),
        "T3": tiers.get("T3", {}).get("lr"),
        "T4": tiers.get("T4", {}).get("lr"),
        "used": tier_used,
    }
    setattr(ev, "device_tiers_report", tiers_report)  # type: ignore

    # U1 (undocumented) label at T3 or T4 (both indicate coordinated ring shape).
    if tier_used in ("T3", "T4"):
        return DetectorResult(pattern="undocumented",
                              affected_txn_ids=[ctx.txn_id],
                              evidence=[ev],
                              tags={"undocumented_coordinated"})
    return DetectorResult(pattern=None, affected_txn_ids=[ctx.txn_id],
                          evidence=[ev])


def detect_undocumented_threshold_burst(ctx: DetectorContext) -> DetectorResult:
    """U2 shape — several online txns each just below a round threshold within
    minutes, from the same card. Uses `near_threshold_burst` query result."""
    burst = ctx.graph_signals.get("near_threshold_burst")
    if not burst or not burst.get("fires"):
        return DetectorResult(pattern=None)

    hand_set_log_lr = 3.0
    ev = Evidence(
        claim=(
            f"Threshold-structured burst: {burst['n']} online txns just below "
            f"${burst.get('p_threshold', 500):.0f} within the window — "
            f"structuring shape."
        ),
        source="graph",
        ref="query:near_threshold_burst  |  prior: hand-set",
        entity_ids=[ctx.txn_id],
        channel="sequence",
        log_lr=hand_set_log_lr,
        direction="for",
    )
    return DetectorResult(pattern="undocumented", affected_txn_ids=[ctx.txn_id],
                          evidence=[ev], tags={"undocumented_coordinated"})


# ---------- false-alarm archetypes --------------------------------------------


def detect_legit_trip(ctx: DetectorContext) -> DetectorResult:
    """"Cleared: trip" — customer travelled to the flagged region.

    Fires when the alert region has been seen on this card historically
    (``prior_cleared_travel_on_card`` OR the region already exists in
    ``home/regions_seen``). Weak signal on its own.
    """
    row = ctx.txn_row
    if row.get("prior_cleared_travel_on_card") is not True:
        return DetectorResult(pattern=None)
    lr = _signal_log_lr("prior_cleared_travel_on_card", ctx.lr_table)  # returns log_lr
    # Interpret: prior cleared-travel on same card is ≈0.66 LR.
    ev = Evidence(
        claim="This card has a prior cleared 'confirmed travel' case — travel-region false-alarm archetype fits.",
        source="graph",
        ref="signal:prior_cleared_travel_on_card",
        entity_ids=[ctx.txn_id],
        channel="memory",
        log_lr=lr,
        direction="against" if lr < 0 else "for",
    )
    return DetectorResult(pattern="none", affected_txn_ids=[], evidence=[ev],
                          tags={"legit_trip_hint"})


def detect_legit_new_phone(ctx: DetectorContext) -> DetectorResult:
    """"Cleared: new phone" — customer confirmed purchase from a new device."""
    row = ctx.txn_row
    if row.get("prior_cleared_new_phone_on_card") is not True:
        return DetectorResult(pattern=None)
    lr = _signal_log_lr("prior_cleared_new_phone_on_card", ctx.lr_table)
    ev = Evidence(
        claim="This card has a prior cleared 'new phone' case — new-device false-alarm archetype fits.",
        source="graph",
        ref="signal:prior_cleared_new_phone_on_card",
        entity_ids=[ctx.txn_id],
        channel="memory",
        log_lr=lr,
        direction="against" if lr < 0 else "for",
    )
    return DetectorResult(pattern="none", affected_txn_ids=[], evidence=[ev],
                          tags={"legit_new_phone_hint"})


def detect_legit_big_purchase(ctx: DetectorContext) -> DetectorResult:
    """"Cleared: big legitimate purchase" — high amount that matches history.

    Fires only when:
      1. Amount > $1,000.
      2. The card has ≥1 prior txn with amount ≥ 50% of the flagged amount.
      3. Device was previously seen on this card OR in-person channel.
      4. id_15 != "New" AND is_new_device_for_card is not True (never fires
         alongside new-device signals).
    """
    row = ctx.txn_row
    amt = float(row.get("TransactionAmt", 0) or 0)
    if amt <= 1000:
        return DetectorResult(pattern=None)

    # Never fire alongside new-device signals.
    if row.get("id_15") == "New" or row.get("is_new_device_for_card") is True:
        return DetectorResult(pattern=None)

    channel = row.get("channel")
    device_seen_before = (channel == "in_person"
                          or row.get("is_new_device_for_card") is False)
    if not device_seen_before:
        return DetectorResult(pattern=None)

    # ≥1 prior txn on the card ≥ 50% of this amount.
    con = ctx.con
    n_prior_big = 0
    try:
        r = con.execute(
            """
            SELECT COUNT(*)
            FROM txn_features t
            WHERE t.customer_id = ?
              AND t.ts < ?
              AND t.TransactionAmt >= ?
            """,
            [ctx.customer_id, row.get("ts"), amt * 0.5],
        ).fetchone()
        n_prior_big = int(r[0] or 0)
    except Exception:  # noqa: BLE001
        n_prior_big = 0
    if n_prior_big < 1:
        return DetectorResult(pattern=None)

    lr = _signal_log_lr("amt_gt_1000", ctx.lr_table)
    ev = Evidence(
        claim=(
            f"Amount ${amt:.2f} > $1,000 on {channel} channel. Card has "
            f"{n_prior_big} prior txn(s) ≥ ${amt*0.5:.0f} and the device is "
            f"not new — big-purchase-cleared archetype fits."
        ),
        source="graph",
        ref="signal:amt_gt_1000 + prior-purchase-history",
        entity_ids=[ctx.txn_id],
        channel="amount",
        log_lr=lr,
        direction="against" if lr < 0 else "for",
    )
    return DetectorResult(pattern="none", affected_txn_ids=[], evidence=[ev],
                          tags={"legit_big_purchase_hint"})


def detect_legit_recurring_r7(ctx: DetectorContext) -> DetectorResult:
    """R7-shaped signal: this charge matches a recurring pattern on this card.

    P5v4-5: R7 tags / actions ONLY when this alert is a customer dispute
    (``trigger_type == 'customer_report'``). On non-dispute triggers the
    same recurring match is emitted as plain "against" evidence — no
    ``is_recurring_match`` tag, no R7 branch in the policy engine.
    """
    rm = ctx.graph_signals.get("recurring_match")
    if not rm or not rm.get("fires"):
        return DetectorResult(pattern=None)
    n_hits = rm.get("n", 0)
    cv = rm.get("cadence_cv", 0)
    n_reg = rm.get("n_distinct_addr1", 0)
    # C6v2-LR: empirical LR (fraud vs neg) for ≥3 prior at same (ProductCD, amount)
    rec_emp = ctx.lr_table.get("recurring_match_empirical") or {}
    empirical_log_lr = float(rec_emp.get("log_lr", -0.476))

    is_dispute = (ctx.trigger_type == "customer_report")
    if is_dispute:
        ev = Evidence(
            claim=(
                f"Card has {n_hits} prior hits at this ProductCD/amount "
                f"(cadence CV={cv:.2f}, {n_reg} distinct addr1). Matches R7's "
                f"'disputed but legitimate' shape."
            ),
            source="graph",
            ref="query:recurring_match  |  R7 (dispute-triggered)",
            entity_ids=[ctx.txn_id],
            channel="amount",
            log_lr=empirical_log_lr,
            direction="against",
        )
        return DetectorResult(pattern="none", affected_txn_ids=[], evidence=[ev],
                              tags={"legit_recurring_r7_hint", "is_recurring_match"})

    # Non-dispute trigger: recurring match is plain "against" evidence.
    ev = Evidence(
        claim=(
            f"Card has {n_hits} prior hits at this ProductCD/amount "
            f"(cadence CV={cv:.2f}). Recurring history is against fraud "
            f"(empirical log_lr={empirical_log_lr:+.2f})."
        ),
        source="graph",
        ref="lr_table:recurring_match_empirical (non-dispute)",
        entity_ids=[ctx.txn_id],
        channel="amount",
        log_lr=empirical_log_lr,
        direction="against",
    )
    return DetectorResult(pattern=None, affected_txn_ids=[], evidence=[ev])


# ---------- risk_score decile evidence ---------------------------------------


def detect_risk_score_decile(ctx: DetectorContext) -> DetectorResult:
    """Risk-score decile evidence — alert-conditional or neutral.

    Per §"Risk score" of the decisions doc: when the alert TRIGGER is
    the risk score itself (``trigger_type == 'risk_score'``), the decile
    contributes 0 log-LR — the 0.5 prior already conditions on being
    alerted, and the README explicitly warns "above 0.7 most flagged are
    legitimate". For customer_report / analyst_request triggers the score
    is genuinely independent evidence, but we use the alert-conditional
    LR (fraud vs cleared, not fraud vs all negatives) and clip to
    |log_lr| ≤ 0.7 to keep it modest.
    """
    row = ctx.txn_row
    d = row.get("risk_score_decile")
    if d is None:
        return DetectorResult(pattern=None)
    if ctx.trigger_type == "risk_score":
        # Score is the trigger — no additional evidence.
        return DetectorResult(pattern=None)

    lr_map = ctx.lr_table.get("risk_decile_alert_conditional", {})
    entry = lr_map.get(str(d)) or lr_map.get(int(d)) if lr_map else None
    if entry is None:
        return DetectorResult(pattern=None)
    log_lr = float(entry["log_lr"])
    # Clip to |log_lr| ≤ 0.7 (LR ∈ [0.5, 2.0]).
    if log_lr > 0.7:
        log_lr = 0.7
    elif log_lr < -0.7:
        log_lr = -0.7
    ev = Evidence(
        claim=(
            f"Model risk_score={row.get('risk_score', 0):.2f} (decile {d}/10). "
            f"Alert-conditional LR (fraud vs cleared) clipped to "
            f"±0.7 log-LR = {log_lr:+.2f}."
        ),
        source="graph",
        ref=f"lr_table:risk_decile_alert_conditional[{d}]",
        entity_ids=[ctx.txn_id],
        channel="history",
        log_lr=log_lr,
        direction="for" if log_lr > 0 else "against",
    )
    return DetectorResult(pattern=None, evidence=[ev])


def detect_prior_fraud_history(ctx: DetectorContext) -> DetectorResult:
    """Prior confirmed_fraud on card tuple / customer."""
    row = ctx.txn_row
    ev: list[Evidence] = []
    if row.get("prior_fraud_on_card_tuple") is True:
        lr = _signal_log_lr("prior_fraud_on_card_tuple", ctx.lr_table)
        ev.append(Evidence(
            claim="This card tuple has ≥1 prior confirmed_fraud case closed before this txn.",
            source="graph",
            ref="signal:prior_fraud_on_card_tuple",
            entity_ids=[ctx.card_tuple_id],
            channel="history",
            log_lr=lr,
            direction="for" if lr > 0 else "against",
        ))
    elif row.get("prior_fraud_on_customer") is True:
        lr = _signal_log_lr("prior_fraud_on_customer", ctx.lr_table)
        ev.append(Evidence(
            claim="This customer has ≥1 prior confirmed_fraud case (on a different card).",
            source="graph",
            ref="signal:prior_fraud_on_customer",
            entity_ids=[ctx.customer_id],
            channel="history",
            log_lr=lr,
            direction="for" if lr > 0 else "against",
        ))
    return DetectorResult(pattern=None, evidence=ev)


# ---------- runner ------------------------------------------------------------


DETECTORS: list = [
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
    detect_risk_score_decile,
    detect_prior_fraud_history,
]


def run_all_detectors(ctx: DetectorContext) -> list[DetectorResult]:
    """Return every DetectorResult; caller decides which pattern to adopt."""
    return [d(ctx) for d in DETECTORS]
