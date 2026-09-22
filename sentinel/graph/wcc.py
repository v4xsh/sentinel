"""Python wrapper for the ``ring_wcc`` installed GSQL query.

Computes the weakly-connected component of the card–device projection
that contains ``seed_card``. Devices with card-degree above
``degree_cap`` (hub devices — public wifi, disposable browsers) are
skipped so a single hub cannot collapse the whole population into one
component.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TypedDict

from sentinel.agent.id_resolver import card_tuple_id
from sentinel.config import TG_GRAPHNAME
from sentinel.graph.client import TGClient


class RingWCC(TypedDict):
    component_id:            str
    size:                    int
    n_devices:               int
    iterations:              int
    component_exposure_usd:  float
    component_n_txns:        int
    cards:                   list[str]
    devices:                 list[str]


def ring_wcc(seed_card: str,
             window_days: int = 60,
             window_end: datetime | None = None,
             degree_cap: int = 100,
             max_iter: int = 8,
             cli: TGClient | None = None) -> RingWCC:
    """Return the ring_wcc for ``seed_card`` (case-pack card_id, e.g. 'C00259-K1')."""
    end = window_end or datetime(2016, 11, 1)
    start = end - timedelta(days=window_days)
    owns = cli is None
    if owns:
        cli = TGClient()
    try:
        body = {
            "p_card":         card_tuple_id(seed_card),
            "p_window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "p_window_end":   end.strftime("%Y-%m-%d %H:%M:%S"),
            "p_degree_cap":   int(degree_cap),
            "p_max_iter":     int(max_iter),
        }
        r = cli.restpp_get(f"query/{TG_GRAPHNAME}/ring_wcc", params=body)
        merged: dict = {}
        for row in r.get("results", []):
            merged.update(row)
        return {
            "component_id":           merged.get("component_id", seed_card),
            "size":                   int(merged.get("size", 0)),
            "n_devices":              int(merged.get("n_devices", 0)),
            "iterations":             int(merged.get("@@iterations", 0)),
            "component_exposure_usd": float(merged.get("component_exposure_usd", 0.0)),
            "component_n_txns":       int(merged.get("component_n_txns", 0)),
            "cards":                  merged.get("cards", []) or [],
            "devices":                merged.get("devices", []) or [],
        }
    finally:
        if owns:
            cli.close()
