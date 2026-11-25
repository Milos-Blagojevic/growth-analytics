"""The Monitors block on Runs: the companies in monitors.csv, which of them have a monitor on Exa or are read from
their job board, New, Edit and Remove.

New adds a company to monitors.csv, Edit changes a company's row and Remove deletes the rows of the companies
picked; each then starts `hunt.py monitor-sync`, which creates, updates and deletes the monitors to match (a
company whose job board has a public feed gets none: its board is read), so the next scheduled `monitor` reads
what they find. The list is changed only once the sync is sure to start (`jobs.Spec.prepare`, under the runner's
lock): a start that is refused leaves the list as it was.

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
SHOWN = 10  # companies the block lists before "Show all"


def listing(root):
    """(companies, error). companies: monitors.csv's, each {"name", "domain", "on_exa", "board", "since"}: whether
    monitor_state.json lists its monitor, the host of the job board it is read from instead (None for a company
    on a monitor), and since when. error: why New cannot add a company (monitors.csv cannot be read, or is missing
    while monitors run), or None."""
    try:
        companies = runs.read_monitors(Path(root) / runs.MONITORS)
    except ValueError as exc:
        return [], f"{runs.MONITORS}: {exc}. Fix the file before adding a company."
    synced = _synced(Path(root) / STATE)
    if not companies:
        missing = _missing(root, synced)
        if missing:
            return [], missing
    return [{"name": name, "domain": domain, "on_exa": domain in synced and not synced[domain]["board"],
             "board": synced.get(domain, {}).get("board"), "since": synced.get(domain, {}).get("since")}
            for name, domain in companies], None


def company(root, domain):
    """(name, domain) of the company monitors.csv lists under `domain`. Raises ValueError for a monitors.csv that
    cannot be read, LookupError for a domain it does not list."""
    domain = runs.normalize_domain(str(domain))
    listed = {listed: name for name, listed in _read(root)}
    if domain not in listed:
        raise LookupError(f"{domain or 'The company'} is not in {runs.MONITORS}")
    return listed[domain], domain


def _read(root):
    try:
        return runs.read_monitors(Path(root) / runs.MONITORS)
    except ValueError as exc:
        raise ValueError(f"{runs.MONITORS}: {exc}") from exc


def _synced(path):
    """{domain: {"since", "board"}} of the companies in monitor_state.json: when Exa created the company's monitor,
    or the host of the job board it is read from and since when. {} when the file is missing or not what a sync
    writes."""
    try:
        monitors = json.loads(path.read_text())["monitors"]
        return {
            domain: {"since": str(entry.get("since" if entry.get("board") else "created_at") or ""),
                     "board": str(entry["board"].get("host") or "") if entry.get("board") else None}
            for domain, entry in monitors.items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _missing(root, synced):
    """Why New may not create monitors.csv, or None: the file is missing while monitor_state.json lists monitors."""
    if (Path(root) / runs.MONITORS).exists() or not synced:
        return None
    return (f"{runs.MONITORS} not found, and {len(synced)} monitor(s) of this folder run on Exa: create it with a "
            "name,domain row per company to keep before adding one. A list of the new company alone would make "
            "its sync delete the others.")


def _sync(root, label, fields, changes, event, change, check=None):
    """The `monitor-sync` job New, Edit and Remove start. `change(path)`, the pipeline/runs.py function that changes
    monitors.csv, runs just before it starts (`jobs.Spec.prepare`) after `check()`: both look at the files again,
    which may have changed since the job was checked."""
    path = Path(root) / runs.MONITORS

    def prepare():
        if check:
            check()
        try:
            change(path)
        except ValueError as exc:  # listed or unlisted meanwhile, or no longer readable
            raise ValueError(f"{runs.MONITORS}: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"{runs.MONITORS} could not be written: {exc}") from exc

    return jobs.Spec("monitor_sync", ["monitor-sync"], label, fields, estimate=jobs.MONITOR_SYNC_ESTIMATE,
                     warnings=[*changes, *jobs.MONITOR_SYNC_WARNINGS], event=event, prepare=prepare)


def new(root, form):
    """The job New starts: `monitor-sync`, with the company added to monitors.csv just before it starts. Raises
    ValueError for a company that cannot be monitored or is monitored already, for a monitors.csv that cannot be
    read, and for a missing one while monitors run (`_missing`)."""
    name, domain = runs.monitored(form.get("name", ""), form.get("domain", ""))
    if domain in {listed for _, listed in _read(root)}:
        raise ValueError(f"{domain} is monitored already")

    def check():
        missing = _missing(root, _synced(Path(root) / STATE))
        if missing:
            raise ValueError(missing)

    check()
    return _sync(root, f"New monitor: {name} ({domain})", {"name": name, "domain": domain},
                 [f"Adds {name} ({domain}) to {runs.MONITORS}."], ("monitor_added", {}),
                 lambda path: runs.add_monitor(path, name, domain), check)


def edit(root, form):
    """The job Edit starts: `monitor-sync`, with the row of the company monitors.csv lists under `current` changed to
    `name` and `domain` just before it starts. Raises ValueError for a company that cannot be monitored, a `current`
    not listed, a domain another row lists, an edit that changes nothing and a monitors.csv that cannot be read."""
    current = runs.normalize_domain(str(form.get("current", "")))
    listed = {listed: name for name, listed in _read(root)}
    if current not in listed:
        raise ValueError(f"{current or 'The company'} is not in {runs.MONITORS}")
    old = listed[current]
    name, domain = runs.monitored(form.get("name", ""), form.get("domain", ""))
    if (name, domain) == (old, current):
        raise ValueError(f"Nothing to change: {runs.MONITORS} lists {name} ({domain}) so already.")
    if domain != current and domain in listed:
        raise ValueError(f"{domain} is monitored already")
    changes = [f"Changes {old} ({current}) to {name} ({domain}) in {runs.MONITORS}."]
    if domain != current:
        changes.append(f"The sync deletes the monitor of {current} and creates one for {domain}; the postings "
                       f"judged for {current} are forgotten.")
    else:
        changes.append("The sync updates the monitor's search on Exa to the new name.")
    return _sync(root, f"Edit monitor: {name} ({domain})", {"current": current, "name": name, "domain": domain},
                 changes, ("monitor_edited", {"name_changed": name != old, "domain_changed": domain != current}),
                 lambda path: runs.edit_monitor(path, current, name, domain))


def remove(root, form):
    """The job Remove starts: `monitor-sync`, with the rows of the companies in `domains` deleted from monitors.csv
    just before it starts, so the sync deletes their monitors. Raises ValueError for no company, one not listed and a
    monitors.csv that cannot be read."""
    given = form.getlist("domains") if hasattr(form, "getlist") else form.get("domains") or []
    wanted = {runs.normalize_domain(str(domain)) for domain in given if str(domain).strip()}
    if not wanted:
        raise ValueError("Pick the companies to remove.")
    companies = _read(root)
    unknown = wanted - {domain for _, domain in companies}
    if unknown:
        raise ValueError(f"Not in {runs.MONITORS}: {', '.join(sorted(unknown))}")
    picked = [(name, domain) for name, domain in companies if domain in wanted]
    names, one = ", ".join(f"{name} ({domain})" for name, domain in picked), len(picked) == 1
    changes = [f"Removes {names} from {runs.MONITORS}.",
               f"The sync deletes {'its monitor' if one else 'their monitors'} on Exa, which stops the billing, and "
               f"forgets the postings judged for {'it' if one else 'them'}."]
    domains = [domain for _, domain in picked]
    return _sync(root, f"Remove monitor: {names}" if one else f"Remove {len(picked)} monitors", {"domains": domains},
                 changes, ("monitors_removed", {"monitor_count": len(picked)}),
                 lambda path: runs.remove_monitors(path, domains))
