"""Exa client for growth-analytics: search, contents and agent runs.

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
import exa_py.api
import requests

load_dotenv()

TIMEOUT_SECONDS = (15, 120)  # (connect, read) for every Exa request; deep searches can take tens of seconds


def _bounded(verb):
    """`verb` with this module's timeout, whatever timeout the caller passes."""

    def call(*args, **kwargs):
        return verb(*args, **{**kwargs, "timeout": TIMEOUT_SECONDS})

    return staticmethod(call)


class _BoundedRequests:
    """Stand-in for the `requests` module inside the Exa SDK: same verbs, always with TIMEOUT_SECONDS.

    The SDK passes one timeout for connecting and reading (600 s unless set), so an unreachable host would hang
    the pipeline for ten minutes per attempt before the retry on `requests.Timeout` fires. Everything else
    resolves to the real module.
    """

    get = _bounded(requests.get)
    post = _bounded(requests.post)
    patch = _bounded(requests.patch)
    delete = _bounded(requests.delete)

    def __getattr__(self, name):
        return getattr(requests, name)


exa_py.api.requests = _BoundedRequests()


def configured():
    return bool(os.environ.get("EXA_API_KEY"))


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


def _http_status(exc):
    """The status code of an HTTP error the SDK raised, None for anything else."""
    match = _STATUS_RE.match(str(exc)) if isinstance(exc, ValueError) else None
    return int(match.group(1)) if match else None


def _is_retryable(exc):
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return _http_status(exc) in RETRYABLE_STATUS


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


