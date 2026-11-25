"""Job boards' public feeds in their wire formats, as the live APIs answered, for the `http` fixture (`responses`)."""

ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{}"
ASHBY_FORM = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
LEVER = "https://api.lever.co/v0/postings/{}"
LEVER_EU = "https://api.eu.lever.co/v0/postings/{}"
WORKABLE = "https://apply.workable.com/api/v1/widget/accounts/{}"
SMARTRECRUITERS = "https://api.smartrecruiters.com/v1/companies/{}/postings"
RECRUITEE = "https://{}.recruitee.com/api/offers/"
TEAMTAILOR = "https://{}.teamtailor.com/jobs.rss"
PERSONIO = "https://{}.jobs.personio.de/xml"
PINPOINT = "https://{}.pinpointhq.com/postings.json"


def ashby_job(job_id, title, *, board="acme", location="Remote - Europe", workplace="Remote", text="", listed=True):
    return {
        "id": job_id,
        "title": title,
        "department": "Data",
        "team": "Analytics",
        "employmentType": "FullTime",
        "location": location,
        "isListed": listed,
        "isRemote": workplace in ("Remote", "Hybrid"),
        "workplaceType": workplace,
        "descriptionHtml": f"<p>{text}</p>",
        "descriptionPlain": text,
        "publishedAt": "2026-11-20T09:15:00.000+00:00",
        "jobUrl": f"https://jobs.ashbyhq.com/{board}/{job_id}",
        "applyUrl": f"https://jobs.ashbyhq.com/{board}/{job_id}/application",
    }


def ashby(http, board, *jobs):
    """Registers the board's feed: every job it lists as open."""
    http.get(ASHBY.format(board), json={"apiVersion": "1", "jobs": list(jobs)})


def greenhouse_job(job_id, title, *, board="acme", location="Berlin, Germany", content="", url=None):
    return {
        "absolute_url": url or f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}",
        "internal_job_id": job_id + 1,
        "location": {"name": location},
        "metadata": [],
        "id": job_id,
        "updated_at": "2026-11-22T10:00:00-05:00",
        "requisition_id": "R-1",
        "title": title,
        "company_name": "Acme",
        "first_published": "2026-11-18T08:30:00-05:00",
        "content": content,  # the posting's HTML, escaped
        "departments": [],
        "offices": [],
    }


def greenhouse(http, board, *jobs):
    http.get(GREENHOUSE.format(board), json={"jobs": list(jobs), "meta": {"total": len(jobs)}})


def lever_job(job_id, title, *, board="acme", location="Belgrade", workplace="remote", text="", host="jobs.lever.co"):
    return {
        "id": job_id,
        "text": title,
        "categories": {"commitment": "Full-time", "department": "Data", "location": location, "team": "Analytics",
                       "allLocations": [location]},
        "createdAt": 1795000000000,  # milliseconds: 2026-11-18
        "descriptionPlain": text,
        "description": f"<div>{text}</div>",
        "lists": [{"text": "What you will do", "content": "<li>Own the dbt models</li><li>Mentor analysts</li>"}],
        "additionalPlain": "We hire across Europe.",
        "workplaceType": workplace,
        "hostedUrl": f"https://{host}/{board}/{job_id}",
        "applyUrl": f"https://{host}/{board}/{job_id}/apply",
    }


def workable_job(shortcode, title, *, city="Athens", country="Greece", remote=True, description=""):
    return {
        "title": title,
        "shortcode": shortcode,
        "code": "",
        "employment_type": "Full-time",
        "telecommuting": remote,
        "department": "Data",
        "url": f"https://apply.workable.com/j/{shortcode}",
        "shortlink": f"https://apply.workable.com/j/{shortcode}",
        "application_url": f"https://apply.workable.com/j/{shortcode}/apply",
        "published_on": "2026-11-17",
        "created_at": "2026-11-15",
        "country": country,
        "city": city,
        "state": "",
        "description": description,
    }


def workable(http, board, *jobs):
    http.get(WORKABLE.format(board), json={"name": "Acme", "description": None, "jobs": list(jobs)})


def smartrecruiters_job(job_id, title, *, company="Acme", location="Poland, Remote, Poland", remote=True):
    return {
        "id": job_id,
        "name": title,
        "uuid": "a6f4c077-a7ac-4b8f-99d4-3756d1544f35",
        "refNumber": "REF1",
        "company": {"identifier": company, "name": "Acme Inc"},
        "releasedDate": "2026-10-28T15:14:43.941Z",
        "location": {"city": "Poland", "region": "Remote", "country": "pl", "remote": remote, "hybrid": False,
                     "fullLocation": location},
        "ref": f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}",
    }


