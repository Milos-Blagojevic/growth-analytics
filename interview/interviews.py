"""Interview files, shared by hunt.py (prep and review with Claude) and the dashboard (create, upload, show).

interviews/<id>/ holds:
- interview.json   what the owner entered: company, role, stage, date, language, posting, what to emphasise
- transcript.json  an uploaded transcript as turns, plus which speaker is the owner
- prep.json        written by `hunt.py interview-prep`: how to present yourself in this interview
- review.json      written by `hunt.py interview-review`: measured talk metrics and Claude's review

The folder is private (git-ignored): transcripts hold the interviewer's words and salary talk. Nothing here calls
a paid API, so the dashboard can import it.
"""

import json
import os
import re
import statistics
from datetime import datetime
from pathlib import Path

DIR = "interviews"
INTERVIEW, TRANSCRIPT, PREP, REVIEW = "interview.json", "transcript.json", "prep.json", "review.json"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")

STAGES = {
    "hr": "HR / recruiter screen",
    "hiring_manager": "Hiring manager",
    "technical": "Technical",
    "final": "Final / team",
    "other": "Other",
}
# Review dimensions, scored 1-5 by Claude (coaching.Scores is built from these keys).
SCORES = {
    "structure": "Structure: answers to the point and the right length",
    "positioning": "Positioning for this role",
    "evidence": "Evidence: concrete stories and numbers",
    "confidence": "Confidence: no self-undermining",
    "exits": "Career moves: reasons for joining and leaving",
    "questions": "Questions asked and listening",
}
FIELD_LIMITS = {
    "company": 120,
    "domain": 120,
    "role": 160,
    "posting_url": 500,
    "posting_text": 20000,
    "stage": 40,
    "scheduled_at": 16,
    "language": 40,
    "interviewer": 120,
    "focus": 2000,
}
FIELDS = tuple(FIELD_LIMITS)

# Approximate filler words per language (lower case, matched on word boundaries). Words with common
# non-filler meanings are left out, so counts are a lower bound meant for tracking change over time.
FILLERS = {
    "english": ("um", "uh", "erm", "you know", "i mean", "sort of", "kind of", "basically", "actually", "literally"),
    "serbian": ("mislim", "ovaj", "kako da kažem", "ne znam", "u suštini", "znači"),
}
BACKCHANNEL_WORDS = 3  # the other side saying "okay" or "right" does not end your answer
SECONDS_PER_WORD = 0.4  # estimates the length of the last turn, which has no next timestamp


def now():
    return datetime.now().isoformat(timespec="seconds")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "x"


def directory(root, interview_id):
    """The folder of an existing interview. Raises LookupError for unknown or malformed ids."""
    if not ID_PATTERN.match(interview_id or ""):
        raise LookupError("No such interview")
    path = Path(root) / DIR / interview_id
    if not (path / INTERVIEW).is_file():
        raise LookupError("No such interview")
    return path


def interview_dirs(root):
    base = Path(root) / DIR
    return sorted(p for p in base.iterdir() if (p / INTERVIEW).is_file()) if base.is_dir() else []


def load_json(path, name):
    file = Path(path) / name
    return json.loads(file.read_text()) if file.exists() else None


