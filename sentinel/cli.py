"""Sentinel CLI entrypoint.

Currently implements ``sentinel backtest`` — runs the agent over a
stratified sample of ClosedCases and reports metrics.

Usage:
    python -m sentinel backtest --n 150 --stratify pattern
    python -m sentinel backtest --n 150 --stratify pattern --seed 42
"""

from __future__ import annotations

import argparse
import sys

from sentinel.backtest.run import run_backtest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="Run the agent over a ClosedCase sample")
    bt.add_argument("--n", type=int, default=150, help="Sample size (fraud + cleared)")
    bt.add_argument("--n-fraud", type=int, default=None)
    bt.add_argument("--n-cleared", type=int, default=None)
    bt.add_argument("--stratify", type=str, default="pattern")
    bt.add_argument("--seed", type=int, default=42)
    bt.add_argument("--out-dir", type=str, default="backtest")
    bt.add_argument("--mode", type=str, default="simulated",
                    choices=["oracle", "simulated"])
    bt.add_argument("--tau", type=float, default=None,
                    help="Simulator threshold; None → tune on the tune-half curve")
    bt.add_argument("--tune-frac", type=float, default=0.5,
                    help="Fraction of sample used to tune τ; rest is held-out eval "
                         "(default 0.5 → 75/75 for n=150)")
    bt.add_argument("--oot", action="store_true",
                    help="Out-of-time eval: tune from Jul–Sep, eval from Oct+")
    args = parser.parse_args(argv)

    if args.cmd == "backtest":
        return run_backtest(
            n=args.n, stratify=args.stratify, seed=args.seed, out_dir=args.out_dir,
            mode=args.mode, tau=args.tau,
            n_fraud=args.n_fraud, n_cleared=args.n_cleared,
            tune_frac=args.tune_frac, oot=args.oot,
        )
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
