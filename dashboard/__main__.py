"""python -m dashboard [--root DIR] [--host HOST] [--port PORT]: serve the dashboard for a data folder.

By default it serves the current directory on 127.0.0.1 only, without a login. Any other --host (for example
0.0.0.0 on Railway) requires DASHBOARD_PASSWORD. Environment:

  DASHBOARD_PASSWORD     turns on the login (at least 12 characters)
  HUNT_SCHEDULE          hunt.py commands to start on cron schedules (see dashboard/schedule.py)
  CHAT_BOT               1/true/yes keeps `hunt.py chat` (the Telegram chat bot) running alongside the dashboard
  RAILWAY_PUBLIC_DOMAIN  set by Railway; accepted as Host name, like the host of DASHBOARD_URL
  PORT                   default for --port (set by Railway)
  POSTHOG_PROJECT_TOKEN  with POSTHOG_HOST, reports your actions, page views, errors and a start record to PostHog
  POSTHOG_HOST           the project's ingestion host, e.g. https://eu.i.posthog.com (optional)
  POSTHOG_SUPPORT_WIDGET 1/true/yes shows PostHog Support's chat widget on the pages behind the login (needs both
                         PostHog settings); what you type in it is sent to PostHog as a ticket
  MCP_TOKEN              serves the MCP endpoint at /mcp (at least 32 characters) to clients that send it as
                         `Authorization: Bearer <MCP_TOKEN>`: your dashboard's data and jobs, as tools
"""

import argparse
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import uvicorn

from clients import posthog_client
from dashboard import schedule
from dashboard.app import LOOPBACK_HOSTS, create_app


def allowed_hosts():
    """Loopback plus the deployment's own names: Railway's public domain and the host of DASHBOARD_URL."""
    extra = (os.environ.get("RAILWAY_PUBLIC_DOMAIN"), urlparse(os.environ.get("DASHBOARD_URL") or "").hostname)
    return LOOPBACK_HOSTS + tuple(dict.fromkeys(h.lower() for h in extra if h))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("."),
                        help="data folder: profile.toml, companies.csv, out/, hunt.db (default: current directory)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8000))
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error(f"--root {args.root} is not a directory")
    password = os.environ.get("DASHBOARD_PASSWORD") or None
    if args.host not in LOOPBACK_HOSTS and not password:
        # Without a login, anyone who can reach the port could read your data and start paid runs.
        parser.error(f"--host {args.host} serves beyond this machine: set DASHBOARD_PASSWORD first")
    chat_bot = os.environ.get("CHAT_BOT", "").strip().lower() in ("1", "true", "yes")
    token, host = os.environ.get("POSTHOG_PROJECT_TOKEN"), os.environ.get("POSTHOG_HOST")
    widget = None
    if os.environ.get("POSTHOG_SUPPORT_WIDGET", "").strip().lower() in ("1", "true", "yes"):
        widget = posthog_client.support_widget(token, host)
        if widget is None:
            parser.error("POSTHOG_SUPPORT_WIDGET needs POSTHOG_PROJECT_TOKEN and POSTHOG_HOST")
    # Before create_app: the MCP SDK sets up logging of its own when nothing has, and the MCP endpoint is
    # instrumented for PostHog when it is built.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    posthog_client.configure(token, host, service="dashboard", exceptions=True)
    try:
        entries = schedule.parse(os.environ.get("HUNT_SCHEDULE", ""))
        app = create_app(args.root, allowed_hosts=allowed_hosts(), password=password, schedule_entries=entries,
                         chat_bot=chat_bot, support_widget=widget, mcp_token=os.environ.get("MCP_TOKEN") or None)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
