"""Company research via Exa: identity, growth signals and matching open roles."""

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import unquote, urlparse

import requests

from clients import ashby, exa_client

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
DEAD_STATUS = {404, 410}
# How a page that answers 200 says that its posting has closed: a sentence of its own, in the words job boards and
# career sites in saved runs use ("This position is no longer active" on Teamtailor, "Sorry, this position has been
# filled" on SuccessFactors, "Job listing has expired", "<company> is no longer taking applications for this job").
# Matched from the sentence's start to its end or a comma, so neither "... until the position has been filled" nor
# "This role is no longer available as a remote position" in an open posting.
CLOSED_RE = re.compile(
    r"(?:(?:we(?:['’]re| are) )?sorry,? |unfortunately,? )?"
    r"(?:(?:this|the) (?:job|position|role|vacancy)(?: (?:opening|listing|posting|offer|ad))?"
    r"(?: you(?: are| were|['’]re) looking for)? "
    r"(?:(?:is|was) no longer (?:available|active|open|online|listed|posted|vacant|accepting applications)"
    r"|is not available any ?more|(?:has|is|has been) expired"
    r"|has been (?:filled|closed|removed|taken down|archived)|is (?:now )?closed)"
    r"|job(?: (?:listing|posting|offer|ad))? (?:has |has been |is )?expired"
    r"|no longer accepting applications"
    r"|.{0,80}\bno longer (?:accepting|taking) applications for this (?:job|position|role|vacancy))"
    r"\s*(?:$|[,(–—-])",
    re.IGNORECASE,
)
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

# Job boards / applicant-tracking systems that host company career pages. Each one is a host a posting in saved
# runs was on; the link check tells an open posting from a closed one on all of them.
ATS_DOMAINS = [
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "apply.workable.com",
    "jobs.workable.com",
    "jobs.smartrecruiters.com",
    "*.recruitee.com",
    "*.teamtailor.com",
    "*.personio.de",
    "*.personio.com",
    "*.pinpointhq.com",
]

SYSTEM_PROMPT = (
    "You research companies for a job seeker. Verify every fact against the company at the given "
    "domain; if sources describe a different company with a similar name, set identity_verified to false. "
    "List only roles that are currently open and have a direct posting URL. Give the posting's URL on the "
    "company's own careers site or its applicant-tracking system (Ashby, Greenhouse, Lever, Workable, etc.); give "
    "a third-party job board's or aggregator's URL only for a role you cannot find there. "
    "Never guess: use an empty open_roles list when none are found and 'unknown' for missing facts."
)

ATS_SYSTEM_PROMPT = (
    "You find currently open job postings for one company on its own careers site and on "
    "applicant-tracking-system job boards. "
    "Many companies share similar names, so set company_verified to true only if the posting clearly "
    "belongs to the company at the given domain. Never guess; return an empty list if nothing matches."
)

MAX_ROLES = 8  # open roles kept per company, as the search schemas ask for
SEEN_TITLE_CHARS = 120  # of a third-party listing's title, as the fallback search's query quotes it

# Open roles from Fiber's jobs database, asked through an Exa agent run (which has no 10-property limit).
FIBER_SCHEMA = {
    "type": "object",
    "properties": {
        "open_roles": {
            "type": "array",
            "maxItems": MAX_ROLES,
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
        }
    },
    "required": ["open_roles"],
}

FIBER_SYSTEM_PROMPT = (
    "You list one company's open job postings from a jobs database. List only postings that belong to the "
    "company at the given domain and are open now. Give each posting's URL on the company's own careers site or "
    "its applicant-tracking system, never an aggregator's copy. Do not leave a posting out for its seniority or "
    "its location: that is judged later. Never guess; return an empty list if nothing matches."
)


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


@lru_cache(maxsize=None)
def _ashby_board(board):
    """(state, listed job ids), cached per run; see ashby.fetch_board for states."""
    state, jobs = ashby.fetch_board(board)
    return state, frozenset(job["id"] for job in jobs)


