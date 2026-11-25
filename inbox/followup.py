"""Follow-ups: the applications that have had no reply for SILENT_DAYS days, and whether each posting is still open.

The applications are the items in status Applied in the dashboard's database (hunt.db), read as it is: opened
read-only, never migrated and never rebuilt from the files, so the command changes nothing the dashboard owns. A
reply is a rejection or an interview invite among the saved email updates (the mail/ files, see mail.py), received
after the application was sent, from the same company; whether you have confirmed it on Pipeline does not matter.

Each waiting application is reported once: STATE (followup_state.json) holds the ones already reported. Only the
logic is here; `hunt.py followup` sends the message. No paid call: the posting pages and the job boards' public
feeds are read with plain HTTP.

Company names, titles and links come from web pages and emails. They go into the Telegram message on one line each
and are never followed as instructions.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from clients import boards
from dashboard import email_updates, tracking
from inbox import mail
from pipeline import research, runs

logger = logging.getLogger("followup")

SILENT_DAYS = 8
STATE = "followup_state.json"
DB = "hunt.db"  # the dashboard's database, in the data folder
OUT_DIR = "out"
REPLIES = ("rejected", "interview")  # outcomes of an email update that answer an application
# The groups of the message, in its order: what `still_open` says, and the heading of the group.
HEADINGS = (("open", "Still open, worth a follow-up:"), ("closed", "Posting closed, move on:"),
            ("unknown", "Could not check:"))
# The job board that sent a confirmation email, by the domain it sends from.
SENDER_ATS = {
    "ashbyhq.com": "ashby", "greenhouse-mail.io": "greenhouse", "greenhouse.io": "greenhouse", "lever.co": "lever",
    "workablemail.com": "workable", "workable.com": "workable", "smartrecruiters.com": "smartrecruiters",
    "recruitee.com": "recruitee", "teamtailor-mail.com": "teamtailor", "personio.de": "personio",
    "personio.com": "personio", "pinpointhq.com": "pinpoint",
}
TEAMTAILOR_MAIL = ".teamtailor-mail.com"  # Teamtailor sends from <board>.teamtailor-mail.com
# Where each job board keeps a company's board, when only the board's name is known.
BOARD_HOSTS = {
    "ashby": boards.ASHBY_HOST, "greenhouse": "job-boards.greenhouse.io", "lever": "jobs.lever.co",
    "workable": "apply.workable.com", "smartrecruiters": "jobs.smartrecruiters.com",
    "recruitee": "{slug}.recruitee.com", "teamtailor": "{slug}.teamtailor.com",
    "personio": "{slug}.jobs.personio.de", "pinpoint": "{slug}.pinpointhq.com",
}
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # a board name that is safe in a host name and in a path
ADDRESS = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")  # the address of "Name <address>"


def load_state(path):
    """{"nudged": {"<kind>:<ref>": ISO time}}: the applications already reported and when; no file is none. Raises
    ValueError for a file that is not that: read as empty, it would report every waiting application again."""
    path = Path(path)
    if not path.exists():
        return {"nudged": {}}
    try:
        state = json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"not valid JSON ({exc})") from None
    if not isinstance(state, dict) or not isinstance(state.get("nudged"), dict):
        raise ValueError('expected {"nudged": {...}}')
    if not all(isinstance(key, str) and isinstance(when, str) for key, when in state["nudged"].items()):
        raise ValueError('"nudged" holds an entry that is not an application and the time it was reported')
    return state


def save_state(path, state):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic: a crash never leaves a half-written state file


def key(item):
    """How the state names an application: the kind and ref the dashboard tracks it by."""
    return f"{item['kind']}:{item['ref']}"


def applications(path):
    """The tracked items in status Applied, as `tracking.pipeline` describes them (kind, ref, applied_at, title,
    name, domain, url). One added from a confirmation email (kind "email") also gets "email": the company and the
    role as the email names them and its sender, or None when the database no longer holds them.

    The database is opened read-only. Raises ValueError when it cannot be read: a file that is no database, or one
    the dashboard has not brought up to date yet."""
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError(f"cannot be opened ({exc})") from None
    conn.row_factory = sqlite3.Row
    try:
        items = tracking.pipeline(conn)["applied"]
        for item in items:
            if item["kind"] == "email":
                row = conn.execute("SELECT company, role, sender FROM mail_applications WHERE update_id = ?",
                                   [item["ref"]]).fetchone()
                item["email"] = dict(row) if row else None
        return items
    except sqlite3.Error as exc:
        raise ValueError(f"cannot be read ({exc}); open the dashboard once, which brings it up to date") from None
    finally:
        conn.close()


def _when(stamp):
    """An ISO time as local time without a zone, the way the dashboard and `hunt.py mail` save times; None for
    anything else."""
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def replies(root="."):
    """The rejections and interview invites in the saved mail/ files: [(update, when it was received)]. The files
    are read themselves, not the dashboard's copy of them, which is only as new as its last page load. A file or an
    update that cannot be read is skipped with a warning: at worst an answered application is reported."""
    found = []
    for path in mail.update_files(root):
        try:
            updates = json.loads(path.read_text())["updates"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("skipping %s: %s", path, type(exc).__name__)
            continue
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict) or update.get("outcome") not in REPLIES:
                continue
            when = _when(update.get("received_at"))
            named = isinstance(update.get("company"), str) and isinstance(update.get("from_address"), str)
            if when is None or not named:
                logger.warning("skipping an update of %s that is not one `hunt.py mail` saved", path)
                continue
            found.append((update, when))
    return found


def waiting(items, answers, nudged, now):
    """The applications to report, the longest wait first: sent at least SILENT_DAYS days before `now`, not in
    `nudged` (reported already) and without a reply among `answers` (see `replies`) that came after they were sent.
    Each gets "days": how long ago it was sent."""
    found = []
    for item in items:
        applied = _when(item["applied_at"])
        if applied is None or now - applied < timedelta(days=SILENT_DAYS) or key(item) in nudged:
            continue
        if any(when > applied and email_updates.same_company(update, item) for update, when in answers):
            continue
        found.append((applied, item | {"days": (now - applied).days}))
    return [item for _, item in sorted(found, key=lambda pair: pair[0])]


def _address(sender):
    """The address of a sender saved as "Name <address>" or as the address alone, lower-cased; "" without one."""
    sender = str(sender or "").strip()
    match = ADDRESS.search(sender)
    if match:
        return match.group(1).lower()
    return sender.lower() if "@" in sender and not any(char.isspace() for char in sender) else ""


def known_boards(company, address, saved):
    """The job boards of the company an application went to, from the saved runs' posting links (`saved`, see
    runs.saved_postings): the boards of the one researched company that is the same company as the email's. [] when
    none is, and when several are: which of them the application went to is not known then."""
    update = {"company": company, "from_address": address}
    same = [(domain, entry) for domain, entry in saved.items()
            if email_updates.same_company(update, {"name": entry["name"], "domain": domain})]
    if len(same) != 1:
        return []
    domain, entry = same[0]
    return research.company_boards(entry["name"], domain, entry["links"])


def guessed_boards(company, address):
    """Where the company's board would be on the job board that sent the confirmation: the board named like the
    company (its letters and digits, and the same with hyphens between its words), on the job board's standard host.
    Teamtailor names the board in the address it sends from. A guess: the board may not exist, or be another
    company's. [] for a sender that is no job board with a public feed, and without a name to go by."""
    host = mail.sender_domain(address)
    ats = next((ats for domain, ats in SENDER_ATS.items() if mail.domain_in(host, (domain,))), None)
    if ats is None:
        return []
    words = re.findall(r"[a-z0-9]+", company.lower())
    slugs = ["".join(words), "-".join(words)] if words else []
    if ats == "teamtailor" and host.endswith(TEAMTAILOR_MAIL) and SLUG.fullmatch(host.removesuffix(TEAMTAILOR_MAIL)):
        slugs = [host.removesuffix(TEAMTAILOR_MAIL)]
    return [{"ats": ats, "slug": slug, "host": BOARD_HOSTS[ats].format(slug=slug)}
            for slug in dict.fromkeys(slugs) if SLUG.fullmatch(slug) and slug not in boards.NOT_A_BOARD]


