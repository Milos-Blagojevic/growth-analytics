"""PostHog: product events, error tracking, AI Observability and logs. The only place that talks to PostHog.

Off until `configure` gets both the project token and the ingestion host (POSTHOG_PROJECT_TOKEN and POSTHOG_HOST,
read by the entry points: hunt.py, `python -m prospects`, `python -m dashboard` and a live `python -m evals` run).
Until then every function here does nothing. None of them raises or waits for PostHog: events and log records are
queued and sent by background threads, and a failure is logged and never reaches the work it reports on. What is
still queued when a process exits is sent then, for a bounded time (TIMEOUT, RETRIES, FLUSH_MILLIS), and dropped
after it.

PostHog is a third party, so what goes to it is limited here, by what these functions take:

- `capture`: something the owner did on the dashboard, by name, with a few properties from fixed sets (a status, a
  job kind, a count). No person: PostHog gets a new random id with every event.
- `generation`, `parse_anthropic`, `agent_turn`, `instrument_tool`: a model call or a tool call as provider, model,
  token counts, seconds, trace and session. Never a prompt, an answer, a tool's arguments or what it returned: those
  hold the CV, emails, interview transcripts and pricing.
- `log`, `command`: records written for PostHog (a command started, a command finished). The programs' own loggers
  are never exported: they name companies, senders and subjects.
- `capture_exception`, and `configure(exceptions=True)` for the ones no code caught: an exception of the dashboard
  with its type and its stack, never its message (`_without_messages`): a message names the file that could not be
  written, quotes the value that did not parse. hunt.py and `python -m prospects` say how a command ended in its
  `finished` record instead.
"""

import atexit
import inspect
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from posthog import Posthog

logger = logging.getLogger("posthog_client")

LOGS_PATH = "/i/v1/logs"  # PostHog's OTLP endpoint for logs, under the ingestion host
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep  # where this code is on this machine
# How long a process that is exiting can wait for a PostHog that does not answer: about 10 seconds, with these.
TIMEOUT = 3  # seconds PostHog gets to answer one request
RETRIES = 1  # of a batch of events that could not be sent; after it the batch is dropped
FLUSH_MILLIS = 2_000  # how long a flush waits for the log records to be sent

_client = None  # the process's posthog.Posthog, once configured
_records = None  # (LoggerProvider, its logger): log records go out through a provider of their own, never the global one
_service = None  # "dashboard", "hunt", "prospects" or "evals": on every event and log record
_session = f"process-{uuid4()}"  # the AI session of the generations one command makes outside an agent turn
_turn = ContextVar("posthog_agent_turn", default=None)  # the agent answer in progress: {"trace_id", "session_id"}


def configure(token, host, *, service, exceptions=False):
    """Turns PostHog on for this process when both `token` and `host` are given; returns whether it did.

    `service` names the program on every event and log record. `exceptions` also reports the exceptions nothing
    handled (the main thread's and other threads'), which is for the dashboard only."""
    global _client, _records, _service
    shutdown()
    if not token or not host:
        if token or host:
            logger.warning("PostHog is off: it needs both POSTHOG_PROJECT_TOKEN and POSTHOG_HOST, and only %s is set",
                           "POSTHOG_PROJECT_TOKEN" if token else "POSTHOG_HOST")
        return False
    host = host.rstrip("/")
    try:
        # privacy_mode: should one of the SDK's own model wrappers ever be used, it leaves prompts and answers out too.
        client = Posthog(token, host=host, timeout=TIMEOUT, max_retries=RETRIES, privacy_mode=True,
                         before_send=_without_messages, enable_exception_autocapture=exceptions,
                         super_properties={"service": service})
        provider = LoggerProvider(resource=Resource.create({"service.name": service}), shutdown_on_exit=False)
        provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(
            endpoint=f"{host}{LOGS_PATH}", headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)))
        records = (provider, provider.get_logger("growth-analytics"))
    except Exception:  # settings the SDKs cannot work with: the program runs without PostHog
        logger.warning("PostHog is off: it could not be set up", exc_info=True)
        return False
    _client, _records, _service = client, records, service
    return True


def _without_messages(event):
    """The SDK's last step before it queues an event (an event this raises on is dropped). An exception keeps its
    type and its stack and loses its message, and each frame says where its file is in the code, not on this
    machine: the first can name a company or quote what was read, the second holds the home folder's name."""
    for raised in event["properties"].get("$exception_list") or []:
        raised["value"] = ""
        for frame in (raised.get("stacktrace") or {}).get("frames") or []:
            frame.pop("abs_path", None)
            for key in frame.keys() & {"filename", "module"}:  # a template, or a module the SDK could not name
                frame[key] = _in_the_code(frame[key])
    return event


