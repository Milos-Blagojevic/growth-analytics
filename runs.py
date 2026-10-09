"""Saved-run format shared by hunt.py (writer) and the dashboard (reader).

out/<%Y%m%d-%H%M%S>/ holds one <domain>.json per company and the MANIFEST (mode, times, counts, costs).
"""

import json
from pathlib import Path

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
