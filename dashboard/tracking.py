"""Your status and notes per posting (keyed by URL) or per company without a posting (keyed by domain)."""

from datetime import datetime

STATUSES = ("new", "saved", "applied", "interviewing", "offer", "rejected", "dismissed")
PIPELINE = ("saved", "applied", "interviewing", "offer", "rejected")  # pipeline page columns, in order
APPLIED = {"applied", "interviewing", "offer", "rejected"}  # statuses that imply an application was sent
KINDS = ("posting", "company")


def key_for(best_role_url, domain):
    """A board card is tracked by its posting URL, or by the company when there is no posting (check careers)."""
    return ("posting", best_role_url) if best_role_url else ("company", domain)


def get(conn, kind, ref):
    row = conn.execute("SELECT * FROM tracking WHERE kind = ? AND ref = ?", [kind, ref]).fetchone()
    if row:
        return dict(row)
    return {"kind": kind, "ref": ref, "status": "new", "notes": "", "applied_at": None, "updated_at": None}


def update(conn, kind, ref, *, status=None, notes=None):
    """Sets the status and/or notes. A "new" item without notes is not stored. Returns the stored state."""
    if kind not in KINDS or not ref:
        raise ValueError(f"unknown item {kind}:{ref}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    current = get(conn, kind, ref)
    status = status or current["status"]
    notes = current["notes"] if notes is None else notes.strip()
    now = datetime.now().isoformat(timespec="seconds")
    applied_at = current["applied_at"] or (now if status in APPLIED else None)
    with conn:
        if status == "new" and not notes:
            conn.execute("DELETE FROM tracking WHERE kind = ? AND ref = ?", [kind, ref])
        else:
            conn.execute(
                "INSERT INTO tracking (kind, ref, status, notes, applied_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (kind, ref) DO UPDATE SET status = excluded.status, notes = excluded.notes,"
                " applied_at = excluded.applied_at, updated_at = excluded.updated_at",
                [kind, ref, status, notes, applied_at, now],
            )
    return get(conn, kind, ref)


def pipeline(conn):
    """Tracked items in PIPELINE statuses, with what is known about each: {status: [item]}, newest update first."""
    columns = {s: [] for s in PIPELINE}
    placeholders = ", ".join("?" for _ in PIPELINE)
    rows = conn.execute(
        f"SELECT * FROM tracking WHERE status IN ({placeholders}) ORDER BY updated_at DESC", PIPELINE
    ).fetchall()
    for row in rows:
        item = dict(row) | _describe(conn, row["kind"], row["ref"])
        columns[item["status"]].append(item)
    return columns


def _describe(conn, kind, ref):
    """Title, company name, domain and URL of a tracked item, from the latest saved data that mentions it."""
    if kind == "company":
        row = conn.execute(
            "SELECT name, domain FROM results WHERE domain = ? ORDER BY run_id DESC LIMIT 1", [ref]
        ).fetchone()
        return {"title": "Check careers page", "name": row["name"] if row else ref, "domain": ref, "url": f"https://{ref}"}
    row = conn.execute(
        "SELECT r.domain, r.title, (SELECT name FROM results WHERE domain = r.domain ORDER BY run_id DESC LIMIT 1) AS name"
        " FROM roles r WHERE r.url = ? ORDER BY r.run_id DESC LIMIT 1",
        [ref],
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT domain, best_role_title AS title, name FROM results WHERE best_role_url = ? ORDER BY run_id DESC LIMIT 1",
            [ref],
        ).fetchone()
    if row:
        return {"title": row["title"], "name": row["name"], "domain": row["domain"], "url": ref}
    watched = conn.execute("SELECT board, title FROM watch_postings WHERE url = ? LIMIT 1", [ref]).fetchone()
    if watched:
        return {"title": watched["title"], "name": watched["board"], "domain": None, "url": ref}
    return {"title": ref, "name": "", "domain": None, "url": ref}
