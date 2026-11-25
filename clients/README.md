# External service clients

Thin clients for the outside services the other folders share: Exa search, TypeSafe's Jev decision model, job boards' public feeds, Telegram and PostHog. Each one is the only place in the repo that talks to its service.

## Files

| File | Purpose |
|---|---|
| `exa_client.py` | Exa client: the only place that talks to Exa, for search, contents, agent runs and monitors (retries, cost logging, cost tally) |
| `jev.py` | TypeSafe's Jev decision model: the only place that calls TypeSafe (retries, usage tally, cost per token) |
| `boards.py` | Job boards with a public feed that needs no key (Ashby, Greenhouse, Lever, Workable, SmartRecruiters, Recruitee, Teamtailor, Personio, Pinpoint): a company's open postings, and a posting's application questions |
| `telegram.py` | Telegram delivery and `getUpdates` polling for the chat bot |
| `posthog_client.py` | PostHog: the only place that talks to PostHog, for the dashboard's events and errors, the MCP endpoint's tool calls (PostHog's MCP Analytics SDK), a generation per model call and the commands' log records (off without its two settings; never sends text), and the settings of the dashboard's opt-in Support widget, the one exception (`support_widget`) |

## Exa client

All functions live in `clients/exa_client.py`.

```python
from clients.exa_client import search, search_structured, contents, fast_search, deep_search

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
| `grounded_answer(query, output_schema, *, system_prompt=None, **kwargs)` | `search_structured()` unwrapped: `(content, grounding)`, with the grounding merged into one `{confidence, urls}` entry per top-level field (lowest confidence, every cited URL). Raises `RuntimeError` when Exa returns no structured output. |
| `agent_answer(query, output_schema, *, system_prompt=None, data_sources=(), effort="low")` | An [Exa Agent](https://exa.ai/docs/agent/quickstart) run, waited for: `(structured, grounding)` in the shape of `grounded_answer()`. `data_sources` attaches [Exa Connect](https://exa.ai/docs/agent/connect/overview) partners such as `"fiber"`. See [Agent runs](#agent-runs). |
| `instant_search` / `fast_search` / `deep_lite_search` / `deep_search` / `deep_reasoning_search` `(query, **kwargs)` | `search()` with `type` preset (see below). |
| `contents(urls, *, max_age_hours=None, highlights=True, **kwargs)` | Page contents for given URLs: highlights only by default (full text only if `text` is passed). `highlights=False` with `text={...}` fetches full text only. |
| `create_monitor(params)` / `get_monitor(id)` / `update_monitor(id, changes)` / `delete_monitor(id)` / `monitors()` | [Exa Monitors](https://exa.ai/docs/monitors/quickstart): searches Exa runs by itself on an interval. `get_monitor` returns `None` and `delete_monitor` `False` for a monitor Exa no longer has. See [Monitors](#monitors). |
| `monitor_runs(id)` / `monitor_run_output(id, run_id)` | A monitor's runs, newest first, read a page at a time; and what one run found: `(content, grounding, pages)`, content and grounding in the shape of `grounded_answer()`. |

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

- **Retries** transient failures with exponential backoff (jittered, capped): HTTP 408/429/500/502/503/504 and `requests` connection/timeout errors. Other errors (400s, invalid options) raise immediately. Tune via module constants `MAX_ATTEMPTS` (default 4 total tries), `BACKOFF_BASE_SECONDS` (1.0), `BACKOFF_MAX_SECONDS` (30.0) and `RETRYABLE_STATUS`. Every request has a timeout (`TIMEOUT_SECONDS`: 15 s to connect, 120 s to read, since deep searches can take tens of seconds), whatever timeout the SDK passes itself (one value for both, 600 s by default); a request that times out is retried like any other transient failure.
- **Logs cost** at INFO on the `exa_client` logger, one line per request (`search:fast cost=$0.0070 search_time=450ms`), and retries at WARNING. Enable with `logging.basicConfig(level=logging.INFO)`.
- **Tallies cost** per label (`search:<type>`, `search_structured:<type>`, `contents`, `agent:<effort>`, `monitors`), using the `costDollars` Exa reports.

```python
from clients.exa_client import cost_summary, reset_costs

cost_summary()
# {"requests": 3, "dollars": 0.021, "by_label": {"search:fast": {"requests": 2, "dollars": 0.014}, ...}}
reset_costs()
```

Notes: the tally is in-process only (not persisted) and counts successful requests, plus every agent run that was created, however it ended; responses without cost data count as requests but add $0. The timeout means a hung connection fails and is retried instead of blocking the process (and, in the pipeline, holding `.hunt.lock`) forever.

### Agent runs

`agent_answer` starts an Exa Agent run and polls it until it ends (every `AGENT_POLL_SECONDS`, 2 s; a run still going after `AGENT_WAIT_SECONDS`, 10 minutes, is cancelled and raises). The agent plans its own searches, so unlike `search` you cannot restrict it to a domain; use it for what the search API cannot do, which here is querying a partner database.

- **Cost.** `effort` sets the run's own price: `minimal` $0.012, `low` $0.025 (the default), `medium` $0.10; `agent_answer` takes only these three, since `auto` and `ultra` are priced by the run. Partner calls are billed on top and are not capped. Fiber charges $0.02 a credit: a search is 2 credits plus 1 per result it returns, a company lookup about 2 per candidate. The agent decides how many calls to make: in the first real runs one run with Fiber cost between $0.09 and $0.75. The run's whole cost, partners included, goes into the tally.
- **Retries.** A create is sent again only when Exa turns it down for rate or concurrency (429). After a timeout, a dropped connection or a 5xx the run may have started, and a second one would be billed too: `agent_answer` raises `RuntimeError` saying a run may have started, and nothing is tallied because Exa reported no cost. A request Exa rejects (400, 401, 403) raises as it is. Status checks are retried like any other request.
- **Failures.** A run that fails, is cancelled or returns no structured output raises `RuntimeError`; whatever it cost until then is still tallied. One odd answer to a status check (a 404 just after the create, say) does not stop a paid run: only after `AGENT_FAILED_CHECKS` (5) failed checks in a row is the run given up. It is then cancelled if Exa still answers (or read once more, in case it ended), and the error says whether it was stopped or may still be running and billed.
- **Ctrl-C.** In the thread that waits, Ctrl-C cancels the run and is raised again. It never reaches worker threads, so a program that waits there calls `stop_agent_runs()` when it is interrupted (`hunt.py run` does): every waiting run is cancelled within one poll and no new one starts. A process that is killed (the dashboard's job cancel) can do neither, and its runs go on to their end at Exa.
- **Grounding.** Field paths come back as `structured.<field>`; the prefix is dropped so the result reads like `grounded_answer()`'s. Grounding is optional in Exa's replies: a run without any returns `{}`, and a field without a confidence, or with one other than low, medium or high, has `None`.

### Monitors

A monitor is a search that Exa runs by itself every period (1 hour at least) and keeps a history for: each run returns only the pages its earlier runs did not, with JSON written from them when the monitor has an `outputSchema`. `pipeline/monitor.py` keeps one per company for new job postings (see [Monitors](../pipeline/README.md#monitors)).

- **Cost.** Exa bills the runs ($0.002 each for up to 10 results), not these requests, and no reply carries a cost: they count as `monitors` requests at $0 in the tally.
- **Retries.** As with agent runs, a create is sent again only when Exa turns it down for rate (429). After a timeout, a dropped connection or a 5xx the monitor may exist, and a second one would run, billed, next to it: `create_monitor` raises `RuntimeError` saying so. The other requests are retried like any other.
- **Schedule.** Only an interval. Exa sets a new monitor's first run itself (9 to 72 minutes after the create in the first trials, for a 4-hour period) and `next_run_at` a moment after the create, so read it with `get_monitor`. An update that sends the trigger the monitor already has leaves its cycle alone. After an update to another period Exa sets the next run itself, within one new period: going from 4h to 12h, some monitors kept the run already set and the others got one up to 12 hours after the update.
- **Webhook.** Required on every monitor: a public HTTPS address (Exa refuses `localhost`, private addresses and `example.com`), called for the events the monitor subscribes to. The runs can be read instead, with `monitor_runs` and `monitor_run_output`.
- **Runs.** `monitor_runs` gives each run's `id`, `status`, `fail_reason` (`insufficient_credits`, `source_not_available`, ...) and `empty`: true when the list of runs itself says the run found nothing new, so only the others are read by id with `monitor_run_output`. Both read Exa's JSON as it comes, not through the SDK's run models, which refuse a whole run for one grounding row without a confidence or a citation: as with a search's, grounding is optional, and odd rows are left out.

## Job boards

`clients/boards.py` reads the feed that a company's job board publishes without a key. It costs nothing and is the only place that talks to these boards. The run, the monitors, the follow-ups and the application kit use it (see Job boards in [Run](../pipeline/README.md#run)).

```python
from clients import boards

board = boards.board_of("https://jobs.ashbyhq.com/acme/1111")   # {"ats": "ashby", "slug": "acme", "host": "jobs.ashbyhq.com"}
state, postings = boards.fetch(board)                           # "ok", [{"id", "title", "url", "hosted", "location", "remote", "posted", "text", "listed"}]
boards.posting_id("https://apply.workable.com/j/5C0FFEE123")    # ("workable", "5C0FFEE123")
boards.questions("https://job-boards.greenhouse.io/acme/jobs/333")  # [{"question", "required"}]
```

| Function | Purpose |
|---|---|
| `board_of(url)` | The board a posting link is on, or `None` for a link on no board with a feed (and for one that names no board, such as `apply.workable.com/j/<id>`). |
| `fetch(board)` | Every posting the board holds as open now: `(state, postings)`. `state` is `ok`, `missing` (the board does not exist: 404) or `unknown` (the request failed, or the answer is not the feed's format). A posting's `url` is the link the feed names for it, which can be a page of the company's own careers site; `hosted` is the posting on the board's own host. `remote` is `remote`, `hybrid`, `on-site` or empty; `posted` a date or empty; `text` the posting as plain text. `listed` is false for a posting that is open by its direct link only (Ashby's unlisted postings): a link to it is live, and it is not one of the roles the board shows. |
| `posting_id(url)` | Which posting of its board a link is: `(ats, id)`, the same for every form the board gives the link. |
| `questions(url, board=None)` | The questions a posting's application form asks for a written answer, without the fields for a name, a CV or a link; at most `MAX_QUESTIONS`. `[]` for a board that does not show its form, and when the request fails. |
| `page(board)`, `label(board)` | The board's own page, and the board in a few words ("Ashby board acme"). |

- **Feeds.** Ashby's [posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), Greenhouse's [Job Board API](https://developers.greenhouse.io/job-board) (also for its EU boards), Lever's postings API (`api.lever.co`, `api.eu.lever.co` for an EU board), Workable's widget API, SmartRecruiters' posting API, Recruitee's `/api/offers/`, Teamtailor's `/jobs.rss`, Personio's `/xml` and Pinpoint's `/postings.json`. Each was read live against real boards when this was written (November 2026).
- **What a feed does not have.** SmartRecruiters' list carries no posting text, and answers a company it does not know like one with nothing open, so a board of its without a posting reads as `unknown`. Pinpoint gives no date. Greenhouse and Personio do not say whether a posting is remote: its text does.
- **Application questions.** Greenhouse answers them in its documented API (`?questions=true`). Ashby's come from the API its own hosted posting page reads its form from, which is not documented: when it changes, `questions` returns `[]`. The other boards do not show their forms without a key.
- **Untrusted content.** Everything a board returns is a company's text on a web page. Titles are put on one line, markup is reduced to its text, a posting without a usable link is left out, and an XML feed with a document type is refused by the parser when it meets one, in whatever encoding the feed is written, before anything the document type declares is read. Callers pass it on as a posting's fields and never as instructions.
- **Requests.** Plain `requests` calls with a 15-second timeout and no retries: a feed that fails is `unknown`, and the caller goes on without it (the run asks its searches, the monitor reports the board and reads it again at the next check).
- **Tests.** `tests/boards_mock.py` holds each feed's wire format.

## Jev screening

[Jev](https://docs.typesafe.ai/introduction) is TypeSafe's decision model: it answers typed questions (yes/no, one of several options, a level on a rubric) about a piece of text with calibrated probabilities instead of written text, in well under a second and for $0.042 per million input tokens. With `TYPESAFE_API_KEY` set, `run`, `mail` and `news` (and the news digests the chat bot makes) use it to drop clear misses before Claude reads them: open roles that fail a hard requirement (see Role screen in [Run](../pipeline/README.md#run)), emails that are clearly not about an application ([Email updates](../inbox/README.md#email-updates)) and irrelevant articles ([News digest](../pipeline/README.md#news-digest)). It only drops what it is confident about; anything uncertain still goes to Claude, so the screen cuts Claude tokens without deciding close calls. Without the key, Claude reads everything, as before.

All TypeSafe calls go through `clients/jev.py` with a 15-second timeout, retries on 408, 429 (waiting as long as `Retry-After` asks, up to 30 seconds), 500, 502-504 and 529 (overloaded), and an in-process usage tally. The model is pinned to `jev-1.14.0` because the thresholds were chosen for it; override with `JEV_MODEL`. A request that still fails, or an answer missing a field the screen reads, counts as a Jev failure: the command carries on without the screen (Claude reads the rest) and logs a warning. Once a request has failed on TypeSafe being down, overloaded or unreachable (not on a rate limit), the rest of the command skips Jev instead of waiting through the retries again. Jev gets what Claude gets for the same step, minus your CV: role titles, locations and posting text with your target roles and `location`; email sender, subject and first 2,000 characters; article titles and excerpts. TypeSafe states it does not train on customer requests.

## PostHog

Optional analytics and observability in your own [PostHog](https://posthog.com) project. With `POSTHOG_PROJECT_TOKEN` (the project token, `phc_...`) and `POSTHOG_HOST` (the project's ingestion host: `https://eu.i.posthog.com` or `https://us.i.posthog.com`) both set, the programs report what they do and nothing they read or write. Without both, nothing is sent; with only one, a warning says which is missing. `hunt.py` takes the two from `.env`; the dashboard takes them from its own environment (on Railway, the service variables), like its other settings, and the jobs it starts inherit them; a live `python -m evals` run takes them from the shell environment, since it loads no `.env`.

Everything goes through `clients/posthog_client.py`, which each entry point turns on (`configure`). The SDK (`posthog` 8) sends events to PostHog's capture v1 endpoints under `POSTHOG_HOST`, `/i/v1/analytics/events` and, for the AI events, `/i/v1/ai/events`; log records go to `/i/v1/logs`.

| What | From | Holds |
|---|---|---|
| Product events | The dashboard, for each action of yours that succeeds, each page you open and each call of a tool of its MCP endpoint (see [PostHog events](../dashboard/README.md#posthog-events)) | The event's name and a few properties that are values from fixed sets, booleans or counts: a status, a job kind, a run's mode, whether a discover was given a focus, its limit, a tool's name. A page view (`$pageview`) holds only the page's route as it is declared (`/c/{domain}`) and the declared route of the dashboard page it was opened from, never an address asked for or its query. Nothing else about the request (no address, no browser), and nothing a tool was given or answered. |
| MCP Analytics (`$mcp_tool_call`, `$mcp_initialize`, `$mcp_tools_list`, `$mcp_unknown_tool`) | The dashboard's [MCP endpoint](../dashboard/README.md#mcp-endpoint), through PostHog's MCP Analytics SDK (`posthog.mcp`, `posthog_client.instrument_mcp`) | A tool call's tool name and description, its declared argument names, its milliseconds, whether it failed and the error's type (refusals included); a handshake's client name and version; a listing's tool names; a call of a tool the endpoint does not have, without the name asked for; on each, the client's user agent, the protocol version, the server's name and a session id the SDK makes up. Never the arguments, the result, an agent's stated intent, a tool name an agent made up or an error's message (`posthog_client._without_mcp_text` takes them off). The SDK's `context`, `llm_model` and conversation-id arguments are off: the tools take and return what they did before. The endpoint keeps no sessions, so at the handshake the SDK's middleware (`posthog_client.mcp_sessions`) gives the client a token in the `Mcp-Session-Id` header (that session id and the client's name and version), which the client sends back with each request: its calls share the session and carry its name. A client that sends none gets the dashboard process's session. |
| Errors (`$exception`) | The dashboard | An exception nothing handled, or that a tool of the MCP endpoint did not expect: its type and its stack (the code's file names, functions and lines with the source around them; no variables), the route as it is declared (`/c/{domain}`, not the address asked for; `/mcp` with the tool's name) and the method. Never its message, which can name a data file or quote a value. `hunt.py` reports no exceptions: how a command ended is in its `finished` record. |
| AI Observability (`$ai_generation`, `$ai_span`) | Each Claude call that returned an answer, each Jev request TypeSafe answered (the ones in Jev's usage tally), each Exa structured search with an answer and each Exa agent run that completed with one; the chat bot's answers and its tool calls | Provider, model, the input and output tokens the provider counts, seconds, a trace and a session; for a tool call its name, its seconds and the type of an exception it raised. Never the prompt, the answer, a tool's arguments or what it returned. A call that failed, an answer the Anthropic SDK could not read and a chat answer that failed part-way are not reported, though they can have cost money. |
| Logs | `hunt.py`, per command; a live `python -m evals` run; the dashboard when it starts | `hunt_command_started`, then `hunt_command_finished` with `command`, `exit_code`, `seconds` and, when the command ended on an exception, its type (`ERROR` when the exit code is not 0); `evals_command_started` and `evals_command_finished`, likewise; `dashboard_started`. Only these records: the programs' own log lines, which name companies, senders and subjects, are never exported. |

- **One owner, no person.** Every event, from every program, has the same distinct id, `growth-analytics-owner` (`posthog_client.OWNER`), so PostHog counts one user and its funnels and retention can follow your actions from one event to the next. No event makes or changes a person profile (each is sent with `process_person_profile: false`), and nothing calls `identify` or sets person properties: the dashboard has one password and no accounts, so there is no one else to tell apart, and signing out has no identity to reset.
- **Traces and sessions.** Each Claude, Jev or Exa call of a command is a trace of its own, and all of one command's calls share a session, so a run reads as one session. The batches of one `mail` check share a trace. In the chat bot an answer is one trace (one generation for the answer's model calls with their tokens summed, a span per tool call, and the Jev and Claude calls of a news digest) and a conversation is one session; `/new` starts another.
- **What PostHog adds or sees.** The SDK adds the Python and OS versions, its own version and `service` (`dashboard`, `hunt` or `evals`) to every event; log records carry the same service name and a random id per process. PostHog also receives the IP address each request comes from (the Railway service's, or yours from the laptop). Whether it keeps it on the event is the project's **IP data capture configuration** setting; projects of an EU organization discard it by default.
- **Support widget, the one exception.** With `POSTHOG_SUPPORT_WIDGET=1` the dashboard's pages load posthog-js for PostHog Support's chat widget (`posthog_client.support_widget`), and what you type in it is sent to PostHog as a ticket, with the name and email its form asks for, your browser, posthog-js's random session id and the address of the page a new ticket or a ticket recovery was started on. Nothing else posthog-js could capture is on, and its events are the widget's own, without an address. Off unless you set it: see [Support widget](../dashboard/README.md#support-widget).
- **Costs in PostHog.** PostHog works a generation's cost out from its model and tokens where it knows the model's prices. It knows none for Jev or Exa, so their generations show timings and counts but no cost. The commands' own output and the saved runs remain the record of what was spent.
- **Nothing waits for PostHog.** Events and records are queued and sent by background threads; a failure to send is logged and changes nothing else. A command sends what is left when it exits. While PostHog cannot be reached that holds the exit for up to about ten seconds (a request gets 3 seconds and a batch of events one more try, on the AI events' lane too, which is sent at the same time: `posthog_client.TIMEOUT`, `RETRIES`), after which what is left is dropped; a dashboard job shows as running that much longer. A job stopped from the dashboard, and the chat bot stopped by a redeploy, lose what was queued in their last seconds and write no `finished` record.
- **Tests.** `tests/posthog_mock.py` answers the two capture endpoints and the logs endpoint in their wire format (the `posthog_api` fixture); without it PostHog is off in every test.
- **When you add a feature.** `.claude/skills/posthog/SKILL.md` lists what a new dashboard action, model call or command has to report, and what may never be sent.
