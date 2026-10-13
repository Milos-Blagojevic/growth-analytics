# Everything Growth

Growth analytics tooling: a thin client over the [Exa](https://exa.ai) search API (raw retrieval, schema-grounded structured answers, content fetching) and a job-hunt pipeline built on it.

## Setup

Requires Python 3.11+ (`tomllib`), an Exa API key ([dashboard.exa.ai](https://dashboard.exa.ai)), and for the job-hunt pipeline an Anthropic API key and a Telegram bot.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set EXA_API_KEY in .env
```

Smoke test (searches "what is exa.ai" and prints 3 results with highlights):

```bash
python exa_client.py
```

## Usage

All functions live in `exa_client.py`.

```python
from exa_client import search, search_structured, contents, fast_search, deep_search

# Raw retrieval: results with query-relevant highlights
results = search("B2B SaaS onboarding benchmarks", num_results=5)
for r in results.results:
    print(r.title, r.url, r.highlights)

# Extra Exa options pass through kwargs
search("PLG pricing teardown", include_domains=["lennysnewsletter.com"], category="news")

# Override the contents view (default is {"highlights": True})
search("inference outage postmortems", num_results=5, contents={"text": {"max_characters": 10000}})

# Freshness: max_age_hours (not a publication-date filter)
search("serverless GPU pricing", max_age_hours=24)     # cached content must be < 24h old
search("current competitor pricing", max_age_hours=0)  # always fetch fresh

# Search-type wrappers
fast_search("who is hiring growth PMs", num_results=5)
deep_search("how are PLG companies pricing AI features", num_results=5)

# Synthesized search: grounded JSON in .output.content, citations in .output.grounding
schema = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
}
out = search_structured("What is product-led growth?", schema)
print(out.output.content, out.output.grounding)

# Fetch content for URLs you already have
page = contents(["https://exa.ai"])
```

### API

| Function | Purpose |
|---|---|
| `get_client()` | Returns an `Exa` client. Raises `RuntimeError` if `EXA_API_KEY` is unset. |
| `search(query, *, type="auto", num_results=10, contents=None, max_age_hours=None, **kwargs)` | Raw search results. `contents` defaults to highlights. |
| `search_structured(query, output_schema, *, type="auto", system_prompt=None, contents=None, max_age_hours=None, **kwargs)` | Synthesized, grounded JSON output. `contents` defaults to highlights. |
| `instant_search` / `fast_search` / `deep_lite_search` / `deep_search` / `deep_reasoning_search` `(query, **kwargs)` | `search()` with `type` preset (see below). |
| `contents(urls, *, max_age_hours=None, highlights=True, **kwargs)` | Page contents for given URLs: highlights only by default (full text only if `text` is passed). `highlights=False` with `text={...}` fetches full text only. |

`max_age_hours` controls content freshness, not publication date: `0` always fetches fresh, `-1` uses cache only, `N` uses cache if newer than `N` hours, `None` lets Exa decide. It overrides any `max_age_hours` inside `contents`.

| Wrapper | `type` | Approx. latency |
|---|---|---|
| `instant_search` | `instant` | ~250 ms |
| `fast_search` | `fast` | ~450 ms |
| `deep_lite_search` | `deep-lite` | ~4 s |
| `deep_search` | `deep` | 4-15 s |
| `deep_reasoning_search` | `deep-reasoning` | 12-40 s |

`search_structured` schema limits: max nesting depth 2, max 10 total properties. Don't add citation or confidence fields; grounding is returned automatically.

### Retries and cost logging

Every request made through the module functions goes through one internal path that:

- **Retries** transient failures with exponential backoff (jittered, capped): HTTP 408/429/500/502/503/504 and `requests` connection/timeout errors. Other errors (400s, invalid options) raise immediately. Tune via module constants `MAX_ATTEMPTS` (default 4 total tries), `BACKOFF_BASE_SECONDS` (1.0), `BACKOFF_MAX_SECONDS` (30.0) and `RETRYABLE_STATUS`.
- **Logs cost** at INFO on the `exa_client` logger, one line per request (`search:fast cost=$0.0070 search_time=450ms`), and retries at WARNING. Enable with `logging.basicConfig(level=logging.INFO)`.
- **Tallies cost** per label (`search:<type>`, `search_structured:<type>`, `contents`), using the `costDollars` Exa reports.

```python
from exa_client import cost_summary, reset_costs

cost_summary()
# {"requests": 3, "dollars": 0.021, "by_label": {"search:fast": {"requests": 2, "dollars": 0.014}, ...}}
reset_costs()
```

Notes: the tally is in-process only (not persisted) and counts successful requests; responses without cost data count as requests but add $0. The Exa SDK sets no HTTP timeout on sync requests, so a hung connection is never retried.

## Job-hunt pipeline

Researches companies with Exa, scores how well they and their open roles fit your profile with Claude, and sends a short notification to Telegram. A dashboard shows the full results. Runs locally, or on Railway with scheduled commands and a login so you can use it from your phone (see Deploy to Railway).

### Configure

1. Add keys to `.env`: `EXA_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (see `.env.example` for how to get the Telegram values).
2. `cp profile.example.toml profile.toml`, set `[candidate].location`, and point `cv_file` at your CV (`cp cv.example.md cv.md`, then edit; leave out contact details). The CV text is sent to the scoring model, never to Exa or Telegram. Without a CV or `experience`, fit is scored on role, seniority and location criteria only. `location` is used to judge whether "remote" roles are open to you (for example, US-only remote is not a fit). Optionally fill `gaps` (see Stretch below).
3. `cp companies.example.csv companies.csv` and list `name,domain` rows (the domain disambiguates similarly named companies).

### Run

```bash
python hunt.py discover --limit 20             # suggests name,domain,why rows; logs them to discovered.csv
python hunt.py discover "fintech" --limit 20   # same, with an extra focus
python hunt.py run --research-only             # Exa only: no Claude, no Telegram
python hunt.py run --dry-run                   # research + score, print the full digest
python hunt.py run                             # research + score + send the notification to Telegram
python hunt.py send                            # send the latest saved run's notification to Telegram (no new research or scoring)
python hunt.py watch                           # send new growth-role postings from watched Ashby boards to Telegram
python -m dashboard                            # dashboard at http://127.0.0.1:8000: results, your tracking, starting runs (see Dashboard)
```

Each run saves raw research and scores to `out/<timestamp>/<domain>.json`, plus `out/<timestamp>/_run.json` with the run's mode (`research_only`, `dry_run` or `deliver`), start and save times, company and error counts, the Exa cost summary and, for scored runs, the scoring model and Claude token totals. It also prints Exa cost and Claude token usage. A failing company is reported in the digest and does not abort the run (exit code 1 if any failed). Missing keys are detected before any paid call.

One `hunt.py` command runs at a time per project folder: each command holds a lock (`.hunt.lock`, git-ignored) while it runs, and a second one exits at once with "Another hunt.py command is running here". This covers commands from a shell, cron and the dashboard alike. A cron `watch` that starts during a run exits with that message and checks again at its next scheduled time. The OS releases the lock when a command exits, so a crashed command never leaves it stuck.

The Telegram notification is short: the digest's count header, one entry per apply, consider and stretch role (score, verdict, company, role title and posting URL, so it works away from the laptop), the names of failed companies, and a link to the dashboard (`DASHBOARD_URL` in `.env`; default `https://<RAILWAY_PUBLIC_DOMAIN>` on Railway, otherwise `http://127.0.0.1:8000`). Reasons, concerns, gap notes and side-project ideas are in the dashboard and in the `--dry-run` digest.

`send` rebuilds the notification from a saved run in `out/` (latest by default, or pass a directory). Use it after a `--dry-run` so what is delivered comes from the run you reviewed, without paying for a second run.

Discovery runs one search per target role plus one deep search (6 searches, roughly $0.15-0.20 for `--limit 20`) because a single search returns only a handful of companies. Results are merged round-robin, filtered (job boards and anonymous employers dropped, `[search].exclude_domains` removed) and appended to `discovered.csv` (`discovered_at,name,domain,why,focus`). Domains already in the history are skipped, so repeated runs only return new companies. The first three columns are in `companies.csv` format. The `why` column is Exa's unverified claim; `run` is what actually researches and scores each company.

Scoring model: `claude-sonnet-5-5` by default, override with `SCORING_MODEL` in `.env`.

Verdicts in the digest: **apply** (score 75+), **consider** (50-74), **stretch** (a role centred on one of your declared gaps; see below), **check careers** (no live role found, but the company scores 50+ on its own: look at its careers page), **skip** (below 50, identity not verified, or the posting excludes your location without relocation support). A live posting is required for apply/consider/stretch.

Stretch: list areas where your experience is thin in `[candidate].gaps` (for example growth engineering with about 6 months of experience). When a posting's core work falls in such an area, the digest shows **STRETCH** instead of apply, with a `Gap:` line stating what you have, up to two `Build:` side-project ideas that would build credible proof, and a score reduced for the gap. Roles outside the gaps are unaffected.

Weak spots and scope: `[candidate].weak_spots` lists specific tools you are weak in (for example PySpark). If a posting requires one, the digest adds it as a concern and gives one preparation step in `Next:`; unlike a gap, it does not change the verdict. When a posting's title and duties disagree (an analyst title with ETL/dbt work, say), the digest adds a `* Scope:` line saying which part of your background fits. The scorer reports compensation only as stated in the posting and never judges whether it is high or low.

Role search: the main Exa call looks for postings. If it finds none for a verified company, a second call restricted to ATS/job-board domains (Ashby, Greenhouse, Lever, Workable, SmartRecruiters, Recruitee, Teamtailor, Personio) runs, costing about $0.007 extra. Only postings Exa marks as belonging to the company are kept; `role_source` in `out/` records which call supplied them (`main`, `ats_fallback`, `none`). The research step only quotes what each posting says about remote regions and relocation; judging eligibility for your `location` is the scorer's job.

Link verification: Exa often returns stale posting URLs (about 30% were dead in one run, including top-scored roles), so every posting is checked before scoring and dead ones are dropped (recorded as `dead_roles` in `out/`). Ashby (`jobs.ashbyhq.com`) answers HTTP 200 for any URL, so its links are checked against Ashby's public posting API, which lists only open jobs. For other sites, 404/410 or a redirect away from the posting (a soft 404, e.g. Workable `?not_found=true`, Greenhouse `?error=true`) means dead. A URL that is only a board or careers homepage, or a check that is blocked or times out, is resolved with a fresh Exa fetch (about $0.001 per link); if that cannot confirm it either, the role is kept as `link_status: "unverified"` and the scorer tells you to confirm it. If every link for a company is dead, the ATS fallback search runs, and a company with no live role becomes "check careers". Not covered: sites that answer 200 for closed postings other than Ashby (none seen yet).

Posting text: for up to 3 live roles per company, the posting text (first 4,000 characters, about $0.001 per page) is fetched and given to the scorer, because search summaries rarely contain responsibilities and required tools. Without it most concerns read "responsibilities not in the evidence". Roles whose link is only unverified are not fetched.

Known limits: Exa is a search engine, not a jobs feed. Open-role coverage varies between runs, and "no roles found" does not mean no roles exist. Posting URLs may point to third-party job boards (for example a VC portfolio board) rather than the company's own ATS. Treat the digest as leads and confirm on the company's careers page.

Score noise: the scoring model does not accept a temperature setting (`temperature is deprecated for this model`), so the same research can score differently between runs: typically within a few points, occasionally more. Verdict thresholds (75, 50) and the gap rule make borderline companies flip (one company went from 68 stretch to 82 apply on identical input). Read scores as bands, not exact values.

### Watch list

`python hunt.py watch` polls the Ashby job boards in `[watch].ashby_boards` (the slug in `jobs.ashbyhq.com/<slug>`) through Ashby's free, keyless posting API, which lists only open postings. Postings whose title contains any `title_keywords` entry (case-insensitive) and none of `title_exclude` are sent to Telegram in one message with title, location and link. Already-reported postings are kept in `watch_state.json` (ID, title, location, remote flag, link and when first seen; entries saved by older versions have only the title and time), so each posting is sent once and the dashboard can list them; the first run reports everything currently open. State is saved only after a successful send, so a failed send is retried on the next run. `--dry-run` prints matches without sending or saving state. A board that cannot be read (typo, API error) is logged, its state is left unchanged, and the exit code is 1. No Exa or Claude calls, so it costs nothing.

Only Ashby boards are supported. To run it on a schedule while the laptop is on, add a cron entry (`crontab -e`), for example every 6 hours:

```
0 */6 * * * cd /path/to/growth-analytics && .venv/bin/python hunt.py watch >> watch.log 2>&1
```

### Dashboard

```bash
python -m dashboard              # http://127.0.0.1:8000
python -m dashboard --port 8001
python -m dashboard --root ~/hunt-data   # data folder other than the current directory
```

Run it from the project directory, or point `--root` at the folder holding `profile.toml`, `companies.csv`, `out/` and the other data files (jobs then run `hunt.py` with that folder as working directory). By default it listens on `127.0.0.1` only and has no login, so every request must name a loopback host. Every change (status, notes, projects, starting a job) must carry a token from the page and come from the dashboard's own origin, so another website open in your browser cannot start a paid run. If the dashboard restarts, open pages hold an old token: changes then show "This page is out of date… Reload it".

With `DASHBOARD_PASSWORD` set (at least 12 characters), every page asks for that password first, and a session lasts 30 days on that browser; **Log out** ends it. Changing the password logs every browser out. After 10 wrong passwords in 15 minutes, logins are refused for everyone, you included, until the 15 minutes pass. Serving beyond this machine (`--host` other than loopback) refuses to start without a password. Besides loopback, the dashboard accepts the host names in `RAILWAY_PUBLIC_DOMAIN` and `DASHBOARD_URL`.

Reading pages costs nothing. Only jobs you start on Runs, or with "Re-run" or "Run selected", and the commands in `HUNT_SCHEDULE` call Exa, Claude or Telegram. Jobs you start ask for confirmation first.

- **Board** (`/`): the latest scored result per company, in tabs for apply, consider, stretch, check careers and skip, with search (company, role, location and your notes), a status filter (dismissed cards are hidden unless you choose All or Dismissed), a minimum score and sorting. Each card shows the score, best role, location assessment, whether the posting link was confirmed live, and your status. A company whose latest run failed keeps its last scored result, and the board names the companies that failed in the most recent run. Click a card for the details: best role, your status and notes, next step, scope and gap notes, reasons and concerns, side-project ideas, score history across runs (with the 75 and 50 thresholds), open roles with link status, dropped dead links, company summary, evidence URLs, and a **Re-run** button that researches just that company. `/c/<domain>` opens the same details as a page.
- **Your status and notes**: set a status (new, saved, applied, interviewing, offer, rejected, dismissed) and free-text notes in a company's details, or a status for watch-list postings in the inbox. Notes save as you type. A role is tracked by its posting URL, so when a later run picks a different best role for a company, the card starts as new again. A company without a posting (check careers) is tracked by its domain. The first time an item reaches applied or later, the date is kept as "applied".
- **Inbox** (`/inbox`): apply, consider and stretch roles first scored in the last 7, 14 or 30 days or all time (a company counts again when its best role changes), and watch-list postings by when `watch` first reported them, each with a status menu.
- **Pipeline** (`/pipeline`): everything you marked saved, applied, interviewing, offer or rejected, in columns, with your notes and applied date. Change a status there to move the card.
- **Projects** (`/projects`): side projects that build proof for your gaps, in idea, active, done and dropped columns. The scorer writes side-project ideas only for stretch roles (a role centred on one of your `[candidate].gaps`). Every idea from any saved run appears as a suggestion, newest run first, with the company, role, that run's score and verdict, and its age. Ideas from older runs stay even when a later run scores the company differently, because they are about your gap, not the company. **Add to ideas** turns one into a project, and **Dismiss** hides it. A handled idea is not suggested again, even if a later run repeats it with different case or spacing. **New project** adds your own, with a title, description, the gap it covers and a repository or write-up link. Deleting a project the scorer suggested hides it rather than letting it come back.
- **Leads** (`/leads`): companies from `discovered.csv`, marked new, queued (in `companies.csv`) or researched (with the latest score). Tick companies and press **Run selected…** to research and score just those (they do not need to be in `companies.csv`), or **Add to companies.csv** to append them to `companies.csv`, so every full run (scheduled ones too) covers them. Companies already listed are not added twice; removing one means editing `companies.csv`.
- **Runs** (`/runs`): start `hunt.py run` (dry run, run and notify, or research only) for every company in `companies.csv`, `discover` (focus, limit), `watch` (optionally dry run) or `send` (pick a saved scored run). Each opens a confirmation first. It shows the exact command, an estimated cost and side effects such as Telegram messages or file updates; for a run you can switch the mode there.
  - **Schedule:** the commands in `HUNT_SCHEDULE` (see Deploy to Railway) with their next time and the job each last started, or why it could not start.
  - **Estimates:** Exa is the average cost per company of saved runs that recorded it (`_run.json`). Claude is the README figure of $0.27 per 19 companies. Discover is the README figure of $0.15–0.20.
  - **Job pages:** each job gets a live log page with a Stop button (which sends SIGTERM; paid calls already made still cost). When the job finishes, the page shows the saved run's verdict counts and cost.
  - **History:** the page lists jobs started from the dashboard (scheduled ones are labelled "Scheduled ·"), plus every saved run in `out/` (from the dashboard, a shell or cron) with its mode, verdict counts, errors, Exa cost and Claude tokens.
  - **Mechanics:** jobs run `python hunt.py …` in the data folder (`--root`) as a separate process, so they behave exactly like the CLI. Logs go to `jobs/<id>.log` and selected-company lists to `jobs/<id>-companies.csv` (both git-ignored). Only one job runs at a time (see the lock above). If the dashboard stops while a job runs, the job shows as interrupted after a restart. Ctrl+C in the dashboard's terminal stops the job too. A job that outlives the dashboard (for example after `kill` of the server process) keeps running and holds the lock until it finishes.

The dashboard copies the pipeline files into `hunt.db` (SQLite, git-ignored) and copies them again whenever one of them changes, so a finished run shows up on the next page load. A run directory that cannot be read (for example while a run is still writing it) is skipped with a warning banner and read again once it changes. Runs saved before `_run.json` existed show no mode or cost. `hunt.db` also holds your statuses, notes, side projects and job history, which exist nowhere else: back it up if they matter, and do not delete it. The copied pipeline data is rebuilt automatically when needed; your own data is never touched by that.

### Deploy to Railway

Runs the dashboard on [Railway](https://railway.com), so you can open it from your phone and have it start `discover`, `run` (with its Telegram notification) and `watch` on a schedule. Railway builds the `Dockerfile`: the code lives in `/app`, and your data (the same files as in the project folder) on a volume mounted at `/data`. Railway bills the always-on service and the volume; scheduled commands cost the same Exa and Claude money as from the laptop.

Railway's own cron services cannot share a volume with the dashboard, so the dashboard process starts the scheduled commands itself, as jobs on the Runs page with the same one-at-a-time lock, logs and history.

1. In Railway: **New project → Deploy from GitHub repo**, pick this repository.
2. Attach a volume to the service with mount path `/data` (right-click the service → **Attach volume**, or `railway volume add --mount-path /data`). Without it the service does not start (`--root /data is not a directory`), so a missing volume never silently loses your data on the next deploy.
3. Set the service variables:

   | Variable | Value |
   |---|---|
   | `DASHBOARD_PASSWORD` | Required: the login password, at least 12 characters. A random one: `python -c "import secrets; print(secrets.token_urlsafe(18))"`. Anyone with it can start paid runs, so keep it to yourself. |
   | `EXA_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optionally `SCORING_MODEL` | As in your `.env` (the container has no `.env`). |
   | `TZ` | Your time zone, for example `Europe/Belgrade`. Schedules and saved-run times use it; default UTC. |
   | `HUNT_SCHEDULE` | Optional: scheduled commands (below). |
   | `DASHBOARD_URL` | Only for a custom domain. Without it, Telegram links point to `https://<RAILWAY_PUBLIC_DOMAIN>`. |

4. **Settings → Networking → Generate domain.** Railway sets `RAILWAY_PUBLIC_DOMAIN`, which the dashboard accepts as its host name.
5. Upload your files to the volume with the [Railway CLI](https://docs.railway.com/cli/volume) (volume paths start at `/data`, so `/profile.toml` is `/data/profile.toml`):

   ```bash
   railway volume files upload profile.toml /profile.toml
   railway volume files upload cv.md /cv.md              # paths in profile.toml are relative to /data
   railway volume files upload companies.csv /companies.csv
   # optional: your history, so nothing is reported or suggested twice
   railway volume files upload discovered.csv /discovered.csv
   railway volume files upload watch_state.json /watch_state.json
   railway volume files upload out /out
   ```

   `railway volume browse` opens an interactive browser for editing these files later (for example to remove a company from `companies.csv`). To keep your statuses, notes and side projects, stop the local dashboard, then `railway volume files upload --overwrite hunt.db /hunt.db` while no job runs on Railway, and restart the service (**Deployments → Restart**).
6. Open the domain, log in, and check the Runs page: the Schedule section lists what will run and when.

`HUNT_SCHEDULE` holds one entry per line, or entries separated by `;` (easier in Railway's variable editor): five cron fields (minute, hour, day of month, month, day of week with 0 or 7 for Sunday; numbers, `*`, ranges, lists and `*/n` steps), then a `hunt.py` command. Supported commands are the ones the Runs page starts: `run`, `run --dry-run`, `run --research-only`, `discover [focus] [--limit N]`, `watch` and `watch --dry-run`. For example, discovery every Monday at 07:00, a scored run with its Telegram notification every day at 07:30, and the watch list every 6 hours:

```
0 7 * * 1 discover --limit 15; 30 7 * * * run; 0 */6 * * * watch
```

An entry that is not valid stops the dashboard from starting, with the reason in the deploy logs. A command that comes due while another one runs waits and starts as soon as that one finishes; one that cannot start (for example `companies.csv` is empty) shows the reason on the Runs page and in the logs. Commands due while the service is down (during a redeploy) are skipped, not caught up. Changing a variable redeploys the service, which interrupts a running job. `discover` only adds leads: use **Add to companies.csv** on Leads so the next scheduled `run` researches them.

Railway and a laptop do not share files: once Railway runs `watch` on a schedule, remove the laptop's cron entry, or both will send the same postings. Railway can [back up the volume](https://docs.railway.com/volumes/backups); `railway volume files download /hunt.db` copies your tracking data to the laptop.

To try the image on the laptop: `docker build -t hunt . && docker run -p 8000:8000 -e DASHBOARD_PASSWORD=… -v "$PWD":/data hunt`, then open `http://127.0.0.1:8000`. The container reads and writes this folder's data files, and its jobs see only the keys you pass with `-e`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests need no keys and make no network calls. Every HTTP request made through `requests` is intercepted with [`responses`](https://github.com/getsentry/responses); a request without a registered mock fails the test. Exa is mocked at the HTTP level with payloads in Exa's wire format (`tests/exa_mock.py`), so the real `exa-py` SDK builds the requests and parses the responses. Ashby, posting pages and Telegram are mocked the same way; Claude is replaced by a fake client. Dashboard tests write pipeline files to a temporary directory (`tests/dashboard_data.py`) and call the app in-process with FastAPI's `TestClient`; job tests replace `hunt.py` with a small stub script, so no paid command runs.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11 and 3.13 for every pull request and every push to `main`.

## Parked ideas

Not built yet; recorded so they can be picked up later.

- **Repeat scoring for borderline companies.** Because scores vary between runs (see Score noise), score companies whose first score lands near a verdict threshold (for example within 5 points of 75 or 50, or a stretch/apply split) two or three times and use the median score and the majority verdict. Costs roughly 2-3x the Claude spend for those companies only (a full 19-company run currently costs about $0.27 in Claude).

## Files

| File | Purpose |
|---|---|
| `exa_client.py` | Exa search client |
| `requirements.txt` | Pinned dependencies (`exa-py`, `python-dotenv`, `anthropic`, `pydantic`, `requests`, and `fastapi`, `uvicorn`, `jinja2`, `python-multipart` for the dashboard) |
| `requirements-dev.txt` | Test dependencies (`pytest`, `responses`, `httpx` for FastAPI's `TestClient`) |
| `tests/` | Test suite; `tests/exa_mock.py` holds the Exa API mock |
| `.github/workflows/ci.yml` | GitHub Actions: run the tests on pull requests and pushes to `main` |
| `AGENTS.md` | Instructions for coding agents working in this repo |
| `.env.example` | Template for required environment variables |
| `.env` | Local secrets (git-ignored) |
| `hunt.py` | Job-hunt CLI: `discover`, `run`, `send` and `watch` |
| `runs.py` | Saved-run format and the one-command-at-a-time lock, shared by `hunt.py` and the dashboard (`out/<timestamp>/`, `_run.json`, `.hunt.lock`) |
| `dashboard/` | Dashboard (`python -m dashboard`): FastAPI app, login, scheduler, SQLite store and ingest, status tracking, side projects, job runner, Jinja templates, CSS and a vendored copy of htmx 2.0.7 |
| `Dockerfile`, `.dockerignore` | Dashboard image for Railway; private files are kept out of it |
| `research.py` | Per-company Exa research (identity, signals, open roles) |
| `scoring.py` | Claude fit scoring (structured output) |
| `telegram.py` | Telegram delivery |
| `watch.py` | Watch list: poll job boards, report new matching postings |
| `ashby.py` | Ashby public job-board API client (used by link checks and the watch list) |
| `profile.example.toml` | Template for `profile.toml` (git-ignored) |
| `cv.example.md` | Template for `cv.md` (git-ignored) |
| `companies.example.csv` | Template for `companies.csv` (git-ignored) |
| `out/` | Raw per-run JSON results (git-ignored) |
| `discovered.csv` | History of every company `discover` has suggested (git-ignored) |
| `watch_state.json` | Postings the watch list has already reported (git-ignored) |
| `hunt.db` | Dashboard database: copies of the files above plus your statuses, notes, side projects and job history (git-ignored; back it up) |
| `jobs/` | Logs and company lists of jobs started from the dashboard (git-ignored) |
| `.hunt.lock` | Lock file that lets one `hunt.py` command run at a time (git-ignored) |
