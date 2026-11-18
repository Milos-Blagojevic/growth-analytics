"""Isolated OpenTelemetry log export for purpose-built PostHog application logs."""

import atexit
import logging

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

POSTHOG_LOGGER_NAME = "posthog.application"
_posthog_logger = logging.getLogger(POSTHOG_LOGGER_NAME)
_logger_provider = None


def configure_posthog_logs(settings):
    """Export only records emitted through the dedicated PostHog application logger."""
    global _logger_provider
    if _logger_provider is not None:
        return _posthog_logger
    if not settings.posthog_configured:
        if not settings.is_production:
            for variable in settings.missing_posthog_variables:
                logging.getLogger(__name__).warning(
                    "%s variable required by PostHog is missing or un-configured, this causes events to be silently missed. "
                    "This error stops appearing once %s is configured",
                    variable,
                    variable,
                )
        return None

    logger_provider = LoggerProvider()
    exporter = OTLPLogExporter(
        endpoint=f"{settings.posthog_host.rstrip('/')}/i/v1/logs",
        headers={"Authorization": f"Bearer {settings.posthog_project_token}"},
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    _posthog_logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    _posthog_logger.setLevel(logging.INFO)
    _posthog_logger.propagate = False
    _logger_provider = logger_provider
    atexit.register(shutdown_posthog_logs)
    return _posthog_logger


def shutdown_posthog_logs():
    """Flush the dedicated exporter during process shutdown without affecting app work."""
    if _logger_provider is None:
        return
    try:
        _logger_provider.force_flush(timeout_millis=2_000)
    except Exception:
        pass