_OPENS_RE = re.compile(r"<!--|</?[a-zA-Z]")  # where a comment or a tag starts
_TAG_RE = re.compile(r"""<(/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>""")  # (closing, name, attributes)
_RAW_END_RE = {tag: re.compile(f"</{tag}", re.IGNORECASE) for tag in ("script", "style")}  # their text is not markup
# Elements whose text is not on the page as it loads: fallbacks, templates, pop-ups, form controls, drawings.
_UNSEEN_TAGS = {"noscript", "template", "dialog", "select", "datalist", "textarea", "svg", "iframe", "object", "canvas"}
# In a tag's attributes, what takes the element off the page or leaves showing it to a script: the `hidden`
# attribute or a utility class, a framework's condition (v-if, x-show, ng-if), an inline style, ARIA's word for it.
_HIDES_RE = re.compile(
    r"(?<![-\w])(?:hidden|d-none|hide|is-hidden|invisible|sr-only|visually-hidden"
    r"|(?:v|x|ng)-(?:if|else-if|else|show|hide|cloak)|\*ngif)(?![-\w])"
    r"|display\s*:\s*none|visibility\s*:\s*hidden|aria-hidden\s*=\s*[\"']?true|role\s*=\s*[\"']?(?:alert)?dialog",
    re.IGNORECASE,
)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# Phrasing tags: a sentence runs on through them, so "until <span>the position has been filled</span>" is one.
_INLINE_TAGS = frozenset(
    "a abbr b bdi bdo br cite code data del dfn em font i ins kbd mark q s samp small span strong sub sup time u var wbr".split()
)
_SENTENCE_END_RE = re.compile(r"[.!?:;|](?:\s|$)")  # one mark and one space: a run of marks must not be rescanned


def _page_blocks(markup):
    """The text a page shows a reader as it loads, one string per block: no comments, scripts or styles, and nothing
    inside an element that is hidden or shown only by a script (_UNSEEN_TAGS, _HIDES_RE), where a page keeps the
    notices that apply to other postings than this one. A class that a style sheet hides is not known here.

    One pass, whatever the markup: the page comes from any site, and html.parser needs minutes for a megabyte of
    unclosed comments on Python 3.13.2. Every step moves on from where the last one ended.
    """
    blocks, words = [], []
    read_to = look_from = 0  # where the text not yet read starts, and where the next tag is looked for
    unseen = None  # [tag, how many of it are open]: the element that hides what is inside it
    while opens := _OPENS_RE.search(markup, look_from):
        at = opens.start()
        if not unseen:
            words += html.unescape(markup[read_to:at]).split()
        if opens.group() == "<!--":
            end = markup.find("-->", at + 4)
            read_to = look_from = end + 3 if end >= 0 else len(markup)  # never closed: nothing after it is text
            continue
        tag = _TAG_RE.match(markup, at)
        if not tag:  # a tag that never ends, as with a quote left open: nothing after it is text
            read_to = look_from = len(markup)
            break
        read_to = look_from = tag.end()
        closing, name, attributes = tag.group(1), tag.group(2).lower(), tag.group(3)
        if name in _RAW_END_RE and not closing:
            ended = _RAW_END_RE[name].search(markup, read_to)
            read_to = look_from = ended.start() if ended else len(markup)
            continue
        if name not in _INLINE_TAGS and words:
            blocks.append(" ".join(words))
            words = []
        if name in _VOID_TAGS or attributes.rstrip().endswith("/"):
            continue
        if unseen:
            if name == unseen[0]:
                unseen[1] += -1 if closing else 1
                unseen = unseen if unseen[1] else None
        elif not closing and (name in _UNSEEN_TAGS or _HIDES_RE.search(attributes)):
            unseen = [name, 1]
    if not unseen:
        words += html.unescape(markup[read_to:]).split()
    return [*blocks, " ".join(words)]


def _says_closed(page):
    """True when the page a posting link answers with says, in a sentence of its own, that the posting has closed."""
    return any(
        CLOSED_RE.match(sentence)
        for block in _page_blocks(page.decode("utf-8", errors="replace"))
        for sentence in _SENTENCE_END_RE.split(block)
    )


