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
4. Optional: `[news].topics` in `profile.toml` for the news digest, and an Outlook sign-in for email updates (see News digest and Email updates below).

### Run

```bash
python hunt.py discover --limit 20             # suggests name,domain,why rows; logs them to discovered.csv
python hunt.py discover "fintech" --limit 20   # same, with an extra focus
python hunt.py run --research-only             # Exa only: no Claude, no Telegram
python hunt.py run --dry-run                   # research + score, print the full digest
python hunt.py run                             # research + score + send the notification to Telegram
python hunt.py send                            # send the latest saved run's notification to Telegram (no new research or scoring)
python hunt.py watch                           # send new growth-role postings from watched Ashby boards to Telegram
python hunt.py outlook-login                   # sign in to Outlook once (read-only), for `mail`
python hunt.py mail                            # rejections and interview invites in new email: Telegram + dashboard suggestions (Claude)
python hunt.py news                            # news digest on your [news].topics to Telegram (Exa + Claude)
python hunt.py interview-prep interviews/<id>  # prep brief for one interview (Claude; see Interviews)
python hunt.py interview-review interviews/<id> # review one interview's transcript (Claude; see Interviews)
python -m dashboard                            # dashboard at http://127.0.0.1:8000: results, your tracking, starting runs (see Dashboard)
```

Each run saves raw research and scores to `out/<timestamp>/<domain>.json`, plus `out/<timestamp>/_run.json` with the run's mode (`research_only`, `dry_run` or `deliver`), start and save times, company and error counts, the Exa cost summary and, for scored runs, the scoring model and Claude token totals. It also prints Exa cost and Claude token usage. A failing company is reported in the digest and does not abort the run (exit code 1 if any failed). Missing keys are detected before any paid call.

One `hunt.py` command runs at a time per project folder: each command holds a lock (`.hunt.lock`, git-ignored) while it runs, and a second one exits at once with "Another hunt.py command is running here". This covers commands from a shell, cron and the dashboard alike. A cron `watch` that starts during a run exits with that message and checks again at its next scheduled time. The OS releases the lock when a command exits, so a crashed command never leaves it stuck. `outlook-login` is the exception: it can wait up to 15 minutes for you to sign in in the browser and touches only `outlook_token.json`, so it does not hold the lock; it only refuses to start while another command runs.

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

### Email updates

`python hunt.py mail` reads new mail in your Outlook inbox and junk folder, finds rejections and interview invites, sends them to Telegram, and suggests the matching status changes on the dashboard's Pipeline page. You confirm or dismiss each suggestion there: email never changes a status by itself.

1. It reads mail received since the last check through Microsoft Graph with read-only access (`Mail.Read`), so it cannot send, move or delete mail. The first check reads `--days` back (default 14); later checks start an hour before the previous one, so mail that arrives late is not missed. One check reads at most 500 emails per folder, oldest first; when a folder (often Junk) holds more, the next check carries on from the newest one read, so nothing is skipped.
2. It keeps only email that looks like application email: sent through an applicant tracking system (Greenhouse, Lever, Ashby, Workable, Workday, SmartRecruiters, Teamtailor, Personio and others; `mail.ATS_DOMAINS`) or using application wording such as "your application", "interview", "unfortunately" or "other candidates" (`mail.KEYWORDS`). Mail from Outlook itself (`no-reply@microsoft.com`, which sends the calendar reminders and daily agendas such as "Today: Interview with …") is always skipped, since it only repeats interviews already on your calendar. Other email never leaves your machine.
3. Claude labels those emails (sender, subject, received time and the first 2,000 characters of the text, 20 emails per call) as rejected, interview or other, with the company, the role and a one-sentence summary. An interview invite includes confirming or moving an interview's time; reminders from other calendar apps count as other. Claude gets no tools and the prompt treats the emails as data: it only returns labels, and what a label leads to is fixed code. A crafted email can at worst earn a wrong label, which you see before confirming anything.
4. Rejections and interview invites are saved to `mail/<timestamp>.json` and sent to Telegram with a link to Pipeline. Every labelled email is remembered for 30 days in `mail_state.json`, so it is not paid for or sent twice. As with `watch`, the state is saved only after a successful send, so a failed send is retried on the next check. An email Claude returns no label for is read and sent again by the next check, at most 3 checks in all; then it is skipped with a warning in the log, so it cannot cost a Claude call on every scheduled check.

