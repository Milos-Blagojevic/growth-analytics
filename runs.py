"""Saved-run format and the run lock, shared by hunt.py and the dashboard.

out/<%Y%m%d-%H%M%S>/ holds one <domain>.json per company and the MANIFEST (mode, times, counts, costs).
LOCK_FILE lets one hunt.py command run at a time, whether started from a shell, cron or the dashboard.
"""

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

MANIFEST = "_run.json"  # the underscore keeps it apart from <domain>.json files
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
MANIFEST = "_run.json"  # the underscore keeps it apart from <domain>.json files


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
