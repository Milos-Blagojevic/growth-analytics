"""Saved-run format, the domain normaliser, the monitors list and the run lock, shared by hunt.py and the dashboard.

out/<%Y%m%d-%H%M%S>/ holds one <domain>.json per company and the MANIFEST (mode, times, counts, costs).
Domains name those files, so companies.csv, discover and the dashboard all normalise them the same way here.
MONITORS lists the companies `monitor-sync` keeps an Exa monitor for; the dashboard's New, Edit and Remove change it.
LOCK_FILE lets one hunt.py command run at a time, whether started from a shell, cron or the dashboard.
"""

import csv
import fcntl
import io
import json
import os
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
    return [company for _, _, company in _monitor_records(path) if company]


def _monitor_records(path):
    """monitors.csv as [(text, fields, company)]: each record with the lines it spans as they are written, its fields,
    and the (name, domain) it lists, None for a blank line or a '#' comment; [] when the file is missing. Raises
    ValueError as `read_monitors` does."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            lines = f.readlines()
        reader = csv.reader(lines)
        rows = [(row, reader.line_num) for row in reader]  # line_num: the lines read so far, a quoted newline included
    except (OSError, UnicodeDecodeError, csv.Error) as exc:  # a directory, a field beyond csv's limit, ...
        raise ValueError(f"cannot be read: {exc}") from exc
    records, start, listed = [], 0, set()
    for row, end in rows:
        company = None
        if "".join(row).strip() and not row[0].strip().startswith("#"):
            company = monitored(*(row + ["", ""])[:2])
            if company[1] in listed:
                raise ValueError(f"{company[1]} is listed twice")
            listed.add(company[1])
        records.append(("".join(lines[start:end]), row, company))
        start = end
    return records


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


def edit_monitor(path, current, name, domain):
    """Changes the monitors.csv row of the company listed under the domain `current` to `name` and `domain`, keeping
    its extra columns and every other line as written; returns (name, domain) as written. Raises ValueError for a
    company `monitored` refuses, a `current` the file does not list, a `domain` another row lists, a row that would
    stay as it is and a file `read_monitors` cannot read."""
    name, domain = monitored(name, domain)
    current = normalize_domain(str(current))
    records = _monitor_records(path)
    listed = {company[1] for _, _, company in records if company}
    if current not in listed:
        raise ValueError(f"{current} is not listed")
    if domain != current and domain in listed:
        raise ValueError(f"{domain} is monitored already")
    texts = []
    for text, row, company in records:
        if company and company[1] == current:
            if company == (name, domain):
                raise ValueError(f"{name} ({domain}) is listed so already")
            out = io.StringIO()
            csv.writer(out, lineterminator="").writerow([name, domain, *row[2:]])
            text = out.getvalue() + text[len(text.rstrip("\r\n")):]  # the line ending it had
        texts.append(text)
    _replace_monitors(path, texts)
    return name, domain


def remove_monitors(path, domains):
    """Deletes the monitors.csv rows of `domains`, keeping every other line as written; returns the (name, domain) of
    each, in the file's order. Raises ValueError for no domain, a domain the file does not list and a file
    `read_monitors` cannot read."""
    wanted = {normalize_domain(str(domain)) for domain in domains if str(domain).strip()}
    if not wanted:
        raise ValueError("no company to remove")
    records = _monitor_records(path)
    unknown = wanted - {company[1] for _, _, company in records if company}
    if unknown:
        raise ValueError(f"not listed: {', '.join(sorted(unknown))}")
    _replace_monitors(path, [text for text, _, company in records if not (company and company[1] in wanted)])
    return [company for _, _, company in records if company and company[1] in wanted]


def _replace_monitors(path, texts):
    """Writes monitors.csv anew at once: a crash never leaves half a list, which the next sync would take for the
    companies to keep and delete the monitors of the rest."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text("".join(texts), encoding="utf-8", newline="")
    os.replace(tmp, path)


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
