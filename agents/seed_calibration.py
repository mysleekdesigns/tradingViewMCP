"""Run the Freqtrade seed strategies through the honest pipeline (calibration baseline).

Usage (from ``agents/``)::

    uv run python seed_calibration.py [--seed-dir seeds] [--journal-dir journal]

Starts a new research run in the real ledger (``cfg.ledger_path``) with the owner's frozen
success criteria, runs every seed's design-window backtest + adversarial reports, judges them,
and writes ``journal/<date>-seed-calibration.md``. Never calls the holdout check. Results are
historical simulations, not forward-looking.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lab.config import load_config
from lab.ledger import Ledger
from lab.research.contract import JOURNAL_DIR, ResearchConfigError, load_research_config
from research.seeds import SEED_DIR, load_seeds, run_seed_calibration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed-dir", type=Path, default=SEED_DIR, help="folder of seed JSON")
    parser.add_argument("--journal-dir", type=Path, default=JOURNAL_DIR, help="report folder")
    args = parser.parse_args(argv)

    try:
        research = load_research_config()
    except ResearchConfigError as exc:
        print(
            f"Cannot start calibration: {exc}\n"
            "The success criteria must be installed by the owner before any research run.",
            file=sys.stderr,
        )
        return 2
    try:
        seeds = load_seeds(args.seed_dir)
    except ValueError as exc:
        print(f"Invalid seeds: {exc}", file=sys.stderr)
        return 2

    cfg = load_config()
    with Ledger(cfg.ledger_path) as ledger:
        result = run_seed_calibration(
            cfg=cfg, ledger=ledger, research=research, seeds=seeds, journal_dir=args.journal_dir
        )
    print(f"research run: {result.run_id}")
    for o in result.outcomes:
        c = o.criteria
        status = (
            "error"
            if c is None
            else ("passed (pre-holdout)" if c.get("pre_holdout_passed") else "failed")
        )
        line = f"  {o.seed.name}: {status}"
        if o.error:
            line += f" [{o.error}]"
        print(line)
    print(f"report: {result.report_path}")
    print("Results are historical simulations, not forward-looking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
