"""Run the online per-encounter evaluators from the CLI.

    python -m app.evals.online <encounter_id> [--deterministic-only]

Runs inside the backend container (needs the app Postgres). Persists the
eval_results rows (replacing prior rows for the encounter), prints the
per-evaluator table plus the launch-criteria table, and exits non-zero when
any deterministic gate fails. All scores are prototype evaluators — not
clinical validation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.evals import evaluators
from app.evals.criteria import build_launch_criteria


async def main(args: argparse.Namespace) -> int:
    try:
        rows = await evaluators.run_online_evals(
            args.encounter_id, include_model_evals=not args.deterministic_only
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    print(f"\nOnline evaluator results for {args.encounter_id} "
          f"[{evaluators.SCORE_LABEL}]")
    for r in rows:
        print(f"  [{'PASS' if r.passed else 'FAIL'}] {r.evaluator:<28} "
              f"({r.kind}) score={r.score:g}")
        print(f"         {r.detail[:220]}")

    print("\nLaunch criteria:")
    for c in build_launch_criteria(rows):
        print(f"  [{'GREEN' if c.passed else 'RED'}] {c.name:<32} "
              f"target {c.target:<10} actual {c.actual}")

    ok, failing = evaluators.gates_passed(rows)
    print(f"\nDeterministic gates: {'ALL PASS' if ok else 'FAILING: ' + ', '.join(failing)}")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("encounter_id")
    parser.add_argument("--deterministic-only", action="store_true",
                        help="skip the four model-graded judges (no model calls)")
    sys.exit(asyncio.run(main(parser.parse_args())))
