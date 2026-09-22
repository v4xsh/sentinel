"""Run HHG-014 through the MCP path and capture a full transcript.

Every ``tigergraph__*`` tool call is logged with its params + a short
excerpt of the response. Output: docs/MCP_TRANSCRIPT_HHG-014.md
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ["SENTINEL_GRAPH_VIA_MCP"] = "1"
os.environ["SENTINEL_LLM_DISABLED"] = "1"        # transcript is about MCP, not prose
os.environ.pop("SENTINEL_WRITE_MEMORY_DISABLED", None)

TRANSCRIPT: list[dict] = []


async def _instrumented():
    """Open one MCP session and route every graph tool call through it,
    recording each call+response into TRANSCRIPT."""
    from sentinel.graph.mcp_client import open_session
    async with open_session() as sess:
        real = sess.call_tool
        async def wrapped(name: str, args: dict) -> object:
            t0 = time.time()
            try:
                resp = await real(name, args)
                content = "\n".join(getattr(c, "text", str(c)) for c in getattr(resp, "content", []))
                TRANSCRIPT.append({
                    "tool":     name,
                    "params":   {k: (str(v)[:120] if not isinstance(v, list) else f"<list len={len(v)}>") for k, v in args.items()},
                    "latency_s": round(time.time() - t0, 3),
                    "resp_head": content[:220],
                    "resp_len":  len(content),
                })
                return resp
            except Exception as e:  # noqa: BLE001
                TRANSCRIPT.append({
                    "tool": name, "params": args,
                    "error": str(e)[:200],
                    "latency_s": round(time.time() - t0, 3),
                })
                raise
        sess.call_tool = wrapped

        # Now run the agent — every MCP call in the pipeline will use this
        # session because open_session() is cached per-run.
        # Actually open_session() creates a new session each time. So we
        # can't easily inject. Fallback: monkey-patch ``open_session`` to
        # return this instance directly.
        return sess


def _patch_open_session_to_use_recorder():
    """Monkey-patch open_session so the recording call_tool wrapper is used
    for every graph call in this process."""
    from contextlib import asynccontextmanager
    from sentinel.graph import mcp_client as m

    real_open = m.open_session
    call_records: list[dict] = TRANSCRIPT

    @asynccontextmanager
    async def wrapped_open():
        async with real_open() as sess:
            real_call = sess.call_tool
            async def wrapped_call(name, args=None):
                args = args or {}
                t0 = time.time()
                try:
                    resp = await real_call(name, args)
                    content = "\n".join(getattr(c, "text", str(c)) for c in getattr(resp, "content", []))
                    call_records.append({
                        "tool":     name,
                        "params":   {k: (f"<vector len={len(v)}>" if isinstance(v, list) and len(v) > 8 else v) for k, v in args.items()},
                        "latency_s": round(time.time() - t0, 3),
                        "resp_head": content[:200],
                        "resp_len":  len(content),
                    })
                    return resp
                except Exception as e:  # noqa: BLE001
                    call_records.append({
                        "tool": name, "params": args,
                        "error": str(e)[:200], "latency_s": round(time.time() - t0, 3),
                    })
                    raise
            sess.call_tool = wrapped_call
            yield sess

    m.open_session = wrapped_open


def main() -> int:
    _patch_open_session_to_use_recorder()

    from sentinel.agent.graph import run_plain
    from sentinel.agent.state import Telemetry
    state = {
        "case_id":     "HHG-014", "txn_id": "3478561", "card_id": "C13487-K1",
        "customer_id": "C13487", "opened_at": "2016-11-22 20:11:00",
        "trigger_type": "analyst_request",
        "trigger_text": "ring MCP transcript",
        "prior_log_odds": 0.0, "telemetry": Telemetry(),
    }
    t0 = time.time()
    state = run_plain(state)
    elapsed = time.time() - t0

    # Write the markdown transcript.
    md_lines: list[str] = []
    md_lines.append("# TigerGraph MCP transcript — HHG-014\n")
    md_lines.append(f"Total wall-clock: **{elapsed:.2f}s** · agent tool_calls counter: "
                    f"**{state['telemetry'].tool_calls}** · MCP call records captured: "
                    f"**{len(TRANSCRIPT)}**.\n")
    md_lines.append(f"Final state: verdict = **{state['verdict']}**, "
                    f"p_initial = {state.get('p_initial', 0):.3f}, "
                    f"p_final = {state['fraud_probability']:.3f}, "
                    f"pattern = **{state['pattern']}**, "
                    f"case vertex = `{state.get('case_vertex_id')}`.\n")
    md_lines.append(f"Ledger channels: `{sorted(state['ledger'].channels())}`.\n")
    md_lines.append("\n## Per-tool call log\n")
    md_lines.append("| # | tool | params (summarised) | latency | response head |")
    md_lines.append("|---|---|---|---|---|")
    for i, c in enumerate(TRANSCRIPT, 1):
        params = str(c.get("params"))[:120].replace("|", "\\|")
        head   = (c.get("resp_head") or c.get("error") or "")[:100].replace("|", "\\|").replace("\n", " ")
        md_lines.append(f"| {i} | `{c['tool']}` | `{params}` | {c.get('latency_s'):.2f}s | `{head}` |")
    md_lines.append("\n## Actions from the answer\n")
    if state.get("policy_decision"):
        for a in state["policy_decision"].actions:
            md_lines.append(f"- **{a.action}** (`{a.route}`) — {a.reason}")

    out = REPO / "docs" / "MCP_TRANSCRIPT_HHG-014.md"
    out.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Wrote {out}  ({len(TRANSCRIPT)} MCP calls, verdict={state['verdict']}, "
          f"p_final={state['fraud_probability']:.3f}, tool_calls={state['telemetry'].tool_calls})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
