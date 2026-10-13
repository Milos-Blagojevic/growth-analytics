"""Scheduled hunt.py commands (HUNT_SCHEDULE), started by the dashboard process.

One entry per line (or separated by ";"): five cron fields, then the hunt.py command, for example

    0 7 * * 1   discover --limit 15
    30 7 * * *  run
    0 */6 * * * watch

Times are the dashboard's local time (the TZ environment variable). Commands are limited to what the Runs page
can start: `run [--dry-run | --research-only]`, `discover [focus] [--limit N]` and `watch [--dry-run]`. A due
command that finds another one running stays pending and starts as soon as the lock is free; commands due while
the dashboard was down are not caught up.
"""

import logging
import shlex
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import runs
from dashboard import jobs

logger = logging.getLogger("dashboard.schedule")

# name, lowest, highest; day of week accepts 7 for Sunday, as cron does
FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))
COMMANDS = "run [--dry-run | --research-only], discover [focus] [--limit N] or watch [--dry-run]"
MAX_CATCH_UP = timedelta(days=1)  # after a long pause (laptop sleep), check at most this many past minutes


def _field(text, name, lo, hi):
    values = set()
    for part in text.split(","):
        base, slash, step = part.partition("/")
        try:
            step = int(step) if slash else 1
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                start, end = (int(v) for v in base.split("-", 1))
            else:
                start = int(base)
                end = hi if slash else start
        except ValueError:
            raise ValueError(f"{name} {text!r}: use numbers, *, ranges (a-b), lists (a,b) and steps (*/n)") from None
        if step < 1 or not lo <= start <= end <= hi:
            raise ValueError(f"{name} {text!r}: values must be within {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def _form(args):
    """hunt.py arguments -> the form fields jobs.build validates, as if submitted from the Runs page."""
    command, rest = (args[0], args[1:]) if args else ("", [])
    if command == "run":
        modes = {"--dry-run": "dry_run", "--research-only": "research_only"}
        if len(rest) <= 1 and all(a in modes for a in rest):
            return {"kind": "run", "mode": modes[rest[0]] if rest else "deliver"}
    elif command == "watch":
        if rest in ([], ["--dry-run"]):
            return {"kind": "watch", "dry_run": "1" if rest else ""}
    elif command == "discover":
        focus, limit = [], "15"
        words = iter(rest)
        for word in words:
            if word == "--limit":
                limit = next(words, "")
                if not limit.isdigit() or not 1 <= int(limit) <= jobs.MAX_DISCOVER_LIMIT:
                    raise ValueError(f"--limit must be a number from 1 to {jobs.MAX_DISCOVER_LIMIT}")
            elif word.startswith("-"):
                break
            else:
                focus.append(word)
        else:
            return {"kind": "discover", "focus": " ".join(focus), "limit": limit}
    raise ValueError(f"unsupported command {shlex.join(args)!r}; use {COMMANDS}")


@dataclass(frozen=True)
class Entry:
    text: str  # the line as configured, shown on the Runs page
    command: str
    form: dict = field(compare=False)
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset  # 0 = Sunday
    any_day: bool  # day of month is "*..."
    any_weekday: bool  # day of week is "*..."

    def _day_matches(self, when):
        day, weekday = when.day in self.days, (when.weekday() + 1) % 7 in self.weekdays
        if self.any_day or self.any_weekday:  # cron: when both are restricted, either one matching is enough
            return day and weekday
        return day or weekday

    def matches(self, when):
        return (
            when.minute in self.minutes
            and when.hour in self.hours
            and when.month in self.months
            and self._day_matches(when)
        )

    def next_after(self, when):
        """The first matching minute after `when`, or None if there is none within 5 years."""
        t = when.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=5 * 366)
        while t < limit:
            if t.month not in self.months:
                t = (t.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            elif not self._day_matches(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minutes:
                t += timedelta(minutes=1)
            else:
                return t
        return None


def parse(text):
    """Entries of a HUNT_SCHEDULE value. Blank lines and '#' comments are skipped. Raises ValueError naming the line."""
    entries = []
    for line in (part.strip() for chunk in (text or "").splitlines() for part in chunk.split(";")):
        if not line or line.startswith("#"):
            continue
        try:
            words = shlex.split(line)
            if len(words) < 6:
                raise ValueError(f"expected five cron fields and a command; use {COMMANDS}")
            sets = [_field(words[i], *FIELDS[i]) for i in range(5)]
            weekdays = frozenset(d % 7 for d in sets[4])
            entry = Entry(line, shlex.join(words[5:]), _form(words[5:]), *sets[:4], weekdays,
                          words[2].startswith("*"), words[4].startswith("*"))
            if entry.next_after(datetime(2000, 1, 1)) is None:
                raise ValueError("this date never occurs")
        except ValueError as exc:
            raise ValueError(f"HUNT_SCHEDULE {line!r}: {exc}") from None
        entries.append(entry)
    return entries


class Scheduler:
    """Starts each entry's command when its time comes. `start(form)` starts a job and returns its id; it raises
    runs.Busy while another command runs (the entry then stays pending) and ValueError/OSError when the command
    cannot start (reported on the Runs page and in the log)."""

    def __init__(self, entries, start, *, clock=datetime.now):
        self.entries = list(entries)
        self._start = start
        self._clock = clock
        self._lock = threading.Lock()
        self._pending = set()  # indexes of entries that came due and have not started yet
        self._last = {}  # index -> (when, job id or None, error or None)
        self._checked = clock().replace(second=0, microsecond=0)
        self._stop = threading.Event()
        self._thread = None

    def tick(self, now=None):
        """Marks entries due in the minutes since the last tick (up to `now`, default the clock) as pending, then
        tries to start every pending one."""
        now = (now or self._clock()).replace(second=0, microsecond=0)
        minute = max(self._checked, now - MAX_CATCH_UP) + timedelta(minutes=1)
        with self._lock:
            while minute <= now:
                self._pending.update(i for i, e in enumerate(self.entries) if e.matches(minute))
                minute += timedelta(minutes=1)
            self._checked = max(self._checked, now)
            pending = sorted(self._pending)
        for i in pending:
            entry = self.entries[i]
            try:
                job_id = self._start(entry.form)
            except runs.Busy:
                continue  # retried on the next tick
            except (ValueError, OSError) as exc:
                logger.warning("scheduled %r did not start: %s", entry.command, exc)
                outcome = (None, str(exc))
            else:
                logger.info("scheduled %r started as job %s", entry.command, job_id)
                outcome = (job_id, None)
            with self._lock:
                self._pending.discard(i)
                self._last[i] = (self._clock(), *outcome)

    def status(self):
        """Per entry, for the Runs page: text, command, next time, pending, last start (when, job id, error)."""
        now = self._clock()
        with self._lock:
            return [
                {"text": e.text, "command": e.command, "next": e.next_after(now), "pending": i in self._pending,
                 "last": self._last.get(i)}
                for i, e in enumerate(self.entries)
            ]

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="hunt-schedule", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.wait(60.5 - self._clock().second):  # just after each minute boundary
            try:
                self.tick()
            except Exception:  # keep scheduling even if one start blows up unexpectedly
                logger.exception("scheduler tick failed")
