"""Company research via Exa: identity, growth signals and matching open roles."""

import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urlparse

import requests

import ashby
import exa_client

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
DEAD_STATUS = {404, 410}
MAX_DESCRIBED_ROLES = 3  # posting texts fetched per company
DESCRIPTION_CHARS = 4000  # per posting; keeps scorer input small

logger = logging.getLogger("research")

# Exa limits: max nesting depth 2, max 10 total properties (array item properties count).
# Currently 10: 5 top-level scalars + open_roles + 4 item fields.
RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "domain": {"type": "string"},
        "identity_verified": {
            "type": "boolean",
            "description": "True only if the evidence is about the company at the given domain, not a similarly named one",
        },
        "business_summary": {"type": "string", "description": "What the company sells and to whom, in 1-2 sentences"},
        "signals": {
            "type": "string",
            "description": (
                "Growth and data-team signals: funding stage and recent rounds, headcount growth, "
                "data/analytics stack, growth or analytics team mentions. Say 'unknown' for what is not found."
            ),
        },
        "open_roles": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string", "description": "Direct URL of the posting"},
                    "location": {"type": "string"},
                    "remote_or_relocation": {
                        "type": "string",
                        "description": (
                            "What the posting says: remote (and for which regions or countries), hybrid, "
                            "on-site, relocation support, or unknown"
                        ),
                    },
                },
                "required": ["title", "url", "location", "remote_or_relocation"],
            },
        },
    },
    "required": ["company", "domain", "identity_verified", "business_summary", "signals", "open_roles"],
}

# Fallback: roles only, with a per-posting company check. 6 properties.
ATS_SCHEMA = {
    "type": "object",
    "properties": {
        "open_roles": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string", "description": "Direct URL of the posting"},
                    "location": {"type": "string"},
                    "remote_or_relocation": {
                        "type": "string",
                        "description": (
                            "What the posting says: remote (and for which regions or countries), hybrid, "
                            "on-site, relocation support, or unknown"
                        ),
                    },
                    "company_verified": {
                        "type": "boolean",
                        "description": "True only if the posting belongs to the company at the given domain",
                    },
                },
                "required": ["title", "url", "location", "remote_or_relocation", "company_verified"],
            },
        }
    },
    "required": ["open_roles"],
}

# Job boards / applicant-tracking systems that host company career pages.
ATS_DOMAINS = [
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "apply.workable.com",
    "jobs.smartrecruiters.com",
    "*.recruitee.com",
    "*.teamtailor.com",
    "*.personio.de",
]

SYSTEM_PROMPT = (
    "You research companies for a job seeker. Verify every fact against the company at the given "
    "domain; if sources describe a different company with a similar name, set identity_verified to false. "
    "List only roles that are currently open and have a direct posting URL on the company's careers page, "
    "an applicant-tracking system (Ashby, Greenhouse, Lever, Workable, etc.) or a job board. "
    "Never guess: use an empty open_roles list when none are found and 'unknown' for missing facts."
)

ATS_SYSTEM_PROMPT = (
    "You find currently open job postings for one company on applicant-tracking-system job boards. "
    "Many companies share similar names, so set company_verified to true only if the posting clearly "
    "belongs to the company at the given domain. Never guess; return an empty list if nothing matches."
)

_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def _compact_grounding(rows):
    """Merge per-subfield grounding (e.g. open_roles[3].url) into one entry per top-level field.

    Keeps the lowest confidence and the union of cited URLs.
    """
    merged = {}
    for row in rows:
        key = row.field.split("[")[0].split(".")[0]
        entry = merged.setdefault(key, {"confidence": row.confidence, "urls": []})
        if _CONFIDENCE_ORDER[row.confidence] < _CONFIDENCE_ORDER[entry["confidence"]]:
            entry["confidence"] = row.confidence
        for citation in row.citations:
            if citation.url not in entry["urls"]:
                entry["urls"].append(citation.url)
    return merged


def _roles_clause(profile):
    search = profile["search"]
    return f"{', '.join(search['roles'])} at {' or '.join(search['seniority'])} level"


def _location_clause(profile):
    location = profile["candidate"].get("location", "").strip()
    if not location:
        return ""
    return (
        f" Prefer postings that are remote from, or offer relocation for, candidates based in {location}."
        " In remote_or_relocation, quote what the posting says about allowed regions and relocation;"
        " do not judge the candidate's eligibility."
    )


def _structured(query, schema, system_prompt, **kwargs):
    response = exa_client.search_structured(query, schema, system_prompt=system_prompt, **kwargs)
    output = response.output
    if output is None or not isinstance(output.content, dict):
        raise RuntimeError(f"Exa returned no structured output for query: {query[:80]}")
    return output


@lru_cache(maxsize=None)
def _ashby_board(board):
    """(state, listed job ids), cached per run; see ashby.fetch_board for states."""
    state, jobs = ashby.fetch_board(board)
    return state, frozenset(job["id"] for job in jobs)


