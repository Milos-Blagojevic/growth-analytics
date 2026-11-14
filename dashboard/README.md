# Dashboard app

The web dashboard (`python -m dashboard`): results, your tracking, interviews and side projects, starting jobs, and the schedule and chat bot when it runs on Railway.

## Files

| File | Purpose |
|---|---|
| `__main__.py` | Entry point and environment settings |
| `app.py` | FastAPI app and request guard |
| `auth.py` | Password login |
| `schedule.py` | `HUNT_SCHEDULE` parsing and scheduler |
| `db.py` | SQLite schema and user migrations |
| `ingest.py` | File ingest and `companies.csv` appends |
| `queries.py` | Page queries |
| `tracking.py` | Statuses and notes |
| `email_updates.py` | Status suggestions from email updates, and applications added from them |
| `projects.py` | Side projects |
| `jobs.py` | Job runner and cost estimates |
| `bot.py` | The chat-bot supervisor (`CHAT_BOT=1`) |
| `templates/` | Jinja templates |
| `static/` | CSS and a vendored copy of htmx 2.0.7 |

## Dashboard

```bash
python -m dashboard              # http://127.0.0.1:8000
python -m dashboard --port 8001
python -m dashboard --root ~/hunt-data   # data folder other than the current directory
```

Run it from the project directory, or point `--root` at the folder holding `profile.toml`, `companies.csv`, `out/` and the other data files (jobs then run `hunt.py` with that folder as working directory). By default it listens on `127.0.0.1` only and has no login, so every request must name a loopback host. Every change (status, notes, projects, starting a job) must carry a token from the page and come from the dashboard's own origin, so another website open in your browser cannot start a paid run. The origin is read from the `Origin` header, which browsers send with every such request; a request without it (a script) is refused, even with the token. If the dashboard restarts, open pages hold an old token: changes then show "This page is out of date… Reload it".

With `DASHBOARD_PASSWORD` set (at least 12 characters), every page asks for that password first, and a session lasts 30 days on that browser; **Log out** ends it. Changing the password logs every browser out. After 10 wrong passwords in 15 minutes, logins are refused for everyone, you included, until the 15 minutes pass. Serving beyond this machine (`--host` other than loopback) refuses to start without a password. Besides loopback, the dashboard accepts the host names in `RAILWAY_PUBLIC_DOMAIN` and `DASHBOARD_URL`. The dashboard reads `DASHBOARD_PASSWORD`, `HUNT_SCHEDULE`, `DASHBOARD_URL`, `RAILWAY_PUBLIC_DOMAIN` and `PORT` from the shell environment only, never from `.env` (`export DASHBOARD_PASSWORD=…`, or `DASHBOARD_PASSWORD=… python -m dashboard`); the `hunt.py` jobs it starts load `.env` as usual.

Reading pages costs nothing. Only jobs you start on Runs, or with "Re-run" or "Run selected", and the commands in `HUNT_SCHEDULE` call Exa, Claude or Telegram. Jobs you start ask for confirmation first.