def _with_retries(label, fn, *, retryable=_is_retryable):
    """fn() with retry on the failures `retryable` accepts (by default the transient ones)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == MAX_ATTEMPTS or not retryable(exc):
                raise
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            delay *= random.uniform(0.5, 1.0)
            logger.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs", label, attempt, MAX_ATTEMPTS, exc, delay)
            time.sleep(delay)


def _call(label, method, *args, **kwargs):
    """Call a client method with retry on transient failures; log and tally its cost."""
    fn = getattr(get_client(), method)
    response = _with_retries(label, lambda: fn(*args, **kwargs))
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


_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def _compact_grounding(rows, *, prefix=""):
    """Merge per-subfield grounding (e.g. open_roles[3].url) into one entry per top-level field.

    Keeps the lowest confidence Exa gives (None when it gives none, or one that is not low, medium or high) and
    the union of cited URLs. `prefix` is dropped from the field paths first: agent runs name them
    "structured.<field>". Grounding is optional in Exa's replies: none at all is an empty result.
    """
    merged = {}
    for row in rows or ():
        confidence = row.confidence.lower() if isinstance(row.confidence, str) else None
        if confidence not in _CONFIDENCE_ORDER:
            confidence = None
        key = row.field.removeprefix(prefix).split("[")[0].split(".")[0]
        entry = merged.setdefault(key, {"confidence": confidence, "urls": []})
        known = entry["confidence"]
        if confidence and (not known or _CONFIDENCE_ORDER[confidence] < _CONFIDENCE_ORDER[known]):
            entry["confidence"] = confidence
        for citation in row.citations or ():
            if citation.url not in entry["urls"]:
                entry["urls"].append(citation.url)
    return merged


def grounded_answer(query, output_schema, *, system_prompt=None, **kwargs):
    """search_structured() unwrapped: (content, grounding).

    content is the schema-shaped dict; grounding is {top-level field: {"confidence", "urls"}}.
    Raises RuntimeError when Exa returns no structured output.
    """
    output = search_structured(query, output_schema, system_prompt=system_prompt, **kwargs).output
    if output is None or not isinstance(output.content, dict):
        raise RuntimeError(f"Exa returned no structured output for query: {query[:80]}")
    return output.content, _compact_grounding(output.grounding)


# Agent runs ------------------------------------------------------------------------------
# An agent run plans its own searches and, with Exa Connect data sources, queries partner databases
# (https://exa.ai/docs/agent/connect/overview). It is asynchronous: created, then polled until it ends.

AGENT_POLL_SECONDS = 2.0  # between status checks of a run
AGENT_WAIT_SECONDS = 600  # a run still going after this is cancelled and counts as failed
AGENT_FAILED_CHECKS = 5  # status checks in a row that may fail, each after its own retries, before the run is given up
AGENT_EFFORTS = ("minimal", "low", "medium")  # the fixed-price ones; "auto" and "ultra" are priced by the run
_AGENT_ENDED = frozenset({"completed", "failed", "cancelled"})
_stop = threading.Event()  # set by stop_agent_runs()


def stop_agent_runs():
    """Makes every agent_answer that is waiting cancel its run and raise KeyboardInterrupt, and every later one
    raise before it starts a run.

    For Ctrl-C in a program that calls agent_answer from worker threads: KeyboardInterrupt reaches the main thread
    only, and each run left alone would go on, billed, to its end while the program waits for it.
    """
    _stop.set()


def agent_answer(query, output_schema, *, system_prompt=None, data_sources=(), exclude=None, effort="low"):
    """Exa Agent run, waited for: (structured, grounding), shaped like grounded_answer().

    data_sources: Exa Connect provider ids, e.g. ["fiber"]; the agent queries them alongside web search.
    exclude: records the agent should not return, e.g. [{"domain": "acme.com"}].
    effort: "minimal", "low" or "medium" have a fixed run price ($0.012, $0.025, $0.10). Partner calls are billed
    on top with no cap (Fiber: $0.02 a credit, a search is 2 credits plus 1 per result), so one run can cost
    from a few cents to a dollar. The whole cost is tallied under "agent:<effort>" in cost_summary().

    A create is sent again only when Exa turns it down for rate or concurrency (429). After any other failure
    that is not a plain refusal (a timeout, a dropped connection, a 5xx) the run may have started, and a second
    one would be billed too: that raises RuntimeError saying so.

    Raises RuntimeError when the run fails, is cancelled, is still going after AGENT_WAIT_SECONDS (it is then
    cancelled), cannot be followed (it is cancelled if Exa still answers) or returns no structured output. What
    Exa last reported as its cost is tallied in every one of those cases. Ctrl-C in the calling thread, or
    stop_agent_runs() from another, cancels the run and raises KeyboardInterrupt.
    """
    if effort not in AGENT_EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(AGENT_EFFORTS)}, not {effort!r}")
    if _stop.is_set():
        raise RuntimeError("Exa agent run not started: the program is stopping")
    runs = get_client().agent.runs
    label = f"agent:{effort}"
    try:
        run = _with_retries(
            label,
            lambda: runs.create(
                query=query,
                system_prompt=system_prompt,
                output_schema=output_schema,
                effort=effort,
                data_sources=[{"provider": provider} for provider in data_sources] or None,
                input={"exclusion": list(exclude)} if exclude else None,
            ),
            retryable=lambda exc: _http_status(exc) == 429,
        )
    except Exception as exc:
        status = _http_status(exc)
        if status is not None and 400 <= status < 500 and status != 408:
            raise  # Exa turned the request down: nothing started
        raise RuntimeError(
            f"Exa agent run may have started and be billed: the request to create it failed ({type(exc).__name__}: {exc})"
        ) from exc

    deadline = time.monotonic() + AGENT_WAIT_SECONDS
    overdue, lost, failed_checks = False, None, 0
    try:
        while run.status not in _AGENT_ENDED:
            if time.monotonic() > deadline:
                overdue = True
                run = _with_retries(label, lambda: runs.cancel(run.id))
                break
            if _stop.wait(AGENT_POLL_SECONDS):  # the pause between checks, cut short by stop_agent_runs()
                raise KeyboardInterrupt
            try:
                run = _with_retries(label, lambda: runs.get(run.id))
                failed_checks = 0
            except Exception:  # one odd answer (a 404 just after the create, say) must not stop a paid run
                failed_checks += 1
                if failed_checks >= AGENT_FAILED_CHECKS:
                    raise
    except BaseException as exc:  # Exa no longer answers for the run, or Ctrl-C: left going, it is billed to the end
        lost = exc
        for last_try in (runs.cancel, runs.get):  # stop it if that still works, else read where it stands
            try:
                run = last_try(run.id)
                break
            except Exception:
                continue
        if not isinstance(exc, Exception):
            _record_cost(label, run)
            raise
    _record_cost(label, run)  # a run that failed or was cancelled can still have been billed
    if run.status != "completed":
        if lost is not None:
            ended = "stopped" if run.status in _AGENT_ENDED else "it may still be running and billed"
            reason = f"could not be followed ({type(lost).__name__}: {lost}); {ended}"
        elif overdue:
            reason = f"not finished after {AGENT_WAIT_SECONDS}s"
        else:
            reason = (run.error.message if run.error else None) or "no reason given"
        raise RuntimeError(f"Exa agent run {run.id} {run.status}: {reason}") from lost
    output = run.output
    if output is None or not isinstance(output.structured, dict):
        raise RuntimeError(f"Exa agent returned no structured output for query: {query[:80]}")
    return output.structured, _compact_grounding(output.grounding, prefix="structured.")


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
    highlights: request query-relevant excerpts (default). Full text is then not requested unless
    `text` is passed; requesting both bills both views. Pass False together with e.g.
    text={"max_characters": 4000} to fetch full text only.
    """
    if max_age_hours is not None:
        kwargs["max_age_hours"] = max_age_hours
    if highlights:
        kwargs["highlights"] = True
        # exa-py adds text={"max_characters": 10000} when no text/summary/extras is given; opt out.
        kwargs.setdefault("text", False)
    return _call("contents", "get_contents", urls, **kwargs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    results = search("what is exa.ai", num_results=3)
    for r in results.results:
        print(f"\n{r.title}\n{r.url}")
        for h in (r.highlights or []):
            print(f"  - {h[:160]}")
    print(f"\ncost summary: {cost_summary()}")
