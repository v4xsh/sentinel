"""Real MCP client — launches the `tigergraph-mcp` stdio server as a subprocess,
authenticates via TG_JWT_TOKEN minted from ``sentinel.graph.token``, and
returns a ``MultiServerMCPClient`` you can pull LangChain tools out of.

Every graph read/write on the agent's investigation path goes through this
client, satisfying the judging criterion that the agent uses TigerGraph MCP.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection

from sentinel.config import (
    TG_GRAPHNAME,
    TG_GS_PORT,
    TG_HOST,
    TG_RESTPP_PORT,
    TG_SECRET,
    TG_TGCLOUD,
)
from sentinel.graph.token import get_jwt

logger = logging.getLogger(__name__)

# Server name in the MultiServerMCPClient's `connections` dict.
SERVER_NAME = "tigergraph"


def _server_env() -> dict[str, str]:
    """Build the env passed to the tigergraph-mcp subprocess."""
    return {
        # Auth: pre-minted JWT so the server never touches /requesttoken.
        "TG_JWT_TOKEN": get_jwt(),
        # Where to reach the workspace.
        "TG_HOST": TG_HOST,
        "TG_GRAPHNAME": TG_GRAPHNAME or "",
        "TG_TGCLOUD": "true" if TG_TGCLOUD else "false",
        "TG_RESTPP_PORT": str(TG_RESTPP_PORT),
        "TG_GS_PORT": str(TG_GS_PORT),
        # Secret is present in case the server tries to refresh internally.
        "TG_SECRET": TG_SECRET or "",
        # Keep tool-call logs disabled so stdio isn't polluted.
        "TG_LOG_TOOL_CALLS": "false",
        # Inherit PATH so `tigergraph-mcp` is found.
        "PATH": os.environ.get("PATH", ""),
        # Python site-packages location for the subprocess.
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }


def build_connections() -> dict[str, StdioConnection]:
    """Return the ``connections`` dict for MultiServerMCPClient."""
    return {
        SERVER_NAME: {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "tigergraph_mcp.main", "--transport", "stdio"],
            "env": _server_env(),
        }
    }


def new_client() -> MultiServerMCPClient:
    """Construct the MCP client. Call ``.get_tools()`` to enumerate tools.

    Note: MultiServerMCPClient is fine to instantiate; the subprocess is
    launched lazily when you first request tools or open a session.
    """
    return MultiServerMCPClient(build_connections())


@asynccontextmanager
async def open_session() -> AsyncIterator[Any]:
    """Convenience: yield an MCP session for direct tool calls.

    Example::

        async with open_session() as sess:
            resp = await sess.call_tool("list_graphs", {})
    """
    client = new_client()
    async with client.session(SERVER_NAME) as sess:
        yield sess


async def call_tool(name: str, args: dict[str, Any] | None = None) -> Any:
    """One-shot tool call for scripts and transcripts."""
    async with open_session() as sess:
        return await sess.call_tool(name, args or {})


# ---- shared-session runtime -------------------------------------------------
#
# ``get_shared_session()`` gives sync callers a way to reuse ONE MCP session
# across every graph call in the process. Under the hood a background thread
# runs an asyncio loop that keeps the session open until process exit.
#
# This exists because each ``asyncio.run(open_session(...))`` re-spawns the
# tigergraph-mcp subprocess and, when four such contexts overlap per case
# across a 20-case bulk run, the stdio pipes deadlock. One long-lived
# session avoids both problems.

import asyncio as _asyncio
import atexit as _atexit
import threading as _threading

_SHARED_LOOP: Any = None
_SHARED_SESSION: Any = None
_SHARED_THREAD: Any = None
_SHARED_LOCK = _threading.Lock()
_SHUTDOWN_EVENT: Any = None


def _shared_loop_ready() -> bool:
    return _SHARED_SESSION is not None


def _start_shared_session() -> None:
    """Boot a background asyncio loop that keeps one MCP session open."""
    global _SHARED_LOOP, _SHARED_SESSION, _SHARED_THREAD, _SHUTDOWN_EVENT

    ready = _threading.Event()

    def _thread_main() -> None:
        global _SHARED_LOOP, _SHARED_SESSION, _SHUTDOWN_EVENT
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        _SHARED_LOOP = loop
        _SHUTDOWN_EVENT = _asyncio.Event()

        async def _run() -> None:
            client = new_client()
            async with client.session(SERVER_NAME) as sess:
                global _SHARED_SESSION
                _SHARED_SESSION = sess
                ready.set()
                # Keep the session open until asked to shut down.
                await _SHUTDOWN_EVENT.wait()

        loop.run_until_complete(_run())
        loop.close()

    _SHARED_THREAD = _threading.Thread(target=_thread_main, daemon=True,
                                       name="mcp-shared-session")
    _SHARED_THREAD.start()
    if not ready.wait(timeout=30):
        raise RuntimeError("MCP shared session failed to start within 30s")


def _stop_shared_session() -> None:
    if _SHARED_LOOP is None or _SHUTDOWN_EVENT is None:
        return
    try:
        _SHARED_LOOP.call_soon_threadsafe(_SHUTDOWN_EVENT.set)
    except Exception:
        pass


_atexit.register(_stop_shared_session)


def sync_call_tool(name: str, args: dict[str, Any] | None = None,
                   timeout: float = 60.0) -> Any:
    """Call an MCP tool through the shared session, blocking the caller.

    Boots the shared session on first use. Subsequent calls reuse it.
    """
    with _SHARED_LOCK:
        if not _shared_loop_ready():
            _start_shared_session()
    fut = _asyncio.run_coroutine_threadsafe(
        _SHARED_SESSION.call_tool(name, args or {}), _SHARED_LOOP,
    )
    return fut.result(timeout=timeout)


def sync_run_installed_query(graph: str, name: str, params: dict) -> dict:
    """Sync wrapper for ``tigergraph__run_installed_query`` on the shared session."""
    resp = sync_call_tool(
        "tigergraph__run_installed_query",
        {"graph_name": graph, "query_name": name, "params": params},
    )
    payload = _parse_json_body(resp)
    if isinstance(payload, dict) and payload.get("success") is False:
        return {"error": True, "message": payload.get("summary", "MCP call failed")[:400],
                "results": []}
    if isinstance(payload, dict) and "data" in payload:
        d = payload["data"]
        if isinstance(d, dict) and "result" in d:
            return {"error": False, "results": d["result"]}
        return {"error": False, "results": d if isinstance(d, list) else [d]}
    return payload if isinstance(payload, dict) else {"error": True, "results": []}


# ---- higher-level primitives used by the investigation path ----------------
#
# The tigergraph-mcp stdio server exposes prefixed tools such as
# ``tigergraph__run_installed_query`` and ``tigergraph__search_top_k_similarity``.
# These helpers wrap them so callers don't have to know the prefix.


def _parse_json_body(resp: Any) -> Any:
    """MCP tool responses come back as a list of content items with a .text
    payload. Concatenate them and try to parse as JSON."""
    import json
    text = "\n".join(getattr(c, "text", str(c)) for c in getattr(resp, "content", []))
    # Some responses come as ```json ...``` blocks.
    if "```json" in text:
        text = text.split("```json", 1)[1].lstrip("` \n")
    text = text[text.find("{"):] if "{" in text else text
    try:
        payload, _ = json.JSONDecoder().raw_decode(text)
        return payload
    except Exception:
        return {"error": True, "message": text[:400]}


async def mcp_run_installed_query(sess, graph: str, name: str, params: dict) -> dict:
    """Run an installed query via ``tigergraph__run_installed_query``.

    Falls back to a REST-shaped ``{"error":..., "results":...}`` dict on
    parse failure so callers can treat MCP + REST paths identically.
    """
    resp = await sess.call_tool(
        "tigergraph__run_installed_query",
        {"graph_name": graph, "query_name": name, "params": params},
    )
    payload = _parse_json_body(resp)
    if isinstance(payload, dict) and payload.get("success") is False:
        return {"error": True, "message": payload.get("summary", "MCP call failed")[:400],
                "results": []}
    # MCP wraps successful bodies under {success: true, data: {...}}.
    if isinstance(payload, dict) and "data" in payload:
        d = payload["data"]
        if isinstance(d, dict) and "result" in d:
            return {"error": False, "results": d["result"]}
        return {"error": False, "results": d if isinstance(d, list) else [d]}
    # Direct REST-style body.
    return payload if isinstance(payload, dict) else {"error": True, "results": []}


async def mcp_vector_search(
    sess, vertex_type: str, vector_attribute: str,
    query_vector: list[float], top_k: int = 10,
    graph_name: str = "FraudGraph",
) -> list[dict]:
    """Wrapper for ``tigergraph__search_top_k_similarity``."""
    resp = await sess.call_tool(
        "tigergraph__search_top_k_similarity",
        {
            "vertex_type": vertex_type, "vector_attribute": vector_attribute,
            "query_vector": query_vector, "top_k": top_k,
            "graph_name": graph_name, "return_vectors": False,
        },
    )
    payload = _parse_json_body(resp)
    if not isinstance(payload, dict) or not payload.get("success"):
        return []
    result = payload.get("data", {}).get("result", [])
    out: list[dict] = []
    for r in result:
        if isinstance(r, dict) and "v" in r:
            out.extend(r["v"])
        elif isinstance(r, dict):
            out.append(r)
    return out


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    async def _demo() -> None:
        async with open_session() as sess:
            tools = await sess.list_tools()
            print(f"MCP session opened. {len(tools.tools)} tools discovered.")
            resp = await sess.call_tool("list_graphs", {})
            for it in resp.content:
                print(getattr(it, "text", it)[:400])

    asyncio.run(_demo())