def smartrecruiters(http, company, *jobs):
    http.get(SMARTRECRUITERS.format(company), json={"offset": 0, "limit": 100, "totalFound": len(jobs), "content": list(jobs)})


def recruitee_offer(slug, title, *, board="acme", location="Amsterdam, Netherlands", remote=False, hybrid=True,
                    description="", careers_url=None):
    return {
        "id": 1001,
        "slug": slug,
        "title": title,
        "company_name": "Acme",
        "careers_url": careers_url or f"https://{board}.recruitee.com/o/{slug}",
        "careers_apply_url": f"https://{board}.recruitee.com/o/{slug}/c/new",
        "location": location,
        "city": "Amsterdam",
        "country_code": "NL",
        "remote": remote,
        "hybrid": hybrid,
        "on_site": not (remote or hybrid),
        "published_at": "2026-11-19 07:00:00 UTC",
        "created_at": "2026-11-10 12:00:00 UTC",
        "description": description,
        "requirements": "<ul><li>SQL</li></ul>",
        "salary": {"min": None, "max": None, "period": None, "currency": None},
    }


def recruitee(http, board, *offers):
    http.get(RECRUITEE.format(board), json={"offers": list(offers)})


def teamtailor_feed(*items):
    """items: (link, title, remote status, city, description HTML)."""
    entries = "".join(
        f"<item><title>{title}</title><description><![CDATA[{description}]]></description>"
        f"<pubDate>Wed, 18 Nov 2026 09:30:00 +0100</pubDate><link>{link}</link><remoteStatus>{remote}</remoteStatus>"
        f"<guid>{link}</guid><tt:locations><tt:location><tt:name>{city} office</tt:name><tt:city>{city}</tt:city>"
        f"<tt:country>Sweden</tt:country></tt:location></tt:locations><tt:department>Data</tt:department></item>"
        for link, title, remote, city, description in items
    )
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:tt="https://teamtailor.com/locations">'
            f"<channel><title>Acme</title>{entries}</channel></rss>")


def personio_feed(*positions):
    """positions: (id, name, office, description HTML)."""
    entries = "".join(
        f"<position><id>{number}</id><subcompany>Acme GmbH</subcompany><office>{office}</office>"
        f"<department>Data</department><recruitingCategory>Engineering</recruitingCategory><name>{name}</name>"
        f"<jobDescriptions><jobDescription><name>Your tasks</name><value><![CDATA[{description}]]></value>"
        f"</jobDescription></jobDescriptions><employmentType>permanent</employmentType><seniority>experienced</seniority>"
        f"<schedule>full-time</schedule><createdAt>2026-11-12T08:00:00+00:00</createdAt></position>"
        for number, name, office, description in positions
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><workzag-jobs>{entries}</workzag-jobs>'


def pinpoint_posting(posting_id, title, *, board="acme", workplace="remote", description="", url=None):
    return {
        "id": "7001",
        "title": title,
        "url": url or f"https://{board}.pinpointhq.com/postings/{posting_id}",
        "path": f"/postings/{posting_id}",
        "description": description,
        "key_responsibilities": "<ul><li>Model data in dbt</li></ul>",
        "skills_knowledge_expertise": "<p>SQL</p>",
        "workplace_type": workplace,
        "workplace_type_text": workplace.title(),
        "location": {"id": "1", "city": "London", "name": "London", "province": "England"},
        "job": {"id": "55", "department": {"name": "Data"}},
    }


def greenhouse_form(*questions):
    """A posting with its application form, as ?questions=true answers. questions: (label, field name, type, required)."""
    return {
        "id": 333,
        "title": "Senior Analytics Engineer",
        "questions": [
            {"description": None, "label": label, "required": required, "fields": [{"name": name, "type": kind, "values": []}]}
            for label, name, kind, required in questions
        ],
    }


def ashby_form(*fields):
    """Ashby's hosted posting page API answer. fields: (title, path, type, required)."""
    entries = [{"isRequired": required, "field": {"title": title, "path": path, "type": kind, "isNullable": not required}}
               for title, path, kind, required in fields]
    return {"data": {"jobPosting": {"applicationForm": {"sections": [{"fieldEntries": entries}]}}}}
