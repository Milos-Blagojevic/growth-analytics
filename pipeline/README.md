# Job-hunt pipeline

The job hunt itself: finding companies, researching them with Exa, screening roles with Jev and scoring fit with Claude, the Ashby watch list and the news digest. `hunt.py` at the repo root runs all of it.

## Files

| File | Purpose |
|---|---|
| `research.py` | Per-company Exa research (identity, signals, open roles) |
| `scoring.py` | Jev role screen and Claude fit scoring (structured output) |
| `watch.py` | Watch list: poll job boards, report new matching postings |
| `news.py` | News digest: Exa news search per topic, Jev's relevance screen, Claude's picks, `news_state.json` |
| `runs.py` | Saved-run format, the domain normaliser and the one-command-at-a-time lock, shared by `hunt.py` and the dashboard (`out/<timestamp>/`, `_run.json`, `.hunt.lock`) |

## Run

```bash
python hunt.py discover --limit 20             # suggests name,domain,why rows; logs them to discovered.csv
python hunt.py discover "fintech" --limit 20   # same, with an extra focus
python hunt.py run --research-only             # Exa only: no Claude, no Telegram
python hunt.py run --dry-run                   # research + score, print the full digest
python hunt.py run                             # research + score + send the notification to Telegram
python hunt.py send                            # send the latest saved run's notification to Telegram (no new research or scoring)
python hunt.py watch                           # send new growth-role postings from watched Ashby boards to Telegram
python hunt.py outlook-login                   # sign in to Outlook once (read, create drafts), for `mail` and `chat`
python hunt.py mail                            # rejections and interview invites in new email: Telegram + dashboard suggestions (Claude)
python hunt.py news                            # news digest on your [news].topics to Telegram (Exa + Claude)
python hunt.py interview-prep interviews/<id>  # prep brief for one interview (Claude; see interview/README.md)
python hunt.py interview-review interviews/<id> # review one interview's transcript (Claude; see interview/README.md)
python hunt.py gmail-login                     # sign in to Gmail once (read, create drafts), for `chat`
python hunt.py chat                            # Telegram chat bot over your mail: summaries, search, reply drafts (Claude; see inbox/README.md)
python -m dashboard                            # dashboard at http://127.0.0.1:8000: results, your tracking, starting runs (see dashboard/README.md)
```

Options (`python hunt.py <command> --help` lists them): `--profile FILE` before the command reads another profile (default `profile.toml`); `run --companies FILE` (default `companies.csv`); `discover --limit N` (default 15) and `--history FILE` (default `discovered.csv`); `send [DIR]` (a directory under `out/`, default the latest run); `watch --state FILE` (default `watch_state.json`); `outlook-login --token FILE` and `mail --token FILE` (default `outlook_token.json`); `mail --state FILE` (default `mail_state.json`) and `--days N` (default 14); `news --state FILE` (default `news_state.json`) and `--days N` (default 7); `gmail-login --token FILE` (default `gmail_token.json`); `chat --outlook-token FILE` and `--gmail-token FILE` (the two sign-in files). `run`, `watch`, `mail` and `news` take `--dry-run`.

