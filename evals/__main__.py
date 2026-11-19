"""Check the public Jev exam for free, or explicitly run paid model calls and save a report."""

import argparse
import json
import sys
from pathlib import Path

from clients import jev
from evals import roles


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exam = commands.add_parser("roles", help="evaluate the production Jev keep/drop screen")
    exam.add_argument("--dataset", type=Path, default=roles.DATASET, help="frozen exam JSON; default: bundled synthetic exam")
    exam.add_argument("--split", choices=("all", "calibration", "holdout"), default="all")
    exam.add_argument("--live", action="store_true", help="make paid TypeSafe calls; otherwise only check the exam")
    exam.add_argument("--output", type=Path, help="new directory for results.json and README.md; required with --live")
    args = parser.parse_args(argv)
    if args.live != (args.output is not None):
        parser.error("--live and --output must be supplied together")
    try:
        dataset, digest = roles.load(args.dataset)
        cases = roles.select(dataset, args.split)
    except (OSError, ValueError) as exc:
        print(f"Invalid exam: {exc}", file=sys.stderr)
        return 2
    if not args.live:
        keeps = sum(case.expected == "keep" for case in cases)
        print(f"Validated {len(cases)} {dataset.kind} cases: {keeps} expected keeps, {len(cases) - keeps} expected drops.")
        for case in cases:
            print(f"  {case.id} [{case.split}]: {case.expected} — {case.label_reason}")
        print("No model calls made. No quality score measured. Use --live --output NEW_DIRECTORY to run the exam.")
        return 0
    if not jev.configured():
        print("TYPESAFE_API_KEY is not set. Export it in the environment; this command does not load .env.", file=sys.stderr)
        return 2
    # Reserve a fresh report directory before paying. Never overwrite a published baseline.
    try:
        args.output.mkdir(parents=True, exist_ok=False)
        with (args.output / "results.json").open("x", encoding="utf-8") as raw:
            with (args.output / "README.md").open("x", encoding="utf-8") as readable:
                print(f"Running {len(cases)} cases on {jev.model()}; up to {jev.MAX_ATTEMPTS} HTTP attempts per case.", flush=True)
                report = roles.run(dataset, digest, cases)
                encoded = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
                rendered = roles.markdown(report)
                raw.write(encoded + "\n")
                readable.write(rendered)
    except (OSError, TypeError, ValueError) as exc:
        print(f"Cannot encode or write reports; no quality claim: {exc}", file=sys.stderr)
        return 2
    counts = report["metrics"]
    print(
        f"{report['status'].upper()}: {counts['correct']}/{counts['evaluated']} evaluated cases correct; "
        f"{counts['false_drops']} false drops, {counts['false_keeps']} false keeps, {counts['errors']} errors."
    )
    print(f"Reports: {args.output / 'results.json'} and {args.output / 'README.md'}")
    print(f"Jev reported usage: {report['usage']['input_tokens']} input tokens; estimated ${report['usage']['dollars']:.6f}.")
    return 2 if counts["errors"] else 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
