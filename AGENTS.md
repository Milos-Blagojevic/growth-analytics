# AGENTS.md

Python 3.11+ CLI tools: an Exa search client (`exa_client.py`), a job-hunt pipeline built on it (`hunt.py`) and a dashboard (`dashboard/`, run locally or on Railway) that shows the pipeline's saved files, keeps the owner's statuses, notes and side projects, and starts `hunt.py` commands, from buttons or on a schedule, and keeps the Telegram chat bot (`hunt.py chat`, a Pydantic AI agent over the owner's Outlook and Gmail mail) running on Railway. `README.md` is the user documentation and describes behavior in detail; read the relevant section before changing a feature.

## Layout

| Module | Role |
|---|---|
| `hunt.py` | CLI entry point: `discover`, `run`, `send`, `watch`, `outlook-login`, `mail`, `news`, `interview-prep`, `interview-review`, `gmail-login`, `chat` |
| `research.py` | Per-company Exa research, posting link checks, posting text fetch |
| `scoring.py` | Jev role screen (`screen_roles`, thresholds `SCREEN_YES`, `SCREEN_NO`, `BELOW_SENIOR_MIN`) and Claude fit scoring: `SYSTEM_PROMPT` rubric plus the `Fit` pydantic model; `candidate()` is what Claude is told about the owner |
| `coaching.py` | Claude interview prep (`Prep`) and transcript review (`Review`, scores keyed by `interviews.SCORES`) |
| `interviews.py` | Interview files (`interviews/<id>/`), transcript parsing and talk metrics; shared by `hunt.py` and the dashboard, no paid clients |
| `exa_client.py` | The only place that talks to Exa: retries, cost logging, cost tally |
| `jev.py` | The only place that talks to TypeSafe's Jev decision model: retries, usage tally, cost per token; optional (`TYPESAFE_API_KEY`) |
| `ashby.py` | Ashby public job-board API (link checks and watch list) |
| `watch.py` | Watch list: new matching postings from Ashby boards |
| `outlook.py` | Outlook through Microsoft Graph: browser sign-in (consents to `Mail.ReadWrite`, never `Mail.Send`; the registration's desktop redirect is `http://localhost`), refresh token in `outlook_token.json`, recent messages for `mail` (read-only tokens, `READ_SCOPE`), and search, full text and drafts for `chat`; never sends |
| `oauth.py` | The browser sign-in both providers share (`browser_code`): authorization code with a loopback redirect on 127.0.0.1; Google blocks Gmail scopes in the device-code flow and Microsoft's device-code consent fails for some accounts |
| `gmail.py` | Gmail through the Gmail REST API: browser sign-in (`oauth.py`, `gmail.readonly` + `gmail.compose`), refresh token in `gmail_token.json`, search, full text and drafts for `chat`; never sends |
| `chat.py` | The Telegram chat bot: Pydantic AI agent on the scoring model, tools over `outlook`/`gmail` (`list_emails`, `read_email`, `draft_email`, `draft_reply`), the in-memory conversation (`Conversation`, `trim`) and the `getUpdates` loop (`serve`) |
| `mail.py` | Email updates: the local filter for what reaches Jev and Claude, `mail_state.json`, `mail/<timestamp>.json` files; shared by `hunt.py` and the dashboard, no paid clients |
| `mail_labels.py` | Jev drops clear "other" emails (`screen`), then Claude labels the rest (`Label`: rejected, interview, other) |
| `news.py` | News digest: Exa news search per `[news].topics` entry, Jev relevance screen (`screen`), Claude picks (`Digest`), `news_state.json` |
| `telegram.py` | Telegram delivery (`send`) and `getUpdates` long polling for the chat bot (`updates`) |
| `runs.py` | Saved-run format (`out/<timestamp>/`, `_run.json` manifest), the domain normaliser (`normalize_domain`: domains name the result files, so `companies.csv`, `discover` and the dashboard spell them the same way) and the run lock (`.hunt.lock`), shared by `hunt.py` and the dashboard |
| `dashboard/` | `python -m dashboard`: entry point and environment settings (`__main__.py`), FastAPI app and request guard (`app.py`), password login (`auth.py`), `HUNT_SCHEDULE` parsing and scheduler (`schedule.py`), SQLite schema and user migrations (`db.py`), file ingest and `companies.csv` appends (`ingest.py`), page queries (`queries.py`), statuses and notes (`tracking.py`), status suggestions from email updates (`email_updates.py`), side projects (`projects.py`), job runner and cost estimates (`jobs.py`), the chat-bot supervisor (`bot.py`, `CHAT_BOT=1`), Jinja templates, CSS, vendored htmx |
| `Dockerfile` | Railway image: code in `/app`, data on the `/data` volume (`--root /data`) |

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest
```

CI runs the same suite on Python 3.11 and 3.13 for every pull request and push to `main`. Keep code 3.11-compatible. The separate Verify workflow runs the `verify` skill on a pull request when it gets the `verify` label (see Skills).

Do not run these without asking: they spend money or send real messages.

- `python exa_client.py`, `hunt.py discover`, `hunt.py run` (any mode): paid Exa calls; `run` without `--research-only` also pays for Claude and, when `TYPESAFE_API_KEY` is set, Jev.
- `hunt.py run` without `--dry-run`, `hunt.py send`, `hunt.py watch` without `--dry-run`: send to Telegram. `watch` also updates `watch_state.json`.
- `hunt.py interview-prep` and `hunt.py interview-review`: paid Claude calls that send the owner's CV or a whole interview transcript to Anthropic.
- `hunt.py mail` (any mode): reads the owner's mailbox and sends matching emails to Jev (when `TYPESAFE_API_KEY` is set) and Claude; without `--dry-run` it also sends to Telegram and updates `mail_state.json`. `hunt.py outlook-login` signs in to the owner's Microsoft account; `hunt.py gmail-login` to their Google account.
- `hunt.py chat`: polls the owner's Telegram bot (two pollers on one token interrupt each other) and answers with paid Claude calls that read the owner's mail and can create drafts in it.
- `hunt.py news` (any mode): paid Exa, Jev and Claude calls; without `--dry-run` it sends to Telegram and updates `news_state.json`.
- `.claude/skills/verify/scripts/ci.py`, and the Verify workflow (adding the `verify` label to a pull request, or starting it from the Actions tab): a paid Claude session of up to `--max-requests` model requests.
- Deploying to Railway, or changing its service variables (a change redeploys and interrupts a running job).

`python -m dashboard` costs nothing by itself, but its Runs page, **Run selected**, **Re-run** and any `HUNT_SCHEDULE` entry start real `hunt.py` commands (paid, and possibly Telegram): always the `hunt.py` next to the `dashboard` package, whatever `--root` is. For a local smoke run, serve a temporary folder of sample files with `.claude/skills/verify/scripts/sandbox.py`, which runs a fake `hunt.py` for every job and `HUNT_SCHEDULE` entry (and, with `--chat-bot`, for the chat bot); never use the owner's folder. With Docker, mount a fake over `/app/hunt.py`. Do not start jobs against the real `hunt.py` without asking. Tests pass a stub through `create_app(root, hunt_command=[...])`.

## Git workspaces

Agents work in separate linked folders (git worktrees), each on its own branch. The main checkout (the first entry of `git worktree list`) owns `main` and holds the owner's private files.

- Never check out `main` in a linked folder. `git checkout main` fails while `main` is checked out elsewhere; do not force it with `--ignore-other-worktrees`.
- To get the latest `main` in a linked folder: `git fetch origin && git switch --detach origin/main`. Start new work with `git switch -c <branch> origin/main`.
- After a PR merges, update `main` only in the main checkout, and only when asked: `git -C <main checkout> pull --ff-only`.

## Private files

`.env`, `profile.toml`, `cv.md`, `companies.csv`, `discovered.csv`, `watch_state.json`, `watch.log`, `out/`, `hunt.db`, `jobs/`, `interviews/`, `outlook_token.json`, `gmail_token.json`, `mail/`, `mail_state.json` and `news_state.json` hold the owner's secrets and personal data (interview transcripts also hold interviewers' words; `mail/` holds email subjects and senders; `outlook_token.json` and `gmail_token.json` give read access to the owner's mail and let drafts be created in it). They are git-ignored and kept out of the Docker image (`.dockerignore`); never commit them, and do not read `.env`, `outlook_token.json` or `gmail_token.json`. The ignore entries are globs (`hunt.db*`, `*_state.json*`, `outlook_token.json*`, `gmail_token.json*`) so that SQLite's WAL sidecars and the `.tmp` files the state writers replace also stay out. On Railway the same files live on the `/data` volume and the keys are service variables. `hunt.db` holds the owner's statuses, notes and side projects, which exist nowhere else: never delete it, locally or on the volume. The dashboard keeps it in WAL mode, so `hunt.db-wal` and `hunt.db-shm` next to it hold the latest changes until a checkpoint: copy or back it up only while the dashboard is stopped, or the copy misses them. When a config or CSV format changes, update the matching template (`.env.example`, `profile.example.toml`, `cv.example.md`, `companies.example.csv`) and the README.

## Conventions

- All Exa requests go through the `exa_client` module functions so retries and cost tracking apply. Never construct `Exa` elsewhere.
- All TypeSafe requests go through `jev.ask` so retries and the usage tally apply. Jev is a screen in front of Claude: it only drops items it is confident about, anything uncertain or any Jev failure goes on to Claude, and a missing `TYPESAFE_API_KEY` means no screen. Keep thresholds as named constants and keep the scoring screen's hard rules in step with `scoring.SYSTEM_PROMPT`.
- Other HTTP goes through `requests` with an explicit timeout (15 s; Telegram uses 30 s). The Exa SDK gets its timeout from `exa_client` (`TIMEOUT_SECONDS`). Tests intercept `requests` only, so another HTTP library would bypass the mocks.
- Exa structured output (`search_structured`) allows nesting depth 2 and at most 10 properties in total, array item properties included. `research.RESEARCH_SCHEMA` is at 10: adding a field means removing one.
- `scoring.SYSTEM_PROMPT` and the `Fit` model must agree. Verdict names also appear in `hunt.VERDICTS`, the digest code, `dashboard/queries.py` (`VERDICTS`, `NEW_VERDICTS`), `dashboard/app.py` (`VERDICT_LABELS`) and the verdict colours in `dashboard/static/app.css`; change them together.
- A change to what `hunt.py` saves (result JSON, `_run.json`, `watch_state.json`, `discovered.csv`, `mail/` files) must keep `dashboard/ingest.py` reading both the new and the already-saved format, since `out/` and `mail/` keep old files.
- Email text is untrusted input. `mail_labels` gives Jev and Claude no tools and only reads probabilities and labels back, and email never changes a status by itself: `hunt.py mail` saves suggestions that the owner confirms on Pipeline (`email_updates.decide`). Keep both. `mail` asks for read-only access tokens (`outlook.READ_SCOPE`) even though the sign-in consents to `Mail.ReadWrite` for `chat`; never request `Mail.Send` or a Gmail scope beyond `gmail.compose`, and never add a tool that sends mail. The chat agent (`chat.py`) reads untrusted email with tools: its tools only read and create drafts, tool errors go back to the model as text, every answer is capped at `chat.MAX_REQUESTS` model calls, and the log never carries message, email or draft text.
- The dashboard does not import `hunt.py` or anything that loads `.env` or the paid clients; shared file-format code lives in `runs.py`, `interviews.py` and `mail.py`.
- `hunt.db` has two kinds of tables (see `dashboard/db.py`). Derived tables are rebuilt from files by `ingest.py`; bump `db.SCHEMA_VERSION` when changing them. User tables (`tracking`, `side_projects`, `jobs`, `mail_decisions`) are never dropped or cleared: change them only by appending a migration to `db.USER_MIGRATIONS`, and never edit an applied one.
- `hunt.main` holds `runs.lock()` for every command except the interactive `outlook-login` (which writes only `outlook_token.json` and refuses to start while the lock is held), `gmail-login` (writes only `gmail_token.json`, which nothing else rewrites) and `chat` (long-lived; it must not hold the lock or no other command could run while it does), so a shell, cron and the dashboard never run two commands at once. Call `cmd_*` functions directly only in tests.
- Every dashboard request must pass the guard in `app.py`: an allowed `Host` (loopback, plus `RAILWAY_PUBLIC_DOMAIN` and the host of `DASHBOARD_URL`) and, when `DASHBOARD_PASSWORD` is set, the session cookie (only `/login` and `/static/` are public). Every non-GET request also needs a same-origin `Origin` and the `X-CSRF-Token` header that `base.html` makes htmx send. New actions should be htmx requests from a page extending `base.html`; do not exempt routes from the guard. `python -m dashboard` refuses a non-loopback `--host` without a password; keep it that way.
- Dashboard jobs run `hunt.py` as a subprocess (`jobs.Runner`), never in-process: `cmd_*` functions call `sys.exit` and the `exa_client` cost tally is per process. Scheduled commands go through the same `jobs.build` validation and `Runner.start` as the Runs page, so a new schedulable command needs both a `jobs.build` kind and a `schedule._form` mapping.
- The chat bot on Railway is `dashboard/bot.py`: with `CHAT_BOT=1` the dashboard runs `hunt.py chat` as a supervised child (restart with backoff, stop at shutdown, output in `jobs/chat.log`), not as a job: no lock, no `jobs` row, `Runner.busy()` unaffected.
- The dashboard reads its settings (`DASHBOARD_PASSWORD`, `HUNT_SCHEDULE`, `CHAT_BOT`, `RAILWAY_PUBLIC_DOMAIN`, `DASHBOARD_URL`, `PORT`) from the environment in `__main__.py` and passes them to `create_app`; it never loads `.env`.
- The scoring model rejects `temperature`; do not add it.
- One failing company must not abort a run: `hunt.process_company` never raises and returns errors in its result.
- Dependencies are pinned in `requirements.txt` (runtime) and `requirements-dev.txt` (tests). `.claude/skills/verify/scripts/ci.py` pins its own in its inline script metadata (run with `uv run`): `pydantic-ai-harness` needs a newer `pydantic-ai-slim` than `chat.py` runs on.

## Tests

- Tests need no keys and make no network calls. The autouse `http` fixture in `tests/conftest.py` fails any test that makes an unmocked request.
- Mock Exa at the HTTP level with the `exa` fixture (`tests/exa_mock.py`, Exa wire format), so the real SDK builds and parses requests. Do not patch `exa_client` functions.
- Mock Ashby, posting pages, Telegram, Microsoft sign-in and Graph through `responses`; mock TypeSafe with the `jev_api` fixture (`tests/jev_mock.py`, its wire format); use the `claude` fixture (`FakeClaude`) instead of the Anthropic client (`fake_claude` for output models other than `Fit`). Microsoft Graph and the Gmail API are mocked through `responses` too; the chat agent runs on a scripted pydantic-ai `FunctionModel` (`tests/test_chat.py::scripted`), never on a real model; browser sign-ins are driven by `tests/browser_login.py`, which plays the redirect against the loopback server with `urllib` (outside the `responses` mock).
- New behavior gets a test in the matching `tests/test_<module>.py` (dashboard: `tests/test_dashboard_<module>.py`, with pipeline files built by `tests/dashboard_data.py` and requests made through FastAPI's `TestClient`).

## Skills

Before reporting a feature or behavior change done, committing it or opening a PR, have it checked by an agent that did not write it: the `verify` skill (`.claude/skills/verify/SKILL.md`). It runs the suite and free end-to-end smoke runs, reviews the diff adversarially and returns risks ranked by production impact with a verdict. In Claude Code it runs in a fresh subagent (`context: fork`); pass it what the change should do. Elsewhere, start a new subagent or session with none of the implementation history and give it the skill's body as its prompt, with that intent in place of `$ARGUMENTS`. On BLOCK or FIX BEFORE MERGE, fix the findings and verify again. README-only and test-only changes skip it. The owner can run it too: `/verify <what the change should do>`.

The Verify workflow (`.github/workflows/verify.yml`) runs the same skill in CI: `.claude/skills/verify/scripts/ci.py` gives SKILL.md, with the pull request's title and description as `$ARGUMENTS`, to a Pydantic AI `Coder` agent working on a copy of the branch in a Docker container without network access, and fails the check on BLOCK or FIX BEFORE MERGE. The copy never holds files that HEAD lacks and that the working tree's ignore rules or the base commit's root `.gitignore` ignore (`ci.stage`), so keep private files git-ignored in the root `.gitignore`. SKILL.md has to keep working there: paths relative to the repo root, `$ARGUMENTS` in the body, and a step 7 report whose columns match `ci.Report` (change them together).

`visualize-changes` (`.claude/skills/visualize-changes/SKILL.md`) builds an HTML report of the branch's changes in the git-ignored `.change-reports/`: short descriptions, before/after behavior, diagrams, old and new dashboard pages side by side, and a map of where the change lands, without a diff. Run it only when the owner asks for it.

## Docs

Update `README.md` in the same change when behavior, commands, config keys, costs or files change.