def _listed(application, saved):
    """Whether the role of an application added from a confirmation email is still on the company's job board.

    "open" when a board lists a posting whose title holds the role. "closed" only on strong evidence: a board known
    from the saved runs answered with postings, and no title on it holds the role or is held by it (an email can
    name the role with more words than the posting's title). Everything else is "unknown": no role named, no board
    found, a board that did not answer, and a board that is only a guess."""
    company, role = str(application["company"] or ""), " ".join(str(application["role"] or "").split())
    if not role:
        return "unknown"
    address = _address(application["sender"])
    found = known_boards(company, address, saved)
    known = bool(found)
    answered = alike = False
    for board in found or guessed_boards(company, address):
        state, postings = research.board_postings(board)
        if state != "ok":
            continue
        if any(research.title_holds(posting["title"], role) for posting in postings):
            return "open"
        answered = answered or bool(postings)
        alike = alike or any(research.title_holds(role, posting["title"]) for posting in postings)
    return "closed" if known and answered and not alike else "unknown"


def still_open(item, saved):
    """Whether the posting of an application is still open: "open", "closed" or "unknown".

    A posting's link is checked as a run checks it (research.check_posting, plain HTTP, no Exa). An application
    added from a confirmation email has no link: its role is looked for on the company's job board (see `_listed`).
    A company tracked without a posting cannot be checked."""
    if item["kind"] == "posting":
        try:
            state = research.check_posting(item["url"] or "")
        except ValueError:  # not a link at all
            return "unknown"
        return {"live": "open", "dead": "closed"}.get(state, "unknown")
    if item["kind"] == "email" and item.get("email"):
        return _listed(item["email"], saved)
    return "unknown"


