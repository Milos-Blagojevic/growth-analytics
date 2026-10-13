# AGENTS.md

Python 3.11+ CLI tools: an Exa search client (`exa_client.py`), a job-hunt pipeline built on it (`hunt.py`) and a dashboard (`dashboard/`, run locally or on Railway) that shows the pipeline's saved files, keeps the owner's statuses, notes and side projects, and starts `hunt.py` commands, from buttons or on a schedule. `README.md` is the user documentation and describes behavior in detail; read the relevant section before changing a feature.

## Layout

| Module | Role |
|---|---|
| `hunt.py` | CLI entry point: `discover`, `run`, `send`, `watch` |
| `research.py` | Per-company Exa research, posting link checks, posting text fetch |
| `scoring.py` | Claude fit scoring: `SYSTEM_PROMPT` rubric plus the `Fit` pydantic model |
| `exa_client.py` | The only place that talks to Exa: retries, cost logging, cost tally |
| `ashby.py` | Ashby public job-board API (link checks and watch list) |
| `watch.py` | Watch list: new matching postings from Ashby boards |
| `telegram.py` | Telegram delivery |
| `runs.py` | Saved-run format (`out/<timestamp>/`, `_run.json` manifest) and the run lock (`.hunt.lock`), shared by `hunt.py` and the dashboard |
| `dashboard/` | `python -m dashboard`: entry point and environment settings (`__main__.py`), FastAPI app and request guard (`app.py`), password login (`auth.py`), `HUNT_SCHEDULE` parsing and scheduler (`schedule.py`), SQLite schema and user migrations (`db.py`), file ingest and `companies.csv` appends (`ingest.py`), page queries (`queries.py`), statuses and notes (`tracking.py`), side projects (`projects.py`), job runner and cost estimates (`jobs.py`), Jinja templates, CSS, vendored htmx |
| `Dockerfile` | Railway image: code in `/app`, data on the `/data` volume (`--root /data`) |

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest
```

CI runs the same suite on Python 3.11 and 3.13 for every pull request and push to `main`. Keep code 3.11-compatible.

Do not run these without asking: they spend money or send real messages.

- `python exa_client.py`, `hunt.py discover`, `hunt.py run` (any mode): paid Exa calls; `run` without `--research-only` also pays for Claude.
- `hunt.py run` without `--dry-run`, `hunt.py send`, `hunt.py watch` without `--dry-run`: send to Telegram. `watch` also updates `watch_state.json`.
- Deploying to Railway, or changing its service variables (a change redeploys and interrupts a running job).

`python -m dashboard` costs nothing by itself, but its Runs page, **Run selected**, **Re-run** and any `HUNT_SCHEDULE` entry start real `hunt.py` commands (paid, and possibly Telegram). For a smoke run, use a temporary directory of sample files with a fake `hunt.py` in it, never the owner's folder (with Docker, mount the fake over `/app/hunt.py`), keep `HUNT_SCHEDULE` to entries that start the fake, and do not start jobs against the real `hunt.py` without asking. Tests pass a stub through `create_app(root, hunt_command=[...])`.

## Git workspaces

Agents work in separate linked folders (git worktrees), each on its own branch. The main checkout (the first entry of `git worktree list`) owns `main` and holds the owner's private files.

- Never check out `main` in a linked folder. `git checkout main` fails while `main` is checked out elsewhere; do not force it with `--ignore-other-worktrees`.
- To get the latest `main` in a linked folder: `git fetch origin && git switch --detach origin/main`. Start new work with `git switch -c <branch> origin/main`.
- After a PR merges, update `main` only in the main checkout, and only when asked: `git -C <main checkout> pull --ff-only`.

## Private files

`.env`, `profile.toml`, `cv.md`, `companies.csv`, `discovered.csv`, `watch_state.json`, `watch.log`, `out/`, `hunt.db` (with its `hunt.db-wal` and `hunt.db-shm` files) and `jobs/` hold the owner's secrets and personal data. They are git-ignored and kept out of the Docker image (`.dockerignore`); never commit them, and do not read `.env`. On Railway the same files live on the `/data` volume and the keys are service variables. `hunt.db` holds the owner's statuses, notes and side projects, which exist nowhere else: never delete it or its `-wal` file (recent writes may still be there), locally or on the volume. When a config or CSV format changes, update the matching template (`.env.example`, `profile.example.toml`, `cv.example.md`, `companies.example.csv`) and the README.

## Conventions

- All Exa requests go through the `exa_client` module functions so retries and cost tracking apply. Never construct `Exa` elsewhere.
- Other HTTP goes through `requests` with an explicit timeout (existing calls use `timeout=15`). Tests intercept `requests` only, so another HTTP library would bypass the mocks.
- Exa structured output (`search_structured`) allows nesting depth 2 and at most 10 properties in total, array item properties included. `research.RESEARCH_SCHEMA` is at 10: adding a field means removing one.
- `scoring.SYSTEM_PROMPT` and the `Fit` model must agree. Verdict names also appear in `hunt.VERDICTS`, the digest code, `dashboard/queries.py` (`VERDICTS`, `NEW_VERDICTS`), `dashboard/app.py` (`VERDICT_LABELS`) and the verdict colours in `dashboard/static/app.css`; change them together.
- A change to what `hunt.py` saves (result JSON, `_run.json`, `watch_state.json`, `discovered.csv`) must keep `dashboard/ingest.py` reading both the new and the already-saved format, since `out/` keeps old runs.
- The dashboard does not import `hunt.py` or anything that loads `.env` or the paid clients; shared file-format code lives in `runs.py`.
- `hunt.db` has two kinds of tables (see `dashboard/db.py`). Derived tables are rebuilt from files by `ingest.py`; bump `db.SCHEMA_VERSION` when changing them. User tables (`tracking`, `side_projects`, `jobs`) are never dropped or cleared: change them only by appending a migration to `db.USER_MIGRATIONS`, and never edit an applied one.
- `hunt.main` holds `runs.lock()` for every command, so a shell, cron and the dashboard never run two commands at once. Call `cmd_*` functions directly only in tests.
- Every dashboard request must pass the guard in `app.py`: an allowed `Host` (loopback, plus `RAILWAY_PUBLIC_DOMAIN` and the host of `DASHBOARD_URL`) and, when `DASHBOARD_PASSWORD` is set, the session cookie (only `/login` and `/static/` are public). Every non-GET request also needs a same-origin `Origin` and the `X-CSRF-Token` header that `base.html` makes htmx send. New actions should be htmx requests from a page extending `base.html`; do not exempt routes from the guard. `python -m dashboard` refuses a non-loopback `--host` without a password; keep it that way.
- Dashboard jobs run `hunt.py` as a subprocess (`jobs.Runner`), never in-process: `cmd_*` functions call `sys.exit` and the `exa_client` cost tally is per process. Scheduled commands go through the same `jobs.build` validation and `Runner.start` as the Runs page, so a new schedulable command needs both a `jobs.build` kind and a `schedule._form` mapping.
- The dashboard reads its settings (`DASHBOARD_PASSWORD`, `HUNT_SCHEDULE`, `RAILWAY_PUBLIC_DOMAIN`, `DASHBOARD_URL`, `PORT`) from the environment in `__main__.py` and passes them to `create_app`; it never loads `.env`.
- The scoring model rejects `temperature`; do not add it.
- One failing company must not abort a run: `hunt.process_company` never raises and returns errors in its result.
- Dependencies are pinned in `requirements.txt` (runtime) and `requirements-dev.txt` (tests).

## Tests

- Tests need no keys and make no network calls. The autouse `http` fixture in `tests/conftest.py` fails any test that makes an unmocked request.
- Mock Exa at the HTTP level with the `exa` fixture (`tests/exa_mock.py`, Exa wire format), so the real SDK builds and parses requests. Do not patch `exa_client` functions.
- Mock Ashby, posting pages and Telegram through `responses`; use the `claude` fixture (`FakeClaude`) instead of the Anthropic client.
- New behavior gets a test in the matching `tests/test_<module>.py` (dashboard: `tests/test_dashboard_<module>.py`, with pipeline files built by `tests/dashboard_data.py` and requests made through FastAPI's `TestClient`).

## Docs

Update `README.md` in the same change when behavior, commands, config keys, costs or files change.
