"""Application updates from email: which emails to label, what was checked, and the saved updates.

Shared by hunt.py (the `mail` command) and the dashboard (which suggests status changes from the saved updates);
no paid clients here. The labelling itself is in mail_labels.py.

mail/<%Y%m%d-%H%M%S>.json holds the rejections and interview invites one check found (only checks that found
some write a file). STATE (mail_state.json) holds when mail was last checked and the emails already labelled.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

DIR = "mail"
STATE = "mail_state.json"
OUTCOMES = ("rejected", "interview", "other")
SUGGESTED_STATUS = {"rejected": "rejected", "interview": "interviewing"}  # outcomes that become a dashboard suggestion
OUTCOME_LABELS = {"rejected": "Rejected", "interview": "Interview invite"}
LOOKBACK_DAYS = 14  # the first check reads this far back
OVERLAP = timedelta(hours=1)  # re-read the hour before the last check, for mail that arrives late
SEEN_DAYS = 30  # how long labelled emails are remembered (they only need to cover OVERLAP)
MAX_TRIES = 3  # checks that may send an email to Claude without getting a label back before it is given up on
BODY_CHARS = 2000  # of each email's text sent to Claude

# Sender domains of applicant tracking systems that send application emails on the employer's behalf.
ATS_DOMAINS = (
    "ashbyhq.com", "bamboohr.com", "breezy.hr", "greenhouse.io", "greenhouse-mail.io", "icims.com", "jobvite.com",
    "lever.co", "myworkday.com", "personio.com", "personio.de", "pinpointhq.com", "recruitee.com",
    "smartrecruiters.com", "teamtailor.com", "teamtailor-mail.com", "workable.com", "workablemail.com",
)
# Phrases (case-insensitive, in the subject or the start of the text) of emails about an application.
KEYWORDS = (
    "your application", "application for", "application to", "applying", "applied for", "candidacy",
    "interview", "not to move forward", "not moving forward", "move forward with other", "other candidates",
    "decided to pursue", "regret to inform", "unfortunately", "next steps", "hiring team", "recruiter",
)
# Outlook's own daily agenda ("Today: Interview with Rho" from no-reply@microsoft.com) repeats interviews already on
# the calendar, so it is skipped here. Only that sender with that subject: other mail from the address still counts,
# and reminders from other calendar apps reach Claude, which labels them "other".
OUTLOOK_AGENDA_SENDER = "no-reply@microsoft.com"
OUTLOOK_AGENDA_SUBJECT = "today:"


def sender_domain(address):
    return address.rpartition("@")[2].lower()


def domain_in(domain, domains):
    """True when `domain` is one of `domains` or a subdomain of one."""
    return any(domain == d or domain.endswith(f".{d}") for d in domains)


def likely(message):
    """True for emails that may be about an application: sent through an ATS, or using application wording, and not
    Outlook's daily agenda. Only these are sent to Claude, so newsletters and personal mail stay on your side."""
    if (message["from_address"] == OUTLOOK_AGENDA_SENDER
            and message["subject"].lower().startswith(OUTLOOK_AGENDA_SUBJECT)):
        return False
    if domain_in(sender_domain(message["from_address"]), ATS_DOMAINS):
        return True
    text = f"{message['subject']}\n{message['body'][:BODY_CHARS]}".lower()
    return any(k in text for k in KEYWORDS)


def excerpt(body):
    """The email's text as sent to Claude: whitespace collapsed, at most BODY_CHARS characters."""
    return " ".join(body.split())[:BODY_CHARS]


def load_state(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path, state):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic: a crash never leaves a half-written state file


def since(state, now, *, days=LOOKBACK_DAYS):
    """Where this check starts (aware datetime): an hour before the last check, or `days` back on the first one."""
    if state.get("checked_until"):
        return datetime.fromisoformat(state["checked_until"]) - OVERLAP
    return now - timedelta(days=days)


