"""Tests for the isotonic calibrator."""

from __future__ import annotations

import json

import numpy as np
import pytest

from sentinel.config import REPO_ROOT

CALIBRATOR_JSON = REPO_ROOT / "sentinel" / "evidence" / "calibrator.json"
LR_TABLE_JSON   = REPO_ROOT / "sentinel" / "evidence" / "lr_table.json"


def _apply_isotonic(xs: np.ndarray, x_th: np.ndarray, y_th: np.ndarray,
                    y_min: float, y_max: float) -> np.ndarray:
    """Piecewise-linear apply of an isotonic mapping stored as (x_th, y_th)."""
    xs = np.clip(xs, x_th[0], x_th[-1])
    ys = np.interp(xs, x_th, y_th)
    return np.clip(ys, y_min, y_max)


@pytest.fixture(scope="module")
def calibrator() -> dict:
    data = json.loads(CALIBRATOR_JSON.read_text())
    return {
        "x_th": np.array(data["x_thresholds"]),
        "y_th": np.array(data["y_thresholds"]),
        "y_min": data["y_min"], "y_max": data["y_max"],
    }


def _tr(c: dict, xs) -> np.ndarray:
    return _apply_isotonic(np.array(xs), c["x_th"], c["y_th"], c["y_min"], c["y_max"])


def test_calibrator_maps_neutral_to_neutral(calibrator: dict) -> None:
    """A raw score of ~0.5 should map to a calibrated score in [0.35, 0.65]."""
    p_cal = float(_tr(calibrator, [0.5])[0])
    assert 0.35 <= p_cal <= 0.65, (
        f"raw 0.5 → cal {p_cal:.3f} — expected [0.35, 0.65]. "
        "The isotonic fit is drifting the neutral prior."
    )


def test_calibrator_is_monotone(calibrator: dict) -> None:
    xs = np.linspace(0.0, 1.0, 21)
    ys = _tr(calibrator, xs)
    diffs = np.diff(ys)
    assert (diffs >= -1e-9).all(), f"non-monotone segment(s): {ys.tolist()}"


def test_calibrator_at_extremes(calibrator: dict) -> None:
    p0 = float(_tr(calibrator, [0.0])[0])
    p1 = float(_tr(calibrator, [1.0])[0])
    assert 0.0 <= p0 <= 0.5
    assert 0.5 <= p1 <= 1.0


def test_lr_table_sanity_checks(calibrator: IsotonicRegression) -> None:
    """Signals matching the CHECKPOINT-4-revised sanity brief."""
    lr = json.loads(LR_TABLE_JSON.read_text())
    s = lr["signals"]

    def approx(name: str, lo: float, hi: float) -> None:
        assert lo <= s[name]["lr"] <= hi, (
            f"{name}: expected LR ∈ [{lo}, {hi}], got {s[name]['lr']}"
        )

    approx("online_R_near_100",         0.30, 0.55)
    approx("P_or_R_anonymous_com_on_R", 0.15, 0.40)
    assert s["proxy_flag"]["lr"] > 1.0, s["proxy_flag"]["lr"]
    approx("amt_gt_1000",               0.40, 1.60)


def test_scoring_prior_is_neutral() -> None:
    lr = json.loads(LR_TABLE_JSON.read_text())
    assert lr["scoring_prior"]["p_fraud"] == 0.5
    assert lr["scoring_prior"]["logit"] == 0.0
