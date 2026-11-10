# Everything Growth

Growth analytics tooling: a thin client over the [Exa](https://exa.ai) search API (raw retrieval, schema-grounded structured answers, content fetching), a job-hunt pipeline built on it, and a prospect pipeline for your own product (lead enrichment, value estimates and routing, kept in DuckDB; see [prospects/README.md](prospects/README.md)).

Researches companies with Exa, scores how well they and their open roles fit your profile with Claude, and sends a short notification to Telegram. A dashboard shows the full results. Runs locally, or on Railway with scheduled commands and a login so you can use it from your phone (see [Deploy to Railway](dashboard/README.md#deploy-to-railway)).

## Layout

| Path | Holds | Docs |
|---|---|---|
| `hunt.py` | The CLI entry point: `python hunt.py <command>` | [Commands](#commands) |
| `clients/` | Clients for the external services the features share: Exa, TypeSafe's Jev, Ashby job boards, Telegram | [clients/README.md](clients/README.md) |
| `pipeline/` | The job hunt: company research, Jev role screen and Claude fit scoring, watch list, news digest, saved-run format and lock | [pipeline/README.md](pipeline/README.md) |
| `inbox/` | Your mailboxes: browser sign-in, Outlook and Gmail APIs, email updates, the Telegram chat bot | [inbox/README.md](inbox/README.md) |
| `interview/` | Interview files and talk metrics, Claude prep briefs and transcript reviews | [interview/README.md](interview/README.md) |
| `prospects/` | Sales prospects for your own product: Exa research, Claude assessment, value range and routing rules, DuckDB warehouse | [prospects/README.md](prospects/README.md) |
| `dashboard/` | The dashboard app and its Railway deploy | [dashboard/README.md](dashboard/README.md) |
| `tests/` | The test suite and Verify in CI | [tests/README.md](tests/README.md) |
| `docs/` | Specs for coding agents and parked ideas | [docs/parked-ideas.md](docs/parked-ideas.md) |

## Setup

Requires Python 3.11+ (`tomllib`), an Exa API key ([dashboard.exa.ai](https://dashboard.exa.ai)), and for the job-hunt pipeline an Anthropic API key and a Telegram bot. A TypeSafe API key ([console.typesafe.ai](https://console.typesafe.ai/keys)) is optional: it lets the Jev decision model screen out clear misses before Claude (see [Jev screening](clients/README.md#jev-screening)).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set EXA_API_KEY in .env
```

Smoke test (searches "what is exa.ai" and prints 3 results with highlights):

```bash
python -m clients.exa_client
```

## Configure

1. Add keys to `.env`: `EXA_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (see `.env.example` for how to get the Telegram values), and optionally `TYPESAFE_API_KEY` (see [Jev screening](clients/README.md#jev-screening)).
2. `cp profile.example.toml profile.toml`. `[search].roles`, `seniority` and `work_mode` are required (a command exits with `[search].<key> is required` otherwise). Under `[candidate]`, set `location` and point `cv_file` at your CV (`cp cv.example.md cv.md`, then edit; leave out contact details). The CV text is sent to the scoring model, never to Exa or Telegram. Without a CV or `experience`, fit is scored on role, seniority and location criteria only. `location` is used to judge whether "remote" roles are open to you (for example, US-only remote is not a fit). `notes` (preferences or deal-breakers) and `weak_spots` are also sent to the scorer (see Weak spots in [Run](pipeline/README.md#run)); optionally fill `gaps` (see Stretch in [Run](pipeline/README.md#run)).
3. `cp companies.example.csv companies.csv` and list `name,domain` rows (the domain disambiguates similarly named companies). Domains are normalised as in `discover` and the dashboard: `https://`, `http://`, `www.` and any path after the host are stripped. A row whose domain still contains a `/` or has no `.` stops the run before any paid call, with the row named. A domain listed twice (case-insensitive) is researched once, with a warning. A file saved by Excel as "CSV UTF-8" (with a byte-order mark) is read as is.
4. Optional: `[news].topics` in `profile.toml` for the news digest, and an Outlook sign-in for email updates (see [News digest](pipeline/README.md#news-digest) and [Email updates](inbox/README.md#email-updates)). Fiber's jobs database as an extra source of open roles is chosen per run, not here: `run --fiber`, or **With Fiber** on the dashboard's Runs page (see Role search in [Run](pipeline/README.md#run); it costs more per company).
5. Optional, for the prospect pipeline: `cp prospects.example.toml prospects.toml` (your product, ideal customer, pricing and routing tiers) and `cp prospects.example.csv prospects.csv` (`name,domain` rows, read like `companies.csv`). See [Prospects](prospects/README.md#configure).

## Commands

`python hunt.py <command> --help` lists each command's options. "Paid calls" are the APIs a command pays for; Jev is used only with `TYPESAFE_API_KEY` set.

| Command | What it does | Paid calls | Telegram | Details |
|---|---|---|---|---|
| `python hunt.py discover` | Suggests new companies to research | Exa | No | [Run](pipeline/README.md#run) |
| `python hunt.py run` | Researches and scores every company in `companies.csv` | Exa, Jev, Claude (and an Exa agent run with Fiber per company with `--fiber`) | Yes, unless `--dry-run` or `--research-only` | [Run](pipeline/README.md#run) |
| `python hunt.py send` | Sends a saved run's notification again | No | Yes | [Run](pipeline/README.md#run) |
| `python hunt.py watch` | New postings from watched Ashby boards | No | Yes, unless `--dry-run` | [Watch list](pipeline/README.md#watch-list) |
| `python hunt.py outlook-login` | Signs in to Outlook once | No | No | [Email updates](inbox/README.md#email-updates) |
| `python hunt.py mail` | Rejections and interview invites in new email | Jev, Claude | Yes, unless `--dry-run` | [Email updates](inbox/README.md#email-updates) |
| `python hunt.py news` | News digest on your `[news].topics` | Exa, Jev, Claude | Yes, unless `--dry-run` | [News digest](pipeline/README.md#news-digest) |
| `python hunt.py interview-prep` | Prep brief for one interview | Claude (and Exa for the posting) | No | [Interviews](interview/README.md#interviews) |
| `python hunt.py interview-review` | Review of one interview's transcript | Claude | No | [Interviews](interview/README.md#interviews) |
| `python hunt.py gmail-login` | Signs in to Gmail once | No | No | [Chat with your mail](inbox/README.md#chat-with-your-mail) |
| `python hunt.py chat` | Telegram chat bot over your mail | Claude | Yes, it answers in Telegram | [Chat with your mail](inbox/README.md#chat-with-your-mail) |
| `python -m prospects discover` | Finds companies that match your ideal customer in Fiber's company database | Exa agent run and Fiber (about $0.75 a run so far; no cap) | No | [Prospects](prospects/README.md#discover) |
| `python -m prospects run` | Researches, assesses, rates and routes every company in `prospects.csv` | Exa, Claude (Exa only with `--research-only`) | No | [Prospects](prospects/README.md#run) |
| `python -m prospects load` | Rebuilds `prospects.duckdb` from the saved runs with the current `prospects.toml` and prints the ranking | No | No | [Prospects](prospects/README.md#the-warehouse) |
| `python -m dashboard` | Dashboard: results, tracking, starting jobs | Only the jobs you start | Only the jobs you start | [Dashboard](dashboard/README.md#dashboard) |
| `python -m pytest` | Test suite, no keys and no network | No | No | [Tests](tests/README.md#run-the-tests) |

## Files

| File | Purpose |
|---|---|
| `requirements.txt` | Pinned dependencies (`exa-py`, `python-dotenv`, `anthropic`, `pydantic`, `requests`, `pydantic-ai-slim[anthropic]` for the chat bot, `fastapi`, `uvicorn`, `jinja2`, `python-multipart` for the dashboard, and `duckdb` for the prospect warehouse) |
| `requirements-dev.txt` | Test dependencies (`pytest`, `responses`, `httpx` for FastAPI's `TestClient`, `PyYAML` for the Verify workflow test) |
| `pytest.ini` | pytest settings: test paths and options |
| `Dockerfile`, `.dockerignore` | Dashboard image for Railway; private files are kept out of it |
| `.github/workflows/ci.yml` | GitHub Actions: run the tests on pull requests and pushes to `main` |
| `.github/workflows/verify.yml` | GitHub Actions: the `verify` skill on a pull request labelled `verify` (see [Verify in CI](tests/README.md#verify-in-ci)) |
| `AGENTS.md` | Instructions for coding agents working in this repo |
| `.claude/skills/verify/` | `verify` skill: a separate agent tests and adversarially reviews each change before it ships (agents run it themselves; you can run `/verify <what the change should do>`); `scripts/sandbox.py` serves the dashboard on sample files with a fake `hunt.py`; `scripts/ci.py` runs the skill in CI (see [Verify in CI](tests/README.md#verify-in-ci)) |
| `.claude/skills/visualize-changes/` | `/visualize-changes` skill, run only when you ask: an HTML report that explains the branch's changes (short descriptions, before/after, diagrams, old and new dashboard pages side by side, where the change lands; no diff), saved in `.change-reports/` (git-ignored) |
| `docs/specs/` | Specs handed to coding agents: `gtm-attribution-model.md` (public attribution, CAC and payback model) and `fee-verification-engine.md` (public payment fee verification engine on synthetic data) |
| `docs/parked-ideas.md` | Ideas not built yet |
| `.env.example` | Template for required environment variables |
| `.env` | Local secrets (git-ignored) |
| `profile.example.toml` | Template for `profile.toml` (git-ignored) |
| `cv.example.md` | Template for `cv.md` (git-ignored) |
| `companies.example.csv` | Template for `companies.csv` (git-ignored) |
| `prospects.example.toml` | Template for `prospects.toml` (git-ignored) |
| `prospects.example.csv` | Template for `prospects.csv` (git-ignored) |
| `prospects_discovered.csv` | Every company `python -m prospects discover` has found, with Fiber's employee count and funding stage (git-ignored) |
| `out/` | Raw per-run JSON results (git-ignored) |
| `discovered.csv` | History of every company `discover` has suggested (git-ignored) |
| `watch_state.json` | Postings the watch list has already reported (git-ignored) |
| `hunt.db` | Dashboard database: copies of the files above plus your statuses, notes, side projects, job history and answers to email suggestions (git-ignored; back it up with the dashboard stopped, since its `hunt.db-wal` and `hunt.db-shm` sidecars hold the newest changes until a checkpoint) |
| `outlook_token.json` | Your Outlook sign-in, from `outlook-login` (git-ignored; gives read access to your mail and lets drafts be created) |
| `gmail_token.json` | Your Gmail sign-in, from `gmail-login` (git-ignored; same access) |
| `mail/` | Rejections and interview invites `mail` found, one file per check that found some (git-ignored) |
| `mail_state.json` | When mail was last checked and the emails already labelled (git-ignored) |
| `news_state.json` | Articles the news digest has already considered (git-ignored) |
| `jobs/` | Logs and company lists of jobs started from the dashboard (git-ignored) |
| `interviews/` | Your interviews: details, transcripts, prep briefs and reviews (git-ignored) |
| `prospect_runs/` | Raw per-run JSON of the prospect pipeline: research, assessment and costs (git-ignored) |
| `prospects.duckdb` | The prospect warehouse, rebuilt from `prospect_runs/` on every `run` and `load` (git-ignored; safe to delete) |
| `.hunt.lock` | Lock file that lets one `hunt.py` command run at a time (git-ignored) |
