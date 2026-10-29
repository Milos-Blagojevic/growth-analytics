import pytest
import requests

import research
from exa_mock import CONTENTS_URL, contents_response, error, grounded, result, search_response, structured_response

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{}"


def role(title, url, location="Remote", remote="Remote (Europe)"):
    return {"title": title, "url": url, "location": location, "remote_or_relocation": remote}


def company(roles, *, verified=True):
    return {
        "company": "Acme",
        "domain": "acme.com",
        "identity_verified": verified,
        "business_summary": "Billing software for SMBs.",
        "signals": "Series B in 2026.",
        "open_roles": roles,
    }


def ashby_board(http, board, *job_ids):
    http.get(ASHBY_API.format(board), json={"jobs": [{"id": j, "title": "t"} for j in job_ids]})


def test_research_drops_dead_links_and_describes_live_roles(exa, http, profile):
    live = role("Senior Analytics Engineer", "https://jobs.ashbyhq.com/acme/1111")
    closed_on_ashby = role("Lead Growth Engineer", "https://jobs.ashbyhq.com/acme/2222")
    not_found = role("Senior Data Engineer", "https://job-boards.greenhouse.io/acme/jobs/333")
    soft_404 = role("Analytics Lead", "https://jobs.lever.co/acme/444")
    careers_page = role("Growth Analyst", "https://acme.com/careers")
    blocked = role("Senior BI Engineer", "https://apply.workable.com/acme/j/555")
    exa.on_search(
        structured_response(
            company([live, closed_on_ashby, not_found, soft_404, careers_page, blocked]),
            [
                grounded("business_summary", "https://acme.com/about"),
                grounded("open_roles[0].url", "https://jobs.ashbyhq.com/acme/1111", confidence="high"),
                grounded("open_roles[4].title", "https://acme.com/careers", confidence="low"),
            ],
        )
    )
    ashby_board(http, "acme", "1111")
    http.get(not_found["url"], status=404)
    http.get(soft_404["url"], status=302, headers={"Location": "https://jobs.lever.co/acme"})
    http.get("https://jobs.lever.co/acme", status=200)
    http.get(blocked["url"], status=403)

    def contents(body):
        if body.get("maxAgeHours") == 0:  # fresh fetch resolving links the HTTP check could not
            assert body["urls"] == [careers_page["url"], blocked["url"]] and body["text"] is False
            return contents_response(statuses=[(careers_page["url"], "success"), (blocked["url"], "error")])
        assert body["urls"] == [live["url"]] and body["text"] == {"maxCharacters": research.DESCRIPTION_CHARS}
        return contents_response([result(live["url"] + "/", text="  Own the dbt models.  ")])

    exa.on_contents(contents)

    out = research.research_company("Acme", "acme.com", profile)

    assert out["role_source"] == "main"
    assert out["content"]["open_roles"] == [
        {**live, "link_status": "live", "description": "Own the dbt models."},
        {**careers_page, "link_status": "unverified"},
    ]
    assert out["dead_roles"] == [
        {"title": r["title"], "url": r["url"]} for r in (closed_on_ashby, not_found, soft_404, blocked)
    ]
    assert out["grounding"] == {
        "business_summary": {"confidence": "high", "urls": ["https://acme.com/about"]},
        "open_roles": {"confidence": "low", "urls": ["https://jobs.ashbyhq.com/acme/1111", "https://acme.com/careers"]},
    }
    assert len(exa.requests()) == 1  # live roles found: no ATS fallback


def test_exa_link_check_failure_keeps_roles_unverified(exa, profile):
    careers_page = role("Growth Analyst", "https://acme.com/careers")
    exa.on_search(structured_response(company([careers_page])))
    exa.on_contents([error(400)])

    out = research.research_company("Acme", "acme.com", profile)

    assert out["content"]["open_roles"] == [{**careers_page, "link_status": "unverified"}]
    assert out["dead_roles"] == []
    assert len(exa.requests(CONTENTS_URL)) == 1  # unverified roles get no description fetch


def test_description_fetch_failure_keeps_roles(exa, http, profile):
    live = role("Senior Analytics Engineer", "https://jobs.ashbyhq.com/acme/1111")
    exa.on_search(structured_response(company([live])))
    exa.on_contents([error(400)])
    ashby_board(http, "acme", "1111")

    out = research.research_company("Acme", "acme.com", profile)

    assert out["content"]["open_roles"] == [{**live, "link_status": "live"}]


