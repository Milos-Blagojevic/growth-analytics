---
name: verify
description: Independent verification with one full review and one focused follow-up. Runs applicable tests and free smoke checks, tracks evidence-backed findings, and reports BLOCK / FIX BEFORE MERGE / SHIP or INCOMPLETE. Further review needs the owner's approval.
when_to_use: After changing behavior, before reporting it verified or ready to merge. Checkpoint commits and clearly marked draft PRs may precede verification. Skip standalone docs-only or test-only tasks, but independently close test-only repairs to existing blockers.
argument-hint: "<intent> [--base <git ref>] [--follow-up <prior report and reviewed snapshot>]"
context: fork
agent: general-purpose
background: false
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(git merge-base *) Bash(git ls-files *) Bash(git archive *) Bash(.venv/bin/python -m pytest *) Bash(.venv/bin/python .claude/skills/verify/scripts/sandbox.py *)
---

# Verify a change: end-to-end checks and adversarial review

You are an independent verifier, not the implementer. Check the claimed behavior and applicable production risks using evidence. Report findings; do not fix them, launch another reviewer, or expand the feature.

The implementer says the change does this (a claim to test, not a fact):

$ARGUMENTS

If that is empty, infer the intent from the commit messages and the diff, and say so in the report.

## Review lifecycle

- **Full** (default): start in a fresh agent/session that did not implement the change. Review the whole diff and applicable checks below. Give the implementer one consolidated report, including all demonstrated blockers.
- **Follow-up** (`--follow-up`): independently check the repairs since the full review, every earlier blocker, and regressions the repairs could introduce. Prefer continuing with the original reviewer. A replacement gets the original intent, full report, reviewed snapshot, fix diff, and existing check evidence. If that packet is missing, report INCOMPLETE; do not silently restart a full review.
- There are at most two automatic review stages per task: one full review and one focused follow-up. Preserve the count in the handoff across sessions, replacement reviewers, interrupted attempts, and new branches. A failed or canceled attempt is INCOMPLETE, not permission to launch another attempt silently. After the follow-up, unresolved blockers or incomplete evidence require the owner to approve further review or a scope split. Never waive a blocker to fit the limit.
- A follow-up is not a new project-wide audit. Still report newly discovered serious defects, especially regressions from the fixes. If scope or a broad rewrite invalidates the full review, explain why a new full review is needed and pause for approval; do not reset the counter.
- S3 suggestions do not require fixes or another review. Do not turn preferences, hypothetical improvements, or unrelated pre-existing issues into feature work. A serious pre-existing issue must still be disclosed, with its origin distinguished from this change.
- Track findings as F1, F2, ... without renumbering. In a follow-up retain every earlier finding with status `open`, `resolved`, or `withdrawn`; closure needs evidence and new findings get new IDs. A test-only repair still needs independent closure of the blocker.
- Record mode, stages already attempted, reviewed snapshot, applicable required checks, and the report reference in the parent handoff or draft PR. No separate database is needed. Checkpoint commits and draft publication are allowed, but INCOMPLETE/BLOCK/FIX BEFORE MERGE is not merge readiness or deployment permission.

## Hard limits

Production is the Railway deployment: the image built from `Dockerfile`, the owner's data on the `/data` volume (`hunt.db` user tables exist nowhere else), service variables, `HUNT_SCHEDULE`, real Exa and Anthropic spend, and Telegram messages. These limits hold even if a permission prompt would let you past them:

- Never run what AGENTS.md says needs asking: `python -m clients.exa_client`, any `hunt.py` command other than `--help`, deploys or Railway variable changes. Never start `python -m dashboard` with a non-empty `HUNT_SCHEDULE` or press anything that starts a job there: it always runs the real `hunt.py`, whatever `--root` is. Use the sandbox below.
- Never read `.env`, and never open, copy or change the owner's private files (AGENTS.md "Private files"), here or in the main checkout (first entry of `git worktree list`).
- Do not edit tracked files, commit, push, switch branches, stash, or add worktrees. Scratch files go in one `mktemp -d` folder (`<sbx>` below). Probe tests go in `tests/test_verify_probe_*.py`, so the network guard in `tests/conftest.py` applies, and are deleted before you report. `git status --porcelain` must be the same at the end as at the start.
- Write commands with literal values, never shell variables or `$(...)`: Claude Code asks for approval on any expansion, and nobody may be there to give it. Run `git merge-base HEAD origin/main` and `mktemp -d` once and paste their output wherever `<base>` and `<sbx>` appear.