def check_posting(url):
    """Check that a posting URL points at a posting that is still open: "live", "dead" or "unknown".

    - Ashby (jobs.ashbyhq.com) answers HTTP 200 for any URL, so it is checked against Ashby's public
      posting API, which lists only open jobs.
    - A URL with no posting path (a board or careers homepage) cannot be confirmed: "unknown".
    - Otherwise: 404/410, a redirect away from the posting (soft 404 landing on the board homepage) or a
      page that says the posting has closed (_says_closed) is dead; other non-200 answers (blocked, timeout,
      5xx) and a redirect to a login or a bot check are "unknown".
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
    if unquote(response.url) != unquote(url):  # requests percent-encodes non-ASCII and spaces before sending
        landed = urlparse(response.url)
        if unquote(landed.path) != unquote(parsed.path) and unquote(parsed.path) in unquote(landed.query):
            # a login or a bot check that keeps the posting's address to send the visitor back: the posting was not seen
            return "unknown"
        posting_id = unquote(segments[-1].split(".")[0])
        if posting_id.lower() not in unquote(response.url).lower():
            return "dead"
    if _says_closed(response.content):
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


def _third_party_leads(roles):
    """Split the postings listed on third-party sites into (leads, dead), each link once.

    Only the free check: a page that is gone or says the posting has closed is dead, any other is a lead. No Exa
    fetch settles an unclear one, since a lead is never offered as a posting.
    """
    if not roles:
        return [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda role: check_posting(role["url"]), roles))
    leads, dead = {}, {}
    for role, state in zip(roles, states):
        if state == "dead":
            dead.setdefault(role["url"], {"title": role["title"], "url": role["url"]})
        else:
            leads.setdefault(role["url"], role)
    if dead:
        logger.info("dropped %d dead third-party link(s): %s", len(dead), ", ".join(d["title"] for d in dead.values()))
    return list(leads.values()), list(dead.values())


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


def find_roles_on_ats(name, domain, profile, *, seen_elsewhere=()):
    """Role search restricted to where the company posts its jobs itself: its own site and the ATS/job-board domains.

    `seen_elsewhere` holds the titles of postings that third-party sites list, for the search to look for. Returns
    (roles, grounding) with only postings the model verified as belonging to the company.
    """
    query = (
        f"Open job postings at {name} ({domain}) for any of: {_roles_clause(profile)}. "
        f"{profile['search']['work_mode']}{_location_clause(profile)}"
    )
    if seen_elsewhere:
        titles = "; ".join(title[:SEEN_TITLE_CHARS] for title in seen_elsewhere)
        query += f" Other sites list these postings of the company; look for each of them: {titles}."
    content, grounding = exa_client.grounded_answer(
        query, ATS_SCHEMA, system_prompt=ATS_SYSTEM_PROMPT, include_domains=[domain, f"*.{domain}", *ATS_DOMAINS]
    )
    roles = [
        {k: v for k, v in role.items() if k != "company_verified"}
        for role in content["open_roles"]
        if _named(role) and role.get("company_verified") and _posts_own_jobs(role["url"], domain)
    ]
    return roles, grounding.get("open_roles")


def _posting_url(url):
    """The posting behind an Ashby link, in the one form the link check knows: no application-form suffix
    (.../<id>/application, whatever follows it), port or upper-case host. Any other URL as it is."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.hostname != ashby.HOST:
        return url
    return f"https://{ashby.HOST}{parsed.path.rstrip('/').removesuffix('/application')}"


_UUID_RE = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _posting_key(url):
    """What two links to one posting share (compared within one company's postings only).

    Host and path count without letter case, "www.", a trailing slash or Ashby's application-form suffix: the
    main search returns jobs.ashbyhq.com/Notion/<id>/application for jobs.ashbyhq.com/notion/<id>. The query and
    the fragment count as they are, apart from utm_ parameters: some career sites name the posting there
    (/careers/job?gh_jid=123), and merging two of those would drop a real role.

    A job board's posting id (a UUID) is the key by itself when the link ends in it and says nothing more, or
    carries it as ?ashby_jid=: a careers page that embeds the board links the posting that way
    (linear.app/careers/<id> for jobs.ashbyhq.com/linear/<id>).
    """
    parsed = urlparse(_posting_url(url))
    host = (parsed.hostname or "").removeprefix("www.")
    path = parsed.path.rstrip("/").lower()
    parts = sorted(part for part in parsed.query.split("&") if part and not part.lower().startswith("utm_"))
    posting_ids = [part.partition("=")[2].lower() for part in parts if part.lower().startswith("ashby_jid=")]
    if not parts and not parsed.fragment:
        posting_ids.append(path.rpartition("/")[2])
    if posting_ids and _UUID_RE.fullmatch(posting_ids[0]):
        return posting_ids[0]
    return f"{host}{path}?{'&'.join(parts)}#{parsed.fragment}"


