"""Thin JWT-bearer HTTP client for TigerGraph 4.x on Savanna.

Talks to two endpoints directly, bypassing pyTG's legacy auth path:

- ``POST /gsql/v1/statements``  — arbitrary GSQL statements
- ``POST /restpp/ddl/<graph>``  — chunked bulk-load via loading job
- ``POST /restpp/query/<graph>/<name>``  — installed queries
- ``GET  /restpp/graph/<graph>`` etc. — vertex/edge upserts + counts
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

import httpx

from sentinel.config import TG_HOST
from sentinel.graph.token import get_jwt

logger = logging.getLogger(__name__)


class TGClient:
    """Session-oriented client. Reuses a single httpx.Client + JWT."""

    def __init__(self, host: str = "", timeout: float = 300.0) -> None:
        self.host = (host or TG_HOST).rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    # ---- low-level -----------------------------------------------------

    def _headers(self, content_type: str = "application/json") -> dict:
        return {
            "Authorization": f"Bearer {get_jwt()}",
            "Content-Type": content_type,
        }

    def close(self) -> None:
        self._client.close()

    # ---- GSQL ----------------------------------------------------------

    def gsql(self, statements: str, graph: Optional[str] = None) -> str:
        """Run one or more GSQL statements. Returns the raw text response."""
        body = ""
        if graph:
            body += f"USE GRAPH {graph}\n"
        body += statements
        r = self._client.post(
            f"{self.host}/gsql/v1/statements",
            headers=self._headers(content_type="text/plain"),
            content=body,
        )
        r.raise_for_status()
        return r.text

    def gsql_ok(self, statements: str, graph: Optional[str] = None) -> tuple[bool, str]:
        """gsql() + a heuristic pass/fail signal on the response text."""
        out = self.gsql(statements, graph=graph)
        low = out.lower()
        ok = ("error" not in low or "0 error" in low or "no error" in low) \
             and "semantic check fails" not in low \
             and "syntax error" not in low
        return ok, out

    # ---- REST++ --------------------------------------------------------

    def restpp_get(self, path: str, params: Optional[dict] = None) -> Any:
        r = self._client.get(
            f"{self.host}/restpp/{path.lstrip('/')}",
            headers=self._headers(),
            params=params or {},
        )
        r.raise_for_status()
        return r.json()

    def restpp_post(self, path: str, body: Any) -> Any:
        r = self._client.post(
            f"{self.host}/restpp/{path.lstrip('/')}",
            headers=self._headers(),
            content=json.dumps(body) if not isinstance(body, (str, bytes)) else body,
        )
        r.raise_for_status()
        return r.json()

    def run_query(
        self,
        graph: str,
        name: str,
        params: Optional[dict] = None,
    ) -> Any:
        """Invoke an installed query. Returns parsed JSON body.

        ``VERTEX<T>``-typed parameters must be passed as ``{"id": <primary_id>}``
        dicts — TigerGraph's REST expects that shape rather than a bare string.
        Callers are responsible for wrapping; this client just POSTs the body.
        POST is used (vs GET) so special characters in IDs (pipes, spaces)
        don't have to be URL-encoded.
        """
        r = self._client.post(
            f"{self.host}/restpp/query/{graph}/{name}",
            headers=self._headers(),
            json=params or {},
        )
        r.raise_for_status()
        return r.json()

    # ---- bulk loading --------------------------------------------------

    def run_loading_job_chunk(
        self,
        graph: str,
        job: str,
        filename_var: str,
        csv_bytes: bytes,
        *,
        sep: str = ",",
        eol: str = "\\n",
    ) -> dict:
        """Send one CSV chunk to a loading job.

        Endpoint: ``POST /restpp/ddl/<graph>?tag=<job>&filename=<file>&sep=<sep>&eol=<eol>``.

        The body is raw CSV; the loading-job DDL wires the fields into vertices/edges.
        """
        params = {
            "tag": job,
            "filename": filename_var,
            "sep": sep,
            "eol": eol,
            "ack": "all",
        }
        r = self._client.post(
            f"{self.host}/restpp/ddl/{graph}",
            headers={
                "Authorization": f"Bearer {get_jwt()}",
                "Content-Type": "text/csv",
                "GSQL-TIMEOUT": "600000",
            },
            params=params,
            content=csv_bytes,
        )
        # 200 body is JSON with load stats.
        try:
            payload = r.json()
        except Exception:
            r.raise_for_status()
            payload = {"raw": r.text}
        if r.status_code >= 400:
            logger.error("load chunk failed: %s %s", r.status_code, payload)
            r.raise_for_status()
        return payload

    def run_loading_job_from_iter(
        self,
        graph: str,
        job: str,
        filename_var: str,
        lines: Iterable[bytes],
        *,
        chunk_rows: int = 20_000,
        header: bytes | None = None,  # accepted for API compat, IGNORED
        on_progress=None,
    ) -> list[dict]:
        """Stream CSV lines into a loading job, chunk_rows at a time.

        **Important**: TigerGraph 4.x's ``/restpp/ddl/<graph>`` endpoint does
        NOT honor ``USING HEADER="true"`` from the loading job definition — it
        parses every line as data. The caller must therefore send *data-only*
        chunks. The ``header`` parameter here is accepted for backward
        compatibility but intentionally ignored; callers should feed us lines
        with the header already stripped.

        ``on_progress(sent_rows, response)`` is called after each chunk.
        """
        results: list[dict] = []
        buf: list[bytes] = []
        n_in_chunk = 0
        sent = 0
        for line in lines:
            buf.append(line)
            n_in_chunk += 1
            if n_in_chunk >= chunk_rows:
                data = b"\n".join(buf) + b"\n"
                res = self.run_loading_job_chunk(graph, job, filename_var, data)
                results.append(res)
                sent += n_in_chunk
                if on_progress:
                    on_progress(sent, res)
                buf = []
                n_in_chunk = 0
        if n_in_chunk:
            data = b"\n".join(buf) + b"\n"
            res = self.run_loading_job_chunk(graph, job, filename_var, data)
            results.append(res)
            sent += n_in_chunk
            if on_progress:
                on_progress(sent, res)
        return results

    # ---- convenience ---------------------------------------------------

    def list_graphs(self) -> list[str]:
        """Return the graphs on this workspace."""
        # gsql "ls" then parse out graph names from the "Graphs:" section.
        text = self.gsql("ls")
        graphs = []
        in_graphs = False
        for line in text.splitlines():
            if line.strip().lower().startswith("graphs:"):
                in_graphs = True
                continue
            if in_graphs:
                s = line.strip()
                if not s or ":" in s:
                    if s.startswith("- "):
                        pass
                    else:
                        continue
                if s.startswith("- Graph "):
                    # "- Graph FraudGraph(Vertices=X, Edges=Y)"
                    n = s[len("- Graph "):].split("(")[0].strip()
                    graphs.append(n)
        return graphs

    def get_graph_vertex_count(self, graph: str, vtype: str) -> int:
        """Total vertex count via builtin ``stat_vertex_number``."""
        r = self.restpp_post(
            f"builtins/{graph}",
            {"function": "stat_vertex_number", "type": vtype},
        )
        if r.get("error"):
            raise RuntimeError(f"count failed: {r}")
        for row in r["results"]:
            if row.get("v_type") == vtype:
                return int(row["count"])
        raise RuntimeError(f"no count returned: {r}")

    def get_graph_edge_count(self, graph: str, etype: str) -> int:
        """Total edge count via builtin ``stat_edge_number``."""
        r = self.restpp_post(
            f"builtins/{graph}",
            {"function": "stat_edge_number", "type": etype},
        )
        if r.get("error"):
            raise RuntimeError(f"count failed: {r}")
        for row in r["results"]:
            if row.get("e_type") == etype:
                return int(row["count"])
        raise RuntimeError(f"no count returned: {r}")