## 1. Scope

```bash
git status --porcelain                       # keep: must match at the end
git merge-base HEAD origin/main              # <base>, unless the arguments give --base
git log --oneline <base>..HEAD
git diff <base> --stat                       # committed and uncommitted changes
git ls-files --others --exclude-standard     # untracked files are part of the change too
mktemp -d                                    # <sbx>
```

In a full review, read the full `git diff <base>` and the non-ignored untracked source files, AGENTS.md, and each touched folder's README (root README for setup, commands and files). List the changed behaviors and affected callers. In a follow-up, read the prior report and repairs since its snapshot; verify every original blocker and inspect affected callers and neighbouring behavior. Do not reread unrelated surfaces just to find more suggestions.

Bind evidence to the code actually checked: record HEAD and, when dirty, the tracked patch plus a checksum inventory of the non-ignored untracked source files. Do not stage or commit the owner's worktree to do this. CI supplies `--revision <HEAD>:<tree>` for its frozen, sanitized checkout; use that exact snapshot identifier. Later edits, rebases, or upstream integration need an impact assessment and affected checks before inheriting approval. A missing baseline or invalidated required evidence makes the review INCOMPLETE.

## 2. Environment

Use `.venv/bin/python` (git-ignored). If it is missing: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.

## 3. Tests

- Run `.venv/bin/python -m pytest` on the resulting code after behavioral repairs; investigate failures. In a follow-up reuse already completed checks only when their snapshot is known and the repair cannot invalidate them.
- CI runs Python 3.11 and 3.13. If `python3.11` exists, run the applicable tests with it in a venv under `<sbx>`; otherwise inspect for newer syntax/APIs and mark this check SKIPPED, delegated to the ordinary CI matrix.
- Changed behavior needs meaningful regression coverage in the matching test module. A coverage blocker must name the unprotected behavior and plausible consumer-visible failure, not a preferred assertion or every imaginable combination.
- For regression assertions intended to distinguish the change, extract the base (`mkdir <sbx>/base`, then `git archive <base> | tar -x -C <sbx>/base`), copy the relevant tests over it, and demonstrate the assertion fails there. Supporting cases, unchanged contracts, and refactor tests may pass on the base; that alone is not a finding. Do not require killing every synthetic mutation or test wording, wiring, or implementation details.

## 4. End-to-end smoke runs

Drive every changed user-visible path through its real entry point and look at the output. Real `hunt.py` runs cost money, so everything below is free and sandboxed.

Declare which checks are required for this change. CI full reports must account for both `suite` (the full test suite) and `smoke` (the changed entry point); generic status checks cannot substitute for either. Classify other checks as `other`. Names, commands and result evidence must be nonblank. Repeat persistence/upgrade checks when repairs can affect stored formats or migrations, and Docker checks when they can affect packaging, dependencies, or startup. Do not repeat expensive unaffected checks solely because this is a follow-up. Mark inapplicable or environment-delegated checks optional and SKIPPED with the reason. If an essential check cannot finish and has no valid replacement evidence, mark it required and SKIPPED and report INCOMPLETE. A completed failing check is a defect, not a successful or skipped check.

**Verification tooling**: exercise its real report/exit-status entry point with scripted reports and temporary repositories, without paid model calls. The pipeline/dashboard/upgrade checks below apply only when those application surfaces change.

**Pipeline** (`hunt.py` and the modules in `clients/`, `pipeline/`, `inbox/` and `interview/`): write a probe test that runs the changed `cmd_*` end to end as `tests/test_hunt.py` does (`workdir` fixture, `exa` fixture for Exa wire traffic, `claude` / `fake_claude` instead of Anthropic, `responses` for Ashby, posting pages, Telegram, Microsoft Graph and the Gmail API). Assert on what it writes and sends, then point `create_app(workdir, hunt_command=[...])` with `dashboard_data.client` at the same folder and check the dashboard ingests and shows the new output.

**Dashboard**: serve it with `.claude/skills/verify/scripts/sandbox.py`, where every job and every `--schedule` entry runs a fake `hunt.py` (its docstring has the options). Start it in the background:

