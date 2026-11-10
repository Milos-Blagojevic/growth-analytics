"""Lead enrichment, scoring and routing: research prospects with Exa, assess them with Claude, rate and route them
with the rules in prospects.toml, and keep every run in prospects.duckdb.

  python -m prospects discover             # find companies that match [icp] in Fiber's database (paid: Exa agent)
  python -m prospects run                  # research + assess every company in prospects.csv (paid: Exa, Claude)
  python -m prospects run --limit 3        # only the first 3 rows
  python -m prospects run --research-only  # Exa only: no Claude
  python -m prospects load                 # rebuild prospects.duckdb with the current prospects.toml (free)

Files (in the current directory): prospects.toml, prospects.csv, prospects_discovered.csv,
prospect_runs/<timestamp>/, prospects.duckdb.
"""

import argparse
import csv
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import duckdb

import hunt
from clients import exa_client
from pipeline import runs, scoring
from prospects import assess, config, discover, enrich, value, warehouse

logger = logging.getLogger("prospects")
WORKERS = 4
RUNS_DIR = Path("prospect_runs")
DB = Path("prospects.duckdb")


def load_config(path):
    try:
        return config.load(path)
    except FileNotFoundError:
        sys.exit(f"{path} not found: cp prospects.example.toml {path}, then edit it.")
    except (OSError, ValueError) as exc:  # unreadable, not TOML, or a missing, invalid or unknown key
        sys.exit(f"{path}: {exc}")


def process(name, domain, settings, *, score):
    """Research (and optionally assess) one prospect. Never raises; errors are returned in the result."""
    result = {"name": name, "domain": domain}
    try:
        result["research"] = enrich.research(name, domain, settings)
        if score:
            assessment, usage = assess.assess(result["research"], settings)
            result["assessment"] = assessment.model_dump()
            result["usage"] = usage
    except Exception as exc:  # one failing prospect must not abort the batch
        logger.exception("%s (%s) failed", name, domain)
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _tokens(results):
    return {
        "input_tokens": sum(r.get("usage", {}).get("input_tokens", 0) for r in results),
        "output_tokens": sum(r.get("usage", {}).get("output_tokens", 0) for r in results),
    }


def start_run(started_at):
    """Creates the folder the run saves into, prospect_runs/<start time>/."""
    run_dir = RUNS_DIR / started_at.strftime(warehouse.RUN_FORMAT)
    try:
        run_dir.mkdir(parents=True)
    except FileExistsError:
        sys.exit(f"{run_dir}/ already exists: another run started this second. Try again.")
    except OSError as exc:
        sys.exit(f"{run_dir}/ cannot be created: {exc}")
    return run_dir


def save_result(run_dir, result):
    """Writes the result; when it cannot be written (a name too long for a file, a full disk) that becomes the
    prospect's error, so the other prospects still finish."""
    try:
        (run_dir / f"{result['domain']}.json").write_text(json.dumps(result, indent=2))
    except (OSError, ValueError) as exc:  # ValueError: a NUL byte in the name
        logger.error("%s (%s) could not be saved: %s", result["name"], result["domain"], exc)
        result["error"] = f"not saved: {type(exc).__name__}: {exc}"


def say(text):
    """print(), with what UTF-8 cannot encode replaced: text cut inside an emoji (half a surrogate pair, which Exa
    can return) would otherwise stop the output of a run that is already paid for."""
    print(text.encode("utf-8", "replace").decode("utf-8"))


def save_manifest(run_dir, results, *, score, started_at):
    """Writes the run's manifest (mode, times, counts, costs) once every prospect is saved."""
    manifest = {
        "mode": "assessed" if score else "research_only",
        "started_at": started_at.isoformat(timespec="seconds"),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "companies": len(results),
        "errors": sum("error" in r for r in results),
        "exa": exa_client.cost_summary(),
        "claude": {"model": os.environ.get("SCORING_MODEL", scoring.DEFAULT_MODEL), **_tokens(results)} if score else None,
    }
    (run_dir / runs.MANIFEST).write_text(json.dumps(manifest, indent=2))


