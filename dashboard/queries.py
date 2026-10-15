"""Read queries behind the dashboard pages. Every function returns plain dicts ready for the templates."""

import json
from datetime import datetime, timedelta

VERDICTS = ("apply", "consider", "stretch", "check_careers", "skip")  # hunt.VERDICTS, in board tab order
NEW_VERDICTS = ("apply", "consider", "stretch")  # what the inbox and Telegram notification surface
JSON_FIELDS = ("reasons", "concerns", "proof_ideas", "dead_roles", "grounding")

# The latest scored result per company, with its run time, the best role's link status and your tracking
# (status "new" when untracked; see tracking.key_for for which key a card uses).
LATEST = """
WITH ranked AS (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.domain ORDER BY r.run_id DESC) AS rank
    FROM results r WHERE r.score IS NOT NULL
),
latest AS (
    SELECT ranked.*, runs.saved_at, roles.link_status AS best_link_status,
        COALESCE(t.status, 'new') AS status, COALESCE(t.notes, '') AS notes
    FROM ranked
    JOIN runs ON runs.id = ranked.run_id
    LEFT JOIN roles ON roles.run_id = ranked.run_id AND roles.domain = ranked.domain
        AND roles.url = ranked.best_role_url
    LEFT JOIN tracking t ON
        (t.kind = 'posting' AND t.ref = ranked.best_role_url)
        OR (t.kind = 'company' AND COALESCE(ranked.best_role_url, '') = '' AND t.ref = ranked.domain)
    WHERE ranked.rank = 1
)
"""

SORTS = {"score": "score DESC, name", "recent": "saved_at DESC, score DESC", "name": "name COLLATE NOCASE"}


def _row(row):
    d = dict(row)
    for field in JSON_FIELDS:
        if field in d and d[field] is not None:
            d[field] = json.loads(d[field])
    return d


def _status_filter(status):
    """SQL condition on latest.status: "" hides dismissed items, "all" shows everything, else that status only."""
    if status == "all":
        return "1", []
    if status:
        return "status = ?", [status]
    return "status != 'dismissed'", []


def verdict_counts(conn, *, status=""):
    condition, params = _status_filter(status)
    rows = conn.execute(
        LATEST + f"SELECT verdict, COUNT(*) AS n FROM latest WHERE {condition} GROUP BY verdict", params
    ).fetchall()
    counts = {v: 0 for v in VERDICTS}
    counts.update({r["verdict"]: r["n"] for r in rows})
    return counts


def board(conn, *, verdict, q="", min_score=0, sort="score", status=""):
    condition, params = _status_filter(status)
    sql = LATEST + f"SELECT * FROM latest WHERE verdict = ? AND score >= ? AND {condition}"
    params = [verdict, min_score, *params]
    if q:
        sql += " AND (name LIKE ? OR domain LIKE ? OR best_role_title LIKE ? OR location_assessment LIKE ? OR notes LIKE ?)"
        params += [f"%{q}%"] * 5
    sql += f" ORDER BY {SORTS.get(sort, SORTS['score'])}"
    return [_row(r) for r in conn.execute(sql, params)]


def latest_run(conn):
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


_RUN_COUNTS = """
    SELECT runs.*,
        SUM(results.verdict = 'apply') AS apply, SUM(results.verdict = 'consider') AS consider,
        SUM(results.verdict = 'stretch') AS stretch, SUM(results.verdict = 'check_careers') AS check_careers,
        SUM(results.verdict = 'skip') AS skip, SUM(results.score IS NOT NULL) AS scored
    FROM runs LEFT JOIN results ON results.run_id = runs.id
"""


def saved_runs(conn, limit=50):
    """Saved runs, newest first, with verdict counts."""
    return [dict(r) for r in conn.execute(_RUN_COUNTS + " GROUP BY runs.id ORDER BY runs.id DESC LIMIT ?", [limit])]


def run_summary(conn, run_id):
    row = conn.execute(_RUN_COUNTS + " WHERE runs.id = ? GROUP BY runs.id", [run_id]).fetchone()
    return dict(row) if row else None


