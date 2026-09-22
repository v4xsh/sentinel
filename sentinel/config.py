"""Central config: paths + env loading."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load .env once on import; subsequent os.getenv() calls see it.
load_dotenv(REPO_ROOT / ".env")

RAW = REPO_ROOT / "data" / "raw"
PARQUET = REPO_ROOT / "data" / "parquet"
GRAPH_LOAD = REPO_ROOT / "data" / "graph_load"
DOCS_DIR = REPO_ROOT / "docs"
CASES = REPO_ROOT / "cases"
CASES_EXTRA = REPO_ROOT / "cases_extra"
BACKTEST = REPO_ROOT / "backtest"

# DuckDB store — a durable file so all scripts share the same tables/views.
DUCKDB_PATH = PARQUET / "sentinel.duckdb"

# TigerGraph
TG_HOST = os.getenv("TG_HOST", "")
TG_GRAPHNAME = os.getenv("TG_GRAPHNAME", "FraudGraph")
TG_SECRET = os.getenv("TG_SECRET", "")
TG_RESTPP_PORT = int(os.getenv("TG_RESTPP_PORT", "443"))
TG_GS_PORT = int(os.getenv("TG_GS_PORT", "443"))
TG_TGCLOUD = os.getenv("TG_TGCLOUD", "true").lower() == "true"

# LLM
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")
LLM_MODEL_CHEAP = os.getenv("LLM_MODEL_CHEAP", "gemini-2.5-flash-lite")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