def _warning_lines(result):
    warnings = (result.get("research") or {}).get("warnings") or []
    return [f"warnings: {'; '.join(warnings)}"] if warnings else []


def _figure_line(figure):
    evidence = " ".join(figure["evidence"].split())  # one line, whatever the page's text held
    return f"{evidence} ({figure['stated_by']}, {figure['as_of'] or 'no date'})"


def format_prospect(result, settings):
    """One assessed prospect: fit, tier and route, the value range, then the brief and what backs the rating: the
    published figure the value ran on and, when the research states several, the ones set aside."""
    assessment = result["assessment"]
    worth = value.estimate(assessment, settings)
    where = value.route(assessment, worth, settings)
    lines = [
        f"[{assessment['fit_score']}] {where['tier']} -> {where['route']} | {result['name']} ({result['domain']})"
        f" | {value.describe(worth)}",
        assessment["brief"],
        f"route: {where['reason']}",
    ]
    if worth["figure"]:
        lines.append(f"{worth['method']}: {_figure_line(worth['figure'])}")
        others = (f for f in assessment[f"{worth['method']}_figures"] if f is not worth["figure"])
        lines += [f"  not used: {_figure_line(other)}" for other in others]
    lines += [f"+ {reason}" for reason in assessment["fit_reasons"]]
    lines += [f"- {concern}" for concern in assessment["fit_concerns"]]
    lines += [f"* {signal}" for signal in assessment["value_signals"]]
    return "\n".join(lines + _warning_lines(result))


def format_research(result):
    """One researched prospect of a --research-only run: every field Exa returned, then the news."""
    research = result["research"]
    lines = [f"{result['name']} ({result['domain']})"]
    for source in ("site", "money"):
        for field, text in (research[source] or {}).items():
            if field != "news":
                lines.append(f"  {field}: {'unknown' if text is None else text}")
    lines += [
        f"  news {item.get('date') or ''}: {item.get('title') or ''} {item.get('url') or ''}"
        for item in (research["money"] or {}).get("news") or []
    ]
    return "\n".join(lines + [f"  {line}" for line in _warning_lines(result)])


def rebuild(settings):
    try:
        run_count, rows = warehouse.build(RUNS_DIR, DB, settings)
    except duckdb.Error as exc:  # e.g. under a folder whose path DuckDB cannot open ("?" or a backslash in it)
        reason = str(exc).partition("\n")[0]
        sys.exit(f"{DB} was not rebuilt: {reason}")
    print(f"{DB}: {rows} prospect result(s) from {run_count} run(s)")


def cmd_run(args):
    if args.limit is not None and args.limit < 1:
        sys.exit("--limit must be 1 or more")
    settings = load_config(args.config)
    try:
        prospects = hunt.load_companies(args.prospects)[: args.limit]
    except FileNotFoundError:
        sys.exit(f"{args.prospects} not found: cp prospects.example.csv {args.prospects}, then list your prospects.")
    except (OSError, ValueError) as exc:  # a folder, no permission, or not UTF-8 (Excel's plain "CSV")
        sys.exit(f"{args.prospects}: {exc}")
    score = not args.research_only

    # Fail before spending anything.
    if not exa_client.configured():
        sys.exit("EXA_API_KEY is not set. Add it to .env.")
    if score and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env (or use --research-only).")

    started_at = datetime.now()
    run_dir = start_run(started_at)
    print(f"Saving raw results to {run_dir}/")

    def research_and_save(prospect):
        # Saved as soon as it is done, so an interrupted run keeps the research it has already paid for.
        result = process(*prospect, settings, score=score)
        save_result(run_dir, result)
        return result

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(research_and_save, prospects))
    save_manifest(run_dir, results, score=score, started_at=started_at)

    for result in results:
        if "error" in result:
            say(f"\n{result['name']}: ERROR {result['error']}")
        else:
            say("\n" + (format_prospect(result, settings) if score else format_research(result)))

    summary, tokens = exa_client.cost_summary(), _tokens(results)
    print(
        f"\nExa: {summary['requests']} requests, ${summary['dollars']:.4f}"
        f" | Claude: {tokens['input_tokens']} input / {tokens['output_tokens']} output tokens"
    )
    rebuild(settings)
    return 1 if any("error" in r for r in results) else 0


