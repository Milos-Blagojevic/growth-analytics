"""News digest: recent articles on your [news].topics from Exa, picked and summarised by Claude, each sent once.

STATE (news_state.json) maps every article URL already given to Claude to when, so a later digest skips it.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

import exa_client
import scoring

logger = logging.getLogger("news")

STATE = "news_state.json"
DAYS = 7  # articles published in the last DAYS days
PER_TOPIC = 8  # Exa results per topic
MAX_PICKS = 8
HIGHLIGHT_CHARS = 1200  # of each article's highlights sent to Claude
KEEP_DAYS = 90  # how long sent URLs are remembered; older articles fall outside the date filter anyway

PROMPT = f"""\
You pick news for a job seeker. You get the candidate's target roles and interests, and numbered articles \
(title, source, date and excerpts) from the last few days. Use only what the excerpts say; never add facts \
from memory.

- picks: at most {MAX_PICKS} articles worth the candidate's time, most relevant first: developments in the job \
market for their target roles (hiring, layoffs, salaries, remote work, demand for skills) and substantive news \
or ideas in their interests. Skip job listings, press releases without substance, ads and near-duplicates \
(keep the better one). Picking none is fine when nothing qualifies.
- takeaway (per pick): one sentence on what the article says and why it matters to this candidate.
- overview: two or three sentences on what moved across the picks; "" when there are no picks."""


class Pick(BaseModel):
    number: int
    takeaway: str


class Digest(BaseModel):
    overview: str
    picks: list[Pick]


def load_config(profile):
    """[news].topics; raises ValueError when there are none."""
    topics = [t.strip() for t in profile.get("news", {}).get("topics", []) if t.strip()]
    if not topics:
        raise ValueError("[news].topics is empty")
    return topics


def load_state(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path, state):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic: a crash never leaves a half-written state file


def search(topics, state, *, days=DAYS):
    """New articles per topic. Returns (articles, problems): articles are {"topic", "url", "title", "source",
    "published", "excerpt"} not in `state`, each URL once; problems are "topic: reason" for failed searches."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    articles, problems, urls = [], [], set(state)
    for topic in topics:
        try:
            response = exa_client.search(topic, num_results=PER_TOPIC, category="news", start_published_date=start)
        except Exception as exc:  # one failing topic must not lose the others
            logger.warning("news search %r failed: %s", topic, exc)
            problems.append(f"{topic}: {type(exc).__name__}")
            continue
        for r in response.results:
            if r.url in urls:
                continue
            urls.add(r.url)
            articles.append({
                "topic": topic,
                "url": r.url,
                "title": (r.title or r.url).strip(),
                "source": r.url.split("/")[2].removeprefix("www.") if "//" in r.url else "",
                "published": (getattr(r, "published_date", None) or "")[:10],
                "excerpt": " ".join(" ".join(r.highlights or []).split())[:HIGHLIGHT_CHARS],
            })
    return articles, problems


def digest(articles, profile, topics, *, client=None, model=None):
    """Returns (overview, picks, usage); picks are the chosen articles, each with its "takeaway"."""
    client = client or scoring._client()
    content = (
        "Candidate:\n" + json.dumps({"target_roles": profile["search"]["roles"], "interests": topics}, indent=2)
        + "\n\nArticles:\n\n"
        + "\n\n".join(
            f"{n}. {a['title']} ({a['source']}, {a['published'] or 'date unknown'})\n{a['excerpt']}"
            for n, a in enumerate(articles, 1)
        )
    )
    response = client.messages.parse(
        model=model or os.environ.get("SCORING_MODEL", scoring.DEFAULT_MODEL),
        max_tokens=4000,
        system=PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=Digest,
    )
    result = response.parsed_output
    if result is None:
        raise RuntimeError(f"Claude returned no parsable news digest (stop_reason={response.stop_reason})")
    picks, chosen = [], set()
    for pick in result.picks:
        if 1 <= pick.number <= len(articles) and pick.number not in chosen and len(picks) < MAX_PICKS:
            chosen.add(pick.number)
            picks.append(articles[pick.number - 1] | {"takeaway": pick.takeaway})
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return result.overview, picks, usage


def next_state(state, articles, now=None):
    """State with `articles` recorded as given to Claude now; entries older than KEEP_DAYS dropped."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    kept = {url: t for url, t in state.items() if t >= cutoff}
    return kept | {a["url"]: now.isoformat(timespec="seconds") for a in articles}


def format_message(overview, picks, problems):
    lines = [f"News digest {datetime.now():%Y-%m-%d}: {len(picks)} pick(s)"]
    if overview:
        lines.append(f"\n{overview}")
    for p in picks:
        lines.append(f"\n• {p['title']} ({p['source']})\n{p['takeaway']}\n{p['url']}")
    if problems:
        lines.append("\nCould not search:\n" + "\n".join(f"- {p}" for p in problems))
    return "\n".join(lines)