def latest_failures(conn):
    """Companies whose research or scoring failed in the most recent run."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT domain, name, error FROM results WHERE error IS NOT NULL"
            " AND run_id = (SELECT MAX(id) FROM runs) ORDER BY name"
        )
    ]


def company(conn, domain):
    """Latest scored result (or latest of any kind if never scored), its roles, and the per-run history."""
    history = [
        dict(r)
        for r in conn.execute(
            "SELECT r.run_id, runs.saved_at, runs.mode, r.score, r.verdict, r.best_role_title, r.error"
            " FROM results r JOIN runs ON runs.id = r.run_id WHERE r.domain = ? ORDER BY r.run_id",
            [domain],
        )
    ]
    if not history:
        return None
    row = conn.execute(
        "SELECT r.*, runs.saved_at FROM results r JOIN runs ON runs.id = r.run_id WHERE r.domain = ?"
        " ORDER BY r.score IS NULL, r.run_id DESC LIMIT 1",
        [domain],
    ).fetchone()
    result = _row(row)
    roles = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM roles WHERE run_id = ? AND domain = ? ORDER BY title", [result["run_id"], domain]
        )
    ]
    newer = [h for h in history if h["run_id"] > result["run_id"]]
    return {"result": result, "roles": roles, "history": history, "newer": newer, "sparkline": sparkline(history)}


def sparkline(history, width=240, height=64, pad=5):
    """SVG points for the score history, or None below 2 scores.

    The y axis runs from a floor (a multiple of 10 at most 40, below the lowest score) to 100, so the 50 and 75
    verdict thresholds are always drawn and run-to-run noise is not exaggerated.
    """
    scores = [h["score"] for h in history if h["score"] is not None]
    if len(scores) < 2:
        return None
    floor = max(0, min(40, (min(scores) - 5) // 10 * 10))

    def y(score):
        return pad + (100 - score) / (100 - floor) * (height - 2 * pad)

    step = (width - 2 * pad) / (len(scores) - 1)
    points = [(pad + i * step, y(s)) for i, s in enumerate(scores)]
    return {
        "width": width,
        "height": height,
        "points": " ".join(f"{px:.1f},{py:.1f}" for px, py in points),
        "dots": [{"x": round(px, 1), "y": round(py, 1), "score": s} for (px, py), s in zip(points, scores)],
        "bands": {"apply": round(y(75), 1), "consider": round(y(50), 1)},
        "min": min(scores),
        "max": max(scores),
    }


def inbox(conn, *, days):
    """Watch postings and apply/consider/stretch roles first seen in the last `days` days (all if days is 0)."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds") if days else ""
    postings = [
        dict(r)
        for r in conn.execute(
            "SELECT w.*, COALESCE(t.status, 'new') AS status FROM watch_postings w"
            " LEFT JOIN tracking t ON t.kind = 'posting' AND t.ref = w.url"
            " WHERE w.first_seen >= ? ORDER BY w.first_seen DESC, w.board, w.title",
            [cutoff],
        )
    ]
    placeholders = ", ".join("?" for _ in NEW_VERDICTS)
    roles = [
        _row(r)
        for r in conn.execute(
            LATEST
            + """, firsts AS (
                SELECT domain, best_role_url, MIN(run_id) AS first_run FROM results
                WHERE score IS NOT NULL GROUP BY domain, best_role_url
            )
            SELECT latest.*, first.saved_at AS first_seen FROM latest
            JOIN firsts ON firsts.domain = latest.domain AND firsts.best_role_url IS latest.best_role_url
            JOIN runs AS first ON first.id = firsts.first_run
            WHERE first.saved_at >= ? AND latest.verdict IN ("""
            + placeholders
            + ") ORDER BY first_seen DESC, score DESC",
            [cutoff, *NEW_VERDICTS],
        )
    ]
    return {"postings": postings, "roles": roles}


def leads(conn, *, status=""):
    """Discovered companies with their research state: new, queued (in companies.csv) or researched."""
    rows = conn.execute(
        LATEST
        + """SELECT leads.*, latest.score, latest.verdict,
            EXISTS (SELECT 1 FROM results WHERE results.domain = leads.domain) AS researched
        FROM leads LEFT JOIN latest ON latest.domain = leads.domain
        ORDER BY leads.discovered_at DESC, leads.name"""
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["status"] = "researched" if d["researched"] else "queued" if d["in_companies"] else "new"
        items.append(d)
    counts = {s: sum(i["status"] == s for i in items) for s in ("new", "queued", "researched")}
    if status in counts:
        items = [i for i in items if i["status"] == status]
    return {"items": items, "counts": counts}


_INTERVIEWS = """SELECT i.*, EXISTS (SELECT 1 FROM results WHERE results.domain = i.domain) AS researched
FROM interviews i"""
UPCOMING_GRACE = timedelta(hours=3)  # an interview stays "upcoming" while it is happening


def _interview(row):
    d = dict(row)
    for key in ("interview", "transcript", "prep", "review"):
        d[key] = json.loads(d[key]) if d[key] else None
    scores = d["review"]["review"]["scores"] if d["review"] else None
    d["overall"] = round(sum(scores.values()) / len(scores), 1) if scores else None
    d["review_stale"] = bool(
        d["review"] and d["transcript"] and d["review"].get("transcript_uploaded_at") != d["transcript"]["uploaded_at"]
    )
    return d


def interview_list(conn):
    """Upcoming interviews (soonest first) and past ones (latest first). Without a date, an interview counts as
    upcoming until it has a transcript."""
    rows = [_interview(r) for r in conn.execute(_INTERVIEWS + " ORDER BY sort_at")]
    cutoff = (datetime.now() - UPCOMING_GRACE).isoformat(timespec="minutes")

    def upcoming(r):
        return r["scheduled_at"] >= cutoff if r["scheduled_at"] else r["transcript"] is None

    return {"upcoming": [r for r in rows if upcoming(r)], "past": [r for r in reversed(rows) if not upcoming(r)]}


def interview_detail(conn, interview_id):
    row = conn.execute(_INTERVIEWS + " WHERE i.id = ?", [interview_id]).fetchone()
    return _interview(row) if row else None


def interview_progress(conn):
    """Reviewed interviews, oldest first, with scores and measured metrics, to track improvement."""
    return [r for r in (_interview(row) for row in conn.execute(_INTERVIEWS + " ORDER BY sort_at")) if r["review"]]


def problems(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'problems'").fetchone()
    return json.loads(row["value"]) if row else []
