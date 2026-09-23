"""Backward-compat re-export. The real implementation lives at
``graph/algorithms/wcc.py`` so ``graph/algorithms/`` isn't empty.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from graph.algorithms.wcc import ring_wcc, RingWCC  # noqa: F401,E402
