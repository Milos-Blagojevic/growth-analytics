"""Side projects: your own entries plus the scorer's ideas (`Fit.proof_ideas`) that you accept or dismiss.

Scorer ideas are not copied into side_projects until you act on one: `suggestions` lists the ideas from every
saved scored result that have no side_projects row yet (matched by `idea_key`). Ideas from older runs stay:
they target your gaps, which a later rescore of the company (verdict noise, a changed role) does not remove.
"""

import hashlib
import json
from datetime import datetime

STATUSES = ("idea", "active", "done", "dropped")  # board columns; "dismissed" rows are hidden suggestions
FIELDS = ("title", "description", "gap", "repo_url")


def idea_key(domain, text):
    return hashlib.sha256(f"{domain}\0{' '.join(text.lower().split())}".encode()).hexdigest()[:20]


def suggestions(conn):
    """Scorer ideas not yet accepted or dismissed, newest run first; an idea repeated across runs is listed once,
    from its newest run: [{key, text, gap, domain, name, role, score, verdict, saved_at}]."""
    known = {r["idea_key"] for r in conn.execute("SELECT idea_key FROM side_projects WHERE idea_key IS NOT NULL")}
    rows = conn.execute(
        "SELECT r.*, runs.saved_at FROM results r JOIN runs ON runs.id = r.run_id"
        " WHERE r.proof_ideas != '[]' ORDER BY r.run_id DESC, r.score DESC, r.name"
    ).fetchall()
    found = []
    for row in rows:
        for text in json.loads(row["proof_ideas"]):
            key = idea_key(row["domain"], text)
            if key not in known:
                known.add(key)
                found.append(
                    {
                        "key": key,
                        "text": text,
                        "gap": row["gap_note"] or "",
                        "domain": row["domain"],
                        "name": row["name"],
                        "role": row["best_role_title"] or "",
                        "score": row["score"],
                        "verdict": row["verdict"],
                        "saved_at": row["saved_at"],
                    }
                )
    return found


def board(conn):
    """{status: [project]} for STATUSES, most recently updated first."""
    columns = {s: [] for s in STATUSES}
    for row in conn.execute("SELECT * FROM side_projects WHERE status != 'dismissed' ORDER BY updated_at DESC"):
        columns[row["status"]].append(dict(row))
    return columns


def get(conn, project_id):
    row = conn.execute("SELECT * FROM side_projects WHERE id = ?", [project_id]).fetchone()
    return dict(row) if row else None


def _now():
    return datetime.now().isoformat(timespec="seconds")


def create(conn, *, title, description="", gap="", repo_url=""):
    title = title.strip()
    if not title:
        raise ValueError("a side project needs a title")
    now = _now()
    with conn:
        cursor = conn.execute(
            "INSERT INTO side_projects (title, description, gap, status, source, repo_url, created_at, updated_at)"
            " VALUES (?, ?, ?, 'idea', 'manual', ?, ?, ?)",
            [title, description.strip(), gap.strip(), repo_url.strip(), now, now],
        )
    return cursor.lastrowid


def accept(conn, key, *, status="idea"):
    """Turns a scorer suggestion into a side project (status "idea") or hides it (status "dismissed")."""
    if status not in ("idea", "dismissed"):
        raise ValueError(f"unknown status {status!r}")
    idea = next((s for s in suggestions(conn) if s["key"] == key), None)
    if idea is None:
        raise LookupError("suggestion not found (already handled, or no longer in the latest results)")
    now = _now()
    with conn:
        cursor = conn.execute(
            "INSERT INTO side_projects (title, gap, status, source, idea_key, origin_domain, origin_name,"
            " created_at, updated_at) VALUES (?, ?, ?, 'scorer', ?, ?, ?, ?, ?)",
            [idea["text"], idea["gap"], status, key, idea["domain"], idea["name"], now, now],
        )
    return cursor.lastrowid


def update(conn, project_id, *, status=None, **fields):
    if get(conn, project_id) is None:
        raise LookupError(f"no side project {project_id}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    changes = {k: v.strip() for k, v in fields.items() if k in FIELDS and v is not None}
    if "title" in changes and not changes["title"]:
        raise ValueError("a side project needs a title")
    if status is not None:
        changes["status"] = status
    changes["updated_at"] = _now()
    with conn:
        conn.execute(
            f"UPDATE side_projects SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
            [*changes.values(), project_id],
        )


def delete(conn, project_id):
    """Deletes your own entry. A scorer idea is kept as "dismissed" so it is not suggested again."""
    project = get(conn, project_id)
    if project is None:
        raise LookupError(f"no side project {project_id}")
    with conn:
        if project["source"] == "scorer":
            conn.execute(
                "UPDATE side_projects SET status = 'dismissed', updated_at = ? WHERE id = ?", [_now(), project_id]
            )
        else:
            conn.execute("DELETE FROM side_projects WHERE id = ?", [project_id])