Each run saves raw research and scores to `out/<timestamp>/<domain>.json` (with the Jev screen of each role in `screen`, see [Jev screening](../clients/README.md#jev-screening), and any research warnings, such as a failed ATS fallback search, in `warnings`), plus `out/<timestamp>/_run.json` with the run's mode (`research_only`, `dry_run` or `deliver`), start and save times, company and error counts, the Exa cost summary and, for scored runs, the scoring model and Claude token totals and, when `TYPESAFE_API_KEY` is set, the Jev model, requests, input tokens and dollars (`jev`). It also prints Exa cost, Claude token usage and Jev usage; a company's research warnings are printed under it in the digest and in the `--research-only` output. A failing company is reported in the digest and does not abort the run (exit code 1 if any failed). Missing keys are detected before any paid call: `run`, `discover` and `news` refuse to start without `EXA_API_KEY` (so does `interview-prep` when it would fetch the posting), and the same check covers `ANTHROPIC_API_KEY` and the Telegram values for the commands that use them.

One `hunt.py` command runs at a time per project folder: each command holds a lock (`.hunt.lock`, git-ignored) while it runs, and a second one exits at once with "Another hunt.py command is running here". This covers commands from a shell, cron and the dashboard alike. A cron `watch` that starts during a run exits with that message and checks again at its next scheduled time. The OS releases the lock when a command exits, so a crashed command never leaves it stuck. Three commands do not hold the lock: `outlook-login` can wait up to 15 minutes for you to sign in in the browser and touches only `outlook_token.json`, so it only refuses to start while another command runs; `gmail-login` touches only `gmail_token.json`, which no other command rewrites; and `chat` runs for as long as you leave it and shares no file with the other commands except `outlook_token.json` (see [Chat with your mail](../inbox/README.md#chat-with-your-mail)).

The Telegram notification is short: the digest's count header, one entry per apply, consider and stretch role (score, verdict, company, role title and posting URL, so it works away from the laptop), the names of failed companies, and a link to the dashboard (`DASHBOARD_URL` in `.env`; default `https://<RAILWAY_PUBLIC_DOMAIN>` on Railway, otherwise `http://127.0.0.1:8000`). Reasons, concerns, gap notes and side-project ideas are in the dashboard and in the `--dry-run` digest.

`send` rebuilds the notification from a saved run in `out/` (latest by default, or pass a directory). Use it after a `--dry-run` so what is delivered comes from the run you reviewed, without paying for a second run. The Telegram header carries the saved run's date, not the day you send it: from `_run.json`, or from the folder name when the run has no readable manifest (older runs, or one interrupted while saving); a directory you picked that has neither is dated today.

Discovery runs one search per target role plus one deep search (6 searches, roughly $0.15-0.20 for `--limit 20`) because a single search returns only a handful of companies. Results are merged round-robin, filtered (job boards and anonymous employers dropped, `[search].exclude_domains` removed) and appended to `discovered.csv` (`discovered_at,name,domain,why,focus`). Domains already in the history are skipped, so repeated runs only return new companies. The printed `name,domain,why` rows load as `companies.csv` rows (extra columns are ignored); `discovered.csv` adds `discovered_at` and `focus` around them. The `why` column is Exa's unverified claim; `run` is what actually researches and scores each company.

Scoring model: `claude-sonnet-5-5` by default, override with `SCORING_MODEL` in `.env`.

Role screen (with `TYPESAFE_API_KEY`): before Claude, Jev answers yes/no and rubric questions about each open role (one request per role, given your target roles and `location`): is it one of your target roles, its seniority level (junior, mid, senior, lead or above), does the posting limit the role to regions that exclude your location, is it on-site or hybrid only, is the office in your country, and does it offer relocation. A role is dropped only when Jev is confident it fails a hard rule of the rubric: not a target role (probability 0.1 or less), junior or mid-level (0.9 or more; like the scoring rubric, the screen counts senior and above, whatever `[search].seniority` lists), or no relocation (0.1 or less) together with a region that excludes you (0.9 or more) or an on-site or hybrid office outside your country. Claude scores the company with the remaining roles; when no role remains, the company is a **skip** with score 0 and, as concerns, what each role failed (at most three: with more than three roles, two roles and a "<n> more open roles also failed the screen" line), and Claude is not called. The location questions are skipped when `location` is empty. Each role's answers and failures are saved under `screen` in `out/`, for tuning the thresholds (`scoring.SCREEN_YES`, `SCREEN_NO`, `BELOW_SENIOR_MIN`). Companies without open roles are not screened.

Verdicts in the digest: **apply** (score 75+), **consider** (50-74), **stretch** (a role centred on one of your declared gaps; see below), **check careers** (no live role found, but the company scores 50+ on its own: look at its careers page), **skip** (below 50, identity not verified, the posting excludes your location without relocation support, or score 0 when the Jev screen dropped every open role). A live posting is required for apply/consider/stretch.

Stretch: list areas where your experience is thin in `[candidate].gaps` (for example growth engineering with about 6 months of experience). When a posting's core work falls in such an area, the digest shows **STRETCH** instead of apply, with a `Gap:` line stating what you have, up to two `Build:` side-project ideas that would build credible proof, and a score reduced for the gap. Roles outside the gaps are unaffected.

Weak spots and scope: `[candidate].weak_spots` lists specific tools you are weak in (for example PySpark). If a posting requires one, the digest adds it as a concern and gives one preparation step in `Next:`; unlike a gap, it does not change the verdict. When a posting's title and duties disagree (an analyst title with ETL/dbt work, say), the digest adds a `* Scope:` line saying which part of your background fits. The scorer reports compensation only as stated in the posting and never judges whether it is high or low.

Role search: the main Exa call looks for postings. If it finds none for a verified company, a second call restricted to ATS/job-board domains (Ashby, Greenhouse, Lever, Workable, SmartRecruiters, Recruitee, Teamtailor, Personio) runs, costing about $0.007 extra. Only postings Exa marks as belonging to the company are kept; `role_source` in `out/` records which call supplied them (`main`, `ats_fallback`, `none`). The research step only quotes what each posting says about remote regions and relocation; judging eligibility for your `location` is the scorer's job.

Link verification: Exa often returns stale posting URLs (about 30% were dead in one run, including top-scored roles), so every posting is checked before scoring and dead ones are dropped (recorded as `dead_roles` in `out/`). Ashby (`jobs.ashbyhq.com`) answers HTTP 200 for any URL, so its links are checked against Ashby's public posting API, which lists only open jobs. For other sites, 404/410 or a redirect away from the posting (a soft 404, e.g. Workable `?not_found=true`, Greenhouse `?error=true`) means dead. A URL that is only a board or careers homepage, or a check that is blocked or times out, is resolved with a fresh Exa fetch (about $0.001 per link); if that cannot confirm it either, the role is kept as `link_status: "unverified"` and the scorer tells you to confirm it. If every link for a company is dead, the ATS fallback search runs, and a company with no live role becomes "check careers". Not covered: sites that answer 200 for closed postings other than Ashby (none seen yet).

Posting text: for up to 3 live roles per company, the posting text (first 4,000 characters, about $0.001 per page) is fetched and given to the scorer, because search summaries rarely contain responsibilities and required tools. Without it most concerns read "responsibilities not in the evidence". Roles whose link is only unverified are not fetched.

Known limits: Exa is a search engine, not a jobs feed. Open-role coverage varies between runs, and "no roles found" does not mean no roles exist. Posting URLs may point to third-party job boards (for example a VC portfolio board) rather than the company's own ATS. Treat the digest as leads and confirm on the company's careers page.

Score noise: the scoring model does not accept a temperature setting (`temperature is deprecated for this model`), so the same research can score differently between runs: typically within a few points, occasionally more. Verdict thresholds (75, 50) and the gap rule make borderline companies flip (one company went from 68 stretch to 82 apply on identical input). Read scores as bands, not exact values.

## Watch list

`python hunt.py watch` polls the Ashby job boards in `[watch].ashby_boards` (the slug in `jobs.ashbyhq.com/<slug>`) through Ashby's free, keyless posting API, which lists only open postings. Postings whose title contains any `title_keywords` entry (case-insensitive) and none of `title_exclude` are sent to Telegram with title, location and link, in as few messages as fit (Telegram allows 4,096 characters per message). Already-reported postings are kept in `watch_state.json` (ID, title, location, remote flag, link and when first seen; entries saved by older versions have only the title and time), so each posting is sent once and the dashboard can list them; the first run reports everything currently open. State is saved only after a successful send, so a failed send is retried on the next run. `--dry-run` prints matches without sending or saving state. A board that cannot be read (typo, API error) is logged, its state is left unchanged, and the exit code is 1. No Exa or Claude calls, so it costs nothing.

Only Ashby boards are supported. To run it on a schedule while the laptop is on, add a cron entry (`crontab -e`), for example every 6 hours:

```
0 */6 * * * cd /path/to/growth-analytics && .venv/bin/python hunt.py watch >> watch.log 2>&1
```

## News digest

`python hunt.py news` searches Exa's news category for each `[news].topics` entry in `profile.toml` (articles published in the last `--days`, default 7; 8 results per topic) and drops articles already considered (`news_state.json`, kept 90 days). With `TYPESAFE_API_KEY` set, Jev then answers a relevance yes/no for each new article (one request per article, given your target roles and topics): an article below 0.3 (`news.RELEVANT_MIN`) is dropped and printed, and only the rest reach Claude. Claude picks at most 8 worth your time (developments in the job market for your target roles, and substantive news in your topics), with a one-sentence takeaway each and a short overview, and the digest goes to Telegram with the links. Jev and Claude get your target roles, the topics and the article titles and excerpts, not your CV. Dropped articles are recorded in `news_state.json` too, so they are not screened again. Cost: about $0.007 per Exa search (one per topic), a fraction of a cent for Jev, plus one Claude call, a few cents; no Claude call when nothing new is relevant. `--dry-run` prints the digest without sending it or updating the state. A topic whose search fails is listed at the end of the message, and the command exits 1. If a Jev request fails, that article and the rest go to Claude. Run it weekly, for example with `0 8 * * 1 news` in `HUNT_SCHEDULE` (see [Deploy to Railway](../dashboard/README.md#deploy-to-railway)) or cron.