def save_json(path, name, data):
    """Writes atomically, so a page load never reads a half-written file."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    tmp = path / f".{name}.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path / name)


def clean(fields):
    """Validated interview fields from a form. Raises ValueError with a message for the user."""
    data = {}
    for name, limit in FIELD_LIMITS.items():
        value = str(fields.get(name) or "").strip()
        if len(value) > limit:
            raise ValueError(f"{name.replace('_', ' ')} is longer than {limit} characters")
        data[name] = value
    for name in ("company", "role"):
        if not data[name]:
            raise ValueError(f"{name} is required")
    if data["stage"] not in STAGES:
        raise ValueError("choose an interview stage")
    data["language"] = data["language"] or "English"
    data["domain"] = re.sub(r"^(https?://)?(www\.)?", "", data["domain"].lower()).split("/")[0]
    if data["posting_url"] and not data["posting_url"].startswith(("https://", "http://")):
        raise ValueError("the posting URL must start with https://")
    if data["scheduled_at"]:
        try:
            data["scheduled_at"] = datetime.fromisoformat(data["scheduled_at"]).isoformat(timespec="minutes")
        except ValueError:
            raise ValueError("the date must look like 2026-10-20T14:00") from None
    return data


def create(root, fields):
    """Creates interviews/<id>/interview.json and returns the id (date, company and role, made unique)."""
    data = clean(fields)
    day = (data["scheduled_at"] or now())[:10].replace("-", "")
    base = f"{day}-{_slug(data['company'])}-{_slug(data['role'])}"[:90]
    interview_id, n = base, 2
    while (Path(root) / DIR / interview_id).exists():
        interview_id, n = f"{base}-{n}", n + 1
    save_json(Path(root) / DIR / interview_id, INTERVIEW, {"id": interview_id, **data, "created_at": now()})
    return interview_id


def update(root, interview_id, fields):
    path = directory(root, interview_id)
    current = load_json(path, INTERVIEW)
    save_json(path, INTERVIEW, {**current, **clean(fields), "id": interview_id, "updated_at": now()})


# Transcripts -------------------------------------------------------------------------------------------------


def _seconds(stamp):
    if stamp is None or stamp == "":
        return None
    parts = [int(p) for p in str(stamp).strip().split(":")]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def fmt_time(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def _wispr_turns(lines):
    """Wispr Flow meeting files (refined.ndjson): one JSON object per line with speaker, text and mm:ss timestamp."""
    turns = []
    for line in lines:
        row = json.loads(line)
        if not isinstance(row, dict) or "text" not in row or "speaker" not in row:
            raise ValueError("not a Wispr Flow line")
        speaker = row["speaker"]
        if isinstance(speaker, dict):
            key, label = str(speaker.get("id")), speaker.get("name") or f"Speaker {speaker.get('id')}"
        else:
            key = label = str(speaker)
        turns.append({"t": _seconds(row.get("timestamp")), "speaker": key, "label": label, "text": row["text"].strip()})
    return turns


TEXT_LINE = re.compile(r"^\s*(?:\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*)?([^:\[\]]{1,40}):\s+(.*\S)\s*$")


def _text_turns(lines):
    """Plain text: `Name: text` per turn, optionally prefixed with a [mm:ss] timestamp; other lines continue the turn."""
    turns = []
    for line in lines:
        match = TEXT_LINE.match(line)
        if match:
            stamp, name, text = match.groups()
            turns.append({"t": _seconds(stamp), "speaker": name.strip(), "label": name.strip(), "text": text})
        elif turns:
            turns[-1]["text"] += " " + line.strip()
    return turns


def parse_transcript(raw, filename=""):
    """A transcript upload (Wispr Flow refined.ndjson or `Name: text` lines) as transcript.json data.

    Raises ValueError when no speakers can be found."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    lines = [line for line in text.splitlines() if line.strip()]
    source = "text"
    try:
        turns, source = _wispr_turns(lines), "wispr_flow"
    except (ValueError, TypeError, AttributeError):
        turns = _text_turns(lines)
    turns = [t for t in turns if t["text"]]
    speakers = {}
    for turn in turns:
        label = turn.pop("label")
        s = speakers.setdefault(turn["speaker"], {"label": label, "words": 0, "sample": []})
        words = len(turn["text"].split())
        s["words"] += words
        if len(s["sample"]) < 2 and words >= 8:
            s["sample"].append(turn["text"][:200])
    if len(speakers) < 2 or len(turns) < 4:
        raise ValueError("found fewer than two speakers or four turns; upload a Wispr Flow meeting file "
                         "(refined.ndjson) or text with one `Name: text` line per turn")
    return {"source": source, "filename": filename[:120], "uploaded_at": now(), "me": None,
            "speakers": speakers, "turns": turns}


def _fillers(language):
    language = (language or "").strip().lower()
    if language.startswith("en"):
        return FILLERS["english"]
    if language.startswith(("serb", "srp", "sr")):
        return FILLERS["serbian"]
    return ()


