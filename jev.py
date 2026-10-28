"""TypeSafe's Jev decision model: typed answers (yes/no, choice, score) with probabilities, for cheap screening
before Claude. The only place that calls TypeSafe: retries, a usage tally and the cost per token live here.

API: POST https://api.typesafe.ai/v1/systemone with a `state` and named `questions`; one answer per question.
Docs: https://docs.typesafe.ai/api/system-one
"""

import logging
import os
import random
import threading
import time

import requests

logger = logging.getLogger("jev")

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.14.0"  # pinned, not jev-latest: the screening thresholds were chosen for this version
DOLLARS_PER_INPUT_TOKEN = 0.042 / 1_000_000  # output tokens are free
TIMEOUT = 15
MAX_ATTEMPTS = 4  # total tries per request (1 initial + 3 retries)
BACKOFF_BASE_SECONDS = 1.0  # delay before retry n is ~BASE * 2**(n-1), jittered, capped
BACKOFF_MAX_SECONDS = 30.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})  # 529: TypeSafe is overloaded
TRANSIENT_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)

_usage_lock = threading.Lock()
_usage = {"requests": 0, "input_tokens": 0}
_down = threading.Event()  # set when a request failed for good on a transient error: later requests skip TypeSafe


class JevError(RuntimeError):
    pass


# The same TypeSafe key under two names: TYPESAFE_API_KEY on Railway, JEV_API_KEY in the local .env.
KEY_VARS = ("TYPESAFE_API_KEY", "JEV_API_KEY")
NO_KEY = "No Jev key (JEV_API_KEY or TYPESAFE_API_KEY) is set"


def api_key():
    """The first of KEY_VARS that is set and not empty, else None."""
    return next((os.environ[var] for var in KEY_VARS if os.environ.get(var)), None)


def configured():
    return api_key() is not None


def model():
    return os.environ.get("JEV_MODEL") or DEFAULT_MODEL


def noul(instructions, *, true=None, false=None):
    """A yes/no question; the answer is the probability of yes."""
    question = {"type": "noul", "instructions": instructions}
    if true or false:
        question["criteria"] = {"true": true or "", "false": false or ""}
    return question


def choice(instructions, options):
    """Pick one of `options` ({key: description}); the answer has the key, its confidence and every probability."""
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions, levels):
    """Rate on ordered `levels` (lowest first); the answer has a probability per level, keyed "0", "1", ..."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


def _probability(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _valid(question, answer):
    """True when `answer` has the fields the screens read, as probabilities in [0, 1]."""
    if not isinstance(answer, dict):
        return False
    if question["type"] == "noul":
        return _probability(answer.get("noul"))
    if question["type"] == "choice":
        picked = answer.get("choice")
        return isinstance(picked, str) and picked in question["criteria"] and _probability(answer.get("confidence"))
    levels = answer.get("probabilities")
    score_value = answer.get("score")
    return (isinstance(score_value, (int, float)) and not isinstance(score_value, bool) and isinstance(levels, dict)
            and all(_probability(levels.get(str(i))) for i in range(len(question["criteria"]))))


def _retry_after(response):
    """Seconds a 429 asks to wait (Retry-After in seconds), else 0."""
    try:
        return float(response.headers.get("Retry-After", 0))
    except (TypeError, ValueError):
        return 0.0


def ask(state, questions):
    """Answers keyed like `questions`, each checked against its question's type. Retries transient failures; raises
    JevError otherwise. After a request fails for good because TypeSafe is down, overloaded or unreachable (not
    rate-limited), later calls in this process raise at once instead of waiting through the retries again."""
    key = api_key()
    if not key:
        raise JevError(NO_KEY)
    if _down.is_set():
        raise JevError("TypeSafe was unavailable earlier in this run")
    body = {"model": model(), "state": state, "questions": questions}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rate_limited = False
        try:
            response = requests.post(API_URL, json=body, headers={"Authorization": f"Bearer {key}"},
                                     timeout=TIMEOUT)
            retryable = response.status_code in RETRYABLE_STATUS
            rate_limited = response.status_code == 429
            problem = None if response.ok else f"HTTP {response.status_code}: {response.text[:300]}"
        except requests.RequestException as exc:  # a broken or dropped connection is transient; a bad request is not
            retryable = isinstance(exc, TRANSIENT_ERRORS)
            problem = f"{type(exc).__name__}: {exc}"
        if problem is None:
            break
        if attempt == MAX_ATTEMPTS or not retryable:
            if retryable and not rate_limited:  # a rate limit passes; an outage lasts longer than one command
                _down.set()
            raise JevError(f"TypeSafe request failed ({problem})")
        delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)) * random.uniform(0.5, 1.0)
        if rate_limited:
            delay = max(delay, min(BACKOFF_MAX_SECONDS, _retry_after(response)))
        logger.warning("Jev failed (attempt %d/%d): %s; retrying in %.1fs", attempt, MAX_ATTEMPTS, problem, delay)
        time.sleep(delay)
    try:
        data = response.json()
    except ValueError as exc:
        raise JevError(f"TypeSafe returned no JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise JevError("TypeSafe returned an unexpected response")
    usage = data.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    with _usage_lock:  # an answered request is billed, whatever its answers look like
        _usage["requests"] += 1
        _usage["input_tokens"] += tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else 0
    answers = data.get("answers")
    if not isinstance(answers, dict):
        answers = {}
    bad = sorted(key for key, question in questions.items() if not _valid(question, answers.get(key)))
    if bad:
        raise JevError(f"TypeSafe returned no usable answer for {', '.join(bad)}")
    return answers


def usage_summary():
    """{"model", "requests", "input_tokens", "dollars"} for the requests TypeSafe answered in this process."""
    with _usage_lock:
        requests_made, tokens = _usage["requests"], _usage["input_tokens"]
    return {"model": model(), "requests": requests_made, "input_tokens": tokens,
            "dollars": round(tokens * DOLLARS_PER_INPUT_TOKEN, 6)}


def reset():
    """Clears the usage tally and the unavailable flag (tests, or a new run in the same process)."""
    with _usage_lock:
        _usage.update(requests=0, input_tokens=0)
    _down.clear()


def summary_line():
    summary = usage_summary()
    return f"Jev: {summary['requests']} requests, {summary['input_tokens']} input tokens, ${summary['dollars']:.6f}"
