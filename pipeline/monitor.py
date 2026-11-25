"""Exa Monitors for job postings: one monitor per company in monitors.csv (runs.MONITORS).

Exa runs each monitor by itself every PERIOD and returns only the pages its earlier runs did not. `sync` makes the
monitors on Exa match the list; `findings` reads what a company's monitor found since the last check, as one
research per new posting for the scorer. STATE keeps each company's monitor, the last run read and the postings
already judged, and the id that marks the monitors on Exa as this data folder's.
"""

import json
import logging
import uuid
from datetime import datetime
from itertools import islice, takewhile
from pathlib import Path
from urllib.parse import urlparse

from clients import exa_client
from pipeline import research, runs

logger = logging.getLogger("monitor")

STATE = "monitor_state.json"
PERIOD = "12h"  # between a monitor's runs (Exa's shortest is 1h); Exa picks the minute the cycle starts at
RUN_DOLLARS = 0.002  # what Exa bills a run that returns up to RESULTS pages
RESULTS = 10
MAX_RUNS = 60  # runs read per monitor in one check: thirty days of them
MAX_TRIES = 3  # checks in a row that may fail to read one run, or to score one posting, before it is given up
ENDED = ("completed", "failed", "cancelled")
SOURCE = "hunt.py"  # in each monitor's metadata: tells these monitors from others on the Exa account
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _is_entry(entry):
    """True for a monitor's entry as `sync` and the checks leave it: what they read without looking again."""
    return (
        isinstance(entry, dict)
        and all(isinstance(entry.get(key), str) for key in ("id", "name"))
        and isinstance(entry.get("params"), dict)
        and isinstance(entry.get("last_run", ""), str)
        and isinstance(entry.get("seen", []), list)
        and all(isinstance(posting, dict) and isinstance(posting.get("url"), str) for posting in entry.get("seen", []))
        and all(isinstance(entry.get(key, {}), dict) and all(isinstance(count, int) for count in entry.get(key, {}).values())
                for key in ("unread", "unscored"))
    )


def load_state(path):
    """{"folder": id, "monitors": {domain: {"id", "name", "params", "created_at", "last_run", "unread", "unscored",
    "seen": [{"title", "url", "first_seen", "verdict", "score"}]}}}; no file is no monitors. "folder" is set by
    the first sync (see `sync`). In an entry, "last_run" is missing until a check has read a run, "seen" until one
    has judged a posting; "unread" ({run id: checks}) and "unscored" ({posting link: checks}) count the checks
    that failed to read a run or to score a posting (see `findings`, `remember`). Raises ValueError for a file
    that is not that: an id that is no id would make other folders' monitors pass for this one's."""
    path = Path(path)
    if not path.exists():
        return {"monitors": {}}
    try:
        state = json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"not valid JSON ({exc})") from None
    if not isinstance(state, dict) or not isinstance(state.get("monitors"), dict):
        raise ValueError('expected {"monitors": {...}}')
    if not isinstance(state.get("folder", "unset"), str) or not state.get("folder", "unset"):
        raise ValueError('"folder" is not the id a monitor-sync saved')
    for domain, entry in state["monitors"].items():
        if not _is_entry(entry):
            raise ValueError(f"the entry of {domain} is not one a monitor-sync or a check saved")
    return state


def save_state(path, state):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic: a crash never leaves a half-written state file


