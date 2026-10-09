"""Exa search client for growth-analytics.

Defaults follow the project setup: type="auto", highlights for content.
Docs: https://docs.exa.ai/reference/search-api-guide-for-coding-agents
"""

import logging
import os
import random
import re
import threading
import time

from dotenv import load_dotenv
from exa_py import Exa
import requests

load_dotenv()


def get_client() -> Exa:
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise RuntimeError("EXA_API_KEY is not set. Copy .env.example to .env and add your key.")
    return Exa(api_key=api_key)


# Retry and cost logging --------------------------------------------------------------
# The Exa SDK has no retries and raises plain ValueError("Request failed with status code
# N: ...") for HTTP errors, so retryable failures are detected from the message.

logger = logging.getLogger("exa_client")

MAX_ATTEMPTS = 4  # total tries per request (1 initial + 3 retries)
BACKOFF_BASE_SECONDS = 1.0  # delay before retry n is ~BASE * 2**(n-1), jittered, capped
BACKOFF_MAX_SECONDS = 30.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_STATUS_RE = re.compile(r"Request failed with status code (\d+)")

_costs_lock = threading.Lock()
_costs = {}  # label -> {"requests": int, "dollars": float}


def _is_retryable(exc):
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, ValueError):
        match = _STATUS_RE.match(str(exc))
        return bool(match) and int(match.group(1)) in RETRYABLE_STATUS
    return False


def _record_cost(label, response):
    cost = getattr(response, "cost_dollars", None)
    dollars = cost.total if cost is not None else None
    with _costs_lock:
        entry = _costs.setdefault(label, {"requests": 0, "dollars": 0.0})
        entry["requests"] += 1
        entry["dollars"] += dollars or 0.0
    cost_text = f"${dollars:.4f}" if dollars is not None else "n/a"
    search_time = getattr(response, "search_time", None)
    time_text = f"{search_time:.0f}ms" if search_time is not None else "n/a"
    logger.info("%s cost=%s search_time=%s", label, cost_text, time_text)


def _call(label, method, *args, **kwargs):
    """Call a client method with retry on transient failures; log and tally its cost."""
    fn = getattr(get_client(), method)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = fn(*args, **kwargs)
            break
        except Exception as exc:
            if attempt == MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            delay *= random.uniform(0.5, 1.0)
            logger.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs", label, attempt, MAX_ATTEMPTS, exc, delay)
            time.sleep(delay)
    _record_cost(label, response)
    return response


def cost_summary():
    """Cost reported by Exa for requests made through this module since import/reset_costs().

    Returns {"requests", "dollars", "by_label": {label: {"requests", "dollars"}}}. Requests
    whose response carried no cost data count toward "requests" but add $0.
    """
    with _costs_lock:
        by_label = {label: dict(entry) for label, entry in _costs.items()}
    return {
        "requests": sum(e["requests"] for e in by_label.values()),
        "dollars": sum(e["dollars"] for e in by_label.values()),
        "by_label": by_label,
    }


def reset_costs():
    with _costs_lock:
        _costs.clear()



def _contents_or_default(contents, max_age_hours=None):
    contents = {"highlights": True} if contents is None else contents
    if max_age_hours is not None:
        contents = {**contents, "max_age_hours": max_age_hours}
    return contents


def search(query, *, type="auto", num_results=10, contents=None, max_age_hours=None, **kwargs):
    """Raw retrieval: results with query-relevant highlights by default.

    contents: Exa contents options, e.g. {"text": {"max_characters": 10000}}.
    Defaults to {"highlights": True}.
    max_age_hours: content freshness (not a publication-date filter). 0 = always fetch
    fresh, -1 = cached only, N > 0 = use cache if newer than N hours else fetch.
    None (default) lets Exa decide. Overrides any max_age_hours inside `contents`.
    Pass include_domains/exclude_domains, category, date filters etc. through kwargs.
    """
    return _call(
        f"search:{type}",
        "search",
        query,
        type=type,
        num_results=num_results,
        contents=_contents_or_default(contents, max_age_hours),
        **kwargs,
    )


def search_structured(
    query, output_schema, *, type="auto", system_prompt=None, contents=None, max_age_hours=None, **kwargs
):
    """Synthesized search: grounded JSON in .output.content, citations in .output.grounding.

    contents, max_age_hours: same as in search().
    Schema limits: max nesting depth 2, max 10 total properties. Do not add
    citation/confidence fields -- grounding is returned automatically.
    """
    return _call(
        f"search_structured:{type}",
        "search",
        query,
        type=type,
        system_prompt=system_prompt,
        output_schema=output_schema,
        contents=_contents_or_default(contents, max_age_hours),
        **kwargs,
    )


# Search-type wrappers. All forward to search(); latencies are approximate (Exa docs).


def instant_search(query, **kwargs):
    """type="instant" (~250 ms): real-time paths such as autocomplete or voice."""
    return search(query, type="instant", **kwargs)


def fast_search(query, **kwargs):
    """type="fast" (~450 ms): latency-sensitive requests."""
    return search(query, type="fast", **kwargs)


def deep_lite_search(query, **kwargs):
    """type="deep-lite" (~4 s): lightweight research and synthesis."""
    return search(query, type="deep-lite", **kwargs)


def deep_search(query, **kwargs):
    """type="deep" (4-15 s): multi-step search with stronger synthesis."""
    return search(query, type="deep", **kwargs)


def deep_reasoning_search(query, **kwargs):
    """type="deep-reasoning" (12-40 s): completeness over latency."""
    return search(query, type="deep-reasoning", **kwargs)


def contents(urls, *, max_age_hours=None, highlights=True, **kwargs):
    """Fetch content for URLs you already have. Note: highlights is top-level here.

    max_age_hours: same semantics as in search().
    highlights: request query-relevant excerpts (default). Pass False together with e.g.
    text={"max_characters": 4000} to fetch full text only; requesting both bills both views.
    """
    if max_age_hours is not None:
        kwargs["max_age_hours"] = max_age_hours
    if highlights:
        kwargs["highlights"] = True
    return _call("contents", "get_contents", urls, **kwargs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    results = search("what is exa.ai", num_results=3)
    for r in results.results:
        print(f"\n{r.title}\n{r.url}")
        for h in (r.highlights or []):
            print(f"  - {h[:160]}")
    print(f"\ncost summary: {cost_summary()}")
