"""The explain node — produces the natural-language investigation summary.

The LLM here is Gemini, and its output is written to `case.summary` in the
answer JSON. It NEVER produces actions, LR values, or policy citations —
those come from the policy engine and evidence ledger. The prompt shows the
LLM the ledger + verdict + actions and asks for a plain-English recap.

GraphRAG: prompts also carry the top-3 similar prior ClosedCases (from the
memory-retrieve node) and top-3 policy chunks retrieved by vector search
on the evidence text.

**Citation guard.** Every prompt is given an explicit ``allowed_case_ids``
whitelist (the case's own ID plus its ``similar_prior_cases``). The
instruction: *cite ONLY these IDs; if the list is empty, cite none.*
After generation we regex all ``CC-\\d{4}`` and ``CASE-[A-Z]+-\\d+`` tokens
in the text; any token outside the whitelist causes the *sentence* to be
stripped and the prompt re-run once with a stronger no-invention instruction.
If the re-run still misbehaves, the offending sentence is dropped from the
final text. Invariant I12 (see ``sentinel/policy/invariants.py``) enforces
this constraint over the persisted answer file.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sentinel.evidence.ledger import EvidenceLedger
from sentinel.llm import generate

logger = logging.getLogger(__name__)


# ---- Case-ID regex used by the citation guard --------------------------
CASE_ID_RE = re.compile(r"\b(?:CC-\d{4,}|CASE-[A-Z0-9]+-\d+)\b")


SUMMARY_PROMPT = """\
You are writing a one-paragraph analyst summary for case {case_id} in
your own words.

Verdict: {verdict}    calibrated p(fraud) = {p_fraud:.2f}
Pattern: {pattern}
Actions decided by the policy engine: {actions}

Evidence ledger (already scored — do not invent LR numbers):
{ledger}

Similar prior cases retrieved from the graph:
{memory_hits}

Relevant policy language (from the doc chunks):
{policy}

**Allowed case IDs you may cite in the prose (whitelist):** {allowed_ids}

Write 4-6 sentences. Rules:
  - Do NOT invent evidence not in the ledger.
  - Do NOT invent rule citations, LR numbers, or actions.
  - Reference the concrete channels (device / region / sequence / amount /
    history / customer / identity_flags) that drove the verdict.
  - Cite ONLY the case IDs in the whitelist above. If the whitelist is
    empty, do not cite any case ID at all — do not invent CC-#### or
    CASE-* IDs; do not make up numbers.
  - If similar prior cases are listed AND the whitelist is non-empty, name
    at least one whitelisted ID and briefly describe how it relates.
  - If the customer denied / confirmed, mention it.
  - End with one sentence on what happens next per the actions list.
"""


PATTERN_DESC_PROMPT = """\
Write 2-3 sentences describing the fraud pattern observed on case
{case_id}, in your own words, based only on the ledger below. Do not
name any pattern the policy engine did not assign. Do not invent LR
numbers or rule citations.

Pattern the policy engine assigned: {pattern}
Ledger:
{ledger}

Similar prior cases:
{memory_hits}

**Allowed case IDs you may cite (whitelist):** {allowed_ids}
Cite ONLY those IDs; if the list is empty, cite none.
"""


SAR_PROMPT = """\
You are drafting a suspicious-activity narrative for FinCEN Form 111.

Case ID: {case_id}
Exposure: ${exposure:.2f}
Pattern: {pattern}
Verdict: {verdict}

Evidence:
{ledger}

Similar prior cases retrieved from the graph:
{memory_hits}

Relevant policy language (from the doc chunks):
{policy}

**Allowed case IDs you may cite (whitelist):** {allowed_ids}

Write a 6-10 sentence narrative that:
  - Names the suspect activity in plain English (no LR numbers).
  - Cites the concrete indicators (device / region / burst / etc.).
  - States the exposure and the actions taken.
  - Cites ONLY the case IDs in the whitelist above. If the list is empty,
    do not cite any case ID at all — do not invent CC-#### or CASE-* IDs.
  - Does NOT allege identity or motive beyond what the ledger supports.
"""


def _ledger_readable(ledger: EvidenceLedger) -> str:
    lines = []
    for e in ledger.items:
        s = f"- [{e.channel}] {e.claim}"
        if e.device_link:
            dl = e.device_link
            s += (f"  (device: id_15={dl.id_15}, id_23={dl.id_23}, "
                  f"first_seen={dl.first_seen_on_card}, "
                  f"window_overlap={dl.activity_window_overlap})")
        lines.append(s)
    return "\n".join(lines) if lines else "(no evidence)"


def _memory_hits_readable(hits: list) -> str:
    if not hits:
        return "(no similar prior cases retrieved)"
    lines = []
    for h in hits[:3]:
        attrs = getattr(h, "attrs", {}) or {}
        outcome = attrs.get("outcome", "?")
        pattern = attrs.get("pattern", "?")
        notes = (attrs.get("analyst_notes") or "")[:220]
        lines.append(f"- {getattr(h, 'id', '?')}  (outcome={outcome}, pattern={pattern}) — {notes}")
    return "\n".join(lines)


def _policy_hits_readable(hits: list) -> str:
    if not hits:
        return "(none retrieved)"
    return "\n".join(f"- {h.attrs.get('text', h.id)[:200]}" for h in hits[:3])


def _split_sentences(text: str) -> list[str]:
    """Very small sentence splitter — good enough to drop bad sentences."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def _invented_ids(text: str, allowed: set[str]) -> set[str]:
    return {tok for tok in CASE_ID_RE.findall(text) if tok not in allowed}


