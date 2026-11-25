"""Company job boards with a public feed that needs no key: Ashby, Greenhouse, Lever, Workable, SmartRecruiters,
Recruitee, Teamtailor, Personio and Pinpoint.

A board's feed lists every posting that is open now, so it answers what a search cannot: which postings a company
has, and whether one is still open. `board_of` says which board a posting link is on, `fetch` reads the board,
`posting_id` says which of its postings a link is, and `questions` reads the free-text questions of a posting's
application form where the board shows them (Greenhouse, Ashby).

Everything a board returns is the company's text on a web page: a title, a location, a posting, a question. Callers
pass it on as a posting's fields and never as instructions.
"""

import html
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import requests

TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
ASHBY_HOST = "jobs.ashbyhq.com"
# Hosts where the board is the first part of the path: jobs.ashbyhq.com/<board>/<posting>.
PATH_HOSTS = {
    ASHBY_HOST: "ashby",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "apply.workable.com": "workable",
    "jobs.smartrecruiters.com": "smartrecruiters",
}
# Hosts where the board is a subdomain: <board>.recruitee.com.
SUBDOMAIN_HOSTS = {
    ".recruitee.com": "recruitee",
    ".teamtailor.com": "teamtailor",
    ".jobs.personio.de": "personio",
    ".jobs.personio.com": "personio",
    ".pinpointhq.com": "pinpoint",
}
NOT_A_BOARD = {"www", "app", "api", "j", "embed", "career", "careers", "jobs", "support", "help"}
NAMES = {"ashby": "Ashby", "greenhouse": "Greenhouse", "lever": "Lever", "workable": "Workable",
         "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee", "teamtailor": "Teamtailor",
         "personio": "Personio", "pinpoint": "Pinpoint"}
SMARTRECRUITERS_PAGE = 100  # postings per request, the most its API gives
SMARTRECRUITERS_PAGES = 5
MAX_QUESTIONS = 12
QUESTION_CHARS = 300
# A form's text fields that ask for a link or a contact detail, not for an answer to write.
NOT_A_QUESTION = re.compile(r"linkedin|github|gitlab|portfolio|website|twitter|\burl\b|\blink\b|phone|e-?mail", re.IGNORECASE)
ASHBY_FORM_API = f"https://{ASHBY_HOST}/api/non-user-graphql?op=ApiJobPosting"
ASHBY_FORM_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
    "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) { "
    "applicationForm { sections { fieldEntries { ... on FormFieldEntry { isRequired field } } } } } }"
)


class _Unreadable(Exception):
    """A feed that answered with something else than its format."""


class _Missing(Exception):
    """A feed that answers 404: no such board, or no such posting. Not a LookupError, which a KeyError is: an answer
    in another shape must not pass for a board that is gone."""


def _segments(parsed):
    return [segment for segment in parsed.path.split("/") if segment]


def _parse(url):
    """(host without "www.", path segments, query) of an http(s) link; ("", [], {}) for anything else."""
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return "", [], {}
    if parsed.scheme not in ("http", "https"):
        return "", [], {}
    return (parsed.hostname or "").removeprefix("www."), _segments(parsed), parse_qs(parsed.query)


def board_of(url):
    """The board a posting link is on: {"ats", "slug", "host"}. None for a link on no board with a public feed, and
    for one that names no board (apply.workable.com/j/<posting>)."""
    host, segments, query = _parse(url)
    ats = PATH_HOSTS.get(host)
    if ats:
        slug = segments[0] if segments else ""
        if ats == "greenhouse" and slug == "embed":  # boards.greenhouse.io/embed/job_board?for=<board>
            slug = (query.get("for") or [""])[0]
        if not slug or slug.lower() in NOT_A_BOARD:
            return None
        # SmartRecruiters spells its company identifiers with capitals; the others take either case.
        return {"ats": ats, "slug": slug if ats == "smartrecruiters" else slug.lower(), "host": host}
    for suffix, ats in SUBDOMAIN_HOSTS.items():
        slug = host.removesuffix(suffix)
        if host.endswith(suffix) and slug and "." not in slug and slug not in NOT_A_BOARD:
            return {"ats": ats, "slug": slug, "host": host}
    return None


