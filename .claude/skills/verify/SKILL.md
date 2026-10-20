---
name: verify
description: Independent, adversarial verification of a feature or behavior change, run by a fresh subagent that did not write the code. Runs the test suite and free end-to-end smoke runs (sandboxed dashboard with a fake hunt.py, pipeline-to-dashboard probe, old-to-new hunt.db upgrade, Docker image), attacks the diff, and returns risks ranked by production impact with a BLOCK / FIX BEFORE MERGE / SHIP verdict.
when_to_use: After implementing or changing any behavior (hunt.py, pipeline modules, dashboard/, Dockerfile, requirements), before reporting the work done, committing or opening a PR. Pass what the change is meant to do as the argument. Skip for README-only or test-only changes.
argument-hint: "<what the change should do> [--base <git ref>]"
context: fork
agent: general-purpose
background: false
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(git merge-base *) Bash(git ls-files *) Bash(git archive *) Bash(.venv/bin/python -m pytest *) Bash(.venv/bin/python .claude/skills/verify/scripts/sandbox.py *)
---

# Verify a change: end-to-end checks and adversarial review

You are the verifier. Someone else wrote this change; you have no stake in it. Prove it works end to end, prove it cannot hurt production, and find what the implementer missed. Treat it as broken until evidence says otherwise. Report findings; do not fix them.

The implementer says the change does this (a claim to test, not a fact):

$ARGUMENTS

If that is empty, infer the intent from the commit messages and the diff, and say so in the report.

## Hard limits

Production is the Railway deployment: the image built from `Dockerfile`, the owner's data on the `/data` volume (`hunt.db` user tables exist nowhere else), service variables, `HUNT_SCHEDULE`, real Exa and Anthropic spend, and Telegram messages. These limits hold even if a permission prompt would let you past them:

- Never run what AGENTS.md says needs asking: `exa_client.py`, any `hunt.py` command other than `--help`, deploys or Railway variable changes. Never start `python -m dashboard` with a non-empty `HUNT_SCHEDULE` or press anything that starts a job there: it always runs the real `hunt.py`, whatever `--root` is. Use the sandbox below.
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

Read the full `git diff <base>`, every untracked file, AGENTS.md, and the README section for each touched feature. List each behavior that changed, and find every caller of each changed function, class or file format: a changed signature or format with a caller left behind is a finding.

## 2. Environment

Use `.venv/bin/python` (git-ignored). If it is missing: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.

## 3. Tests

- `.venv/bin/python -m pytest`: every failure is a finding.
- CI runs Python 3.11 and 3.13. If `python3.11` exists, run the suite with it in a venv under `<sbx>`; otherwise read the diff for syntax or stdlib APIs newer than 3.11 and say the run was skipped.
- New behavior with no test in the matching `tests/test_<module>.py` is a finding.
- Tests that pass without the change prove nothing. Extract the base code (`mkdir <sbx>/base`, then `git archive <base> | tar -x -C <sbx>/base`), copy the changed and new test files over it, and run them there: tests for new behavior must fail on the base code. Report any that pass (refactors excepted).

## 4. End-to-end smoke runs

Drive every changed user-visible path through its real entry point and look at the output. Real `hunt.py` runs cost money, so everything below is free and sandboxed.

**Pipeline** (`hunt.py`, `research.py`, `scoring.py`, `coaching.py`, `watch.py`, `telegram.py`, `runs.py`, `interviews.py`): write a probe test that runs the changed `cmd_*` end to end as `tests/test_hunt.py` does (`workdir` fixture, `exa` fixture for Exa wire traffic, `claude` / `fake_claude` instead of Anthropic, `responses` for Ashby, posting pages and Telegram). Assert on what it writes and sends, then point `create_app(workdir, hunt_command=[...])` with `dashboard_data.client` at the same folder and check the dashboard ingests and shows the new output.

**Dashboard**: serve it with `.claude/skills/verify/scripts/sandbox.py`, where every job and every `--schedule` entry runs a fake `hunt.py` (its docstring has the options). Start it in the background:

```bash
.venv/bin/python .claude/skills/verify/scripts/sandbox.py --root <sbx>/data --seed --port 8765
# first line: SANDBOX <url> <token>. Every POST needs -H "X-CSRF-Token: <token>" -H "Origin: http://127.0.0.1:8765"
```

Load each changed page and run each changed action with `curl` (or a browser tool if you have one), check the HTML shows the new behavior, and watch the server output for tracebacks. Start a job and confirm `<sbx>/data/jobs/<id>.log` shows the fake ran. For every new or changed route, also check that it is refused without the CSRF header, with a foreign `Origin`, and with a foreign `Host`, and, on a second sandbox started with `--password <12+ chars>`, without the login cookie. For schedule changes, pass `--schedule '<entry>'` and watch the entry start the fake.

