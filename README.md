# Everything Growth

Growth analytics tooling. Currently provides a thin client over the [Exa](https://exa.ai) search API for web research: raw retrieval with highlights, schema-grounded structured answers, and content fetching for known URLs.

## Setup

Requires Python 3.9+ and an Exa API key ([dashboard.exa.ai](https://dashboard.exa.ai)).

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
| `contents(urls, *, max_age_hours=None, **kwargs)` | Page contents (with highlights) for given URLs. |

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

## Files

| File | Purpose |
|---|---|
| `exa_client.py` | Exa search client |
| `requirements.txt` | Pinned dependencies (`exa-py`, `python-dotenv`) |
| `.env.example` | Template for required environment variables |
| `.env` | Local secrets (git-ignored) |
