import pytest
import requests

from pipeline import research
from exa_mock import AGENT_URL, CONTENTS_URL, agent_run, contents_response, error, grounded, result, structured_response

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


# open roles from Fiber's jobs database: hunt.py run --fiber -----------------------------------------


def test_fiber_roles_come_first_and_the_main_searchs_other_roles_follow(exa, http, profile):
    from_main = role("Senior Analytics Engineer", "https://jobs.ashbyhq.com/acme/1111")
    # the same posting as each source names it: the main search links the application form, on a capitalised board
    in_both_main = role("Growth Engineer", "https://jobs.ashbyhq.com/Acme/2222/application")
    in_both_fiber = role("Growth Engineer", "https://jobs.ashbyhq.com/acme/2222/")
    only_fiber = role("Data Engineer, Product", "https://jobs.ashbyhq.com/acme/3333/application", location="New York")
    own_site = role("Analytics Engineer", "https://careers.acme.com/jobs/77")
    closed = role("Lead Data Analyst", "https://jobs.ashbyhq.com/acme/4444")
    # an aggregator's page answers 200 for a job the company never posted: never checked, never kept
    aggregator = role("Staff Data Engineer", "https://agraph.ai/jobs/j_acme_2")
    posting = "https://jobs.ashbyhq.com/acme/3333"  # the posting behind the application form
    exa.on_search(structured_response(company([from_main, in_both_main]), [grounded("open_roles[0].url", from_main["url"])]))
    exa.on_agent(agent_run(
        {"open_roles": [only_fiber, in_both_fiber, aggregator, own_site, closed, role("No link", " "), only_fiber]},
        [grounded("open_roles[0]", posting)],
        partners={"fiber": 0.18},
    ))  # fmt: skip
    exa.on_contents(contents_response())
    ashby_board(http, "acme", "1111", "2222", "3333")
    http.get(own_site["url"], status=200)

    out = research.research_company("Acme", "acme.com", profile, fiber=True)

    assert [(r["title"], r["url"], r["link_status"]) for r in out["content"]["open_roles"]] == [
        ("Data Engineer, Product", posting, "live"),
        ("Growth Engineer", in_both_main["url"], "live"),  # listed by both: once, under the link earlier runs saved
        ("Analytics Engineer", own_site["url"], "live"),
        ("Senior Analytics Engineer", from_main["url"], "live"),
    ]
    assert out["role_source"] == "fiber+main"
    assert out["dead_roles"] == [{"title": closed["title"], "url": closed["url"]}]
    assert out["grounding"]["open_roles"] == {"confidence": "high", "urls": [posting, from_main["url"]]}
    (sent,) = exa.requests(AGENT_URL)
    assert sent["dataSources"] == [{"provider": "fiber"}] and sent["outputSchema"] == research.FIBER_SCHEMA
    # every seniority is asked for: the screen and the scorer judge it, not the database search
    assert "analytics engineer, growth engineer. Include every matching posting whatever its seniority" in sent["query"]
    assert len(exa.requests()) == 1  # live roles: no ATS fallback


def test_fiber_alone_supplies_the_roles_when_the_main_search_has_none(exa, http, profile):
    listed = [role(f"Data Engineer {n}", f"https://jobs.ashbyhq.com/acme/{n}") for n in range(10)]
    closed = role("Lead Data Analyst", "https://jobs.ashbyhq.com/acme/closed")
    exa.on_search(structured_response(company([closed]), [grounded("open_roles[0].url", closed["url"])]))
    exa.on_agent(agent_run({"open_roles": listed}))
    exa.on_contents(contents_response())
    ashby_board(http, "acme", *(str(n) for n in range(10)))

    out = research.research_company("Acme", "acme.com", profile, fiber=True)

    assert out["role_source"] == "fiber"
    assert [r["title"] for r in out["content"]["open_roles"]] == [r["title"] for r in listed[: research.MAX_ROLES]]
    assert "open_roles" not in out["grounding"]  # the main search's sources say nothing about these roles
    assert len(exa.requests()) == 1


def test_fiber_postings_are_kept_only_on_the_companys_own_site_or_a_job_board(exa, profile):
    kept = [
        "https://acme.com/careers/1",
        "https://www.acme.com/careers/2",
        "https://Careers.ACME.com:443/jobs/3",
        "https://boards.greenhouse.io/acme/jobs/4",
        "https://acme.recruitee.com/o/5",
    ]
    left_out = [
        "https://agraph.ai/jobs/j_acme_1",  # an aggregator's copy
        "https://notacme.com/jobs/2",
        "https://acme.com.evil.io/jobs/3",
        "https://evil.io\\.acme.com/jobs/4",
        "https://recruitee.com.evil.io/o/5",
        "https://notrecruitee.com/o/5",
        "https://evil.io\\@acme.com/jobs/6",  # a browser opens evil.io
        "https://user@acme.com/jobs/7",
        "javascript://acme.com/%0Aalert(1)",
        "ftp://acme.com/jobs/9",
        "//acme.com/jobs/10",
        "acme.com/careers/11",  # no scheme: nothing to check or to open
        "http://[not-a-url/12",
        # an Ashby link is rewritten to its posting: never one that was not a plain web link to begin with
        "//jobs.ashbyhq.com/acme/13",
        "javascript://jobs.ashbyhq.com/acme/14",
        "https://user:pass@jobs.ashbyhq.com/acme/15",
    ]
    exa.on_agent(agent_run({"open_roles": [role(f"Role {n}", url) for n, url in enumerate(kept + left_out)]}))

    roles, _ = research.find_roles_on_fiber("Acme", "acme.com", profile)

    assert [r["url"] for r in roles] == kept