def cmd_load(args):
    rebuild(load_config(args.config))
    for row in warehouse.ranking(DB):
        if row["tier"] is None:  # researched only: nothing to rate yet
            print(f"[--] not assessed | {row['name']} ({row['domain']})")
            continue
        worth = {"low": row["value_low"], "high": row["value_high"], "method": row["value_method"]}
        print(f"[{row['fit_score']}] {row['tier']} -> {row['route']} | {row['name']} ({row['domain']}) | {value.describe(worth)}")
    return 0


def cmd_discover(args):
    if not 1 <= args.limit <= discover.MAX_LIMIT:
        sys.exit(f"--limit must be from 1 to {discover.MAX_LIMIT}")
    settings = load_config(args.config)
    try:
        known = discover.known_domains(args.prospects, args.history)
    except (OSError, ValueError, csv.Error) as exc:  # no permission, not UTF-8, a field beyond csv's limit
        sys.exit(f"{args.prospects} or {args.history} cannot be read: {exc}")
    if not exa_client.configured():  # fail before spending anything
        sys.exit("EXA_API_KEY is not set. Add it to .env.")
    try:  # before paying for companies that could then not be kept
        discover.check_writable(args.history)
    except OSError as exc:
        sys.exit(f"{args.history} cannot be written: {exc}")

    try:
        found = discover.find(settings, limit=args.limit, focus=args.focus, known=known)
    except Exception as exc:  # the run failed or was refused; say what it cost all the same
        sys.exit(f"Discovery failed: {type(exc).__name__}: {exc} | Exa: ${exa_client.cost_summary()['dollars']:.4f}")
    spent = f"Exa: ${exa_client.cost_summary()['dollars']:.4f}"
    not_saved = None
    try:  # saved before printing and printed whatever happens to the file: either failing must not lose a paid run
        if found:
            discover.save(args.history, found, args.focus)
    except OSError as exc:
        not_saved = exc
    writer = csv.writer(sys.stdout, lineterminator="\n")
    for company in found:
        writer.writerow([company[field] for field in discover.FIELDS[:5]])
    sys.stdout.flush()
    if not_saved:
        sys.exit(f"{args.history} could not be written ({not_saved}): the rows printed above were not saved | {spent}")
    logger.info(
        "%d new compan%s%s | %d known domain%s not listed again | %s",
        len(found), "y" if len(found) == 1 else "ies", f" saved to {args.history}" if found else "",
        len(known), "" if len(known) == 1 else "s", spent,
    )  # fmt: skip
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="python -m prospects", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="prospects.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="research, assess, rate and route the prospects; save the run")
    run.add_argument("--prospects", default="prospects.csv", help="CSV of name,domain rows")
    run.add_argument("--limit", type=int, help="only the first N rows")
    run.add_argument("--research-only", action="store_true", help="Exa research only; skip Claude")
    run.set_defaults(func=cmd_run)

    load = sub.add_parser("load", help="rebuild prospects.duckdb from the saved runs and print the ranking")
    load.set_defaults(func=cmd_load)

    found = sub.add_parser("discover", help="find new companies that match [icp] in Fiber's company database")
    found.add_argument("focus", nargs="?", default="", help="optional narrowing, e.g. 'fintech, 10 to 100 employees'")
    found.add_argument("--limit", type=int, default=10, help=f"companies to ask for, up to {discover.MAX_LIMIT}")
    found.add_argument("--history", default=discover.HISTORY, help="CSV of every company found so far")
    found.add_argument("--prospects", default="prospects.csv", help="companies listed here are not returned again")
    found.set_defaults(func=cmd_discover)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