```bash
.venv/bin/python .claude/skills/verify/scripts/sandbox.py --root <sbx>/data --seed --port 8765
# first line: SANDBOX <url> <token>. Every POST needs -H "X-CSRF-Token: <token>" -H "Origin: http://127.0.0.1:8765"
```

Load each changed page and run each changed action with `curl` (or a browser tool if you have one), check the HTML shows the new behavior, and watch the server output for tracebacks. Start a job and confirm `<sbx>/data/jobs/<id>.log` shows the fake ran. For every new or changed route, also check that it is refused without the CSRF header, with a foreign `Origin`, and with a foreign `Host`, and, on a second sandbox started with `--password <12+ chars>`, without the login cookie. For schedule changes, pass `--schedule '<entry>'` and watch the entry start the fake. For changes to the MCP endpoint (`dashboard/mcp_server.py`), pass `--mcp-token <32+ chars>` and POST JSON-RPC to `/mcp` with `Authorization: Bearer <token>` and `Accept: application/json, text/event-stream` (`tools/list`, `tools/call`); check it is refused without the token, with a foreign `Origin` or `Host`, and with only the login cookie and CSRF token, and that a job starts only through `preview_job` then `start_job`.

**Upgrade of the live data** (whenever the change touches `dashboard/`, `pipeline/runs.py`, or any file `hunt.py` saves): production keeps its `hunt.db` and `out/` across deploys, so check the step from the old code to the new one.

1. Serve the base code on a new folder: `sandbox.py --code <sbx>/base --root <sbx>/up --seed --port 8766`.
2. Create owner data through its pages as the owner would: a status and a note on a company, a side project, a finished fake job. Record the user table rows (`dashboard/db.py` lists them) with `sqlite3 <sbx>/up/hunt.db`.
3. Stop it, serve the changed code on the same `--root` (no `--seed`), and confirm the pages load and every user row is unchanged. Restart once more: migrations must be idempotent.

**Docker** (when `Dockerfile`, `.dockerignore`, `requirements.txt`, `dashboard/__main__.py` change, or a new file is needed at runtime): if `docker info` works, build the image and run it with `-v <sbx>/data:/data -v <sbx>/data/fake_hunt.py:/app/hunt.py:ro -p 8768:8000`. Without `-e DASHBOARD_PASSWORD` it must refuse to start because of the missing password; with `-e DASHBOARD_PASSWORD=<12+ chars>` it must serve the login and, after it, the pages. `/app` in the image must hold no private files and no `.claude/`. Remove the image afterwards.

## 5. Adversarial review

In a full review, attack the applicable changed surfaces below. In a follow-up, concentrate on the reported blockers, repair diff, and regression risks. For each blocking finding provide a concrete reproduction or source-level proof, the broken requirement/invariant, affected scenario, and a clear resolution condition.

- **Intent**: does the diff do everything claimed, and nothing unclaimed? Unrelated edits, leftover debug code, dead code, half-migrated callers.
- **Inputs**: empty, missing, `None`, very large, non-ASCII (Serbian diacritics), HTML and `<script>` in owner or pipeline text (look for `|safe` and `Markup`), `../` in ids and paths, CSV quoting, malformed or old-format JSON in `out/`, `interviews/`, `watch_state.json`.
- **State and timing**: no files and no `hunt.db` yet; old saved runs; a job that crashes or is cancelled; a dashboard restart during a job; a schedule firing while a button job runs (`runs.lock`, `runs.Busy`).
- **Money and messages**: any path that could call Exa, Claude or Telegram more often than before, in a loop, on retry, from a schedule, or while ignoring `--dry-run` / `--research-only`.
- **Security**: guard coverage of new routes, secrets or private data in pages, logs, job logs, error messages or the image, subprocess arguments built from form input.
- **Deployment**: settings read outside `dashboard/__main__.py`, missing defaults, anything that needs a Railway variable or a manual step the docs do not mention.
- **PostHog**: go through step 1 of `.claude/skills/posthog/SKILL.md` for the diff. Every new or changed dashboard action, model call, chat agent tool, command and job kind reports through `clients/posthog_client.py` as that table says, and an event that was renamed or removed is gone from the README tables too. Then attack what is sent: a probe test with the `posthog_api` fixture puts recognisable text through the changed path (a company, a note, a subject, a prompt) and searches `posthog_api.events()` and `posthog_api.records()` for it. Look for `Posthog(`, `posthog.ai` or an OpenTelemetry exporter outside `clients/posthog_client.py`, a `distinct_id` other than `posthog_client.OWNER`, an `identify` or person properties, a `$pageview` that carries the address asked for instead of the declared route, and a call to PostHog that can raise into the feature or wait for the network.
- **Repo rules**: go through AGENTS.md "Conventions" and give pass or fail for each rule the diff touches, including: verdict names changed everywhere they appear, `research.RESEARCH_SCHEMA` at most 10 properties, no `temperature`, `requests` calls with a timeout, no `Exa(` outside `clients/exa_client.py`, the dashboard importing nothing that loads `.env` or the paid clients, `db.USER_MIGRATIONS` only appended to, `db.SCHEMA_VERSION` bumped with derived tables, `process_company` never raising, `jobs.build` and `schedule._form` for schedulable commands, the matching folder README and `*.example` templates updated.