_HOST_RE = re.compile(r"[\w.-]+")
# Under a two-letter country code these are part of the suffix (acme.co.uk), not the name a site is registered under.
_SUFFIX_LABELS = {"ac", "co", "com", "edu", "gov", "net", "org"}
CAREERS_WORDS = ("jobs", "job", "careers", "career")  # acme-jobs.com: a company's careers site on a domain of its own
SIBLING_NAME_CHARS = 4  # a shorter company name is registered by too many others to count on another domain


def _host(url):
    """The host of a plain http(s) link, without "www."; "" for anything else, also for a link written to read like
    another host (evil.io\\.acme.com, evil.io\\@acme.com)."""
    try:
        parsed = urlparse(url)
    except ValueError:  # not a URL at all
        return ""
    host = (parsed.hostname or "").rstrip(".").removeprefix("www.")
    if parsed.scheme not in ("http", "https") or "@" in parsed.netloc or "\\" in url or not _HOST_RE.fullmatch(host):
        return ""
    return host


def _registered(host):
    """(name, subdomain) a host is registered under: ("acme-jobs", "careers") for careers.acme-jobs.com,
    ("acme", "") for acme.co.uk."""
    labels = host.split(".")
    suffix = 2 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in _SUFFIX_LABELS else 1
    if len(labels) <= suffix:
        return "", ""
    return labels[-suffix - 1], ".".join(labels[: -suffix - 1])


def _posts_own_jobs(url, domain):
    """True for an http(s) link on a site where the company posts its jobs itself: its own site, one of the job-board
    hosts in ATS_DOMAINS, or a site registered under its name on another domain: the name alone (careers.acme.eu
    for acme.com) or with a careers word (acme-jobs.com, careers-acme.io), so neither acme-labs.com, acmejobs.com
    nor acme.someboard.io. Any other host is a third party: an aggregator's page answers 200 for a job that has
    closed, or that the company never posted, and may ask for an account or payment before it shows where to
    apply."""
    host = _host(url)
    if not host:
        return False
    if host == domain or host.endswith(f".{domain}"):
        return True
    if any(host.endswith(ats[1:]) if ats.startswith("*.") else host == ats for ats in ATS_DOMAINS):
        return True
    name, under = _registered(domain)
    if len(name) < SIBLING_NAME_CHARS or under:
        return False  # a name too short to tell from another's, or a company site under another's name (acme.github.io)
    site, subdomain = _registered(host)
    if subdomain not in ("", *CAREERS_WORDS):
        return False  # someone.acme.site: a page anyone can put up on a platform of that name
    return site == name or any(site in (f"{name}-{word}", f"{word}-{name}") for word in CAREERS_WORDS)


def _is_third_party(url, domain):
    """True for a web link on a site where the company does not post its jobs itself (see _posts_own_jobs)."""
    return bool(_host(url)) and not _posts_own_jobs(url, domain)


def _named(role):
    """True for a listed role with a title and a link, both text: without them there is nothing to check or to show."""
    return isinstance(role, dict) and all(isinstance(role.get(k), str) and role[k].strip() for k in ("title", "url"))


def _by_site(name, roles, domain):
    """Split the roles a search lists into (own, elsewhere): postings where the company posts its jobs itself, and
    web links on third-party sites. What is neither (no title, no web link, or a link written to read like another
    host) is left out: there is nothing to check or to open."""
    own = [role for role in roles if _named(role) and _posts_own_jobs(role["url"], domain)]
    elsewhere = [role for role in roles if _named(role) and _is_third_party(role["url"], domain)]
    if len(own) + len(elsewhere) < len(roles):
        logger.info("%s: left out %d role(s) without a title or a usable link", name, len(roles) - len(own) - len(elsewhere))
    return own, elsewhere


def is_listed(url, roles):
    """True when `url` is the link of one of `roles`, whatever form it takes (see _posting_key)."""
    try:
        key = _posting_key(url)
    except ValueError:  # not a URL at all
        return False
    return any(_posting_key(role["url"]) == key for role in roles)


def _title_key(title):
    """A title's words, lower-cased, with a space around each: " senior data analyst "."""
    words = re.sub(r"[\W_]+", " ", title.casefold()).strip()
    return f" {words} "