def check_posting(url):
    """Check that a posting URL points at a posting that is still open: "live", "dead" or "unknown".

    - Ashby (jobs.ashbyhq.com) answers HTTP 200 for any URL, so it is checked against Ashby's public
      posting API, which lists only open jobs.
    - A URL with no posting path (a board or careers homepage) cannot be confirmed: "unknown".
    - Otherwise: 404/410 or a redirect away from the posting (soft 404 landing on the board homepage)
      is dead; other non-200 answers (blocked, timeout, 5xx) are "unknown".
    """
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2 and parsed.netloc != ashby.HOST:
        return "unknown"
    if parsed.netloc == ashby.HOST:
        if len(segments) < 2:
            return "unknown"
        state, listed = _ashby_board(segments[0].lower())
        if state == "missing":
            return "dead"
        if state == "unknown":
            return "unknown"
        return "live" if segments[1].lower() in listed else "dead"
    try:
        response = requests.get(url, headers=BROWSER_HEADERS, timeout=15, allow_redirects=True)
    except requests.RequestException:
        return "unknown"
    if response.status_code in DEAD_STATUS:
        return "dead"
    if response.status_code != 200:
        return "unknown"
    if response.url != url:
        posting_id = segments[-1].split(".")[0]
        if posting_id.lower() not in response.url.lower():
            return "dead"
    return "live"


def verify_roles(roles):
    """Split roles into (usable, dead). Usable roles get link_status "live" or "unverified".

    Ambiguous HTTP results are resolved with a fresh Exa fetch (about $0.001 per URL): an Exa error
    means dead; otherwise the link is kept as "unverified" (reachable but not confirmed open).
    """
    if not roles:
        return [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda role: check_posting(role["url"]), roles))
    unknown = [role["url"] for role, state in zip(roles, states) if state == "unknown"]
    exa_errors = set()
    if unknown:
        try:
            response = exa_client.contents(unknown, max_age_hours=0)
            exa_errors = {s.id for s in (response.statuses or []) if s.status == "error"}
        except Exception as exc:  # keep the links as unverified rather than lose roles
            logger.warning("Exa link check failed (%s: %s); keeping %d link(s) unverified", type(exc).__name__, exc, len(unknown))
    usable, dead = [], []
    for role, state in zip(roles, states):
        if state == "dead" or (state == "unknown" and role["url"] in exa_errors):
            dead.append({"title": role["title"], "url": role["url"]})
        else:
            usable.append({**role, "link_status": "live" if state == "live" else "unverified"})
    if dead:
        logger.info("dropped %d dead posting link(s): %s", len(dead), ", ".join(d["title"] for d in dead))
    return usable, dead


def attach_descriptions(roles):
    """Add the posting text as `description` to the first MAX_DESCRIBED_ROLES live roles.

    Search summaries rarely contain a posting's responsibilities and required tools, which the scorer
    needs. Costs about $0.001 per page. Failures are logged and the roles are returned unchanged.
    """
    targets = [r for r in roles if r.get("link_status") == "live"][:MAX_DESCRIBED_ROLES]
    if not targets:
        return roles
    try:
        response = exa_client.contents(
            [r["url"] for r in targets], highlights=False, text={"max_characters": DESCRIPTION_CHARS}
        )
    except Exception as exc:
        logger.warning("fetching posting text failed (%s: %s)", type(exc).__name__, exc)
        return roles
    texts = {}
    for result in response.results:
        text = (result.text or "").strip()
        if text:
            texts[result.url.rstrip("/")] = text
            texts[result.id.rstrip("/")] = text
    described = []
    for role in roles:
        text = texts.get(role["url"].rstrip("/")) if role in targets else None
        described.append({**role, "description": text} if text else role)
    return described


def find_roles_on_ats(name, domain, profile):
    """Fallback role search restricted to ATS/job-board domains.

    Returns (roles, grounding) with only postings the model verified as belonging to the company.
    """
    query = (
        f"Open job postings at {name} ({domain}) for any of: {_roles_clause(profile)}. "
        f"{profile['search']['work_mode']}{_location_clause(profile)}"
    )
    output = _structured(query, ATS_SCHEMA, ATS_SYSTEM_PROMPT, include_domains=ATS_DOMAINS)
    roles = [
        {k: v for k, v in role.items() if k != "company_verified"}
        for role in output.content["open_roles"]
        if role.get("company_verified")
    ]
    return roles, _compact_grounding(output.grounding).get("open_roles")


def research_company(name, domain, profile, *, search_type="auto"):
    """Research one company.

    Returns {"name", "domain", "content": <schema dict>, "grounding": {field: {confidence, urls}},
    "role_source": "main" | "ats_fallback" | "none", "dead_roles": [{"title", "url"}]}.
    Every posting URL is checked and dead ones are dropped. If no live role remains for a verified
    company, a second search restricted to ATS domains looks for postings (also checked).
    """
    query = (
        f"{name} ({domain}): what the company does, recent funding and growth signals, and its data/analytics "
        f"team. Also currently open job postings for any of: {_roles_clause(profile)}. "
        f"{profile['search']['work_mode']}{_location_clause(profile)}"
    )
    output = _structured(query, RESEARCH_SCHEMA, SYSTEM_PROMPT, type=search_type)
    content = output.content
    grounding = _compact_grounding(output.grounding)
    roles, dead_roles = verify_roles(content["open_roles"])
    content["open_roles"] = roles
    role_source = "main" if roles else "none"

    if not roles and content["identity_verified"]:
        logger.info("%s: no live roles in main search; trying ATS fallback", name)
        fallback_roles, roles_grounding = find_roles_on_ats(name, domain, profile)
        roles, dead = verify_roles(fallback_roles)
        dead_roles += [d for d in dead if d not in dead_roles]
        if roles:
            content["open_roles"] = roles
            if roles_grounding:
                grounding["open_roles"] = roles_grounding
            role_source = "ats_fallback"

    content["open_roles"] = attach_descriptions(content["open_roles"])

    return {
        "name": name,
        "domain": domain,
        "content": content,
        "grounding": grounding,
        "role_source": role_source,
        "dead_roles": dead_roles,
    }