def metrics(transcript, language):
    """Measured, not judged: talk share, answer lengths and filler words of the speaker marked as you.

    An answer is a run of your turns; a short interjection from the other side (BACKCHANNEL_WORDS or fewer
    words) does not end it. Times need timestamps on every turn and are None otherwise."""
    me, turns = transcript.get("me"), transcript["turns"]
    if me is None:
        return None
    timed = all(t["t"] is not None for t in turns)
    ends = []
    for i, turn in enumerate(turns):
        estimate = turn["t"] + len(turn["text"].split()) * SECONDS_PER_WORD if timed else None
        ends.append((turns[i + 1]["t"] if i + 1 < len(turns) else estimate) if timed else None)
    words = {"me": 0, "other": 0}
    seconds = {"me": 0, "other": 0}
    answers = []  # (start index, end index exclusive)
    i = 0
    while i < len(turns):
        side = "me" if turns[i]["speaker"] == me else "other"
        words[side] += len(turns[i]["text"].split())
        if timed:
            seconds[side] += max(0, ends[i] - turns[i]["t"])
        if side == "me":
            start = i
            j = i + 1
            while j < len(turns) and (turns[j]["speaker"] == me or len(turns[j]["text"].split()) <= BACKCHANNEL_WORDS):
                j_side = "me" if turns[j]["speaker"] == me else "other"
                words[j_side] += len(turns[j]["text"].split())
                if timed:
                    seconds[j_side] += max(0, ends[j] - turns[j]["t"])
                j += 1
            answers.append((start, j))
            i = j
        else:
            i += 1
    my_text = " ".join(t["text"] for t in turns if t["speaker"] == me).lower()
    counts = {}
    for word in _fillers(language):
        n = len(re.findall(rf"(?<!\w){re.escape(word)}(?!\w)", my_text))
        if n:
            counts[word] = n
    answer_words = [sum(len(t["text"].split()) for t in turns[a:b] if t["speaker"] == me) for a, b in answers]
    answer_seconds = [ends[b - 1] - turns[a]["t"] for a, b in answers] if timed else []
    longest = max(range(len(answers)), key=lambda k: answer_seconds[k] if timed else answer_words[k]) if answers else None
    total_words = words["me"] + words["other"]
    total_seconds = seconds["me"] + seconds["other"]
    return {
        "duration_s": round(ends[-1] - turns[0]["t"]) if timed else None,
        "my_words": words["me"],
        "word_share": round(words["me"] / total_words, 3) if total_words else None,
        "time_share": round(seconds["me"] / total_seconds, 3) if timed and total_seconds else None,
        "answers": len(answers),
        "median_answer_words": round(statistics.median(answer_words)) if answer_words else None,
        "longest_answer_s": round(answer_seconds[longest]) if timed and longest is not None else None,
        "longest_answer_words": answer_words[longest] if longest is not None else None,
        "longest_answer_at": fmt_time(turns[answers[longest][0]]["t"]) if timed and longest is not None else "",
        "fillers": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "fillers_per_100_words": round(100 * sum(counts.values()) / words["me"], 1) if words["me"] and _fillers(language) else None,
    }


def render(transcript):
    """The transcript as `[mm:ss] YOU|<label>: text` lines, for Claude."""
    me, labels = transcript["me"], {k: s["label"] for k, s in transcript["speakers"].items()}
    lines = []
    for t in transcript["turns"]:
        who = "YOU" if t["speaker"] == me else labels.get(t["speaker"], t["speaker"])
        lines.append(f"[{fmt_time(t['t'])}] {who}: {t['text']}" if t["t"] is not None else f"{who}: {t['text']}")
    return "\n".join(lines)


def past_reviews(root, *, exclude=None, limit=5):
    """The latest reviewed interviews, oldest first: what was asked of you and what to do next time."""
    rows = []
    for path in interview_dirs(root):
        if path.name == exclude:
            continue
        review = load_json(path, REVIEW)
        if not review:
            continue
        interview = load_json(path, INTERVIEW)
        rows.append({
            "id": path.name,
            "date": (interview.get("scheduled_at") or review["created_at"])[:10],
            "company": interview["company"],
            "role": interview["role"],
            "stage": STAGES.get(interview["stage"], interview["stage"]),
            "scores": review["review"]["scores"],
            "next_time": review["review"]["next_time"],
            "metrics": review.get("metrics"),
        })
    return sorted(rows, key=lambda r: r["date"])[-limit:]
