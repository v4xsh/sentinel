"""JWT minting for TigerGraph 4.x on Savanna.

Central place that owns the token lifecycle. Every other module (the JWT-bearer
REST client, the MCP server startup, integration tests) reads from this module
so we only speak to ``/gsql/v1/tokens`` once per lifetime.

Env vars consumed (see also ``sentinel.config``):

    TG_HOST         - Savanna URL, https://...
    TG_SECRET       - graph secret from Savanna (workspace-wide if unbound)
    TG_GRAPHNAME    - optional; if provided AND the graph exists, mint a
                      graph-scoped token, otherwise fall back to workspace-wide.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from sentinel.config import TG_GRAPHNAME, TG_HOST, TG_SECRET

logger = logging.getLogger(__name__)

# Default: mint for a week. Safe for a hackathon run.
DEFAULT_LIFETIME_SECS = 7 * 24 * 3600
# Refresh 10 min before expiry so callers never see a stale JWT.
REFRESH_MARGIN_SECS = 600


@dataclass(frozen=True)
class MintedToken:
    jwt: str
    scope: str          # "graph:<name>" or "workspace"
    expires_at: float   # unix seconds


class _TokenCache:
    """Thread-safe, lazily-refreshed cache for a single (host, graph) tuple."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[MintedToken] = None

    def get(self) -> MintedToken:
        with self._lock:
            now = time.time()
            if self._current and self._current.expires_at - REFRESH_MARGIN_SECS > now:
                return self._current
            # Always workspace-wide by default: graph-scoped JWTs can't run
            # global GSQL like ``ls``. Callers who genuinely need a
            # graph-scoped token should mint one explicitly.
            fresh = _mint(TG_HOST, TG_SECRET, None, DEFAULT_LIFETIME_SECS)
            self._current = fresh
            logger.info(
                "Minted TG JWT: scope=%s expires_in=%ds",
                fresh.scope,
                int(fresh.expires_at - now),
            )
            return fresh

    def clear(self) -> None:
        with self._lock:
            self._current = None


_cache = _TokenCache()


def _mint(host: str, secret: str, graph: Optional[str], lifetime: int) -> MintedToken:
    """One-shot JWT mint against ``POST /gsql/v1/tokens``.

    Falls back to workspace-scope on 404 "graph not found" — the pre-Phase-2
    state where our target graph doesn't exist yet is normal.
    """
    if not host or not secret:
        raise RuntimeError("TG_HOST and TG_SECRET must be set to mint a JWT")

    url = f"{host.rstrip('/')}/gsql/v1/tokens"

    def _post(body: dict) -> httpx.Response:
        return httpx.post(url, json=body, timeout=30)

    now = time.time()
    if graph:
        r = _post({"secret": secret, "lifetime": lifetime, "graph": graph})
        if r.status_code == 200 and not r.json().get("error"):
            return MintedToken(r.json()["token"], f"graph:{graph}", now + lifetime)
        logger.warning("graph-scoped mint failed (%s): %s — falling back to workspace",
                       r.status_code, r.text[:200])

    r = _post({"secret": secret, "lifetime": lifetime})
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(f"JWT mint failed: {body}")
    return MintedToken(body["token"], "workspace", now + lifetime)


# ---- Public API -----------------------------------------------------------


def get_jwt() -> str:
    """Return a valid JWT string, refreshing if near expiry."""
    return _cache.get().jwt


def get_token() -> MintedToken:
    """Return the full :class:`MintedToken` (JWT + scope + expiry)."""
    return _cache.get()


def clear_cache() -> None:
    """Force the next :func:`get_jwt` call to mint fresh."""
    _cache.clear()


def export_to_env(*, env_var: str = "TG_JWT_TOKEN") -> str:
    """Set ``os.environ[env_var]`` to a fresh JWT and return it.

    Convenience for launching ``tigergraph-mcp`` — the server reads
    ``TG_JWT_TOKEN`` from the environment at startup.
    """
    tok = get_jwt()
    os.environ[env_var] = tok
    return tok


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    t = get_token()
    print(f"JWT scope={t.scope}  expires_at={t.expires_at}  len={len(t.jwt)}  head={t.jwt[:40]}…")
    export_to_env()
    print("TG_JWT_TOKEN exported to environment.")
