"""The Monitors block on Runs: the companies in monitors.csv, which of them have a monitor on Exa, and New.

New adds a company to monitors.csv and starts `hunt.py monitor-sync`, which creates its monitor, so the next
scheduled `monitor` reads what it finds. The company is written only once the sync is sure to start
(`jobs.Spec.prepare`, under the runner's lock): a start that is refused leaves the list as it was.

The sync deletes the monitor of every company monitors.csv leaves out. So New never creates the file while
monitor_state.json lists monitors: a list of the new company alone would delete the others, which is also why
`hunt.py monitor-sync` refuses to run without the file then.
"""

import json
from pathlib import Path

from dashboard import jobs
from pipeline import runs

# pipeline/monitor.py's STATE: that module loads the Exa client, which the dashboard never imports.
STATE = "monitor_state.json"


def listing(root):
    """(companies, error). companies: monitors.csv's, each {"name", "domain", "on_exa", "since"}: whether
    monitor_state.json lists its monitor, and when Exa created it. error: why New cannot add a company (monitors.csv
    cannot be read, or is missing while monitors run), or None."""
    try:
        companies = runs.read_monitors(Path(root) / runs.MONITORS)
    except ValueError as exc:
        return [], f"{runs.MONITORS}: {exc}. Fix the file before adding a company."
    synced = _synced(Path(root) / STATE)
    if not companies:
        missing = _missing(root, synced)
        if missing:
            return [], missing
    return [{"name": name, "domain": domain, "on_exa": domain in synced, "since": synced.get(domain)}
            for name, domain in companies], None


def _synced(path):
    """{domain: created_at} of the monitors in monitor_state.json; {} when it is missing or not what a sync writes."""
    try:
        monitors = json.loads(path.read_text())["monitors"]
        return {domain: str(entry.get("created_at") or "") for domain, entry in monitors.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _missing(root, synced):
    """Why New may not create monitors.csv, or None: the file is missing while monitor_state.json lists monitors."""
    if (Path(root) / runs.MONITORS).exists() or not synced:
        return None
    return (f"{runs.MONITORS} not found, and {len(synced)} monitor(s) of this folder run on Exa: create it with a "
            "name,domain row per company to keep before adding one. A list of the new company alone would make "
            "its sync delete the others.")


def new(root, form):
    """The job New starts: `monitor-sync`, with the company added to monitors.csv just before it starts. Raises
    ValueError for a company that cannot be monitored or is monitored already, for a monitors.csv that cannot be
    read, and for a missing one while monitors run (`_missing`)."""
    path = Path(root) / runs.MONITORS
    name, domain = runs.monitored(form.get("name", ""), form.get("domain", ""))
    try:
        listed = {listed for _, listed in runs.read_monitors(path)}
    except ValueError as exc:
        raise ValueError(f"{runs.MONITORS}: {exc}") from exc
    if domain in listed:
        raise ValueError(f"{domain} is monitored already")
    missing = _missing(root, _synced(Path(root) / STATE))
    if missing:
        raise ValueError(missing)

    def add():
        missing = _missing(root, _synced(Path(root) / STATE))  # again: the files may have changed since
        if missing:
            raise ValueError(missing)
        try:
            runs.add_monitor(path, name, domain)
        except ValueError as exc:  # the file changed since: listed meanwhile, or no longer readable
            raise ValueError(f"{runs.MONITORS}: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"{runs.MONITORS} could not be written: {exc}") from exc

    return jobs.Spec("monitor_sync", ["monitor-sync"], f"New monitor: {name} ({domain})", {"kind": "monitor_sync"},
                     estimate=jobs.MONITOR_SYNC_ESTIMATE, warnings=[f"Adds {name} ({domain}) to {runs.MONITORS}.",
                                                                    *jobs.MONITOR_SYNC_WARNINGS],
                     prepare=add)