def test_fiber_lists_each_posting_once_whatever_form_its_link_takes(exa, profile):
    one_ashby_posting = [
        "https://jobs.ashbyhq.com/acme/1111",
        "https://jobs.ashbyhq.com/Acme/1111/application?utm_source=fiber",
        "https://JOBS.ASHBYHQ.COM:443/acme/1111/application/",
    ]
    one_site_posting = ["https://acme.com/jobs/7", "https://www.acme.com/jobs/7/", "https://acme.com/jobs/7?utm_campaign=x"]
    # a careers page that embeds the job board links a posting by the board's id for it
    board_id = "f272fc4a-91a7-4dcf-9cb5-3600ebaf14e1"
    one_embedded_posting = [
        f"https://acme.com/careers/{board_id}",
        f"https://jobs.ashbyhq.com/acme/{board_id.upper()}",
        f"https://acme.com/open-roles?ashby_jid={board_id}&utm_source=x",
    ]
    # the query string or the fragment can be what names the posting, also after an id of something else: six
    distinct = [
        "https://acme.com/careers/job?gh_jid=111",
        "https://acme.com/careers/job?gh_jid=222",
        "https://acme.com/careers/open#job-1",
        "https://acme.com/careers/open#job-2",
        "https://acme.com/boards/0a1b2c3d-0000-4000-8000-000000000000?job=1",
        "https://acme.com/boards/0a1b2c3d-0000-4000-8000-000000000000?job=2",
    ]
    listed = [*one_ashby_posting, "https://JOBS.ASHBYHQ.COM/acme/2222", *one_site_posting, *one_embedded_posting, *distinct]
    exa.on_agent(agent_run({"open_roles": [role(f"Role {n}", url) for n, url in enumerate(listed)]}))

    roles, _ = research.find_roles_on_fiber("Acme", "acme.com", profile)

    assert [r["url"] for r in roles] == [
        "https://jobs.ashbyhq.com/acme/1111",
        "https://jobs.ashbyhq.com/acme/2222",  # the form the Ashby link check reads
        "https://acme.com/jobs/7",
        one_embedded_posting[0],
        *distinct,
    ]


def test_a_posting_both_sources_list_keeps_the_main_searchs_link_unless_only_fibers_is_live(exa, http, profile):
    # the dashboard keeps a status and a note by link: turning Fiber on must not move a posting to another one
    www, bare = "https://www.acme.com/jobs/{}", "https://acme.com/jobs/{}"
    main_source, fiber_source = "https://acme.com/careers", "https://www.linkedin.com/company/acme/jobs"
    from_main = [role(title, www.format(n)) for n, title in enumerate("ABCDE", start=1)]
    from_main.insert(4, role("D", bare.format(4) + "/"))  # the main search lists D twice
    exa.on_search(structured_response(company(from_main), [grounded("open_roles[0].url", main_source, confidence="low")]))
    exa.on_agent(agent_run(
        {"open_roles": [role(title, bare.format(n)) for n, title in enumerate("ABCDE", start=1)]},
        [grounded("open_roles[0].url", fiber_source)],
    ))  # fmt: skip
    exa.on_contents(contents_response())
    checks = {
        www.format(1): 200, bare.format(1): 403,  # A: only the main search's link can be checked
        www.format(2): 403, bare.format(2): 200,  # B: only Fiber's
        www.format(3): 403, bare.format(3): 403,  # C: neither
        www.format(4): 403, bare.format(4) + "/": 200, bare.format(4): 403,  # D: the main search's second link
        www.format(5): 404, bare.format(5): 200,  # E: the main search's link is dead, the posting is not
    }  # fmt: skip
    for url, status in checks.items():
        http.get(url, status=status)

    out = research.research_company("Acme", "acme.com", profile, fiber=True)

    assert [(r["title"], r["url"], r["link_status"]) for r in out["content"]["open_roles"]] == [
        ("A", www.format(1), "live"),
        ("B", bare.format(2), "live"),
        ("D", bare.format(4) + "/", "live"),
        ("E", bare.format(5), "live"),
        ("C", www.format(3), "unverified"),
    ]
    assert out["role_source"] == "fiber"  # Fiber lists every one of them
    assert out["dead_roles"] == []  # E is open under its other link
    # some kept links are the main search's own: its sources still apply, at the lower confidence of the two
    assert out["grounding"]["open_roles"] == {"confidence": "low", "urls": [fiber_source, main_source]}