def posting_id(url):
    """Which posting of its board a link is: (ats, id), the same for every form the board gives the link
    (apply.workable.com/j/<id> and apply.workable.com/<board>/j/<id>). None for a link on no board, or to none of
    its postings."""
    host, segments, query = _parse(url)
    ats = PATH_HOSTS.get(host)
    if ats == "ashby":
        return ("ashby", segments[1].lower()) if len(segments) > 1 else None
    if ats == "greenhouse":
        after = segments[segments.index("jobs") + 1:] if "jobs" in segments else []
        found = after[0] if after else (query.get("gh_jid") or query.get("token") or [""])[0]
        return ("greenhouse", found) if found.isdigit() else None
    if ats == "lever":
        return ("lever", segments[1].lower()) if len(segments) > 1 else None
    if ats == "workable":
        after = segments[segments.index("j") + 1:] if "j" in segments else []
        return ("workable", after[0].upper()) if after else None
    if ats == "smartrecruiters":
        digits = re.match(r"\d+", segments[1]) if len(segments) > 1 else None
        return ("smartrecruiters", digits.group()) if digits else None
    board = board_of(url)
    if not board or not segments:
        return None
    last = segments[-1]
    if board["ats"] == "recruitee":
        return ("recruitee", f"{board['slug']}/{last.lower()}") if len(segments) > 1 and segments[-2] == "o" else None
    if board["ats"] == "teamtailor":
        digits = re.match(r"\d+", last) if len(segments) > 1 and segments[-2] == "jobs" else None
        return ("teamtailor", f"{board['slug']}/{digits.group()}") if digits else None
    if board["ats"] == "personio":
        return ("personio", f"{board['slug']}/{last}") if len(segments) > 1 and segments[-2] == "job" and last.isdigit() else None
    return ("pinpoint", f"{board['slug']}/{last.lower()}") if len(segments) > 1 and segments[-2] == "postings" else None


def page(board):
    """The board's own page, where its postings are listed for a visitor."""
    if board["ats"] in SUBDOMAIN_HOSTS.values():
        return f"https://{board['host']}"
    return f"https://{board['host']}/{board['slug']}"


def label(board):
    """The board in a few words, for a log line or a message: "Ashby board acme"."""
    return f"{NAMES[board['ats']]} board {board['slug']}"