def _not_found(leads, roles):
    """The third-party listings that none of the kept postings stands for. A posting does when its title holds the
    listing's, word for word: "Senior Data Analyst (Remote, EU)" for "Senior Data Analyst", not "Data Analyst"."""
    titles = [_title_key(role["title"]) for role in roles]
    return [lead for lead in leads if not any(_title_key(lead["title"]) in title for title in titles)]


def _cited(groundings):
    """One grounding for roles that several searches supplied: every source, at the lowest of their confidences."""
    levels = ("low", "medium", "high")
    known = [g["confidence"] for g in groundings if g["confidence"] in levels]
    urls = list(dict.fromkeys(url for g in groundings for url in g["urls"]))
    return {"confidence": min(known, key=levels.index) if known else None, "urls": urls}


def find_roles_on_fiber(name, domain, profile):
    """Open postings from Fiber's jobs database, asked through an Exa agent run: (roles, grounding).

    It asks for the target roles at any seniority: asked for senior roles only, the agent leaves out postings
    whose title does not say so, and seniority is the screen's and the scorer's call anyway. Only postings on the
    company's own site or a job board it posts on are kept (see _posts_own_jobs). One agent run plus Fiber's own
    charge per search, $0.09 to $0.40 a company in the first runs.
    """
    search = profile["search"]
    query = (
        f"Using a B2B jobs database, list the job postings currently open at {name} ({domain}) whose work is any "
        f"of: {', '.join(search['roles'])}. Include every matching posting whatever its seniority or location; "
        f"when more than {MAX_ROLES} match, prefer {' or '.join(search['seniority'])} level. For each return the "
        "title, the direct URL of the posting, the location and what the posting says about remote work or "
        f"relocation.{_location_clause(profile)}"
    )
    content, grounding = exa_client.agent_answer(
        query, FIBER_SCHEMA, system_prompt=FIBER_SYSTEM_PROMPT, data_sources=["fiber"]
    )
    roles, elsewhere = {}, set()
    for role in content.get("open_roles") or []:
        if not _named(role):
            continue  # nothing to check or to show
        try:
            own = _posts_own_jobs(role["url"].strip(), domain)  # judged as given, before an Ashby link is rewritten
            url = _posting_url(role["url"])
            key = _posting_key(url)
        except ValueError:  # not a URL at all
            continue
        if not own:
            elsewhere.add(urlparse(url).netloc or url)
            continue
        roles.setdefault(key, {
            "title": role["title"].strip(),
            "url": url,
            "location": str(role.get("location") or "unknown"),
            "remote_or_relocation": str(role.get("remote_or_relocation") or "unknown"),
        })  # fmt: skip
    if elsewhere:
        logger.info("%s: left out Fiber postings on other sites (%s)", name, ", ".join(sorted(elsewhere)))
    return list(roles.values()), grounding.get("open_roles")


