"""Job-hunt pipeline: research companies with Exa, score fit with Claude, deliver via Telegram.

    python hunt.py discover [focus]          # suggest companies (name,domain) to research
    python hunt.py run [--dry-run]           # research + score + deliver for companies.csv
    python hunt.py run --research-only       # Exa research only (no Claude, no Telegram)
    python hunt.py send [out/<run>]          # send a saved run's digest to Telegram (default: latest)
    python hunt.py watch [--dry-run]         # check watched Ashby boards for new matching postings
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

import exa_client
import research
import scoring
import telegram
import watch

logger = logging.getLogger("hunt")
WORKERS = 4
VERDICTS = ("apply", "consider", "stretch", "check_careers", "skip")
OUT_DIR = Path("out")


def load_profile(path):
    with open(path, "rb") as f:
        profile = tomllib.load(f)
    search = profile.get("search", {})
    for key in ("roles", "seniority", "work_mode"):
        if not search.get(key):
            sys.exit(f"{path}: [search].{key} is required")
    candidate = profile.setdefault("candidate", {})
    candidate.setdefault("experience", "")
    candidate.setdefault("notes", "")
    candidate.setdefault("location", "")
    candidate.setdefault("gaps", "")
    candidate.setdefault("weak_spots", "")
    cv_file = candidate.get("cv_file")
    if cv_file:
        cv_path = Path(cv_file)
        if not cv_path.is_file():
            sys.exit(f"{path}: [candidate].cv_file {cv_file!r} not found")
        parts = (cv_path.read_text().strip(), candidate["experience"].strip())
        candidate["experience"] = "\n\n".join(p for p in parts if p)
    if not candidate["experience"].strip():
        logger.warning("%s: [candidate].experience is empty; fit is scored on role/location criteria only", path)
    return profile


def load_companies(path):
    """CSV rows of name,domain (extra columns ignored). Blank lines and '#' comments are skipped."""
    companies = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if not row or row[0].strip().startswith("#"):
                continue
            if len(row) < 2 or not row[0].strip() or not row[1].strip():
                sys.exit(f"{path}: each row needs name,domain; got {row!r}")
            companies.append((row[0].strip(), row[1].strip()))
    if not companies:
        sys.exit(f"{path}: no companies found")
    return companies


def process_company(name, domain, profile, *, score):
    """Research (and optionally score) one company. Never raises; errors are returned in the result."""
    result = {"name": name, "domain": domain}
    try:
        result["research"] = research.research_company(name, domain, profile)
        if score:
            fit, usage = scoring.score_company(result["research"], profile)
            result["fit"] = fit.model_dump()
            result["usage"] = usage
    except Exception as exc:  # one failing company must not abort the batch
        logger.exception("%s (%s) failed", name, domain)
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def format_company(result):
    fit = result["fit"]
    lines = [f"[{fit['score']}] {fit['verdict'].upper().replace('_', ' ')}: {result['name']} ({result['domain']})"]
    if fit["best_role_title"]:
        lines.append(f"Role: {fit['best_role_title']}")
        if fit["best_role_url"]:
            lines.append(fit["best_role_url"])
    if fit.get("scope_note"):
        lines.append(f"* Scope: {fit['scope_note']}")
    lines.append(f"Location: {fit['location_assessment']}")
    lines += [f"+ {reason}" for reason in fit["reasons"]]
    lines += [f"- {concern}" for concern in fit["concerns"]]
    if fit.get("gap_note"):
        lines.append(f"Gap: {fit['gap_note']}")
    lines += [f"Build: {idea}" for idea in fit.get("proof_ideas", [])]
    lines.append(f"Next: {fit['next_step']}")
    return "\n".join(lines)


def build_digest(results):
    """Returns text blocks: header, apply/consider by score, stretch roles, check-careers leads, skipped, errors."""
    scored = sorted((r for r in results if "fit" in r), key=lambda r: r["fit"]["score"], reverse=True)
    by_verdict = {v: [r for r in scored if r["fit"]["verdict"] == v] for v in VERDICTS}
    shortlist = [r for r in scored if r["fit"]["verdict"] in ("apply", "consider")]
    stretch = by_verdict["stretch"]
    check = by_verdict["check_careers"]
    skipped = by_verdict["skip"]
    errors = [r for r in results if "error" in r]

    header = (
        f"Job hunt digest {datetime.now():%Y-%m-%d} | {len(results)} companies | "
        f"{len(by_verdict['apply'])} apply, {len(by_verdict['consider'])} consider, {len(stretch)} stretch, "
        f"{len(check)} check careers, {len(skipped)} skip, {len(errors)} errors"
    )
    blocks = [header] + [format_company(r) for r in shortlist + stretch + check]
    if skipped:
        blocks.append(
            "Skipped:\n"
            + "\n".join(
                f"- {r['name']} [{r['fit']['score']}]: {r['fit']['concerns'][0] if r['fit']['concerns'] else 'no fit'}"
                for r in skipped
            )
        )
    if errors:
        blocks.append("Errors:\n" + "\n".join(f"- {r['name']}: {r['error']}" for r in errors))
    return blocks


def save_results(results):
    run_dir = OUT_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        (run_dir / f"{result['domain']}.json").write_text(json.dumps(result, indent=2))
    return run_dir


def log_costs(results):
    summary = exa_client.cost_summary()
    tokens_in = sum(r.get("usage", {}).get("input_tokens", 0) for r in results)
    tokens_out = sum(r.get("usage", {}).get("output_tokens", 0) for r in results)
    print(
        f"\nExa: {summary['requests']} requests, ${summary['dollars']:.4f}"
        f" | Claude: {tokens_in} input / {tokens_out} output tokens"
    )


def cmd_run(args):
    profile = load_profile(args.profile)
    companies = load_companies(args.companies)
    score = not args.research_only
    deliver = not (args.dry_run or args.research_only)

    # Fail before spending anything.
    if score and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env (or use --research-only).")
    if deliver and not telegram.configured():
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set. Add them to .env (or use --dry-run).")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda c: process_company(*c, profile, score=score), companies))
    run_dir = save_results(results)
    print(f"Saved raw results to {run_dir}/")

    if args.research_only:
        for r in results:
            if "error" in r:
                print(f"\n{r['name']}: ERROR {r['error']}")
                continue
            c = r["research"]["content"]
            print(f"\n{r['name']} ({r['domain']}) verified={c['identity_verified']}: {c['business_summary']}")
            print(f"  roles: {len(c['open_roles'])} (source: {r['research']['role_source']})")
            for role in c["open_roles"]:
                print(f"  - {role['title']} | {role['location']} | {role['remote_or_relocation']} | {role['url']}")
    else:
        blocks = build_digest(results)
        if deliver:
            messages = telegram.pack_blocks(blocks)
            for message in messages:
                telegram.send(message)
            print(f"Sent {len(messages)} Telegram message(s)")
        else:
            print("\n\n".join(blocks))

    log_costs(results)
    return 1 if any("error" in r for r in results) else 0


def normalize_domain(domain):
    domain = domain.strip().lower()
    for prefix in ("https://", "http://", "www."):
        domain = domain.removeprefix(prefix)
    return domain.split("/")[0]


HISTORY_FIELDS = ["discovered_at", "name", "domain", "why", "focus"]


def append_history(path, discovered, focus, *, limit=None):
    """Append (name, domain, why) rows whose domain is not yet in the history CSV.

    Duplicates within `discovered` are dropped; at most `limit` new rows are kept. Returns the new rows.
    """
    path = Path(path)
    seen = set()
    if path.exists():
        with open(path, newline="") as f:
            seen = {row["domain"] for row in csv.DictReader(f)}
    new = []
    for name, domain, why in discovered:
        if limit is not None and len(new) >= limit:
            break
        if domain not in seen:
            seen.add(domain)
            new.append((name, domain, why))
    if new:
        write_header = not path.exists() or path.stat().st_size == 0
        stamp = datetime.now().isoformat(timespec="seconds")
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(HISTORY_FIELDS)
            for name, domain, why in new:
                writer.writerow([stamp, name, domain, why, focus])
    return new


EXCLUDED_DOMAINS = {"linkedin.com", "indeed.com", "glassdoor.com", "wellfound.com", "ziprecruiter.com"}
ANONYMOUS_NAME = re.compile(r"\b(undisclosed|confidential|anonymous)\b", re.IGNORECASE)
DISCOVERY_SYSTEM_PROMPT = (
    "Only include real operating companies with a verifiable primary company website (not a free-hosting "
    "subdomain, recruitment agency or job board) and evidence of current hiring. One entry per company."
)


def discovery_queries(profile, focus):
    """One auto search per target role plus one deep search over all roles: [(label, query, type)].

    A single search yields few companies (the structured answer covers only well-evidenced ones), so the
    union of several differently-angled searches is used.
    """
    search = profile["search"]
    seniority = " or ".join(search["seniority"])
    location = profile["candidate"].get("location", "").strip()
    location_clause = (
        f" Prefer companies that hire remotely across Europe (candidate is in {location}) or offer relocation."
        if location
        else ""
    )
    suffix = f"{search['work_mode']}{location_clause} {focus}".strip()
    queries = [
        (role, f"Companies currently hiring a {seniority} {role}. {suffix}", "auto") for role in search["roles"]
    ]
    queries.append(
        ("all roles (deep)", f"Companies currently hiring {seniority}-level {', '.join(search['roles'])}. {suffix}", "deep")
    )
    return queries


def run_discovery_query(query, search_type, limit):
    schema = {
        "type": "object",
        "properties": {
            "companies": {
                "type": "array",
                "maxItems": limit,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "domain": {"type": "string", "description": "Primary website domain, e.g. ramp.com"},
                        "why": {"type": "string", "description": "One sentence: why this company matches"},
                    },
                    "required": ["name", "domain", "why"],
                },
            }
        },
        "required": ["companies"],
    }
    response = exa_client.search_structured(
        query,
        schema,
        type=search_type,
        system_prompt=DISCOVERY_SYSTEM_PROMPT,
        num_results=min(100, limit + 10),  # one result rarely yields one company
    )
    if response.output is None or not isinstance(response.output.content, dict):
        raise RuntimeError("Exa returned no structured output")
    rows = []
    for company in response.output.content["companies"]:
        name, domain = company["name"].strip(), normalize_domain(company["domain"])
        if "." in domain and domain not in EXCLUDED_DOMAINS and not ANONYMOUS_NAME.search(name):
            rows.append((name, domain, company["why"].strip()))
    return rows


def cmd_discover(args):
    profile = load_profile(args.profile)
    queries = discovery_queries(profile, args.focus)

    def run_one(item):
        label, query, search_type = item
        try:
            rows = run_discovery_query(query, search_type, args.limit)
            logger.info("discovery [%s]: %d companies", label, len(rows))
            return rows
        except Exception as exc:  # one failing query must not lose the others
            logger.error("discovery [%s] failed: %s: %s", label, type(exc).__name__, exc)
            return None

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(run_one, queries))
    if all(r is None for r in results):
        sys.exit("All discovery searches failed")

    exclude = {normalize_domain(d) for d in profile["search"].get("exclude_domains", [])}
    # Round-robin across queries so the limit does not favour the first role.
    merged = [
        row
        for group in zip_longest(*(r or [] for r in results))
        for row in group
        if row and row[1] not in exclude
    ]
    new = append_history(args.history, merged, args.focus, limit=args.limit)
    writer = csv.writer(sys.stdout)
    for row in new:
        writer.writerow(row)
    logger.info(
        "%d unique candidates, %d new saved to %s (limit %d)",
        len({d for _, d, _ in merged}),
        len(new),
        args.history,
        args.limit,
    )
    return 0


def cmd_send(args):
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        runs = sorted(OUT_DIR.iterdir()) if OUT_DIR.is_dir() else []
        if not runs:
            sys.exit(f"No saved runs in {OUT_DIR}/")
        run_dir = runs[-1]
    results = [json.loads(f.read_text()) for f in sorted(run_dir.glob("*.json"))]
    if not any("fit" in r for r in results):
        sys.exit(f"{run_dir}: no scored results (was it a --research-only run?)")
    if not telegram.configured():
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set. Add them to .env.")
    messages = telegram.pack_blocks(build_digest(results))
    for message in messages:
        telegram.send(message)
    print(f"Sent {len(messages)} Telegram message(s) from {run_dir}/")
    return 0


def cmd_watch(args):
    profile = load_profile(args.profile)
    try:
        boards, keywords, exclude = watch.load_config(profile)
    except ValueError as exc:
        sys.exit(f"{args.profile}: {exc}")
    if not args.dry_run and not telegram.configured():
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set. Add them to .env (or use --dry-run).")

    new_matches, problems, new_state = watch.check(boards, keywords, exclude, watch.load_state(args.state))
    for problem in problems:
        logger.warning("watch: %s", problem)
    if not new_matches:
        print(f"No new matching postings on {len(boards) - len(problems)}/{len(boards)} board(s).")
    else:
        message = watch.format_message(new_matches, problems)
        if args.dry_run:
            print(message)
        else:
            for part in telegram.pack_blocks([message]):
                telegram.send(part)
            print(f"Sent {len(new_matches)} new posting(s) to Telegram.")
    if not args.dry_run:
        watch.save_state(args.state, new_state)  # only after a successful send, so failures retry next run
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="profile.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="research, score and deliver")
    run.add_argument("--companies", default="companies.csv")
    run.add_argument("--dry-run", action="store_true", help="print the digest instead of sending to Telegram")
    run.add_argument("--research-only", action="store_true", help="Exa research only; skip Claude and Telegram")
    run.set_defaults(func=cmd_run)

    discover = sub.add_parser("discover", help="suggest companies as name,domain,why CSV rows")
    discover.add_argument("focus", nargs="?", default="", help="optional extra focus, e.g. 'fintech'")
    discover.add_argument("--limit", type=int, default=15)
    discover.add_argument("--history", default="discovered.csv", help="CSV log of every discovered company")
    discover.set_defaults(func=cmd_discover)

    send = sub.add_parser("send", help="send the digest of a saved run to Telegram (no new research or scoring)")
    send.add_argument("run_dir", nargs="?", help="a directory under out/; default: the latest run")
    send.set_defaults(func=cmd_send)

    watch_cmd = sub.add_parser("watch", help="check watched Ashby boards and send new matching postings to Telegram")
    watch_cmd.add_argument("--state", default="watch_state.json", help="JSON file of postings already reported")
    watch_cmd.add_argument("--dry-run", action="store_true", help="print new matches; do not send or save state")
    watch_cmd.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
