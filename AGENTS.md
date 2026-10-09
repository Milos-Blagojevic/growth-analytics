# AGENTS.md

Python 3.11+ CLI tools: an Exa search client (`exa_client.py`), a job-hunt pipeline built on it (`hunt.py`) and a local read-only dashboard over the pipeline's saved files (`dashboard/`). `README.md` is the user documentation and describes behavior in detail; read the relevant section before changing a feature.

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
| `runs.py` | Saved-run format (`out/<timestamp>/`, `_run.json` manifest) shared by `hunt.py` and the dashboard |
| `dashboard/` | `python -m dashboard`: FastAPI app (`app.py`), SQLite schema (`db.py`), file ingest (`ingest.py`), page queries (`queries.py`), Jinja templates, CSS, vendored htmx |

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest
```

CI runs the same suite on Python 3.11 and 3.13 for every pull request and push to `main`. Keep code 3.11-compatible.

Do not run these without asking: they spend money or send real messages.

- `python exa_client.py`, `hunt.py discover`, `hunt.py run` (any mode): paid Exa calls; `run` without `--research-only` also pays for Claude.
- `hunt.py run` without `--dry-run`, `hunt.py send`, `hunt.py watch` without `--dry-run`: send to Telegram. `watch` also updates `watch_state.json`.

`python -m dashboard` is free and read-only; for a smoke run, point it at a temporary directory of sample files instead of the owner's data.

## Git workspaces

Agents work in separate linked folders (git worktrees), each on its own branch. The main checkout (the first entry of `git worktree list`) owns `main` and holds the owner's private files.

- Never check out `main` in a linked folder. `git checkout main` fails while `main` is checked out elsewhere; do not force it with `--ignore-other-worktrees`.
- To get the latest `main` in a linked folder: `git fetch origin && git switch --detach origin/main`. Start new work with `git switch -c <branch> origin/main`.
- After a PR merges, update `main` only in the main checkout, and only when asked: `git -C <main checkout> pull --ff-only`.

## Private files

`.env`, `profile.toml`, `cv.md`, `companies.csv`, `discovered.csv`, `watch_state.json`, `watch.log`, `out/` and `hunt.db` hold the owner's secrets and personal data. They are git-ignored; never commit them, and do not read `.env`. When a config or CSV format changes, update the matching template (`.env.example`, `profile.example.toml`, `cv.example.md`, `companies.example.csv`) and the README.

## Conventions

- All Exa requests go through the `exa_client` module functions so retries and cost tracking apply. Never construct `Exa` elsewhere.
- Other HTTP goes through `requests` with an explicit timeout (existing calls use `timeout=15`). Tests intercept `requests` only, so another HTTP library would bypass the mocks.
- Exa structured output (`search_structured`) allows nesting depth 2 and at most 10 properties in total, array item properties included. `research.RESEARCH_SCHEMA` is at 10: adding a field means removing one.
- `scoring.SYSTEM_PROMPT` and the `Fit` model must agree. Verdict names also appear in `hunt.VERDICTS`, the digest code, `dashboard/queries.py` (`VERDICTS`, `NEW_VERDICTS`), `dashboard/app.py` (`VERDICT_LABELS`) and the verdict colours in `dashboard/static/app.css`; change them together.
- A change to what `hunt.py` saves (result JSON, `_run.json`, `watch_state.json`, `discovered.csv`) must keep `dashboard/ingest.py` reading both the new and the already-saved format, since `out/` keeps old runs.
- The dashboard does not import `hunt.py` or anything that loads `.env` or the paid clients; shared file-format code lives in `runs.py`.
- Every `hunt.db` table is rebuilt from files by `ingest.py`; bump `db.SCHEMA_VERSION` when changing the schema.
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