def _in_the_code(path):
    """An absolute path as the path inside this repo, or inside site-packages, or else the file's name alone."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return path
    if path.startswith(CODE):
        return path[len(CODE):]
    _, installed, inside = path.rpartition(f"{os.sep}site-packages{os.sep}")
    return inside if installed else os.path.basename(path)


def configured():
    return _client is not None


def flush():
    """Sends what is queued, waiting a few seconds at most (a long-lived process calls it when it stops)."""
    if _client is None:
        return
    try:
        _client.flush()
        _records[0].force_flush(timeout_millis=FLUSH_MILLIS)
    except Exception:
        logger.warning("PostHog flush failed", exc_info=True)


def shutdown():
    """Sends what is queued and turns PostHog off again. Runs when the process exits; tests call it between cases."""
    global _client, _records, _service
    client, records = _client, _records
    _client = _records = _service = None
    if client is None:
        return
    for step in (client.shutdown, lambda: records[0].force_flush(timeout_millis=FLUSH_MILLIS), records[0].shutdown):
        try:
            step()
        except Exception:
            logger.warning("PostHog shutdown failed", exc_info=True)


atexit.register(shutdown)


# Product events and errors (the dashboard) ------------------------------------------------------------------

def capture(event, properties=None):
    """One thing the owner did, by name (`object_verb`: project_created), with properties from fixed sets only."""
    if _client is None:
        return
    try:
        _client.capture(event, properties=properties or {})
    except Exception:
        logger.warning("PostHog capture of %s failed", event, exc_info=True)


def capture_exception(exc, properties=None):
    """An exception nothing handled: its type and stack (see `_without_messages`), plus `properties`."""
    if _client is None:
        return
    try:
        _client.capture_exception(exc, properties=properties or {})
    except Exception:
        logger.warning("PostHog capture of an exception failed", exc_info=True)


# AI Observability -------------------------------------------------------------------------------------------
# Manual capture (https://posthog.com/docs/ai-observability/installation/manual-capture): the SDK's wrappers cover
# neither messages.parse nor Jev and Exa, and they record prompts and answers unless told not to.

def new_trace_id():
    return str(uuid4())


def new_session_id():
    return str(uuid4())


def generation(provider, model, *, input_tokens=None, output_tokens=None, latency=None, trace_id=None):
    """One answered model call ($ai_generation): who answered, the tokens it counted and the seconds it took.

    Inside an `agent_turn` it joins that answer's trace and the conversation's session; elsewhere it is a trace of
    its own (or `trace_id`, for calls that belong together) in the session of this process."""
    if _client is None:
        return
    try:
        turn = _turn.get()
        properties = {
            "$ai_trace_id": trace_id or (turn["trace_id"] if turn else new_trace_id()),
            "$ai_session_id": turn["session_id"] if turn else _session,
            "$ai_span_name": "model_request",
            "$ai_provider": provider,
            "$ai_model": model,
        }
        for key, value in (("$ai_input_tokens", input_tokens), ("$ai_output_tokens", output_tokens),
                           ("$ai_latency", latency)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                properties[key] = value
        _client.capture("$ai_generation", properties=properties)
    except Exception:
        logger.warning("PostHog capture of a %s generation failed", provider, exc_info=True)


def parse_anthropic(client, *, trace_id=None, **request):
    """client.messages.parse(**request), reported as one generation once Claude has answered."""
    started = time.monotonic()
    response = client.messages.parse(**request)
    usage = getattr(response, "usage", None)
    generation("anthropic", request.get("model"), input_tokens=getattr(usage, "input_tokens", None),
               output_tokens=getattr(usage, "output_tokens", None), latency=time.monotonic() - started,
               trace_id=trace_id)
    return response


@contextmanager
def agent_turn(session_id):
    """One answer of an agent: every generation and tool span made inside shares a trace, in the conversation's
    session (`new_session_id`). The tools run in other threads, which Pydantic AI starts with this context."""
    token = _turn.set({"trace_id": new_trace_id(), "session_id": session_id})
    try:
        yield
    finally:
        _turn.reset(token)


def instrument_tool(func):
    """`func` (plain or async) as an agent tool that reports each call inside an `agent_turn` as a span ($ai_span):
    the tool's name, the seconds it took and, when it raised, the exception's type. Never its arguments or what it
    returned."""
    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def tool(*args, **kwargs):
            with _spanned(func.__name__):
                return await func(*args, **kwargs)
    else:
        @wraps(func)
        def tool(*args, **kwargs):
            with _spanned(func.__name__):
                return func(*args, **kwargs)

    return tool


@contextmanager
def _spanned(name):
    started, error = time.monotonic(), None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        _span(name, time.monotonic() - started, error)


def _span(name, seconds, error):
    turn = _turn.get()
    if _client is None or turn is None:
        return
    try:
        properties = {"$ai_trace_id": turn["trace_id"], "$ai_session_id": turn["session_id"],
                      "$ai_span_id": str(uuid4()), "$ai_span_name": name, "$ai_latency": seconds}
        if error:
            properties |= {"$ai_is_error": True, "$ai_error": error}
        _client.capture("$ai_span", properties=properties)
    except Exception:
        logger.warning("PostHog capture of a %s span failed", name, exc_info=True)


# Logs -------------------------------------------------------------------------------------------------------

def log(name, *, error=False, **attributes):
    """One log record: a fixed `name` and attributes from fixed sets or numbers, at INFO (ERROR with `error`)."""
    if _records is None:
        return
    try:
        _records[1].emit(timestamp=time.time_ns(), body=name, attributes=attributes,
                         severity_number=SeverityNumber.ERROR if error else SeverityNumber.INFO,
                         severity_text="ERROR" if error else "INFO")
    except Exception:
        logger.warning("PostHog log record %s failed", name, exc_info=True)


@contextmanager
def command(name):
    """Log records around one command of a CLI: <service>_command_started, then <service>_command_finished with
    its exit code, its seconds and, when it raised, the exception's type (never its message). The exit code is the
    one the command exits with (`sys.exit`); a `main` that returns its code instead sets it as `.code` on what
    this yields."""
    log(f"{_service}_command_started", command=name)
    started, ended, raised = time.monotonic(), SimpleNamespace(code=0), {}
    try:
        yield ended
    except SystemExit as exc:  # sys.exit(code), or sys.exit("what is wrong"), which exits with 1
        ended.code = exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
        raise
    except BaseException as exc:
        ended.code, raised = 1, {"error_type": type(exc).__name__}
        raise
    finally:
        log(f"{_service}_command_finished", error=ended.code != 0, command=name, exit_code=ended.code,
            seconds=round(time.monotonic() - started, 1), **raised)