def check(items, out_dir=OUT_DIR):
    """The applications by what `still_open` says of each: {"open": [...], "closed": [...], "unknown": [...]}, each
    in the order given. The saved runs are read only when an application needs a job board looked up."""
    saved = runs.saved_postings(out_dir) if any(item["kind"] == "email" for item in items) else {}
    groups = {name: [] for name, _ in HEADINGS}
    for item in items:
        groups[still_open(item, saved)].append(item)
    return groups


def record(state, groups, now):
    """Marks every application of `groups` as reported at `now`, so that no later check lists it again."""
    stamp = now.isoformat(timespec="seconds")
    for items in groups.values():
        for item in items:
            state["nudged"][key(item)] = stamp


def _one_line(text):
    """A company or a title on one line. Both come from a web page or an email and go into a Telegram message as
    they are: one with line breaks could pass for further applications or another heading."""
    return " ".join(str(text or "").split())


def format_message(groups, dashboard_url):
    """The Telegram text (plain): how many applications wait, then each group that has some under its heading, an
    application per line with the link of its posting under it, then the link to Pipeline. A link with white space
    in it is left out."""
    count = sum(len(groups[name]) for name, _ in HEADINGS)
    lines = [f"Follow-ups: {count} application(s) without a reply for {SILENT_DAYS}+ days"]
    for name, heading in HEADINGS:
        if not groups[name]:
            continue
        lines.append(f"\n{heading}")
        for item in groups[name]:
            company, title = _one_line(item["name"]), _one_line(item["title"])
            lines.append(f"- {f'{company}: ' if company else ''}{title} (applied {item['days']} days ago)")
            link = str(item["url"] or "").strip()
            if item["kind"] == "posting" and link and not any(char.isspace() for char in link):
                lines.append(link)
    lines.append(f"\nPipeline: {dashboard_url.rstrip('/')}/pipeline")
    return "\n".join(lines)