`--dry-run` labels and prints, without saving, sending or updating the state (the Claude call still costs). Cost: one Claude call per 20 matching emails, a few cents each, and nothing when no email matches; reading Outlook is free.

On **Pipeline**, "From your email" lists each update (subject, sender, Claude's summary, an Open in Outlook link) with the tracked applications it matches: items you marked saved or beyond whose company name matches the one in the email, or extends it at the end ("Acme" and "Acme Analytics", but not "Analytics Co"; suffixes like Inc or GmbH are ignored, and "SE Ranking" also matches `seranking.com`), or whose domain the email was sent from. Applications whose title shares words with the role in the email come first. **Mark rejected** or **Mark interviewing** sets that status on one application; **Dismiss** hides the update. An update never moves an application backwards (an interview invite does not touch one at offer), and it is hidden when the applications it matches already have that status. An update that matches nothing you track is shown with Dismiss only: set the status yourself if it is yours. Your answers are kept in `hunt.db`.

Setup, once:

1. Register an app in the [Microsoft Entra admin center](https://entra.microsoft.com) (**App registrations → New registration**). Any name; for an outlook.com or hotmail.com address choose an account type that includes personal Microsoft accounts; no redirect URI.
2. In the app: **Authentication → Allow public client flows: Yes** (the sign-in below is a device-code flow), and **API permissions → Add a permission → Microsoft Graph → Delegated → `Mail.Read`**.
3. Copy its **Application (client) ID** to `.env` as `OUTLOOK_CLIENT_ID`. If the registration accepts personal accounts only, also set `OUTLOOK_TENANT=consumers` (default `common`).
4. `python hunt.py outlook-login` prints a web address and a code. Open it, sign in and allow reading your mail. The sign-in is saved to `outlook_token.json` (git-ignored, readable only by you). Treat that file like a password: it gives read access to your mail. Each `mail` replaces it with the new token Microsoft returns. Microsoft ends the sign-in after 90 days without use, after a password change, or when you remove the app's access in your Microsoft account; then run `outlook-login` again.

On Railway, add `OUTLOOK_CLIENT_ID` (and `OUTLOOK_TENANT` if you set it) to the service variables, run `outlook-login` on the laptop and upload the file: `railway volume files upload outlook_token.json /outlook_token.json`. Run `mail` in one place only (as with `watch`), or both will send the same updates. The dashboard refuses to start a `mail` job until `outlook_token.json` exists.

### News digest

`python hunt.py news` searches Exa's news category for each `[news].topics` entry in `profile.toml` (articles published in the last `--days`, default 7; 8 results per topic) and drops articles already considered (`news_state.json`, kept 90 days). Claude picks at most 8 worth your time (developments in the job market for your target roles, and substantive news in your topics), with a one-sentence takeaway each and a short overview, and the digest goes to Telegram with the links. Claude gets your target roles, the topics and the article titles and excerpts, not your CV. Cost: about $0.007 per Exa search (one per topic) plus one Claude call, a few cents; no Claude call when nothing is new. `--dry-run` prints the digest without sending it or updating the state. A topic whose search fails is listed at the end of the message, and the command exits 1. Run it weekly, for example with `0 8 * * 1 news` in `HUNT_SCHEDULE` (see Deploy to Railway) or cron.

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
- **Pipeline** (`/pipeline`): everything you marked saved, applied, interviewing, offer or rejected, in columns, with your notes and applied date. Change a status there to move the card. Above the columns, "From your email" lists the rejections and interview invites `hunt.py mail` found, each with the applications it matches and a button to set the status (see Email updates).
- **Interviews** (`/interviews`): your interviews on a board by stage of preparation (needs prep, ready for the call, to review, reviewed), the watch-outs for your next interview, and progress across reviewed interviews. Each interview has its own page to open on your phone before and during the call. See Interviews below.
- **Projects** (`/projects`): side projects that build proof for your gaps, in idea, active, done and dropped columns. The scorer writes side-project ideas only for stretch roles (a role centred on one of your `[candidate].gaps`). Every idea from any saved run appears as a suggestion, newest run first, with the company, role, that run's score and verdict, and its age. Ideas from older runs stay even when a later run scores the company differently, because they are about your gap, not the company. **Add to ideas** turns one into a project, and **Dismiss** hides it. A handled idea is not suggested again, even if a later run repeats it with different case or spacing. **New project** adds your own, with a title, description, the gap it covers and a repository or write-up link. Deleting a project the scorer suggested hides it rather than letting it come back.
- **Leads** (`/leads`): companies from `discovered.csv`, marked new, queued (in `companies.csv`) or researched (with the latest score). Tick companies and press **Run selected…** to research and score just those (they do not need to be in `companies.csv`), or **Add to companies.csv** to append them to `companies.csv`, so every full run (scheduled ones too) covers them. Companies already listed are not added twice; removing one means editing `companies.csv`.
- **Runs** (`/runs`): start `hunt.py run` (dry run, run and notify, or research only) for every company in `companies.csv`, `discover` (focus, limit), `watch`, `mail` or `news` (each optionally dry run) or `send` (pick a saved scored run). Each opens a confirmation first. It shows the exact command, an estimated cost and side effects such as Telegram messages, file updates or what is sent to Claude; for a run you can switch the mode there.
  - **Schedule:** the commands in `HUNT_SCHEDULE` (see Deploy to Railway) with their next time and the job each last started, or why it could not start.
  - **Estimates:** Exa is the average cost per company of saved runs that recorded it (`_run.json`). Claude is the README figure of $0.27 per 19 companies. Discover is the README figure of $0.15–0.20.
  - **Job pages:** each job gets a live log page with a Stop button (which sends SIGTERM; paid calls already made still cost). When the job finishes, the page shows the saved run's verdict counts and cost.
  - **History:** the page lists jobs started from the dashboard (scheduled ones are labelled "Scheduled ·"), plus every saved run in `out/` (from the dashboard, a shell or cron) with its mode, verdict counts, errors, Exa cost and Claude tokens.
  - **Mechanics:** jobs run `python hunt.py …` in the data folder (`--root`) as a separate process, so they behave exactly like the CLI. Logs go to `jobs/<id>.log` and selected-company lists to `jobs/<id>-companies.csv` (both git-ignored). Only one job runs at a time (see the lock above). If the dashboard stops while a job runs, the job shows as interrupted after a restart. Ctrl+C in the dashboard's terminal stops the job too. A job that outlives the dashboard (for example after `kill` of the server process) keeps running and holds the lock until it finishes.

The dashboard copies the pipeline files into `hunt.db` (SQLite, git-ignored) and copies them again whenever one of them changes, so a finished run shows up on the next page load. A run directory that cannot be read (for example while a run is still writing it) is skipped with a warning banner and read again once it changes. Runs saved before `_run.json` existed show no mode or cost. `hunt.db` also holds your statuses, notes, side projects, job history and answers to email suggestions, which exist nowhere else: back it up if they matter, and do not delete it. The copied pipeline data is rebuilt automatically when needed; your own data is never touched by that.

### Interviews

Add an interview with **New interview…** on the Interviews page, or with **Prepare an interview…** in a researched company's details (which fills in the company and its best role). Give the company, role, stage (HR / recruiter screen, hiring manager, technical, final), date, interview language, and ideally the posting text: some job pages cannot be fetched, and the brief is only as specific as the posting. "What you want to emphasise" is passed on as is (for example a story to lead with, or your salary range).

- **Prep brief** (**Write prep…**, runs `hunt.py interview-prep`): one Claude call with your profile and CV, the posting (fetched from the posting URL through Exa, about $0.001, if you did not paste it), the company's saved research, and the findings of your last five reviewed interviews. The interview's page shows your positioning for this role, a 60–90-second "tell me about yourself" in the interview language, stories mapped to what the role needs, likely questions with answer points, questions to ask, how to handle salary, requirements against your experience, and gaps with honest ways to handle them. The brief's watch-outs (reminders drawn from your past reviews) are shown on the Interviews page, for your next interview that has a brief; without one, it shows the "next time" actions of your latest review. Claude is told to use only facts from your CV; where a number would help it writes `[add: …]` instead of inventing one. Rewriting replaces the brief.
- **Transcript**: upload a Wispr Flow meeting file (`~/Library/Application Support/Wispr Flow/meetings/<id>/refined.ndjson`; in the file picker press ⌘⇧G and paste the folder path) or paste text with one `Name: text` line per turn, optionally starting with `[mm:ss]`. Then choose which speaker is you. The page measures, for free: your share of the talking (by time when the transcript has timestamps, otherwise by words), the number and median length of your answers, your longest answer and when it started, and filler words per 100 words (English and Serbian lists; approximate, meant for tracking change). A one- to three-word reply from the other side ("okay") does not end your answer.
- **Review** (**Review the interview…**, runs `hunt.py interview-review`): one Claude call with the whole transcript, the measured numbers, the posting and the prep brief you had. It scores six things from 1 to 5 (structure, positioning for the role, evidence, confidence, career moves, questions asked), quotes what worked and what to improve with timestamps and what to say instead, and lists up to five things to do next time. Those "next time" items become the next prep brief's watch-outs. Uploading a new transcript marks the review as outdated until you review again.
- **Progress**: the Interviews page shows the latest value of each review score and of your talk share, longest answer and filler words, with a small bar per reviewed interview (oldest first) for the scores and talk share.

Each Claude call costs a few cents; the saved prep and review record the tokens used. Both send personal data to Anthropic: the prep sends your profile and CV, the review sends the whole transcript including the interviewer's words. Interviews live in `interviews/<id>/` (`interview.json`, `transcript.json`, `prep.json`, `review.json`; git-ignored and kept out of the Docker image). On Railway they are on the `/data` volume with your other files, so you can read the brief on your phone during the call.

### Deploy to Railway

Runs the dashboard on [Railway](https://railway.com), so you can open it from your phone and have it start `discover`, `run` (with its Telegram notification), `watch`, `mail` and `news` on a schedule. Railway builds the `Dockerfile`: the code lives in `/app`, and your data (the same files as in the project folder) on a volume mounted at `/data`. Railway bills the always-on service and the volume; scheduled commands cost the same Exa and Claude money as from the laptop.

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
   | `OUTLOOK_CLIENT_ID`, optionally `OUTLOOK_TENANT` | Only for email updates: as in your `.env` (see Email updates). |
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
   # optional: email updates (see Email updates), and their history
   railway volume files upload outlook_token.json /outlook_token.json
   railway volume files upload mail_state.json /mail_state.json
   railway volume files upload news_state.json /news_state.json
   ```

   `railway volume browse` opens an interactive browser for editing these files later (for example to remove a company from `companies.csv`). To keep your statuses, notes and side projects, stop the local dashboard, then `railway volume files upload --overwrite hunt.db /hunt.db` while no job runs on Railway, and restart the service (**Deployments → Restart**).
6. Open the domain, log in, and check the Runs page: the Schedule section lists what will run and when.

`HUNT_SCHEDULE` holds one entry per line, or entries separated by `;` (easier in Railway's variable editor): five cron fields (minute, hour, day of month, month, day of week with 0 or 7 for Sunday; numbers, `*`, ranges, lists and `*/n` steps), then a `hunt.py` command. Supported commands are the ones the Runs page starts: `run`, `run --dry-run`, `run --research-only`, `discover [focus] [--limit N]`, `watch`, `mail` and `news`, the last three also with `--dry-run`. For example, discovery every Monday at 07:00, a scored run with its Telegram notification every day at 07:30, the watch list every 6 hours, email every 2 hours from 08:00 to 20:00, and the news digest on Mondays at 08:30:

```
0 7 * * 1 discover --limit 15; 30 7 * * * run; 0 */6 * * * watch; 0 8-20/2 * * * mail; 30 8 * * 1 news
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
- **Chat with the job hunt over Telegram.** A Pydantic AI agent you message in Telegram ("what is waiting on a reply for more than two weeks?") that answers from `hunt.db` with read-only tools. Needs incoming Telegram messages (polling or a webhook, accepting only `TELEGRAM_CHAT_ID`) and a new pinned dependency.

## Files

| File | Purpose |
|---|---|
| `exa_client.py` | Exa search client |
| `requirements.txt` | Pinned dependencies (`exa-py`, `python-dotenv`, `anthropic`, `pydantic`, `requests`, and `fastapi`, `uvicorn`, `jinja2`, `python-multipart` for the dashboard) |
| `requirements-dev.txt` | Test dependencies (`pytest`, `responses`, `httpx` for FastAPI's `TestClient`) |
| `tests/` | Test suite; `tests/exa_mock.py` holds the Exa API mock |
| `.github/workflows/ci.yml` | GitHub Actions: run the tests on pull requests and pushes to `main` |
| `AGENTS.md` | Instructions for coding agents working in this repo |
| `.claude/skills/verify/` | `verify` skill: a separate agent tests and adversarially reviews each change before it ships (agents run it themselves; you can run `/verify <what the change should do>`); `scripts/sandbox.py` serves the dashboard on sample files with a fake `hunt.py` |
| `.claude/skills/visualize-changes/` | `/visualize-changes` skill, run only when you ask: an HTML report that explains the branch's changes (short descriptions, before/after, diagrams, old and new dashboard pages side by side, where the change lands; no diff), saved in `.change-reports/` (git-ignored) |
| `docs/specs/` | Specs handed to coding agents: `gtm-attribution-model.md` (public attribution, CAC and payback model) and `fee-verification-engine.md` (public payment fee verification engine on synthetic data) |
| `.env.example` | Template for required environment variables |
| `.env` | Local secrets (git-ignored) |
| `hunt.py` | Job-hunt CLI: `discover`, `run`, `send`, `watch`, `outlook-login`, `mail`, `news`, `interview-prep` and `interview-review` |
| `runs.py` | Saved-run format and the one-command-at-a-time lock, shared by `hunt.py` and the dashboard (`out/<timestamp>/`, `_run.json`, `.hunt.lock`) |
| `dashboard/` | Dashboard (`python -m dashboard`): FastAPI app, login, scheduler, SQLite store and ingest, status tracking, side projects, job runner, Jinja templates, CSS and a vendored copy of htmx 2.0.7 |
| `Dockerfile`, `.dockerignore` | Dashboard image for Railway; private files are kept out of it |
| `research.py` | Per-company Exa research (identity, signals, open roles) |
| `scoring.py` | Claude fit scoring (structured output) |
| `telegram.py` | Telegram delivery |
| `watch.py` | Watch list: poll job boards, report new matching postings |
| `ashby.py` | Ashby public job-board API client (used by link checks and the watch list) |
| `outlook.py` | Outlook through Microsoft Graph, read-only: device-code sign-in, token refresh, recent messages |
| `mail.py` | Email updates: which emails go to Claude, `mail_state.json`, the saved `mail/` files; shared by `hunt.py` and the dashboard |
| `mail_labels.py` | Claude labels application emails: rejected, interview or other (structured output) |
| `news.py` | News digest: Exa news search per topic, Claude's picks, `news_state.json` |
| `profile.example.toml` | Template for `profile.toml` (git-ignored) |
| `cv.example.md` | Template for `cv.md` (git-ignored) |
| `companies.example.csv` | Template for `companies.csv` (git-ignored) |
| `out/` | Raw per-run JSON results (git-ignored) |
| `discovered.csv` | History of every company `discover` has suggested (git-ignored) |
| `watch_state.json` | Postings the watch list has already reported (git-ignored) |
| `hunt.db` | Dashboard database: copies of the files above plus your statuses, notes, side projects, job history and answers to email suggestions (git-ignored; back it up) |
| `outlook_token.json` | Your Outlook sign-in, from `outlook-login` (git-ignored; gives read access to your mail) |
| `mail/` | Rejections and interview invites `mail` found, one file per check that found some (git-ignored) |
| `mail_state.json` | When mail was last checked and the emails already labelled (git-ignored) |
| `news_state.json` | Articles the news digest has already considered (git-ignored) |
| `jobs/` | Logs and company lists of jobs started from the dashboard (git-ignored) |
| `interviews/` | Your interviews: details, transcripts, prep briefs and reviews (git-ignored) |
| `interviews.py` | Interview files, transcript parsing and talk metrics, shared by `hunt.py` and the dashboard |
| `coaching.py` | Claude interview prep and transcript review (structured output) |
| `.hunt.lock` | Lock file that lets one `hunt.py` command run at a time (git-ignored) |
