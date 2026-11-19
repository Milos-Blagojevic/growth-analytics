"""Saved-run format, the domain normaliser, the monitors list and the run lock, shared by hunt.py and the dashboard.

out/<%Y%m%d-%H%M%S>/ holds one <domain>.json per company and the MANIFEST (mode, times, counts, costs).
Domains name those files, so companies.csv, discover and the dashboard all normalise them the same way here.
MONITORS lists the companies `monitor-sync` keeps an Exa monitor for; the dashboard's New adds to it.
LOCK_FILE lets one hunt.py command run at a time, whether started from a shell, cron or the dashboard.
"""

import csv
import fcntl
import json
import re
from contextlib import contextmanager
from pathlib import Path

MANIFEST = "_run.json"  # the underscore keeps it apart from <domain>.json files
MONITORS = "monitors.csv"
MONITOR_NAME_CHARS = 100  # the name goes into the monitor's Exa query and every Telegram line about the company
HOST_NAME = re.compile(r"[\w-]+(\.[\w-]+)+")  # labels of letters, digits and hyphens, joined by dots
LOCK_FILE = ".hunt.lock"


class Busy(RuntimeError):
    """Another hunt.py command holds the lock."""


@contextmanager
def lock(root="."):
    """Holds the run lock for the block; raises Busy at once if another process holds it.

    flock locks are released by the OS when the process exits, so a crashed command never leaves it stuck.
    """
    with open(Path(root) / LOCK_FILE, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another hunt.py command is running") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def is_locked(root="."):
    """True while a hunt.py command holds the lock (checked by taking and releasing it)."""
    try:
        with lock(root):
            return False
    except Busy:
        return True


def without_scheme(domain):
    """`domain` lower-cased, without a leading https://, http:// or www.; the path (if any) is kept."""
    domain = domain.strip().lower()
    for prefix in ("https://", "http://", "www."):
        domain = domain.removeprefix(prefix)
    return domain


def normalize_domain(domain):
    """The bare host of `domain` (scheme, www. and path dropped, lower-cased): what names the <domain>.json result file,
    so every reader and writer of companies.csv and discovered.csv agrees on one spelling per company."""
    return without_scheme(domain).split("/")[0]


def monitored(name, domain):
    """(name, domain) of one monitors.csv row: the name on one line, the domain spelled as in companies.csv.
    Raises ValueError for a row that cannot name a monitor: no name, one longer than MONITOR_NAME_CHARS, a name the
    file would read as a comment, a domain that is no bare host name (a path, no dot, a space or a comma)."""
    name, given = " ".join(str(name).split()), str(domain).strip()
    domain = normalize_domain(given)
    if (not name or len(name) > MONITOR_NAME_CHARS or name.startswith("#") or not HOST_NAME.fullmatch(domain)
            or "/" in without_scheme(given).rstrip("/")):
        raise ValueError(f"each company needs a name and a domain such as acme.com, the name at most "
                         f"{MONITOR_NAME_CHARS} characters; got {name[:40]!r}, {given[:80]!r}")
    return name, domain


def read_monitors(path):
    """[(name, domain)] of monitors.csv (name,domain rows; extra columns, blank lines and '#' comments are skipped);
    [] when the file is missing. Raises ValueError for a file that cannot be read, a row `monitored` refuses and a
    domain listed twice: `monitor-sync` deletes the monitor of every company the list leaves out, so a row is never
    skipped."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:  # a directory, a field beyond csv's limit, ...
        raise ValueError(f"cannot be read: {exc}") from exc
    companies, listed = [], set()
    for row in rows:
        if not "".join(row).strip() or row[0].strip().startswith("#"):
            continue
        name, domain = monitored(*(row + ["", ""])[:2])
        if domain in listed:
            raise ValueError(f"{domain} is listed twice")
        listed.add(domain)
        companies.append((name, domain))
    return companies


def add_monitor(path, name, domain):
    """Appends one company to monitors.csv (created when missing); returns its (name, domain) as written. Raises
    ValueError for a company `monitored` refuses, one listed already and a file `read_monitors` cannot read."""
    name, domain = monitored(name, domain)
    if domain in {listed for _, listed in read_monitors(path)}:
        raise ValueError(f"{domain} is monitored already")
    path = Path(path)
    ends = path.read_bytes()[-1:] if path.exists() else b"\n"
    with open(path, "a", newline="", encoding="utf-8") as f:
        if ends not in (b"", b"\n"):
            f.write("\n")
        csv.writer(f, lineterminator="\n").writerow([name, domain])
    return name, domain


def run_dirs(out_dir):
    """Saved run directories, oldest first (names are timestamps)."""
    out_dir = Path(out_dir)
    return sorted(p for p in out_dir.iterdir() if p.is_dir()) if out_dir.is_dir() else []


def load_results(run_dir):
    """The per-company results of a saved run, sorted by file name."""
    return [json.loads(f.read_text()) for f in sorted(Path(run_dir).glob("*.json")) if f.name != MANIFEST]


def load_manifest(run_dir):
    """The run's MANIFEST, or None for runs saved before it existed."""
    path = Path(run_dir) / MANIFEST
    return json.loads(path.read_text()) if path.exists() else None