- **Board** (`/`): the latest scored result per company, in tabs for apply, consider, stretch, check careers and skip, with search (company, role, location and your notes), a status filter (dismissed cards are hidden unless you choose All or Dismissed), a minimum score and sorting. Each card shows the score, best role, location assessment, whether the posting link was confirmed live, a "third-party listing" tag when the research saw a role only on a third-party site, and your status. A company whose latest run failed keeps its last scored result, and the board names the companies that failed in the most recent run. Click a card for the details: best role, your status and notes, next step, scope and gap notes, reasons and concerns, side-project ideas, score history across runs (with the 75 and 50 thresholds), open roles with link status, roles seen only on third-party sites (leads to look for on the company's careers page, see Third-party sites in [Run](../pipeline/README.md#run)), dropped dead links, research warnings (for example a failed ATS fallback search), company summary, evidence URLs, and a **Re-run** button that researches just that company. `/c/<domain>` opens the same details as a page.
- **Your status and notes**: set a status (new, saved, applied, interviewing, offer, rejected, dismissed) and free-text notes in a company's details, or a status for watch-list postings in the inbox. Notes save as you type. A role is tracked by its posting URL, so when a later run picks a different best role for a company, the card starts as new again. A company without a posting (check careers) is tracked by its domain, and an application added from an email update (see Pipeline) by that email. The first time an item reaches applied or later, the date is kept as "applied".
- **Inbox** (`/inbox`): apply, consider and stretch roles first scored in the last 7, 14 or 30 days or all time (a company counts again when its best role changes), and watch-list postings by when `watch` first reported them, each with a status menu.
- **Pipeline** (`/pipeline`): everything you marked saved, applied, interviewing, offer or rejected, in columns, with your notes and applied date. Change a status there to move the card. Above the columns, "From your email" lists the rejections and interview invites `hunt.py mail` found, each with the applications it matches and a button to set the status. An update that matches nothing you track (an application you made outside the board) has **Add to pipeline** instead, which adds it as a card with that status (see [Email updates](../inbox/README.md#email-updates)).
- **Interviews** (`/interviews`): your interviews on a board by stage of preparation (needs prep, ready for the call, to review, reviewed), the watch-outs for your next interview, and progress across reviewed interviews. Each interview has its own page to open on your phone before and during the call. See [Interviews](../interview/README.md#interviews).
- **Projects** (`/projects`): side projects that build proof for your gaps, in idea, active, done and dropped columns. The scorer writes side-project ideas only for stretch roles (a role centred on one of your `[candidate].gaps`). Every idea from any saved run appears as a suggestion, newest run first, with the company, role, that run's score and verdict, and its age. Ideas from older runs stay even when a later run scores the company differently, because they are about your gap, not the company. **Add to ideas** turns one into a project, and **Dismiss** hides it. A handled idea is not suggested again, even if a later run repeats it with different case or spacing. **New project** adds your own, with a title, description, the gap it covers and a repository or write-up link. Deleting a project the scorer suggested hides it rather than letting it come back.
- **Leads** (`/leads`): companies from `discovered.csv`, marked new, queued (in `companies.csv`) or researched (with the latest score). Tick companies and press **Run selected…** to research and score just those (they do not need to be in `companies.csv`), or **Add to companies.csv** to append them to `companies.csv`, so every full run (scheduled ones too) covers them. Companies already listed are not added twice (domains are compared as `run` reads them, so `https://www.Acme.com/` and `acme.com` are the same company); removing one means editing `companies.csv`.
- **Runs** (`/runs`): start `hunt.py run` (dry run, run and notify, or research only, each optionally with Fiber) for every company in `companies.csv`, `discover` (focus, limit), `watch`, `mail` or `news` (each optionally dry run) or `send` (pick a saved scored run). Each opens a confirmation first. It shows the exact command, an estimated cost and side effects such as Telegram messages, file updates or what is sent to Claude; for a run you can switch the mode and **With Fiber** there, also for **Re-run** and **Run selected**. With Fiber, the run also asks Fiber's jobs database for each verified company's open roles (`run --fiber`, one paid Exa agent run per company; see Fiber as a role source in [Run](../pipeline/README.md#run)); it is off unless you tick it, whatever `profile.toml` says. Saved runs made with Fiber are marked "· Fiber" in the history. A `discover` focus may not start with `-`, so it cannot be read as a flag.
  - **Schedule:** the commands in `HUNT_SCHEDULE` (see [Deploy to Railway](#deploy-to-railway)) with their next time and the job each last started, or why it could not start.
  - **Chat bot:** with `CHAT_BOT=1`, whether `hunt.py chat` is running (since when, starts, the tail of `jobs/chat.log`, refreshed every 5 seconds); otherwise a note on how to turn it on (see [Chat with your mail](../inbox/README.md#chat-with-your-mail)).
  - **Estimates:** Exa is the average cost per company of saved runs that recorded it (`_run.json`), runs with Fiber and runs without averaged apart; until a run with Fiber is saved, a run with Fiber adds $0.09 to $0.40 a company to the estimate without it. Claude is the figure in [Parked ideas](../docs/parked-ideas.md) of $0.27 per 19 companies, an upper bound now that the Jev screen skips Claude for some companies. Jev is a fraction of a cent. Discover is the figure in [Run](../pipeline/README.md#run) of $0.15–0.20.
  - **Job pages:** each job gets a live log page with a Stop button (which sends SIGTERM; paid calls already made still cost). When the job finishes, the page shows the saved run's verdict counts and cost.
  - **History:** the page lists jobs started from the dashboard (scheduled ones are labelled "Scheduled ·"), plus every saved run in `out/` (from the dashboard, a shell or cron) with its mode, verdict counts, errors, Exa cost and Claude tokens.
  - **Mechanics:** jobs run `python hunt.py …` in the data folder (`--root`) as a separate process, so they behave exactly like the CLI. Logs go to `jobs/<id>.log` and selected-company lists to `jobs/<id>-companies.csv` (both git-ignored). Only one job runs at a time (see the lock in [Run](../pipeline/README.md#run)). If the dashboard stops while a job runs, the job shows as interrupted after a restart. Ctrl+C in the dashboard's terminal stops the job too. A job that outlives the dashboard (for example after `kill` of the server process) keeps running and holds the lock until it finishes.

The dashboard copies the pipeline files into `hunt.db` (SQLite, git-ignored) and copies them again whenever one of them changes, so a finished run shows up on the next page load. A run directory that cannot be read (for example while a run is still writing it) is skipped with a warning banner and read again once it changes; the banner also reports a `watch_state.json`, `discovered.csv` or `companies.csv` that cannot be read, and the pages keep working without it (with `companies.csv` unreadable, Runs counts no companies, a full run cannot be started and **Add to companies.csv** is refused until the file is fixed; Leads still lists every company, none marked as queued). Companies are keyed by their domain as `run` spells it (`runs.normalize_domain`: lower-case, without `https://`, `www.` or a path), so results saved under an older spelling such as `www.Acme.com` and new ones under `acme.com` make one card, and a check-careers status or note saved under the older spelling follows it (a one-time migration of `hunt.db` on the first start). Runs saved before `_run.json` existed show no mode or cost. `hunt.db` also holds your statuses, notes, side projects, job history, answers to email suggestions and the applications you added from email, which exist nowhere else: back it up if they matter, and do not delete it. The database is in SQLite's WAL mode, so the newest changes sit in `hunt.db-wal` and `hunt.db-shm` next to it until a checkpoint: copy or back up `hunt.db` only while the dashboard is stopped, or the copy misses them. The copied pipeline data is rebuilt automatically when needed; your own data is never touched by that.

## Deploy to Railway

Runs the dashboard on [Railway](https://railway.com), so you can open it from your phone and have it start `discover`, `run` (with its Telegram notification), `watch`, `mail` and `news` on a schedule. Railway builds the `Dockerfile`: the code lives in `/app`, and your data (the same files as in the project folder) on a volume mounted at `/data`. Railway bills the always-on service and the volume; scheduled commands cost the same Exa and Claude money as from the laptop.

Railway's own cron services cannot share a volume with the dashboard, so the dashboard process starts the scheduled commands itself, as jobs on the Runs page with the same one-at-a-time lock, logs and history.

1. In Railway: **New project → Deploy from GitHub repo**, pick this repository.
2. Attach a volume to the service with mount path `/data` (right-click the service → **Attach volume**, or `railway volume add --mount-path /data`). Without it the service does not start (`--root /data is not a directory`), so a missing volume never silently loses your data on the next deploy.
3. Set the service variables:

   | Variable | Value |
   |---|---|
   | `DASHBOARD_PASSWORD` | Required: the login password, at least 12 characters. A random one: `python -c "import secrets; print(secrets.token_urlsafe(18))"`. Anyone with it can start paid runs, so keep it to yourself. |
   | `EXA_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optionally `SCORING_MODEL`, `TYPESAFE_API_KEY` and `JEV_MODEL` | As in your `.env` (the container has no `.env`). |
   | `TZ` | Your time zone, for example `Europe/Belgrade`. Schedules and saved-run times use it; default UTC. |
   | `HUNT_SCHEDULE` | Optional: scheduled commands (below). |
   | `OUTLOOK_CLIENT_ID`, optionally `OUTLOOK_TENANT` | Only for email updates and the chat bot: as in your `.env` (see [Email updates](../inbox/README.md#email-updates)). |
   | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` | Only for the chat bot's Gmail access: as in your `.env` (see [Chat with your mail](../inbox/README.md#chat-with-your-mail)). |
   | `CHAT_BOT` | `1` keeps the Telegram chat bot (`hunt.py chat`) running alongside the dashboard (see [Chat with your mail](../inbox/README.md#chat-with-your-mail)). |
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
   # optional: email updates and the chat bot (see Email updates and Chat with your mail in inbox/README.md), and their history
   railway volume files upload outlook_token.json /outlook_token.json
   railway volume files upload gmail_token.json /gmail_token.json
   railway volume files upload mail_state.json /mail_state.json
   railway volume files upload news_state.json /news_state.json
   ```

   `railway volume browse` opens an interactive browser for editing these files later (for example to remove a company from `companies.csv`). To keep your statuses, notes and side projects, stop the local dashboard, then `railway volume files upload --overwrite hunt.db /hunt.db` while no job runs on Railway, and restart the service (**Deployments → Restart**).
6. Open the domain, log in, and check the Runs page: the Schedule section lists what will run and when, and with `CHAT_BOT=1` the Chat bot section shows the bot running (since when, how many starts, the tail of its output). The dashboard starts `hunt.py chat` when it starts, restarts it whenever it exits (after 10 seconds, then 1 and 5 minutes while it keeps exiting soon after starting, for example without a signed-in mailbox), and stops it at shutdown; its output goes to `jobs/chat.log` on the volume, truncated at each dashboard start. The bot is not a job: it holds no lock, so scheduled commands and Run buttons work as before.

`HUNT_SCHEDULE` holds one entry per line, or entries separated by `;` (easier in Railway's variable editor): five cron fields (minute, hour, day of month, month, day of week with 0 or 7 for Sunday; numbers, `*`, ranges, lists and `*/n` steps), then a `hunt.py` command. Supported commands are the ones the Runs page starts: `run`, `run --dry-run`, `run --research-only`, each also with `--fiber` (Fiber's jobs database as well, paid per company), `discover [focus] [--limit N]`, `watch`, `mail` and `news`, the last three also with `--dry-run`. For example, discovery every Monday at 07:00, a scored run with its Telegram notification every day at 07:30, the watch list every 6 hours, email every 2 hours from 08:00 to 20:00, and the news digest on Mondays at 08:30:

```
0 7 * * 1 discover --limit 15; 30 7 * * * run; 0 */6 * * * watch; 0 8-20/2 * * * mail; 30 8 * * 1 news
```

An entry that is not valid stops the dashboard from starting, with the reason in the deploy logs. A command that comes due while another one runs waits and starts as soon as that one finishes; one that cannot start (for example `companies.csv` is empty) shows the reason on the Runs page and in the logs. Commands due while the service is down (during a redeploy) are skipped, not caught up. Changing a variable redeploys the service, which interrupts a running job. `discover` only adds leads: use **Add to companies.csv** on Leads so the next scheduled `run` researches them.

Railway and a laptop do not share files: once Railway runs `watch` on a schedule, remove the laptop's cron entry, or both will send the same postings. Railway can [back up the volume](https://docs.railway.com/volumes/backups); `railway volume files download /hunt.db` copies your tracking data to the laptop. Do either while the service is stopped (or at least no dashboard is running): the dashboard keeps the newest changes in `hunt.db-wal` until a checkpoint, and a copy of `hunt.db` taken while it runs misses them.

To try the image on the laptop, from the project folder: `docker build -t hunt . && docker run -p 8000:8000 -e DASHBOARD_PASSWORD=… -v "$PWD":/data hunt`, then open `http://127.0.0.1:8000`. The container reads and writes the project folder's data files, and its jobs see only the keys you pass with `-e`.
