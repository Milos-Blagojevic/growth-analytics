"""Status changes suggested by email updates (`hunt.py mail`, copied into mail_updates), which you confirm or dismiss.

An update is matched to your tracked applications by company: the name Claude read from the email against each
item's company name and domain, or the sender's domain against the item's domain. Nothing changes until you press
a button; your answer is kept in mail_decisions, so a handled update is not suggested again.
"""

import re
from datetime import datetime

import mail
from dashboard import tracking

# The statuses an update may move an item from, per suggested status: an update never moves an item backwards.
MOVES_FROM = {"rejected": ("saved", "applied", "interviewing", "offer"), "interviewing": ("saved", "applied")}
IGNORED_WORDS = {"inc", "llc", "ltd", "limited", "gmbh", "ag", "bv", "sa", "plc", "corp", "corporation", "co", "the",
                 "hq", "group", "team", "careers", "jobs", "talent"}
ACTIONS = ("apply", "dismiss")


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and ")) if w not in IGNORED_WORDS]


def _contains(words, part):
    n = len(part)
    return any(words[i:i + n] == part for i in range(len(words) - n + 1))


def _same_name(a, b):
    if not a or not b:
        return False
    if "".join(a) == "".join(b):  # "SE Ranking" and seranking.com
        return True
    shorter, longer = sorted((a, b), key=len)
    return len("".join(shorter)) >= 3 and _contains(longer, shorter)  # "Acme" and "Acme Analytics"


def same_company(update, item):
    """True when the update's company (or sender) and the tracked item's company look the same."""
    domain = item.get("domain")
    if domain and mail.domain_in(mail.sender_domain(update["from_address"]), (domain,)):  # acme.com, mail.acme.com
        return True
    company = _words(update["company"])
    names = [_words(item.get("name"))] + ([_words(domain.split(".")[0])] if domain else [])
    return any(_same_name(company, name) for name in names)


def _role_overlap(role, title):
    return len(set(_words(role)) & set(_words(title)))


def suggestions(conn):
    """Unhandled updates, newest first, each with the status it suggests ("status") and the tracked items it could
    apply to ("targets", best role match first; empty when no tracked application matches). Updates whose matching
    items all have that status already, or a later one, are left out."""
    rows = conn.execute(
        "SELECT u.* FROM mail_updates u LEFT JOIN mail_decisions d ON d.update_id = u.id"
        " WHERE d.update_id IS NULL ORDER BY u.received_at DESC"
    ).fetchall()
    if not rows:
        return []
    items = [item for column in tracking.pipeline(conn).values() for item in column]
    result = []
    for row in rows:
        update = dict(row)
        status = mail.SUGGESTED_STATUS[update["outcome"]]
        matches = [i for i in items if same_company(update, i)]
        targets = [i for i in matches if i["status"] in MOVES_FROM[status]]
        if matches and not targets:
            continue
        targets.sort(key=lambda i: -_role_overlap(update["role"], i["title"]))
        result.append(update | {"status": status, "targets": targets})
    return result


def decide(conn, update_id, action, kind="", ref=""):
    """Applies the suggested status to the tracked item (kind, ref), or dismisses the update.
    Raises LookupError for an unknown or handled update, ValueError for an item the update cannot apply to."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    suggestion = next((s for s in suggestions(conn) if s["id"] == update_id), None)
    if suggestion is None:
        raise LookupError("This email update was already handled. Reload the page.")
    now = datetime.now().isoformat(timespec="seconds")
    if action == "dismiss":
        with conn:
            conn.execute("INSERT INTO mail_decisions (update_id, decision, decided_at) VALUES (?, 'dismissed', ?)",
                         [update_id, now])
        return
    if not any(i["kind"] == kind and i["ref"] == ref for i in suggestion["targets"]):
        raise ValueError("This email does not match that application.")
    tracking.update(conn, kind, ref, status=suggestion["status"])
    with conn:
        conn.execute(
            "INSERT INTO mail_decisions (update_id, decision, kind, ref, status, decided_at) VALUES (?, 'applied', ?, ?, ?, ?)",
            [update_id, kind, ref, suggestion["status"], now],
        )
