"""PostHog AI Observability setup shared by the pipeline's Claude call paths."""

import atexit
import json
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

from dashboard.config import get_settings

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_posthog_client = None
_missing_configuration_warned = False
_active_agent_turn = ContextVar("active_agent_turn", default=None)


def _settings():
    """Return configured settings, warning once without interrupting production work."""
    global _missing_configuration_warned
    settings = get_settings()
    if settings.posthog_configured or settings.is_production or _missing_configuration_warned:
        return settings
    for variable in settings.missing_posthog_variables:
        logger.warning(
            "%s variable required by PostHog is missing or un-configured, this causes events to be silently missed. "
            "This error stops appearing once %s is configured",
            variable,
            variable,
        )
    _missing_configuration_warned = True
    return settings


def _posthog():
    """Build the one process-wide client used for AI observability captures."""
    global _posthog_client
    settings = _settings()
    if not settings.posthog_configured:
        return None
    with _lock:
        if _posthog_client is None:
            from posthog import Posthog

            _posthog_client = Posthog(
                settings.posthog_project_token,
                host=settings.posthog_host,
                privacy_mode=False,
            )
            atexit.register(_posthog_client.shutdown)
    return _posthog_client


def new_trace_id():
    """Create a trace identifier for one manual AI turn."""
    return str(uuid4())


def capture_manual_generation(
    provider,
    model,
    input_state,
    output_state,
    *,
    input_tokens=None,
    output_tokens=None,
    latency=None,
    trace_id=None,
    session_id=None,
    include_content=True,
):
    """Queue an AI generation from a provider without a supported PostHog wrapper."""
    try:
        posthog_client = _posthog()
        if posthog_client is None:
            return
        properties = {
            "$ai_trace_id": trace_id or new_trace_id(),
            "$ai_session_id": session_id or f"process-{_process_session_id}",
            "$ai_span_name": "model_request",
            "$ai_provider": provider,
            "$ai_model": model,
        }
        if include_content:
            properties["$ai_input"] = [{"role": "user", "content": json.dumps(input_state, ensure_ascii=False, default=str)}]
            properties["$ai_output_choices"] = [
                {"role": "assistant", "content": json.dumps(output_state, ensure_ascii=False, default=str)}
            ]
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
            properties["$ai_input_tokens"] = input_tokens
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
            properties["$ai_output_tokens"] = output_tokens
        if latency is not None:
            properties["$ai_latency"] = latency
        posthog_client.capture("$ai_generation", properties=properties)
    except Exception:
        logger.exception("PostHog AI generation capture failed")


def parse_anthropic(client, **request):
    """Call Claude unchanged, then capture its successful generation without delaying it."""
    trace_id = request.pop("posthog_trace_id", None)
    started_at = time.monotonic()
    response = client.messages.parse(**request)
    usage = getattr(response, "usage", None)
    capture_manual_generation(
        "anthropic",
        request["model"],
        {"system": request.get("system"), "messages": request.get("messages")},
        getattr(response, "parsed_output", None) or getattr(response, "content", None),
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        latency=time.monotonic() - started_at,
        trace_id=trace_id,
    )
    return response


def new_session_id():
    """Create a safe session identifier for one Pydantic AI conversation."""
    return str(uuid4())


@contextmanager
def agent_turn(session_id):
    """Keep all manual generations and tool spans for one answer in the same trace."""
    turn = {"session_id": session_id, "trace_id": new_trace_id()}
    token = _active_agent_turn.set(turn)
    try:
        yield turn
    finally:
        _active_agent_turn.reset(token)


def capture_agent_turn(turn, model, input_state, output_state, usage):
    """Record Pydantic AI's completed turn without weakening its existing content redaction."""
    capture_manual_generation(
        "anthropic",
        model,
        input_state,
        output_state,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        trace_id=turn["trace_id"],
        session_id=turn["session_id"],
        include_content=False,
    )


def instrument_tool(func):
    """Capture a Pydantic AI tool span when it executes within an active agent turn."""
    @wraps(func)
    def wrapped(ctx, *args, **kwargs):
        started_at = time.monotonic()
        result = func(ctx, *args, **kwargs)
        try:
            turn = _active_agent_turn.get()
            posthog_client = _posthog()
            if turn is not None and posthog_client is not None:
                posthog_client.capture(
                    "$ai_span",
                    properties={
                        "$ai_trace_id": turn["trace_id"],
                        "$ai_session_id": turn["session_id"],
                        "$ai_span_id": str(uuid4()),
                        "$ai_span_name": func.__name__,
                        "$ai_latency": time.monotonic() - started_at,
                    },
                )
        except Exception:
            logger.exception("PostHog AI tool span capture failed")
        return result

    return wrapped


_process_session_id = uuid4()