def test_ats_fallback_when_main_search_has_no_live_roles(exa, http, profile):
    stale = role("Senior Data Engineer", "https://job-boards.greenhouse.io/acme/jobs/333")
    found = role("Senior Analytics Engineer", "https://jobs.ashbyhq.com/acme/1111")
    other_company = role("Growth Engineer", "https://jobs.ashbyhq.com/acme-labs/9999")
    main = structured_response(company([stale]), [grounded("open_roles[0].url", stale["url"])])
    fallback = structured_response(
        {"open_roles": [{**found, "company_verified": True}, {**other_company, "company_verified": False}]},
        [grounded("open_roles[0].url", found["url"], confidence="medium")],
    )
    exa.on_search(lambda body: fallback if "includeDomains" in body else main)
    exa.on_contents(contents_response([result(found["url"], text="SQL and dbt.")]))
    http.get(stale["url"], status=404)
    ashby_board(http, "acme", "1111")

    out = research.research_company("Acme", "acme.com", profile)

    fallback_request = exa.requests()[1]
    assert fallback_request["includeDomains"] == research.ATS_DOMAINS
    assert fallback_request["outputSchema"] == research.ATS_SCHEMA
    assert out["role_source"] == "ats_fallback"
    assert out["content"]["open_roles"] == [{**found, "link_status": "live", "description": "SQL and dbt."}]
    assert out["dead_roles"] == [{"title": stale["title"], "url": stale["url"]}]
    assert out["grounding"]["open_roles"] == {"confidence": "medium", "urls": [found["url"]]}


def test_ats_fallback_failure_keeps_the_main_research(exa, http, profile):
    stale = role("Senior Data Engineer", "https://job-boards.greenhouse.io/acme/jobs/333")
    main = structured_response(company([stale]))
    exa.on_search(lambda body: error(500) if "includeDomains" in body else main)
    http.get(stale["url"], status=404)

    out = research.research_company("Acme", "acme.com", profile)

    assert out["content"]["business_summary"] == "Billing software for SMBs."
    assert out["content"]["identity_verified"] is True
    assert out["content"]["open_roles"] == []
    assert out["role_source"] == "none"
    assert out["dead_roles"] == [{"title": stale["title"], "url": stale["url"]}]
    assert "open_roles" not in out["grounding"]
    (warning,) = out["warnings"]
    assert warning.startswith("ATS fallback search failed: ValueError: Request failed with status code 500")


def test_no_ats_fallback_for_unverified_identity(exa, profile):
    exa.on_search(structured_response(company([], verified=False)))

    out = research.research_company("Acme", "acme.com", profile)

    assert out["role_source"] == "none"
    assert out["content"]["open_roles"] == []
    assert len(exa.requests()) == 1


@pytest.mark.parametrize(
    "payload",
    [search_response(), structured_response("plain text instead of JSON")],
    ids=["no-output", "text-output"],
)
def test_missing_structured_output_raises(exa, profile, payload):
    exa.on_search(payload)

    with pytest.raises(RuntimeError, match="no structured output"):
        research.research_company("Acme", "acme.com", profile)


@pytest.mark.parametrize(
    ("setup", "url", "expected"),
    [
        (lambda http: http.get(ASHBY_API.format("gone"), status=404), "https://jobs.ashbyhq.com/gone/1", "dead"),
        (lambda http: http.get(ASHBY_API.format("acme"), status=500), "https://jobs.ashbyhq.com/acme/1", "unknown"),
        (lambda http: None, "https://jobs.ashbyhq.com/acme", "unknown"),
        (
            lambda http: (
                http.get("https://jobs.lever.co/acme/444", status=301, headers={"Location": "https://jobs.lever.co/acme/444/apply"}),
                http.get("https://jobs.lever.co/acme/444/apply", status=200),
            ),
            "https://jobs.lever.co/acme/444",
            "live",
        ),
        (lambda http: http.get("https://acme.com/jobs/1", status=503), "https://acme.com/jobs/1", "unknown"),
        (
            lambda http: http.get("https://acme.com/jobs/1", body=requests.ConnectionError("reset")),
            "https://acme.com/jobs/1",
            "unknown",
        ),
    ],
    ids=["ashby-board-missing", "ashby-api-error", "ashby-board-root", "redirect-keeps-id", "server-error", "network-error"],
)
def test_check_posting(http, setup, url, expected):
    setup(http)

    assert research.check_posting(url) == expected


def test_check_posting_keeps_a_live_url_with_non_ascii_or_spaces(http):
    http.get("https://acme.com/jobs/ing%C3%A9nieur-donn%C3%A9es%20senior", status=200)

    assert research.check_posting("https://acme.com/jobs/ingénieur-données senior") == "live"


def test_check_posting_still_sees_a_redirect_away_from_an_encoded_url_as_dead(http):
    http.get("https://acme.com/jobs/ing%C3%A9nieur", status=302, headers={"Location": "https://acme.com/careers"})
    http.get("https://acme.com/careers", status=200)

    assert research.check_posting("https://acme.com/jobs/ingénieur") == "dead"
