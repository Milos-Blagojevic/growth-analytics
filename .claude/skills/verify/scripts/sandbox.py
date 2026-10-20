"""Serve the dashboard on a throwaway data folder where every job runs a fake hunt.py, for /verify smoke runs.

    .venv/bin/python .claude/skills/verify/scripts/sandbox.py --root DIR [--code DIR] [--port 8765] [--seed]
        [--schedule HUNT_SCHEDULE] [--password DASHBOARD_PASSWORD]

`python -m dashboard` always starts the hunt.py next to the dashboard package (jobs.HUNT_SCRIPT), whatever --root
is, so a Run button or HUNT_SCHEDULE entry there spends money and can message Telegram. This launcher passes
`hunt_command` instead: jobs from buttons and from --schedule run DIR/fake_hunt.py, which prints its arguments,
sleeps FAKE_HUNT_SECONDS (default 1) and exits with FAKE_HUNT_EXIT (default 0).

DIR must be new or empty, or a folder this script created before (it leaves a .verify-sandbox marker), so it never
runs migrations on a copy of real data. --code is the code tree to serve (default: the current directory), so the
same DIR can be served first by the base commit's code (`git archive <base> | tar -x -C <folder>`) and then by the
changed code. --seed fills an empty DIR with sample files built by that tree's tests/dashboard_data.py.
--schedule and --password take the place of HUNT_SCHEDULE and DASHBOARD_PASSWORD (the environment is ignored).
The first output line is `SANDBOX <base url> <csrf token>`.
"""

import argparse
import shutil
import sys
from pathlib import Path

MARKER = ".verify-sandbox"

FAKE_HUNT = """import os, sys, time
print("fake hunt.py", *sys.argv[1:], flush=True)
time.sleep(float(os.environ.get("FAKE_HUNT_SECONDS", "1")))
sys.exit(int(os.environ.get("FAKE_HUNT_EXIT", "0")))
"""


def claim(root):
    """Accepts a new or empty folder outside any git checkout, or one claimed before; marks it as a sandbox."""
    for folder in (root, *root.parents):
        if (folder / ".git").exists():
            sys.exit(f"{root} is inside the git checkout {folder}: use a mktemp -d folder")
    if root.is_dir() and any(root.iterdir()) and not (root / MARKER).exists():
        sys.exit(f"{root} holds files this script did not create: use a new mktemp -d folder")
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).touch()


def seed(root, code):
    sys.path.insert(0, str(code / "tests"))
    import dashboard_data as data  # the served tree's builders, so the files match its format

    shutil.copy(code / "profile.example.toml", root / "profile.toml")
    shutil.copy(code / "companies.example.csv", root / "companies.csv")
    roles = [("Analytics Engineer", "https://jobs.ashbyhq.com/acme/1", "live")]
    # One run in the old format (no _run.json) and one in the current one: both stay in out/ on the volume.
    data.write_run(root, "20260901-090000", [data.result("Acme", "acme.com", fit=data.fit(70, "consider"))])
    data.write_run(root, "20261001-090000", [
        data.result("Acme", "acme.com", fit=data.fit(84, "apply", *roles[0][:2]), roles=roles),
        data.result("Beta <b>&</b> Co", "beta.io", fit=data.fit(55, "stretch")),
        data.result("Broken", "broken.dev", error="Exa timed out"),
    ], manifest={"mode": "full", "started_at": "2026-10-01T08:55:00", "saved_at": "2026-10-01T09:00:00",
                 "companies": 3, "errors": 1, "exa": {"requests": 9, "dollars": 0.12},
                 "claude": {"model": "claude-sonnet-4-5", "input_tokens": 9000, "output_tokens": 1200}})
    data.write_interview(root, transcript=data.wispr(*data.INTERVIEW_TURNS), me="2",
                         prep=data.PREP, review=data.review_fields())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="sandbox data folder (created if missing)")
    parser.add_argument("--code", type=Path, default=Path("."), help="code tree to serve (default: .)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--seed", action="store_true", help="write sample files into a new --root")
    parser.add_argument("--schedule", default="", help="HUNT_SCHEDULE value; entries run the fake hunt.py")
    parser.add_argument("--password", default=None, help="DASHBOARD_PASSWORD value (turns on the login)")
    args = parser.parse_args()
    root, code = args.root.resolve(), args.code.resolve()
    seedable = not root.exists() or not any(root.iterdir())
    claim(root)
    sys.path.insert(0, str(code))
    if args.seed:
        if not seedable:
            sys.exit(f"--seed needs a new or empty --root; {root} is not empty")
        seed(root, code)
    fake = root / "fake_hunt.py"
    fake.write_text(FAKE_HUNT)

    import uvicorn
    from dashboard import schedule
    from dashboard.app import create_app

    app = create_app(root, hunt_command=[sys.executable, str(fake)], password=args.password,
                     schedule_entries=schedule.parse(args.schedule))
    print(f"SANDBOX http://127.0.0.1:{args.port} {app.state.csrf_token}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