def next_state(state, now, labelled_ids, missed=()):
    """State after a check that started at `now` (aware), and the emails given up on.

    `labelled_ids` are remembered as seen. `missed` are emails sent to Claude that came back without a label: the
    next check starts early enough to read them again, until an email has been missed MAX_TRIES times; then it is
    remembered as seen too and returned, so a skipped email cannot make every later check pay for it again.
    """
    tries = dict(state.get("tries", {}))
    retry, given_up = [], []
    for m in missed:
        tries[m["id"]] = tries.get(m["id"], 0) + 1
        (given_up if tries[m["id"]] >= MAX_TRIES else retry).append(m)
    until = min([now] + [datetime.fromisoformat(m["received_at"]).astimezone() + OVERLAP for m in retry])
    stamp = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(days=SEEN_DAYS)).isoformat(timespec="seconds")
    seen = {i: t for i, t in state.get("seen", {}).items() if t >= cutoff}
    seen.update({i: stamp for i in [*labelled_ids, *(m["id"] for m in given_up)]})
    state = {"checked_until": until.isoformat(timespec="seconds"), "seen": seen,
             "tries": {m["id"]: tries[m["id"]] for m in retry}}
    return state, given_up


def event(message, label):
    """A saved update: the email's identity plus Claude's label (outcome, company, role, summary)."""
    sender = message["from_name"] or message["from_address"]
    if message["from_name"] and message["from_address"]:
        sender = f"{message['from_name']} <{message['from_address']}>"
    return {
        "id": message["id"],
        "received_at": message["received_at"],
        "from": sender,
        "from_address": message["from_address"],
        "subject": message["subject"],
        "web_link": message["web_link"],
        **{k: label[k] for k in ("outcome", "company", "role", "summary")},
    }


def save_updates(root, updates, **meta):
    """Writes mail/<timestamp>.json with the updates and the check's details; returns the path."""
    saved_at = datetime.now()
    directory = Path(root) / DIR
    directory.mkdir(exist_ok=True)
    path = directory / f"{saved_at:%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"saved_at": saved_at.isoformat(timespec="seconds"), **meta, "updates": updates},
                               indent=2))
    return path


def update_files(root):
    """Saved update files, oldest first (names are timestamps)."""
    directory = Path(root) / DIR
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _group(updates):
    """Updates in receipt order, grouped: emails that name the same company (case-insensitive) with the same outcome
    and the same role, or no role on one of them, are one group (invite, confirmation, new time). Emails that name no
    company stay apart, because a shared ATS sender can stand for several employers."""
    groups = []
    for u in sorted(updates, key=lambda u: u["received_at"]):
        company, role = u["company"].casefold(), u["role"].casefold()
        group = next((g for g in groups if company and g["outcome"] == u["outcome"] and g["company"] == company
                      and (not role or not g["role"] or g["role"] == role)), None)
        if group is None:
            group = {"outcome": u["outcome"], "company": company, "role": role, "emails": []}
            groups.append(group)
        group["role"] = group["role"] or role
        group["emails"].append(u)
    return [g["emails"] for g in groups]


def format_message(updates, dashboard_url):
    """One entry per group (see _group), with the newest email's summary and the role any of its emails named."""
    groups = _group(updates)
    lines = [f"Email: {len(groups)} application update(s)"
             + (f" from {len(updates)} emails" if len(groups) < len(updates) else "")]
    for emails in groups:
        latest = emails[-1]
        role = next((u["role"] for u in reversed(emails) if u["role"]), "")
        head = f"{OUTCOME_LABELS[latest['outcome']]}: {latest['company'] or latest['from']}" + (f" · {role}" if role else "")
        if len(emails) > 1:
            head += f" ({len(emails)} emails)"
        lines.append(f"\n{head}\n{latest['summary']}")
    lines.append(f"\nConfirm the status changes on the dashboard: {dashboard_url.rstrip('/')}/pipeline")
    return "\n".join(lines)
