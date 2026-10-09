"""Watch list: poll Ashby job boards for postings whose titles match keywords; report new ones once."""

import json
import logging
from datetime import datetime
from pathlib import Path

import ashby

logger = logging.getLogger("watch")


def load_config(profile):
    """Returns (boards, keywords, exclude) from [watch]; raises ValueError on missing boards or keywords."""
    watch = profile.get("watch", {})
    boards = [b.strip().lower() for b in watch.get("ashby_boards", []) if b.strip()]
    keywords = [k.strip().lower() for k in watch.get("title_keywords", []) if k.strip()]
    exclude = [k.strip().lower() for k in watch.get("title_exclude", []) if k.strip()]
    if not boards:
        raise ValueError("[watch].ashby_boards is empty")
    if not keywords:
        raise ValueError("[watch].title_keywords is empty")
    return boards, keywords, exclude


def title_matches(title, keywords, exclude):
    title = title.lower()
    return any(k in title for k in keywords) and not any(x in title for x in exclude)


def load_state(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path, state):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic: a crash never leaves a half-written state file


def check(boards, keywords, exclude, state):
    """Poll every board. Returns (new_matches, problems, new_state).

    new_matches: [{"board", "id", "title", "location", "remote", "url"}] not seen before.
    problems: ["board: reason"] for boards that could not be read; their state is left unchanged.
    new_state: state with new matches added ({board: {job_id: {"title", "location", "remote", "url", "first_seen"}}};
    entries written before location/remote/url were stored have only "title" and "first_seen").
    Seen IDs are never removed, so a posting that closes and reopens under the same ID is not re-sent.
    """
    now = datetime.now().isoformat(timespec="seconds")
    new_state = {board: dict(seen) for board, seen in state.items()}
    new_matches, problems = [], []
    for board in boards:
        status, jobs = ashby.fetch_board(board)
        if status == "missing":
            problems.append(f"{board}: no such Ashby board")
            continue
        if status != "ok":
            problems.append(f"{board}: Ashby API request failed")
            continue
        seen = new_state.setdefault(board, {})
        for job in jobs:
            if job["id"] in seen or not title_matches(job["title"], keywords, exclude):
                continue
            match = {
                "board": board,
                "id": job["id"],
                "title": job["title"],
                "location": job.get("location") or "unknown",
                "remote": bool(job.get("isRemote")),
                "url": job.get("jobUrl") or f"https://{ashby.HOST}/{board}/{job['id']}",
            }
            seen[job["id"]] = {k: match[k] for k in ("title", "location", "remote", "url")} | {"first_seen": now}
            new_matches.append(match)
    return new_matches, problems, new_state


def format_message(new_matches, problems):
    lines = [f"Watch list: {len(new_matches)} new matching posting(s)"]
    for m in new_matches:
        remote = ", remote" if m["remote"] and "remote" not in m["location"].lower() else ""
        lines.append(f"\n{m['board']}: {m['title']} ({m['location']}{remote})\n{m['url']}")
    if problems:
        lines.append("\nCould not check:\n" + "\n".join(f"- {p}" for p in problems))
    return "\n".join(lines)
