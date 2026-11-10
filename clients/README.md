# External service clients

Thin clients for the outside services the other folders share: Exa search, TypeSafe's Jev decision model, Ashby job boards and Telegram. Each one is the only place in the repo that talks to its service.

## Files

| File | Purpose |
|---|---|
| `exa_client.py` | Exa search client: the only place that talks to Exa (retries, cost logging, cost tally) |
| `jev.py` | TypeSafe's Jev decision model: the only place that calls TypeSafe (retries, usage tally, cost per token) |
| `ashby.py` | Ashby public job-board API client (used by link checks and the watch list) |
| `telegram.py` | Telegram delivery and `getUpdates` polling for the chat bot |

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
| `agent_answer(query, output_schema, *, system_prompt=None, data_sources=(), exclude=None, effort="low")` | An [Exa Agent](https://exa.ai/docs/agent/quickstart) run, waited for: `(structured, grounding)` in the shape of `grounded_answer()`. `data_sources` attaches [Exa Connect](https://exa.ai/docs/agent/connect/overview) partners such as `"fiber"`; `exclude` lists records it must not return. See [Agent runs](#agent-runs). |
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

- **Retries** transient failures with exponential backoff (jittered, capped): HTTP 408/429/500/502/503/504 and `requests` connection/timeout errors. Other errors (400s, invalid options) raise immediately. Tune via module constants `MAX_ATTEMPTS` (default 4 total tries), `BACKOFF_BASE_SECONDS` (1.0), `BACKOFF_MAX_SECONDS` (30.0) and `RETRYABLE_STATUS`. Every request has a timeout (`TIMEOUT_SECONDS`: 15 s to connect, 120 s to read, since deep searches can take tens of seconds), whatever timeout the SDK passes itself (one value for both, 600 s by default); a request that times out is retried like any other transient failure.
- **Logs cost** at INFO on the `exa_client` logger, one line per request (`search:fast cost=$0.0070 search_time=450ms`), and retries at WARNING. Enable with `logging.basicConfig(level=logging.INFO)`.
- **Tallies cost** per label (`search:<type>`, `search_structured:<type>`, `contents`, `agent:<effort>`), using the `costDollars` Exa reports.

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

## Jev screening

[Jev](https://docs.typesafe.ai/introduction) is TypeSafe's decision model: it answers typed questions (yes/no, one of several options, a level on a rubric) about a piece of text with calibrated probabilities instead of written text, in well under a second and for $0.042 per million input tokens. With `TYPESAFE_API_KEY` set, `run`, `mail` and `news` use it to drop clear misses before Claude reads them: open roles that fail a hard requirement (see Role screen in [Run](../pipeline/README.md#run)), emails that are clearly not about an application ([Email updates](../inbox/README.md#email-updates)) and irrelevant articles ([News digest](../pipeline/README.md#news-digest)). It only drops what it is confident about; anything uncertain still goes to Claude, so the screen cuts Claude tokens without deciding close calls. Without the key, Claude reads everything, as before.

All TypeSafe calls go through `clients/jev.py` with a 15-second timeout, retries on 408, 429 (waiting as long as `Retry-After` asks, up to 30 seconds), 500, 502-504 and 529 (overloaded), and an in-process usage tally. The model is pinned to `jev-1.14.0` because the thresholds were chosen for it; override with `JEV_MODEL`. A request that still fails, or an answer missing a field the screen reads, counts as a Jev failure: the command carries on without the screen (Claude reads the rest) and logs a warning. Once a request has failed on TypeSafe being down, overloaded or unreachable (not on a rate limit), the rest of the command skips Jev instead of waiting through the retries again. Jev gets what Claude gets for the same step, minus your CV: role titles, locations and posting text with your target roles and `location`; email sender, subject and first 2,000 characters; article titles and excerpts. TypeSafe states it does not train on customer requests.