def _plain(text):
    """Markup as the text it shows, a block per line."""
    text = html.unescape(str(text or ""))
    text = re.sub(r"(?i)<\s*(?:br|/p|/li|/ul|/ol|/div|/h[1-6]|/tr)\b[^>]*>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]*>", " ", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _posting(posting_id, title, hosted, given=None, *, location="", remote="", posted="", text=""):
    """One posting as `fetch` returns it, or None without a usable link. `hosted` is the posting on the board's own
    host; `given` is the link the feed names for it, which can be a page of the company's own careers site. A
    posting the board gives no title is kept, with "": it is open all the same."""
    usable = [link for link in (str(given or "").strip(), str(hosted or "").strip())
              if link.startswith(("https://", "http://")) and not any(char.isspace() for char in link)]
    if not usable:
        return None
    return {
        "id": str(posting_id),
        "title": " ".join(str(title or "").split()),
        "url": usable[0],
        "hosted": usable[-1],
        "location": " ".join(str(location or "").split()),
        "remote": remote if remote in ("remote", "hybrid", "on-site") else "",
        "posted": str(posted or "")[:10],
        "text": _plain(text) if text else "",
    }


def _get(url, **params):
    """The feed's answer as a `requests` response; raises _Missing for a board that does not exist and _Unreadable
    for any other answer than 200."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT, params=params or None)
    except requests.RequestException as exc:
        raise _Unreadable(str(exc)) from exc
    if response.status_code == 404:
        raise _Missing(url)
    if response.status_code != 200:
        raise _Unreadable(f"HTTP {response.status_code}")
    return response


def _workplace(value):
    """A board's word for where the work is done, as one of "remote", "hybrid", "on-site" or ""."""
    word = re.sub(r"[^a-z]", "", str(value or "").lower())
    return {"remote": "remote", "fully": "remote", "hybrid": "hybrid", "onsite": "on-site", "none": "on-site"}.get(word, "")


def _ashby(board):
    jobs = _get(f"https://api.ashbyhq.com/posting-api/job-board/{board['slug']}").json()["jobs"]
    return [
        _posting(str(job["id"]).lower(), job.get("title"), f"https://{ASHBY_HOST}/{board['slug']}/{job['id']}",
                 job.get("jobUrl"),
                 location=job.get("location"), remote=_workplace(job.get("workplaceType")),
                 posted=job.get("publishedAt"), text=job.get("descriptionPlain") or job.get("descriptionHtml"))
        for job in jobs
    ]


def _greenhouse(board):
    jobs = _get(f"https://boards-api.greenhouse.io/v1/boards/{board['slug']}/jobs", content="true").json()["jobs"]
    return [
        _posting(job["id"], job.get("title"), f"https://{board['host']}/{board['slug']}/jobs/{job['id']}",
                 job.get("absolute_url"),
                 location=(job.get("location") or {}).get("name"),
                 posted=job.get("first_published") or job.get("updated_at"), text=job.get("content"))
        for job in jobs
    ]


def _lever(board):
    api = "api.eu.lever.co" if board["host"] == "jobs.eu.lever.co" else "api.lever.co"
    jobs = _get(f"https://{api}/v0/postings/{board['slug']}", mode="json").json()
    if not isinstance(jobs, list):
        raise _Unreadable("not a list of postings")
    postings = []
    for job in jobs:
        lists = "\n".join(f"{item.get('text', '')}\n{_plain(item.get('content'))}" for item in job.get("lists") or [])
        text = "\n".join(part for part in (job.get("descriptionPlain"), lists, job.get("additionalPlain")) if part)
        created = job.get("createdAt")  # milliseconds
        posted = (datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat()
                  if isinstance(created, (int, float)) else "")
        postings.append(_posting(str(job["id"]).lower(), job.get("text"),
                                 f"https://{board['host']}/{board['slug']}/{job['id']}", job.get("hostedUrl"),
                                 location=(job.get("categories") or {}).get("location"),
                                 remote=_workplace(job.get("workplaceType")), posted=posted, text=text))
    return postings


def _workable(board):
    jobs = _get(f"https://apply.workable.com/api/v1/widget/accounts/{board['slug']}", details="true").json()["jobs"]
    return [
        _posting(str(job["shortcode"]).upper(), job.get("title"),
                 f"https://apply.workable.com/{board['slug']}/j/{job['shortcode']}",
                 location=", ".join(part for part in (job.get("city"), job.get("country")) if part),
                 remote="remote" if job.get("telecommuting") else "",
                 posted=job.get("published_on") or job.get("created_at"), text=job.get("description"))
        for job in jobs
    ]


def _smartrecruiters(board):
    """Its list has no posting text: a posting's page has to be read for it, as for a posting a search found. A
    company it does not know is answered like one that has no posting open, so neither counts as read."""
    postings = []
    for page_number in range(SMARTRECRUITERS_PAGES):
        answer = _get(f"https://api.smartrecruiters.com/v1/companies/{board['slug']}/postings",
                      limit=SMARTRECRUITERS_PAGE, offset=page_number * SMARTRECRUITERS_PAGE).json()
        for job in answer["content"]:
            place = job.get("location") or {}
            remote = "remote" if place.get("remote") else "hybrid" if place.get("hybrid") else ""
            postings.append(_posting(job["id"], job.get("name"),
                                     f"https://jobs.smartrecruiters.com/{board['slug']}/{job['id']}",
                                     location=place.get("fullLocation") or place.get("city"), remote=remote,
                                     posted=job.get("releasedDate")))
        if len(postings) >= answer.get("totalFound", 0) or not answer["content"]:
            break
    if not postings:
        raise _Unreadable("no postings, or no such company")
    return postings


def _recruitee(board):
    offers = _get(f"https://{board['host']}/api/offers/").json()["offers"]
    postings = []
    for offer in offers:
        remote = "remote" if offer.get("remote") else "hybrid" if offer.get("hybrid") else "on-site" if offer.get("on_site") else ""
        text = "\n".join(part for part in (offer.get("description"), offer.get("requirements")) if part)
        postings.append(_posting(f"{board['slug']}/{offer['slug']}".lower(), offer.get("title"),
                                 f"https://{board['host']}/o/{offer['slug']}", offer.get("careers_url"),
                                 location=offer.get("location"), remote=remote,
                                 posted=offer.get("published_at") or offer.get("created_at"), text=text))
    return postings


def _xml(response):
    """The feed's XML. One with a document type is refused: a feed has none, and a parser expands what one declares."""
    if b"<!DOCTYPE" in response.content or b"<!ENTITY" in response.content:
        raise _Unreadable("XML with a document type")
    try:
        return ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise _Unreadable(str(exc)) from exc


def _teamtailor(board):
    spaces = {"tt": "https://teamtailor.com/locations"}
    postings = []
    for item in _xml(_get(f"https://{board['host']}/jobs.rss")).iter("item"):
        link = (item.findtext("link") or "").strip()
        last = link.rstrip("/").rpartition("/")[2]
        digits = re.match(r"\d+", last)
        places = [place.findtext("tt:name", "", spaces) or place.findtext("tt:city", "", spaces)
                  for place in item.iterfind("tt:locations/tt:location", spaces)]
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            posted = ""
        postings.append(_posting(f"{board['slug']}/{digits.group() if digits else last}", item.findtext("title"),
                                 f"https://{board['host']}/jobs/{last}", link,
                                 location=", ".join(place for place in places if place),
                                 remote=_workplace(item.findtext("remoteStatus")), posted=posted,
                                 text=item.findtext("description")))
    return postings


def _personio(board):
    postings = []
    for position in _xml(_get(f"https://{board['host']}/xml")).iter("position"):
        number = (position.findtext("id") or "").strip()
        text = "\n".join(f"{part.findtext('name') or ''}\n{part.findtext('value') or ''}"
                         for part in position.iterfind("jobDescriptions/jobDescription"))
        postings.append(_posting(f"{board['slug']}/{number}", position.findtext("name"),
                                 f"https://{board['host']}/job/{number}", location=position.findtext("office"),
                                 posted=position.findtext("createdAt"), text=text))
    return postings


def _pinpoint(board):
    postings = []
    for job in _get(f"https://{board['host']}/postings.json").json()["data"]:
        hosted = f"https://{board['host']}{job.get('path') or ''}"
        last = (job.get("path") or job.get("url") or "").rstrip("/").rpartition("/")[2].lower()
        text = "\n".join(str(job[key]) for key in ("description", "key_responsibilities", "skills_knowledge_expertise")
                         if job.get(key))
        postings.append(_posting(f"{board['slug']}/{last}", job.get("title"), hosted, job.get("url"),
                                 location=(job.get("location") or {}).get("name"),
                                 remote=_workplace(job.get("workplace_type")), text=text))
    return postings


FEEDS = {"ashby": _ashby, "greenhouse": _greenhouse, "lever": _lever, "workable": _workable,
         "smartrecruiters": _smartrecruiters, "recruitee": _recruitee, "teamtailor": _teamtailor,
         "personio": _personio, "pinpoint": _pinpoint}


def fetch(board):
    """Every posting the board lists as open now: (state, postings).

    state: "ok", "missing" (the board does not exist) or "unknown" (the request failed, or the answer is not the
    feed's format; on SmartRecruiters also a board without a posting); postings is empty for the last two.
    postings: [{"id", "title", "url", "hosted", "location", "remote", "posted", "text"}]. `id` is the second part
    of `posting_id` for a link to the posting on its board. `url` is the link the feed names for the posting, which
    can be a page of the company's own careers site; `hosted` is the posting on the board's own host (the same
    link on most boards). `title` is on one line, "" when the board gives none.
    `remote` is "remote", "hybrid", "on-site" or "" when the board does not say; `posted` is a date (YYYY-MM-DD) or
    ""; `text` is the posting as plain text, "" on SmartRecruiters, whose list has none.
    """
    try:
        return "ok", [posting for posting in FEEDS[board["ats"]](board) if posting]
    except _Missing:
        return "missing", []
    except (_Unreadable, ValueError, KeyError, TypeError, AttributeError, IndexError):  # ValueError: not JSON
        return "unknown", []


def _asked(label_text, required):
    text = " ".join(str(label_text or "").split())[:QUESTION_CHARS]
    if not text or NOT_A_QUESTION.search(text):
        return None
    return {"question": text, "required": bool(required)}


def _greenhouse_questions(board, number):
    job = _get(f"https://boards-api.greenhouse.io/v1/boards/{board['slug']}/jobs/{number}", questions="true").json()
    return [
        _asked(question.get("label"), question.get("required"))
        for question in job.get("questions") or []
        # Its own fields are first_name, resume, cover_letter, ...; the questions a company adds are question_<id>.
        if any(str(f.get("name", "")).startswith("question_") and f.get("type") in ("input_text", "textarea")
               for f in question.get("fields") or [])
    ]


def _ashby_questions(board, posting):
    """From the API Ashby's own hosted posting page reads its form from. It is not a documented one: when it
    changes, the answer is no questions."""
    try:
        response = requests.post(ASHBY_FORM_API, headers=HEADERS, timeout=TIMEOUT, json={
            "operationName": "ApiJobPosting", "query": ASHBY_FORM_QUERY,
            "variables": {"organizationHostedJobsPageName": board["slug"], "jobPostingId": posting},
        })
    except requests.RequestException as exc:
        raise _Unreadable(str(exc)) from exc
    if response.status_code != 200:
        raise _Unreadable(f"HTTP {response.status_code}")
    sections = response.json()["data"]["jobPosting"]["applicationForm"]["sections"]
    return [
        _asked(entry["field"].get("title"), entry.get("isRequired"))
        for section in sections for entry in section.get("fieldEntries") or []
        if isinstance(entry.get("field"), dict) and entry["field"].get("type") in ("String", "LongText")
        and not str(entry["field"].get("path", "")).startswith("_systemfield")
    ]


def questions(url, board=None):
    """The questions a posting's application form asks for a written answer: [{"question", "required"}], in the
    form's order, MAX_QUESTIONS at most, without the fields for a name, a CV or a link.

    Greenhouse and Ashby show a form's questions; [] for any other board, and when the request fails. `board` is the
    company's board when `url` is the posting on the company's own site (acme.com/careers?gh_jid=<id>)."""
    found = posting_id(url)
    board = board_of(url) or board
    if not board:
        return []
    _, _, query = _parse(url)
    try:
        if board["ats"] == "greenhouse":
            number = found[1] if found and found[0] == "greenhouse" else (query.get("gh_jid") or [""])[0]
            asked = _greenhouse_questions(board, number) if number.isdigit() else []
        elif board["ats"] == "ashby":
            posting = found[1] if found and found[0] == "ashby" else (query.get("ashby_jid") or [""])[0].lower()
            asked = _ashby_questions(board, posting) if posting else []
        else:
            return []
    except (_Missing, _Unreadable, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return []
    unique = {question["question"]: question for question in asked if question}
    return list(unique.values())[:MAX_QUESTIONS]