def research_company(name, domain, profile, *, fiber=False, search_type="auto"):
    """Research one company.

    Returns {"name", "domain", "content": <schema dict>, "grounding": {field: {confidence, urls}},
    "role_source": "main" | "fiber" | "fiber+main" | "ats_fallback" | "none" (or the first three with
    "+ats_fallback"), "dead_roles": [{"title", "url"}], "third_party_roles": [<role as listed>],
    "warnings": [str]}.
    Open roles are postings where the company posts its jobs itself (_posts_own_jobs): every such URL is checked
    and dead ones are dropped. A posting the main search lists on a third-party site is never an open role: it is
    a lead. With fiber (hunt.py run --fiber), a verified company's open postings are also asked from Fiber's jobs
    database: live links first, Fiber's before the main search's that it does not list, then unverified ones,
    MAX_ROLES in all. If no live role remains for a verified company, or no open role stands for one of its leads,
    a second search restricted to its own site and the ATS domains looks for postings (also checked) and adds the
    ones not listed yet. The leads still not found are returned as "third_party_roles". When the Fiber search or
    the ATS search fails, the main research is kept and the failure is listed in "warnings".
    """
    query = (
        f"{name} ({domain}): what the company does, recent funding and growth signals, and its data/analytics "
        f"team. Also currently open job postings for any of: {_roles_clause(profile)}. "
        f"{profile['search']['work_mode']}{_location_clause(profile)}"
    )
    content, grounding = exa_client.grounded_answer(query, RESEARCH_SCHEMA, system_prompt=SYSTEM_PROMPT, type=search_type)
    own, elsewhere = _by_site(name, content["open_roles"], domain)
    roles, dead_roles = verify_roles(own)
    leads, dead = _third_party_leads(elsewhere)
    dead_roles += dead
    content["open_roles"] = roles
    role_source = "main" if roles else "none"

    warnings = []
    if fiber and content["identity_verified"]:
        try:
            listed, listed_grounding = find_roles_on_fiber(name, domain, profile)
        except Exception as exc:  # a second source: the main research is still worth saving
            logger.warning("%s: Fiber job search failed (%s); keeping the main search's roles", name, exc)
            warnings.append(f"Fiber job search failed: {type(exc).__name__}: {exc}")
            listed, listed_grounding = [], None
        from_fiber, dead = verify_roles(listed)
        dead_roles += [d for d in dead if d not in dead_roles]
        if from_fiber:
            mains = {}  # the main search's copy of each posting: its live one when it lists a posting twice
            for role in roles:
                key = _posting_key(role["url"])
                if key not in mains or (role["link_status"] == "live" and mains[key]["link_status"] != "live"):
                    mains[key] = role
            # A posting both list keeps the main search's link, unless only Fiber's is live: that link is the one
            # earlier runs saved, and the dashboard keeps the owner's status and note by link.
            theirs = {}
            for role in from_fiber:
                key = _posting_key(role["url"])
                same = mains.get(key)
                theirs[key] = same if same and (same["link_status"] == "live" or role["link_status"] != "live") else role
            only_main = [role for role in roles if _posting_key(role["url"]) not in theirs]
            # Checked links first: an unverified one must not push a live one past the cap.
            kept = sorted([*theirs.values(), *only_main], key=lambda role: role["link_status"] != "live")[:MAX_ROLES]
            fibers = sum(_posting_key(role["url"]) in theirs for role in kept)
            if fibers:
                main_links = any(role is copy for role in kept for copy in roles)
                roles = content["open_roles"] = kept
                role_source = "fiber+main" if fibers < len(kept) else "fiber"
                # Cite what the kept links came from, Fiber's sources first, at the lower of the two confidences.
                cited = [g for g in (listed_grounding, grounding.get("open_roles") if main_links else None) if g]
                if cited:
                    grounding["open_roles"] = _cited(cited)
                else:
                    grounding.pop("open_roles", None)

    wanted = _not_found(leads, roles)
    if content["identity_verified"] and (not roles or wanted):
        logger.info("%s: %s; trying ATS fallback", name,
                    f"{len(wanted)} posting(s) only on third-party sites" if roles else "no live roles so far")
        try:
            found, found_grounding = find_roles_on_ats(
                name, domain, profile, seen_elsewhere=[lead["title"] for lead in wanted]
            )
        except Exception as exc:  # the fallback is a bonus: the main research is still worth saving
            logger.warning("%s: ATS fallback search failed (%s); keeping main research", name, exc)
            warnings.append(f"ATS fallback search failed: {type(exc).__name__}: {exc}")
            found, found_grounding = [], None
        found, dead = verify_roles(found)
        dead_roles += [d for d in dead if d not in dead_roles]
        new = {}  # what the fallback adds: each posting once, none that is listed already
        listed = {_posting_key(role["url"]) for role in roles}
        for role in found:
            key = _posting_key(role["url"])
            if key not in listed:
                new.setdefault(key, role)
        # Checked links first, as with Fiber's: an unverified one must not push a live one past the cap.
        kept = sorted([*roles, *new.values()], key=lambda role: role["link_status"] != "live")[:MAX_ROLES]
        earlier = [role for role in kept if any(role is copy for copy in roles)]
        if len(kept) > len(earlier):
            cited = [g for g in (grounding.get("open_roles") if earlier else None, found_grounding) if g]
            if cited:
                grounding["open_roles"] = _cited(cited)
            role_source = f"{role_source}+ats_fallback" if earlier else "ats_fallback"
            roles = content["open_roles"] = kept

    # a posting that is open under one link is not a dead one for its other link
    kept_keys = {_posting_key(role["url"]) for role in roles}
    dead_roles = [d for d in dead_roles if _posting_key(d["url"]) not in kept_keys]
    content["open_roles"] = attach_descriptions(roles)

    return {
        "name": name,
        "domain": domain,
        "content": content,
        "grounding": grounding,
        "role_source": role_source,
        "dead_roles": dead_roles,
        "third_party_roles": _not_found(leads, roles),
        "warnings": warnings,
    }