def test_postings_named_by_their_query_string_are_not_taken_for_one(exa, http, profile):
    # some career sites put the posting's id in the query: merging those would drop real roles without a trace
    page = "https://acme.com/careers/job?gh_jid={}"
    tracked = page.format(111) + "&utm_source=exa"  # the first posting again, with a tracking parameter
    exa.on_search(structured_response(company([role("Analyst", page.format(333)), role("Data Engineer", tracked)])))
    exa.on_agent(agent_run({"open_roles": [role("Data Engineer", page.format(111)), role("Growth Engineer", page.format(222))]}))
    exa.on_contents(contents_response())
    for url in (page.format(111), page.format(222), page.format(333), tracked):
        http.get(url, status=200)

    out = research.research_company("Acme", "acme.com", profile, fiber=True)

    # the first posting is listed by both: once, under the main search's link
    assert [r["url"] for r in out["content"]["open_roles"]] == [tracked, page.format(222), page.format(333)]
    assert out["role_source"] == "fiber+main"
    assert out["dead_roles"] == []


def test_an_unchecked_fiber_link_never_comes_before_or_pushes_out_a_live_role(exa, http, profile):
    live = [role(f"Analytics Engineer {n}", f"https://jobs.ashbyhq.com/acme/{n}") for n in range(research.MAX_ROLES)]
    unchecked = role("Data Engineer", "https://acme.com/careers")  # a careers page: no posting to confirm
    exa.on_search(lambda body: structured_response(company(live[:1] if "Acme" in body["query"] else live)))
    exa.on_agent(agent_run({"open_roles": [unchecked]}))
    exa.on_contents(contents_response())
    ashby_board(http, "acme", *(str(n) for n in range(research.MAX_ROLES)))

    one_live = research.research_company("Acme", "acme.com", profile, fiber=True)
    all_live = research.research_company("Busy", "acme.com", profile, fiber=True)

    assert [(r["url"], r["link_status"]) for r in one_live["content"]["open_roles"]] == [
        (live[0]["url"], "live"),
        (unchecked["url"], "unverified"),
    ]
    assert one_live["role_source"] == "fiber+main"
    # eight live roles from the main search: Fiber's unchecked link does not make the cut
    assert [r["url"] for r in all_live["content"]["open_roles"]] == [r["url"] for r in live]
    assert all_live["role_source"] == "main"


@pytest.mark.parametrize(
    ("answer", "warning"),
    [
        (agent_run({"open_roles": []}), None),
        (agent_run({"open_roles": None}), None),
        (agent_run(status="failed", error="Run timed out"), "Fiber job search failed: RuntimeError: Exa agent run agent_run_1 failed: Run timed out"),
        (error(403, "FEATURE_DISABLED"), "Fiber job search failed: ValueError: Request failed with status code 403"),
    ],
    ids=["none-listed", "null-list", "run-failed", "not-enabled"],
)
def test_without_fiber_roles_the_main_searchs_roles_stand(exa, http, profile, answer, warning):
    from_main = role("Senior Analytics Engineer", "https://jobs.ashbyhq.com/acme/1111")
    exa.on_search(structured_response(company([from_main])))
    exa.on_agent(answer)
    exa.on_contents(contents_response())
    ashby_board(http, "acme", "1111")

    out = research.research_company("Acme", "acme.com", profile, fiber=True)

    assert out["role_source"] == "main"
    assert [r["url"] for r in out["content"]["open_roles"]] == [from_main["url"]]
    assert [w[: len(warning)] for w in out["warnings"]] == ([warning] if warning else [])


def test_the_ats_fallback_still_runs_when_neither_fiber_nor_the_main_search_has_a_live_role(exa, http, profile):
    found = role("Senior Analytics Engineer", "https://jobs.ashbyhq.com/acme/1111")
    fallback = structured_response({"open_roles": [{**found, "company_verified": True}]})
    exa.on_search(lambda body: fallback if "includeDomains" in body else structured_response(company([])))
    exa.on_agent(agent_run({"open_roles": [role("Closed", "https://jobs.ashbyhq.com/acme/9999")]}))
    exa.on_contents(contents_response())
    ashby_board(http, "acme", "1111")

    out = research.research_company("Acme", "acme.com", profile, fiber=True)

    assert out["role_source"] == "ats_fallback"
    assert [r["url"] for r in out["content"]["open_roles"]] == [found["url"]]
    assert out["dead_roles"] == [{"title": "Closed", "url": "https://jobs.ashbyhq.com/acme/9999"}]


def test_fiber_is_not_asked_about_a_company_whose_identity_is_not_verified(exa, profile):
    exa.on_search(structured_response(company([], verified=False)))
    exa.on_agent(agent_run({"open_roles": []}))

    research.research_company("Acme", "acme.com", profile, fiber=True)

    assert exa.requests(AGENT_URL) == []  # an agent run costs 10 to 30 times the main search


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