def webhook_url(dashboard_url):
    """The address each monitor is created with. Exa asks for a public HTTPS one on every monitor; the monitors
    subscribe it to their own deletion only (see `params`), so the dashboard's address serves and nothing is sent
    there while a monitor runs. Raises ValueError for an address Exa would refuse."""
    parsed = urlparse(dashboard_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname in LOOPBACK:
        raise ValueError(
            f"Exa wants a public HTTPS address on every monitor and {dashboard_url!r} is not one: set DASHBOARD_URL "
            "to your dashboard's address (on Railway its public domain is used)"
        )
    return dashboard_url


def params(name, domain, profile, webhook, folder):
    """The monitor Exa is asked for: a search for the company's new postings in the profile's target roles, on the
    sites where it posts its jobs itself, answered as research.ATS_SCHEMA with each new page's text.

    Seniority and location stay out of the query, as in research.find_roles_on_fiber: asked for senior roles
    only, a search leaves out postings whose title does not say so, and both are the screen's and the scorer's
    call. The webhook Exa insists on is subscribed to the monitor's deletion only: the runs are read, not pushed.
    The metadata says whose the monitor is: this program's, the data folder with this `folder` id, the company.
    """
    return {
        "name": f"Jobs: {name}",
        "search": {
            "query": f"New job openings at {name} ({domain}) for any of: {', '.join(profile['search']['roles'])}.",
            "numResults": RESULTS,
            "includeDomains": [domain, f"*.{domain}", *research.ATS_DOMAINS],
            "contents": {"text": {"maxCharacters": research.DESCRIPTION_CHARS}},
        },
        "trigger": {"type": "interval", "period": PERIOD},
        "outputSchema": research.ATS_SCHEMA,
        "metadata": {"source": SOURCE, "folder": folder, "domain": domain},
        "webhook": {"url": webhook, "events": ["monitor.deleted"]},
    }


def _on_exa(live):
    """A monitor as Exa has it, in the shape of `params`: what one that is taken over is compared with."""
    dump = live.model_dump(by_alias=True, exclude_none=True)
    return {key: dump.get(key) for key in ("name", "search", "trigger", "outputSchema", "metadata", "webhook")}


def _sync_one(name, domain, wanted, entries, mine, save):
    """Creates or updates one company's monitor; returns (what was done, the monitor as Exa has it now).

    A company without a monitor in the state (or with one Exa no longer has) takes over the monitor that this
    folder created for its domain and lost track of, when `mine` holds one: the reply to a create was lost, or
    the state file is an older copy. Only when there is none is a monitor created, so a company never gets two.
    """
    entry = entries.get(domain)
    live = exa_client.get_monitor(entry["id"]) if entry else None
    done = None
    if live is None:
        judged = {"seen": entry["seen"]} if entry and entry.get("seen") else {}  # kept: no run of the old one is read
        twin = next((m for m in mine if m.metadata.get("domain") == domain), None)
        if twin is None:
            created = exa_client.create_monitor(wanted)
            entries[domain] = {"id": created.id, "name": name, "params": wanted, "created_at": created.created_at, **judged}
            save()
            done = "created again (Exa no longer had it)" if entry else "created"
            return done, exa_client.get_monitor(created.id) or created  # the next run is set a moment after the create
        mine.remove(twin)
        entry = entries[domain] = {"id": twin.id, "name": name, "params": _on_exa(twin), "created_at": twin.created_at, **judged}
        save()
        live, done = twin, "found on Exa and taken over"
    changed = {key: value for key, value in wanted.items() if entry["params"].get(key) != value}
    if not changed:
        return done or "unchanged", live
    exa_client.update_monitor(entry["id"], changed)
    entry.update(name=name, params=wanted)
    save()
    return done or f"updated ({', '.join(changed)})", exa_client.get_monitor(entry["id"]) or live


def sync(companies, profile, webhook, state, save):
    """Makes this folder's monitors on Exa match `companies`: creates the missing ones, updates those whose search
    no longer matches the profile, deletes those of companies no longer listed. `state` is changed in place and
    `save()`d after every change on Exa, so an interrupted sync never loses track of a monitor it created.

    A monitor is this folder's when its metadata carries the state's "folder" id, made up by the first sync. One
    that the state does not list (a create whose reply was lost) is taken over by its company, or deleted when
    nothing lists it: a company never has two, and none runs unaccounted for. Monitors this program created
    under another folder id are another data folder's (or this one's from before the state file was lost): they
    are never taken over, changed or deleted.

    Returns (lines, problems). lines: what was done for each company and when its monitor runs next. problems: a
    company whose request failed, a monitor Exa no longer runs by itself, and each monitor of another folder id:
    it runs and is billed, and only the owner can tell whether to delete it. One company's problem does not stop
    the others. When the account's monitors cannot be listed nothing is changed: a create could then give a
    company a second monitor.
    """
    entries = state["monitors"]
    lines, problems = [], []
    if "folder" not in state:
        state["folder"] = uuid.uuid4().hex[:12]
        save()  # before any create: the next sync has to know the monitors this one marks as the folder's
    try:
        ours = [m for m in exa_client.monitors() if (m.metadata or {}).get("source") == SOURCE]
    except Exception as exc:
        return [], [f"the account's monitors could not be listed, so nothing was changed ({type(exc).__name__}: {exc})"]
    known = {entry["id"] for entry in entries.values()}
    mine = [m for m in ours if m.id not in known and m.metadata.get("folder") == state["folder"]]
    others = [m for m in ours if m.id not in known and m.metadata.get("folder") != state["folder"]]
    listed = {domain for _, domain in companies}
    for domain in [d for d in entries if d not in listed]:
        entry = entries[domain]
        try:
            exa_client.delete_monitor(entry["id"])
        except Exception as exc:
            problems.append(f"{entry['name']} ({domain}): monitor {entry['id']} not deleted ({type(exc).__name__}: {exc})")
            continue
        del entries[domain]
        save()
        lines.append(f"{entry['name']} ({domain}): deleted")
    unsettled = set()  # companies whose monitor could not be looked at: a second one of theirs is left alone
    for name, domain in companies:
        try:
            wanted = params(name, domain, profile, webhook, state["folder"])
            done, live = _sync_one(name, domain, wanted, entries, mine, save)
        except Exception as exc:  # one company's failure must not stop the others
            problems.append(f"{name} ({domain}): {type(exc).__name__}: {exc}")
            unsettled.add(domain)
            continue
        if live.status != "active":
            problems.append(f"{name} ({domain}): the monitor is {live.status} on Exa and does not run by itself")
            lines.append(f"{name} ({domain}): {done}; {live.status} on Exa")
        elif live.next_run_at:
            lines.append(f"{name} ({domain}): {done}; next run {datetime.fromisoformat(live.next_run_at).astimezone():%Y-%m-%d %H:%M}")
        else:
            lines.append(f"{name} ({domain}): {done}")
    for left in [m for m in mine if m.metadata.get("domain") not in unsettled]:
        try:
            exa_client.delete_monitor(left.id)
        except Exception as exc:
            problems.append(f"{left.name or 'unnamed'} ({left.id}): not deleted ({type(exc).__name__}: {exc})")
            continue
        lines.append(f"{left.name or 'unnamed'} ({left.id}): deleted; this folder created it and nothing lists it")
    problems += [
        f"{m.name or 'unnamed'} ({m.id}, created {m.created_at[:10]}) runs on Exa, billed, under another folder's id: "
        "delete it on Exa's dashboard, or with `monitor-sync` in the folder that created it, unless that folder uses it"
        for m in others
    ]
    return lines, problems


def _runs_since(entry):
    """The monitor's runs after the last one read, oldest first: MAX_RUNS at most, the newest ones. The newest
    runs that are still going are left to the next check. A run still going behind one that has ended is not:
    Exa cancels a run when the next one starts, so that one is not waited for."""
    latest = islice(exa_client.monitor_runs(entry["id"]), MAX_RUNS)
    unread = list(takewhile(lambda run: run.id != entry.get("last_run"), latest))
    going = len(list(takewhile(lambda run: run.status not in ENDED, unread)))
    return unread[going:][::-1]


def _page_text(pages, role):
    """The text a run fetched from the posting's own page, "" when the posting's page is not among its pages."""
    for page in pages:
        url, text = (page.get("url"), page.get("text")) if isinstance(page, dict) else (None, None)
        if isinstance(url, str) and isinstance(text, str) and text.strip() and research.is_listed(url, [role]):
            return text.strip()[: research.DESCRIPTION_CHARS]
    return ""


def _one_line(role):
    """The posting with its title on one line, or None when its link has white space in it. Both come from a web
    page and go into a Telegram message as they are: a title with line breaks could pass for further postings."""
    url = role["url"].strip()
    if any(char.isspace() for char in url):
        return None
    return {**role, "title": " ".join(role["title"].split()), "url": url}


def _as_research(name, domain, role, cited):
    """One posting as the scorer reads a company's research. The company counts as verified: the owner named it,
    and the posting is on a site where it posts its jobs itself. Nothing else is known about it here."""
    return {
        "name": name,
        "domain": domain,
        "content": {"company": name, "domain": domain, "identity_verified": True, "business_summary": "unknown",
                    "signals": "unknown", "open_roles": [role]},
        "grounding": {"open_roles": cited} if cited else {},
        "role_source": "monitor",
        "dead_roles": [],
        "third_party_roles": [],
        "warnings": [],
    }


def findings(domain, entry):
    """What a company's monitor found since the last check: (found, dead, progress, problems).

    found: one research per new posting, shaped as research.research_company returns it, so the Jev screen and
    the scorer read it as they read a run's. The posting is its only open role, with the page's text as
    `description`: the text the run fetched when the posting's page is one of its pages, else fetched for every
    posting whose link is live. A posting is new when Exa marked it as the company's, its link is on a site
    where the company posts its jobs itself and is not dead, and no earlier check judged it (entry["seen"]). A
    link that cannot be checked is kept as "unverified", without the paid fetch a run settles it with: a posting
    is judged once here, and a fetch that failed must not pass for a posting that closed.
    dead: [{"title", "url"}] of the postings found whose link is closed already.
    progress: {"last_run", "unread"}, for `advance` once the postings are judged. last_run is the newest run
    read, which the next check starts after. unread is {run id: checks} for the runs whose output could not be
    read: the runs after such a run wait, since runs are read in order, until MAX_TRIES checks have failed on
    it. It is then given up for good: not asked for again, also when the same runs are gone through once more.
    problems: runs that failed on Exa's side, that could not be read, that are still going behind a later one, or
    that returned pages without a list of postings.
    Raises when the monitor's runs cannot be listed.
    """
    roles, cited, problems = [], {}, []
    last_run, unread, tried = entry.get("last_run"), {}, entry.get("unread") or {}
    for run in _runs_since(entry):
        if run.status == "failed":
            problems.append(f"run {run.id} failed on Exa: {run.fail_reason or 'no reason given'}")
        elif run.status not in ENDED:
            problems.append(f"run {run.id} is still {run.status} though a later run has ended: not read")
        elif run.status == "completed" and not run.empty and tried.get(run.id, 0) >= MAX_TRIES:
            unread[run.id] = tried[run.id]  # given up by an earlier check
        elif run.status == "completed" and not run.empty:
            try:
                content, grounding, pages = exa_client.monitor_run_output(entry["id"], run.id)
            except Exception as exc:
                unread[run.id] = tried.get(run.id, 0) + 1
                why = f"{type(exc).__name__}: {exc}"
                if unread[run.id] < MAX_TRIES:
                    problems.append(f"run {run.id} could not be read ({why}); the next check tries again")
                    break
                problems.append(f"run {run.id} could not be read in {MAX_TRIES} checks ({why}); given up")
            else:
                listed = content.get("open_roles") if isinstance(content, dict) else None
                if not isinstance(listed, list):
                    if pages:
                        problems.append(f"run {run.id} returned {len(pages)} page(s) without a list of postings")
                    listed = []
                for role in filter(None, map(_one_line, research.own_postings(listed, domain))):
                    if research.is_listed(role["url"], [*entry.get("seen", []), *roles]):
                        continue
                    text = _page_text(pages, role)
                    roles.append({**role, "description": text} if text else role)
                    cited[role["url"]] = grounding.get("open_roles")
        last_run = run.id
    usable, dead = research.verify_roles(roles, settle=False)
    fetched = iter(research.attach_descriptions([role for role in usable if "description" not in role], limit=None))
    usable = [role if "description" in role else next(fetched) for role in usable]
    found = [_as_research(entry["name"], domain, role, cited[role["url"]]) for role in usable]
    return found, dead, {"last_run": last_run, "unread": unread}, problems


def _put(entry, key, value):
    """Sets a key of an entry, or takes it out when there is nothing to keep."""
    if value:
        entry[key] = value
    else:
        entry.pop(key, None)


def advance(entry, progress, *, hold=False):
    """Puts a check's progress (see `findings`) into the entry.

    With `hold`, the last run read stays where it was: a posting is to be scored again (see `remember`), so its
    run has to be read again. The runs that were given up are then kept on record, so that going through them
    again does not start their count anew; otherwise only a run that is still being tried is kept.
    """
    _put(entry, "unread", {run: checks for run, checks in progress["unread"].items() if hold or checks < MAX_TRIES})
    if not hold:
        _put(entry, "last_run", progress["last_run"])


def remember(entry, results, dead):
    """Adds what a check judged to the entry's seen postings, so that later checks pass them by: each scored
    posting with its verdict and score, each dead link as "closed".

    Returns the results to judge again: those that failed. Their postings are not remembered, and the entry's
    "unscored" counts the checks each has failed. At MAX_TRIES a posting is remembered as "unscored" and passed by
    like the others, so one that Claude never scores is not paid for at every check.
    """
    now = datetime.now().isoformat(timespec="seconds")
    seen, tries, again = entry.setdefault("seen", []), dict(entry.get("unscored") or {}), []
    seen += [{"title": d["title"], "url": d["url"], "first_seen": now, "verdict": "closed", "score": None} for d in dead]
    for result in results:
        role = result["research"]["content"]["open_roles"][0]
        earlier = tries.pop(role["url"], 0)  # also when it scored at last: the count is over
        failed = earlier + 1 if "error" in result else 0
        if 0 < failed < MAX_TRIES:
            tries[role["url"]] = failed
            again.append(result)
            continue
        fit = result.get("fit") or {"verdict": "unscored", "score": None}
        seen.append({"title": role["title"], "url": role["url"], "first_seen": now,
                     "verdict": fit["verdict"], "score": fit["score"]})
    # a posting that failed once and has closed since is remembered now: nothing is left to count for it
    _put(entry, "unscored", {url: count for url, count in tries.items() if not research.is_listed(url, seen)})
    return again


def format_message(results, problems):
    """The Telegram text: the postings worth a look, best first, then what could not be checked."""
    lines = [f"Monitors: {len(results)} new posting(s) for you"]
    for result in sorted(results, key=lambda r: r["fit"]["score"], reverse=True):
        role, fit = result["research"]["content"]["open_roles"][0], result["fit"]
        lines.append(f"\n[{fit['score']}] {fit['verdict'].upper()} {result['name']}: {role['title']}\n{role['url']}")
    if problems:
        lines.append("\nCould not check:\n" + "\n".join(f"- {p}" for p in problems))
    return "\n".join(lines)
