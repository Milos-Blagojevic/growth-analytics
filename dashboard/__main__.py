"""python -m dashboard [--port 8000]: serve the dashboard for the current directory on 127.0.0.1 only."""

import argparse
import logging

import uvicorn

from dashboard.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Loopback only: the dashboard has no authentication.
    uvicorn.run(create_app("."), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
