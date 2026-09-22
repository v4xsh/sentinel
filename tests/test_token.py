"""Token minting/refresh smoke test — needs network to Savanna."""

from __future__ import annotations

import os
import pytest

from sentinel.graph.token import (
    DEFAULT_LIFETIME_SECS,
    REFRESH_MARGIN_SECS,
    clear_cache,
    get_jwt,
    get_token,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TG_HOST") or not os.getenv("TG_SECRET"),
    reason="TG_HOST and TG_SECRET must be set to run token tests",
)


def test_get_jwt_returns_a_bearer():
    clear_cache()
    jwt = get_jwt()
    assert isinstance(jwt, str)
    # JWTs are three base64url segments separated by dots.
    assert jwt.count(".") == 2
    assert len(jwt) > 50


def test_scope_falls_back_to_workspace_when_graph_missing():
    """Our target graph 'FraudGraph' doesn't exist yet in Phase 0/1, so mint
    should fall back to workspace scope."""
    clear_cache()
    tok = get_token()
    # Until Phase 2 creates FraudGraph, this will be workspace.
    assert tok.scope in ("workspace", f"graph:{os.environ.get('TG_GRAPHNAME')}")


def test_cache_returns_same_token_until_refresh_margin():
    clear_cache()
    t1 = get_token()
    t2 = get_token()
    assert t1 is t2


def test_lifetime_sane():
    clear_cache()
    tok = get_token()
    # We asked for a week; it should be at least a few hours in the future.
    import time

    assert tok.expires_at - time.time() > 3600
    assert tok.expires_at - time.time() <= DEFAULT_LIFETIME_SECS + 60
