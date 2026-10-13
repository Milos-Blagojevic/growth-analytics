import sys
from datetime import datetime, timedelta

import pytest

import runs
from dashboard import db, jobs, schedule
from dashboard.app import create_app
from dashboard_data import client
from test_dashboard_jobs import STUB, finished

MONDAY = datetime(2026, 10, 12, 6, 59, 30)


def test_parse_reads_lines_and_semicolons_and_skips_comments():
    entries = schedule.parse("# weekly discovery\n0 7 * * 1 discover \"b2b saas\" --limit 20; 30 7 * * * run\n\n0 */6 * * * watch --dry-run")
    assert [e.form for e in entries] == [
        {"kind": "discover", "focus": "b2b saas", "limit": "20"},
        {"kind": "run", "mode": "deliver"},
        {"kind": "watch", "dry_run": "1"},
    ]
    assert schedule.parse("0 8 * * * run --dry-run")[0].form == {"kind": "run", "mode": "dry_run"}


@pytest.mark.parametrize(
    "line, message",
    [
        ("0 7 * * * send", "unsupported command"),
        ("0 7 * * * run --dry-run --research-only", "unsupported command"),
        ("0 7 * * * discover --limit 99", "--limit must be a number from 1 to 50"),
        ("0 7 * * * discover --verbose", "unsupported command"),
        ("0 24 * * * run", "hour '24'"),
        ("0 7 * * mon run", "day of week 'mon'"),
        ("0 7 30 2 * run", "never occurs"),
        ("0 7 * * run", "five cron fields"),
    ],
)
def test_parse_rejects_bad_entries_naming_the_line(line, message):
    with pytest.raises(ValueError, match=message) as error:
        schedule.parse(f"0 6 * * * watch\n{line}")
    assert line in str(error.value)


def test_next_after_follows_cron_rules():
    def next_of(line, after=MONDAY):
        return schedule.parse(line)[0].next_after(after)

    assert next_of("0 7 * * 1 watch") == datetime(2026, 10, 12, 7, 0)
    assert next_of("0 7 * * 1 watch", datetime(2026, 10, 12, 7, 0)) == datetime(2026, 10, 19, 7, 0)
    assert next_of("0 9 * * 7 watch") == datetime(2026, 10, 18, 9, 0)  # 7 is Sunday too
    assert next_of("*/20 9-10 * * * watch", datetime(2026, 10, 12, 10, 41)) == datetime(2026, 10, 13, 9, 0)
    # Both day fields restricted: either one matching is enough (the 15th, or any Friday).
    assert next_of("0 7 15 * 5 watch") == datetime(2026, 10, 15, 7, 0)
    assert next_of("0 7 15 * 5 watch", datetime(2026, 10, 15, 8, 0)) == datetime(2026, 10, 16, 7, 0)
    assert next_of("0 0 29 2 * watch") == datetime(2028, 2, 29, 0, 0)


class Starter:
    def __init__(self):
        self.busy = False
        self.error = None
        self.started = []

    def __call__(self, form):
        if self.busy:
            raise runs.Busy("busy")
        if self.error:
            raise ValueError(self.error)
        self.started.append(form["kind"])
        return len(self.started)


def test_due_commands_start_once_and_wait_while_another_runs():
    starter = Starter()
    sched = schedule.Scheduler(schedule.parse("0 7 * * * run; 0 7 * * * watch"), starter, clock=lambda: MONDAY)

    starter.busy = True  # e.g. a manual run still going at 07:00
    sched.tick(datetime(2026, 10, 12, 7, 0, 5))
    assert starter.started == [] and [s["pending"] for s in sched.status()] == [True, True]

    starter.busy = False
    sched.tick(datetime(2026, 10, 12, 7, 3))
    assert starter.started == ["run", "watch"]
    sched.tick(datetime(2026, 10, 12, 7, 4))
    assert starter.started == ["run", "watch"]  # 07:00 is not due again

    statuses = sched.status()
    assert [s["last"][1] for s in statuses] == [1, 2] and statuses[0]["next"] == datetime(2026, 10, 12, 7, 0)


def test_a_command_that_cannot_start_is_reported_not_retried():
    starter = Starter()
    starter.error = "companies.csv is missing or empty"
    sched = schedule.Scheduler(schedule.parse("0 7 * * * run"), starter, clock=lambda: MONDAY)
    sched.tick(datetime(2026, 10, 12, 7, 0))
    starter.error = None
    sched.tick(datetime(2026, 10, 12, 7, 1))
    assert starter.started == []
    assert sched.status()[0]["last"][1:] == (None, "companies.csv is missing or empty")


def test_scheduled_commands_run_as_dashboard_jobs(tmp_path):
    (tmp_path / "companies.csv").write_text("Acme,acme.com\n")
    (tmp_path / "stub_hunt.py").write_text(STUB)
    app = create_app(tmp_path, hunt_command=[sys.executable, str(tmp_path / "stub_hunt.py")],
                     schedule_entries=schedule.parse("* * * * * run"))
    app.state.scheduler.tick(datetime.now() + timedelta(minutes=1))

    conn = db.connect(tmp_path)
    [job] = jobs.recent(conn)
    conn.close()
    job = finished(tmp_path, job["id"])
    assert (job["label"], job["args"], job["status"]) == ("Scheduled · Run and notify · 1 companies in companies.csv",
                                                           ["run"], "succeeded")
    page = client(app).get("/runs").text
    assert "<code>* * * * * run</code>" in page and f'href="/runs/jobs/{job["id"]}">job {job["id"]}' in page
