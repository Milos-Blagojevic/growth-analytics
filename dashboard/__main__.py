"""python -m dashboard [--root DIR] [--host HOST] [--port PORT]: serve the dashboard for a data folder.

By default it serves the current directory on 127.0.0.1 only, without a login. Any other --host (for example
0.0.0.0 on Railway) requires DASHBOARD_PASSWORD. Environment:

  DASHBOARD_PASSWORD     turns on the login (at least 12 characters)
  HUNT_SCHEDULE          hunt.py commands to start on cron schedules (see dashboard/schedule.py)
  RAILWAY_PUBLIC_DOMAIN  set by Railway; accepted as Host name, like the host of DASHBOARD_URL
  PORT                   default for --port (set by Railway)
"""

import argparse
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import uvicorn

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
    try:
        entries = schedule.parse(os.environ.get("HUNT_SCHEDULE", ""))
        app = create_app(args.root, allowed_hosts=allowed_hosts(), password=password, schedule_entries=entries)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