**Upgrade of the live data** (whenever the change touches `dashboard/`, `runs.py`, or any file `hunt.py` saves): production keeps its `hunt.db` and `out/` across deploys, so check the step from the old code to the new one.

1. Serve the base code on a new folder: `sandbox.py --code <sbx>/base --root <sbx>/up --seed --port 8766`.
2. Create owner data through its pages as the owner would: a status and a note on a company, a side project, a finished fake job. Record the user table rows (`dashboard/db.py` lists them) with `sqlite3 <sbx>/up/hunt.db`.
3. Stop it, serve the changed code on the same `--root` (no `--seed`), and confirm the pages load and every user row is unchanged. Restart once more: migrations must be idempotent.

**Docker** (when `Dockerfile`, `.dockerignore`, `requirements.txt`, `dashboard/__main__.py` change, or a new file is needed at runtime): if `docker info` works, build the image and run it with `-v <sbx>/data:/data -v <sbx>/data/fake_hunt.py:/app/hunt.py:ro -p 8768:8000`. Without `-e DASHBOARD_PASSWORD` it must refuse to start because of the missing password; with `-e DASHBOARD_PASSWORD=<12+ chars>` it must serve the login and, after it, the pages. `/app` in the image must hold no private files and no `.claude/`. Remove the image afterwards.

## 5. Adversarial review

Try to break the change. For each attack, aim for a failing probe test or a concrete repro rather than a hunch.

- **Intent**: does the diff do everything claimed, and nothing unclaimed? Unrelated edits, leftover debug code, dead code, half-migrated callers.
- **Inputs**: empty, missing, `None`, very large, non-ASCII (Serbian diacritics), HTML and `<script>` in owner or pipeline text (look for `|safe` and `Markup`), `../` in ids and paths, CSV quoting, malformed or old-format JSON in `out/`, `interviews/`, `watch_state.json`.
- **State and timing**: no files and no `hunt.db` yet; old saved runs; a job that crashes or is cancelled; a dashboard restart during a job; a schedule firing while a button job runs (`runs.lock`, `runs.Busy`).
- **Money and messages**: any path that could call Exa, Claude or Telegram more often than before, in a loop, on retry, from a schedule, or while ignoring `--dry-run` / `--research-only`.
- **Security**: guard coverage of new routes, secrets or private data in pages, logs, job logs, error messages or the image, subprocess arguments built from form input.
- **Deployment**: settings read outside `dashboard/__main__.py`, missing defaults, anything that needs a Railway variable or a manual step the README does not mention.
- **Repo rules**: go through AGENTS.md "Conventions" and give pass or fail for each rule the diff touches, including: verdict names changed everywhere they appear, `research.RESEARCH_SCHEMA` at most 10 properties, no `temperature`, `requests` calls with a timeout, no `Exa(` outside `exa_client.py`, the dashboard importing nothing that loads `.env` or the paid clients, `db.USER_MIGRATIONS` only appended to, `db.SCHEMA_VERSION` bumped with derived tables, `process_company` never raising, `jobs.build` and `schedule._form` for schedulable commands, README and `*.example` templates updated.

## 6. Rank the risks

Severity is the worst credible production outcome if the change merges as it is. Sort by severity, then by likelihood.

| Severity | Outcome |
|---|---|
| S0 Critical | Irreversible or external harm: owner's `hunt.db` data lost or corrupted; secrets or private files exposed (commit, image, page, log); pages or job starts reachable without login, from another host or cross-site; paid calls or Telegram messages nobody asked for; the deploy fails to start |
| S1 High | Wrong results without an error: wrong scores or verdicts, dropped companies or postings, saved `out/` runs no longer readable, one company aborting a run, two `hunt.py` commands at once |
| S2 Medium | Visible breakage of the changed feature or a neighbouring one; CI failure (Python 3.11 included); new behavior without a test, or tests that pass on the base code |
| S3 Low | Maintainability, docs, naming, minor UX |

Every ranked risk needs evidence: `file:line`, a command and its output, or a probe test that fails. Suspicions you could not prove go under "Unverified", with what would settle them.

## 7. Clean up and report

Stop the servers, delete `<sbx>`, the probe tests and any Docker image you built, and check `git status --porcelain` against step 1. If something could not be deleted, name it in the report. Then reply with exactly this report; it is all the implementer sees:

```
Verdict: BLOCK | FIX BEFORE MERGE | SHIP
(BLOCK: any S0 or S1. FIX BEFORE MERGE: any S2. SHIP: S3 only, or nothing.)

Change: <what the diff does, in one or two lines; where it differs from the claim>

Risks, highest impact first
| # | Sev | Risk | Evidence | Repro | Fix direction |

Unverified
- <concern>: <what would confirm or rule it out>

Checks run
| Check | Command | Result |
<pytest, Python 3.11, new tests on base code, pipeline probe, sandbox pages and actions, guard probes, upgrade, Docker; SKIPPED with the reason where not run>
```