def _scrub_invented(text: str, allowed: set[str]) -> tuple[str, set[str]]:
    """Return (cleaned_text, invented_ids_removed).

    Any sentence carrying an ID outside ``allowed`` is dropped.
    """
    invented = _invented_ids(text, allowed)
    if not invented:
        return text, set()
    kept = []
    for s in _split_sentences(text):
        if _invented_ids(s, allowed):
            continue
        kept.append(s)
    return " ".join(kept).strip(), invented


class LLMGenerationError(RuntimeError):
    """Raised when the LLM cannot produce a summary — no template fallback."""


class LLMSkipped(RuntimeError):
    """Raised when SENTINEL_LLM_DISABLED=1 blocks the call — used by backtests."""


def _generate_and_guard(prompt: str, *, allowed_ids: set[str],
                         temperature: float, max_output_tokens: int,
                         retry_note: str = "") -> str:
    """Generate, scrub invented IDs, retry once with a stronger warning."""
    text = generate(prompt, temperature=temperature,
                    max_output_tokens=max_output_tokens).strip()
    cleaned, invented = _scrub_invented(text, allowed_ids)
    if not invented:
        return cleaned
    logger.warning("citation guard: dropping sentences citing invented IDs %s",
                   sorted(invented))
    # Retry once with a strengthened instruction.
    strengthened = (
        prompt
        + "\n\n**REMINDER — HARD CONSTRAINT:** Your previous draft mentioned "
        + f"case IDs {sorted(invented)!r} which are NOT in the whitelist "
        + "above. NEVER cite any CC-#### or CASE-* ID unless it is in the "
        + "whitelist. If the whitelist is empty, cite no case IDs. "
        + f"{retry_note}"
    )
    text2 = generate(strengthened, temperature=max(0.0, temperature - 0.1),
                     max_output_tokens=max_output_tokens).strip()
    cleaned2, invented2 = _scrub_invented(text2, allowed_ids)
    if invented2:
        logger.warning("citation guard: retry still cited invented IDs %s — "
                       "dropping those sentences from final text",
                       sorted(invented2))
    return cleaned2


def _allowed_from(case_id: str, similar_prior_cases: list | None) -> set[str]:
    """Whitelist = the case's own ID + every ID in similar_prior_cases."""
    out = {case_id}
    for x in (similar_prior_cases or []):
        s = str(x).strip()
        if s:
            out.add(s)
    return out


def write_summary(case_id: str, verdict: str, p_fraud: float, pattern: str,
                  actions: list[dict], ledger: EvidenceLedger,
                  memory_hits: list | None = None,
                  policy_hits: list | None = None,
                  similar_prior_cases: list | None = None) -> str:
    """Return the case summary paragraph."""
    allowed = _allowed_from(case_id, similar_prior_cases)
    prompt = SUMMARY_PROMPT.format(
        case_id=case_id, verdict=verdict, p_fraud=p_fraud, pattern=pattern,
        actions=", ".join(a["action"] for a in actions),
        ledger=_ledger_readable(ledger),
        memory_hits=_memory_hits_readable(memory_hits or []),
        policy=_policy_hits_readable(policy_hits or []),
        allowed_ids=sorted(allowed),
    )
    try:
        return _generate_and_guard(prompt, allowed_ids=allowed,
                                    temperature=0.2, max_output_tokens=350)
    except Exception as e:  # noqa: BLE001
        from sentinel.llm import LLMDisabled
        if isinstance(e, LLMDisabled):
            raise LLMSkipped("LLM disabled by SENTINEL_LLM_DISABLED") from e
        raise LLMGenerationError(f"summary generation failed: {e}") from e


def write_pattern_description(case_id: str, pattern: str,
                              ledger: EvidenceLedger,
                              memory_hits: list | None = None,
                              similar_prior_cases: list | None = None) -> str:
    """Prose pattern_description for undocumented (or fallback-labelled) patterns."""
    allowed = _allowed_from(case_id, similar_prior_cases)
    prompt = PATTERN_DESC_PROMPT.format(
        case_id=case_id, pattern=pattern,
        ledger=_ledger_readable(ledger),
        memory_hits=_memory_hits_readable(memory_hits or []),
        allowed_ids=sorted(allowed),
    )
    try:
        return _generate_and_guard(prompt, allowed_ids=allowed,
                                    temperature=0.2, max_output_tokens=180)
    except Exception as e:  # noqa: BLE001
        from sentinel.llm import LLMDisabled
        if isinstance(e, LLMDisabled):
            raise LLMSkipped("LLM disabled") from e
        raise LLMGenerationError(f"pattern description generation failed: {e}") from e


def write_sar(case_id: str, verdict: str, exposure_usd: float, pattern: str,
              policy_hits: list, ledger: EvidenceLedger,
              memory_hits: list | None = None,
              similar_prior_cases: list | None = None) -> str:
    """Return the SAR narrative paragraph."""
    allowed = _allowed_from(case_id, similar_prior_cases)
    prompt = SAR_PROMPT.format(
        case_id=case_id, exposure=exposure_usd, pattern=pattern,
        verdict=verdict, ledger=_ledger_readable(ledger),
        memory_hits=_memory_hits_readable(memory_hits or []),
        policy=_policy_hits_readable(policy_hits or []),
        allowed_ids=sorted(allowed),
    )
    try:
        return _generate_and_guard(prompt, allowed_ids=allowed,
                                    temperature=0.15, max_output_tokens=500)
    except Exception as e:  # noqa: BLE001
        from sentinel.llm import LLMDisabled
        if isinstance(e, LLMDisabled):
            raise LLMSkipped("LLM disabled by SENTINEL_LLM_DISABLED") from e
        raise LLMGenerationError(f"SAR generation failed: {e}") from e
