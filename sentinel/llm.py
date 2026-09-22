"""Single wrapper for every LLM call.

Split by purpose:
  - **Embeddings** run on a *local* sentence-transformers model
    (`BAAI/bge-small-en-v1.5`, 384-dim, CPU). No free-tier quota, no rate
    limits, deterministic across runs.
  - **Generation** uses Google Gemini via the free AI Studio key (kept for
    natural-language work: summaries, SAR narratives, the adversarial critic).

Every call is:
  - **Retried with exponential backoff** on transient failures (only relevant
    to Gemini generation now).
  - **Disk-cached** — embeddings keyed by
    ``sha256("embed", model_name, dim, text)``, completions by their prompt.

The wrapper never sees the raw graph. Callers pass compact evidence summaries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sentinel.config import (
    EMBEDDING_MODEL,
    GOOGLE_API_KEY,
    LLM_MODEL,
    LLM_MODEL_CHEAP,
    REPO_ROOT,
)

logger = logging.getLogger(__name__)

CACHE_DIR = REPO_ROOT / "data" / "llm_cache"
EMBED_CACHE_DIR = CACHE_DIR / "embed"
COMPLETION_CACHE_DIR = CACHE_DIR / "gen"
EMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
COMPLETION_CACHE_DIR.mkdir(parents=True, exist_ok=True)

EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384  # native dim of BAAI/bge-small-en-v1.5.


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    calls: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    total_seconds: float = 0.0

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"hits={self.hits}  misses={self.misses}  calls={self.calls}  "
            f"tok_in={self.tokens_input}  tok_out={self.tokens_output}  "
            f"total_s={self.total_seconds:.1f}"
        )


STATS = CacheStats()


# ---- lazy client construction ------------------------------------------------


_client = None


def _get_client():
    global _client
    if _client is None:
        if not GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY is not set")
        from google import genai

        _client = genai.Client(api_key=GOOGLE_API_KEY)
    return _client


# ---- cache -------------------------------------------------------------------


def _hash_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def _cache_get(dir_: Path, key: str) -> dict | None:
    path = dir_ / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_put(dir_: Path, key: str, value: dict) -> None:
    path = dir_ / f"{key}.json"
    path.write_text(json.dumps(value), encoding="utf-8")


# ---- retry -------------------------------------------------------------------


RETRY_MAX_ATTEMPTS = 3          # Fail fast: don't burn 15+ min on quota-exceeded loops.
RETRY_CAP_SECONDS = 20.0

# Free-tier rate limit for gemini-embedding-001 is ~100 requests/minute.
# Throttle to 80/min = 1 request per 0.75s to stay comfortably below.
EMBED_MIN_INTERVAL_SECS = 0.75
_last_call_ts = 0.0


def _throttle(min_interval: float) -> None:
    """Sleep just enough to keep >=min_interval between successive calls."""
    global _last_call_ts
    now = time.time()
    wait = _last_call_ts + min_interval - now
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


def _sleep_backoff(attempt: int, e: Exception | None = None,
                   base: float = 2.0, cap: float = RETRY_CAP_SECONDS) -> float:
    """Sleep before the next retry.

    If the exception carries an explicit ``retryDelay`` hint (Gemini
    RESOURCE_EXHAUSTED does), we sleep that long plus a small jitter. Otherwise
    exponential backoff clamped to ``cap``.
    """
    delay = min(cap, base ** attempt) + 0.5 * (attempt % 3)
    if e is not None:
        msg = str(e)
        m = re.search(r"'retryDelay':\s*'(\d+)s'", msg) or re.search(r"retry in (\d+)", msg)
        if m:
            try:
                delay = max(delay, float(m.group(1)) + 1.0)
            except ValueError:
                pass
    time.sleep(delay)
    return delay


def _is_transient(e: Exception) -> bool:
    name = type(e).__name__.lower()
    if "clienterror" in name and "429" in str(e):
        return True
    if "resourceexhausted" in name or "toomany" in name:
        return True
    msg = str(e).lower()
    return any(k in msg for k in [
        "429", "rate limit", "quota", "resource exhausted", "resource_exhausted",
        "500", "503", "504", "deadline", "unavailable",
        "server error", "connection", "timeout",
    ])


# ---- embeddings (local sentence-transformers, CPU) ---------------------------


_st_model = None


def _get_st_model():
    """Lazy-load the sentence-transformers embedder. CPU-only."""
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("loading %s on CPU …", EMBEDDING_MODEL_NAME)
        _st_model = SentenceTransformer(EMBEDDING_MODEL_NAME, device="cpu")
    return _st_model


def embed_one(text: str, model: str = EMBEDDING_MODEL_NAME, dim: int = EMBED_DIM) -> list[float]:
    """Return an embedding vector for one string. Cached to disk.

    Cache key = sha256("embed", model_name, dim, text).
    """
    key = _hash_key("embed", model, str(dim), text)
    cached = _cache_get(EMBED_CACHE_DIR, key)
    if cached is not None:
        STATS.hits += 1
        return cached["v"]

    m = _get_st_model()
    t0 = time.time()
    v = m.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0].tolist()
    STATS.misses += 1
    STATS.calls += 1
    STATS.total_seconds += time.time() - t0
    _cache_put(EMBED_CACHE_DIR, key, {"v": v})
    return v


def embed_many(
    texts: Iterable[str],
    model: str = EMBEDDING_MODEL_NAME,
    dim: int = EMBED_DIM,
    batch_size: int = 64,
    on_progress=None,
) -> list[list[float]]:
    """Embed many texts, batching cache-misses on the local ST model.

    ``on_progress(sent, total)`` invoked after every ``batch_size`` requests.
    """
    texts = list(texts)
    out: list[list[float] | None] = [None] * len(texts)
    misses_i: list[int] = []
    misses_t: list[str] = []
    for i, t in enumerate(texts):
        key = _hash_key("embed", model, str(dim), t)
        cached = _cache_get(EMBED_CACHE_DIR, key)
        if cached is not None:
            out[i] = cached["v"]
            STATS.hits += 1
        else:
            misses_i.append(i)
            misses_t.append(t)

    if misses_t:
        m = _get_st_model()
        for start in range(0, len(misses_t), batch_size):
            batch_t = misses_t[start : start + batch_size]
            batch_i = misses_i[start : start + batch_size]
            t0 = time.time()
            vs = m.encode(batch_t, normalize_embeddings=True, convert_to_numpy=True).tolist()
            STATS.total_seconds += time.time() - t0
            STATS.calls += 1
            STATS.misses += len(batch_t)
            for idx, i in enumerate(batch_i):
                out[i] = vs[idx]
                key = _hash_key("embed", model, str(dim), batch_t[idx])
                _cache_put(EMBED_CACHE_DIR, key, {"v": vs[idx]})
            if on_progress:
                on_progress(min(start + batch_size, len(misses_t)), len(misses_t))
    return out  # type: ignore[return-value]


# ---- generation --------------------------------------------------------------


LAST_PROVIDER: dict = {"name": None, "model": None, "key_index": None}


# ---- Multi-key Gemini rotation ---------------------------------------------


def _google_api_keys() -> list[str]:
    """Return the list of Gemini API keys in the environment, in rotation order."""
    keys: list[str] = []
    primary = os.getenv("GOOGLE_API_KEY", "").strip()
    if primary:
        keys.append(primary)
    for i in range(2, 6):
        k = os.getenv(f"GOOGLE_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    return keys


_CLIENTS: dict[int, object] = {}          # keyed by index
_KEY_STATE: dict = {"index": 0, "exhausted": set()}


def _get_client_for_index(idx: int):
    """Return a Gemini client for the given key index, cached across calls."""
    global _CLIENTS
    if idx in _CLIENTS:
        return _CLIENTS[idx]
    keys = _google_api_keys()
    if idx >= len(keys):
        raise RuntimeError(f"no Gemini key at index {idx}")
    from google import genai
    c = genai.Client(api_key=keys[idx])
    _CLIENTS[idx] = c
    return c


def _all_keys_exhausted() -> bool:
    keys = _google_api_keys()
    return len(_KEY_STATE["exhausted"]) >= len(keys) and len(keys) > 0


class LLMDisabled(RuntimeError):
    """Raised when SENTINEL_LLM_DISABLED=1 blocks a generation call."""


def _llm_disabled() -> bool:
    return os.getenv("SENTINEL_LLM_DISABLED", "").strip() in ("1", "true", "TRUE", "yes")


def _is_daily_quota_error(e: Exception) -> bool:
    """Return True on a 429 whose retryDelay is 0 or whose message names a daily quota."""
    msg = str(e).lower()
    if "429" not in msg and "resource_exhausted" not in msg:
        return False
    if "perday" in msg or "per-day" in msg or "requestsperdayperproject" in msg:
        return True
    if "retrydelay': '0s'" in msg or "'retrydelay': '0'" in msg:
        return True
    return False


# ---- Groq (llama-3.x) fallback ------------------------------------------------

_GROQ_MODEL = "llama-3.3-70b-versatile"


def _groq_generate(prompt: str, temperature: float, max_output_tokens: int) -> tuple[str, int, int]:
    """Call Groq's OpenAI-compatible chat completions endpoint.

    Returns (text, prompt_tokens, completion_tokens). Raises RuntimeError
    if GROQ_API_KEY is missing or the call fails.
    """
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set — cannot use Groq fallback")
    import httpx  # already a repo dep

    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": _GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    text = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def generate(
    prompt: str,
    *,
    model: str = "",
    cheap: bool = False,
    temperature: float = 0.2,
    max_output_tokens: int = 800,
) -> str:
    """Return a completion. Cached to disk by (model, prompt, temp).

    Behaviour:
      * If ``SENTINEL_LLM_DISABLED=1`` → raise :class:`LLMDisabled` (never
        touch the network, never emit template fallback).
      * Try Gemini first (``LLM_MODEL`` or the cheap variant).
      * On 429 whose ``retryDelay`` is 0 or whose message names a *daily*
        quota, immediately fall through to Groq — do NOT retry Gemini.
      * On any other transient error, use up to :data:`RETRY_MAX_ATTEMPTS`
        retries with exponential backoff (short: cap ~20s, 3 tries).
      * ``LAST_PROVIDER`` is updated with the name/model that produced
        the returned text so callers can attribute per-case output.
    """
    if _llm_disabled():
        raise LLMDisabled("SENTINEL_LLM_DISABLED=1 — LLM generation blocked")

    resolved_model = model or (LLM_MODEL_CHEAP if cheap else LLM_MODEL)
    key = _hash_key("gen", resolved_model, str(temperature), str(max_output_tokens), prompt)
    cached = _cache_get(COMPLETION_CACHE_DIR, key)
    if cached is not None:
        STATS.hits += 1
        LAST_PROVIDER.update({"name": cached.get("provider", "cache"),
                              "model": cached.get("model", resolved_model)})
        return cached["text"]

    # ---- Try Gemini with key rotation on daily-quota 429 ---------------
    keys = _google_api_keys()
    if not keys:
        raise RuntimeError("no GOOGLE_API_KEY* set — cannot call Gemini")
    idx = _KEY_STATE["index"]
    while idx < len(keys) and idx in _KEY_STATE["exhausted"]:
        idx += 1
    _KEY_STATE["index"] = idx

    def _try_key(key_idx: int) -> tuple[bool, str, Exception | None]:
        """Attempt a single Gemini call on ``key_idx``. Returns (ok, text, err)."""
        try:
            c = _get_client_for_index(key_idx)
            t0 = time.time()
            resp = c.models.generate_content(
                model=resolved_model,
                contents=prompt,
                config={"temperature": temperature,
                        "max_output_tokens": max_output_tokens},
            )
            text = resp.text or ""
            usage = getattr(resp, "usage_metadata", None)
            if usage:
                STATS.tokens_input += getattr(usage, "prompt_token_count", 0) or 0
                STATS.tokens_output += getattr(usage, "candidates_token_count", 0) or 0
            STATS.calls += 1; STATS.misses += 1
            STATS.total_seconds += time.time() - t0
            LAST_PROVIDER.update({"name": "gemini", "model": resolved_model,
                                  "key_index": key_idx})
            _cache_put(COMPLETION_CACHE_DIR, key, {
                "text": text, "provider": "gemini", "model": resolved_model,
                "key_index": key_idx,
            })
            logger.info("gemini call served by key index %d (model=%s)",
                        key_idx, resolved_model)
            return True, text, None
        except Exception as e:  # noqa: BLE001
            return False, "", e

    while idx < len(keys):
        for attempt in range(RETRY_MAX_ATTEMPTS):
            ok, text, err = _try_key(idx)
            if ok:
                return text
            if _is_daily_quota_error(err):
                logger.info("gemini key idx=%d exhausted (daily quota) — rotating", idx)
                _KEY_STATE["exhausted"].add(idx)
                break
            if not _is_transient(err) or attempt == RETRY_MAX_ATTEMPTS - 1:
                logger.info("gemini key idx=%d non-transient/no-retry (%s) — rotating", idx, err)
                break
            delay = _sleep_backoff(attempt, err)
            logger.info("gemini idx=%d transient (%s) — retry #%d after %.1fs",
                        idx, type(err).__name__, attempt + 1, delay)
        idx += 1
        _KEY_STATE["index"] = idx

    # ---- Groq fallback --------------------------------------------------
    try:
        t0 = time.time()
        text, tin, tout = _groq_generate(prompt, temperature, max_output_tokens)
        STATS.tokens_input += tin
        STATS.tokens_output += tout
        STATS.calls += 1
        STATS.misses += 1
        STATS.total_seconds += time.time() - t0
        LAST_PROVIDER.update({"name": "groq", "model": _GROQ_MODEL})
        _cache_put(COMPLETION_CACHE_DIR, key, {
            "text": text, "provider": "groq", "model": _GROQ_MODEL,
        })
        return text
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"both Gemini and Groq failed: {e}") from e


# ---- introspection -----------------------------------------------------------


def cache_stats_snapshot() -> CacheStats:
    return CacheStats(**vars(STATS))


def cache_directory_stats() -> dict:
    def _dir(d: Path) -> dict:
        files = list(d.glob("*.json"))
        return {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}
    return {
        "embed_cache": _dir(EMBED_CACHE_DIR),
        "gen_cache": _dir(COMPLETION_CACHE_DIR),
    }


def list_generation_models() -> list[str]:
    """Return the generation-capable model IDs the current API key can call.

    Prints the full list to stdout so it appears in the run log.
    """
    client = _get_client()
    models = list(client.models.list())
    gen_models: list[str] = []
    for m in models:
        supp = getattr(m, "supported_actions", []) or []
        if "generateContent" in supp or "generateText" in supp or not supp:
            name = getattr(m, "name", "")
            if name:
                gen_models.append(name.replace("models/", ""))
    return gen_models


def probe_generation_model(candidates: list[str] | None = None) -> str:
    """Probe every gemini-3/flash-family model that supports generateContent.

    Prefers the newest gemini-3 family that answers, then 2.5, then flash-
    latest. Returns the first that answers with "pong". Prints the raw
    ``client.models.list()`` output first for observability.
    """
    client = _get_client()
    print("=== raw client.models.list() (generateContent-capable) ===")
    all_gen: list[str] = []
    for m in client.models.list():
        name = getattr(m, "name", "").replace("models/", "")
        supp = getattr(m, "supported_actions", []) or []
        if "generateContent" in supp:
            all_gen.append(name)
            print(f"  {name}")
    print(f"=== {len(all_gen)} generateContent-capable models discovered ===")

    if not candidates:
        # Preferred order: gemini-3.x flash → 3-flash-preview → 2.5-flash
        # (skip *-tts, *-image, *-live, *-audio, gemma, veo, aqa, lyria).
        SKIP_SUBSTR = ("tts", "image", "live", "audio", "gemma", "veo", "aqa",
                       "lyria", "robotics", "transcribe", "computer-use",
                       "antigravity", "deep-research", "banana", "embed")
        candidates_pref = []
        for pat in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                    "gemini-3.5-flash", "gemini-3-flash-preview",
                    "gemini-flash-latest", "gemini-2.5-flash",
                    "gemini-flash-lite-latest", "gemini-3.5-flash-lite",
                    "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"):
            if pat in all_gen and not any(s in pat for s in SKIP_SUBSTR):
                candidates_pref.append(pat)
        candidates = candidates_pref
    for name in candidates:
        try:
            txt = generate("Reply with the single word: pong.",
                           model=name, temperature=0.0, max_output_tokens=8)
            if txt and "pong" in txt.lower():
                logger.info("probe: %s works — text=%r", name, txt.strip())
                return name
            logger.info("probe: %s replied but no 'pong': %r", name, txt)
        except Exception as e:  # noqa: BLE001
            logger.info("probe: %s failed — %s", name, e)
    raise RuntimeError("no generation-capable Gemini model responded to the probe")


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    v = embed_one("hello world")
    print(f"embed_one: dim={len(v)}")
    v2 = embed_many(["one", "two", "three"])
    print(f"embed_many: {[len(x) for x in v2]}")
    print("cache stats:", cache_stats_snapshot())
    print("cache dir  :", cache_directory_stats())