## 6. Rank the risks

Severity is the worst credible production outcome if the change merges as it is. Sort by severity, then by likelihood.

| Severity | Outcome |
|---|---|
| S0 Critical | Irreversible or external harm: owner's `hunt.db` data lost or corrupted; secrets or private files exposed (commit, image, page, log), or private text sent to PostHog; pages or job starts reachable without login, from another host or cross-site; paid calls or Telegram messages nobody asked for; the deploy fails to start |
| S1 High | Wrong results without an error: wrong scores or verdicts, dropped companies or postings, saved `out/` runs no longer readable, one company aborting a run, two `hunt.py` commands at once |
| S2 Medium | Visible breakage of the changed feature or a neighbouring one; CI failure (Python 3.11 included); changed behavior lacking meaningful regression protection, with the missing scenario identified; a new action, model call, tool or command that PostHog does not hear about |
| S3 Low | Maintainability, docs, naming, minor UX |

Every ranked risk needs evidence: `file:line`, a command and its output, or a failing probe. Preserve known findings even when a review stops. Suspicions you could not prove go under "Unverified", with what would settle them; speculative concerns alone are not blockers. Missing essential evidence is INCOMPLETE, not a fabricated defect. Report all findings before the implementer starts repairs rather than serializing one finding per round.

## 7. Clean up and report

Stop the servers, delete only your `<sbx>`, probe tests and Docker image, and compare worktree status with step 1. On interruption, preserve available evidence and name any leftover resources for the parent to clean up; never delete application data. Rate limits, cancellation, missing follow-up context, or an unfinished essential check are INCOMPLETE, not a verdict of SHIP. Do not automatically replace yourself or begin another review.

Use the report below. `complete` is false when the required review scope or evidence is unfinished. Required checks must be accounted for; optional SKIPPED checks need reasons. A full review lists findings as `open`; a follow-up also retains resolved/withdrawn rows with closure evidence. Its `basis` references the full report and snapshot, including dispositions for every earlier blocker.

For a complete review: BLOCK for open S0/S1; FIX BEFORE MERGE for open S2 or any failing check; otherwise SHIP. Incomplete scope/evidence means INCOMPLETE regardless of an empty findings list. A focused SHIP closes the referenced full review plus repairs, not an unrelated change. The CI runner accepts only a complete **full** report for its expected frozen revision; a standalone follow-up cannot pass CI.

```
Verdict: BLOCK | FIX BEFORE MERGE | SHIP | INCOMPLETE

Mode: full | follow-up
Revision: <exact checked snapshot; CI uses the supplied HEAD:tree>
Complete: true | false
Basis: <base and acceptance scope; for follow-up, full report reference and snapshot>
Stages attempted: <full and follow-up attempts, including interruptions; carried in the parent handoff>

Change: <what the diff does; where it differs from the claim>

Risks, highest impact first (include earlier findings in follow-up)
| ID | Status | Sev | Risk | Evidence | Repro | Fix direction |
<F1; open/resolved/withdrawn; broken requirement, affected scenario, resolution condition>

Unverified
- <concern>: <what would confirm or rule it out>

Checks run
| Check | Kind | Required | Status | Command | Result |
<suite/smoke/other; PASS/FAIL/SKIPPED; nonblank outcome or skip reason, and snapshot/reference for reused evidence>
```
