import time

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
    assert fallback_request["includeDomains"] == ["acme.com", "*.acme.com", *research.ATS_DOMAINS]
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


def test_no_ats_fallback_for_unverified_identity(exa, http, profile):
    listed_elsewhere = role("Senior Analytics Engineer", "https://remoteboard.io/jobs/acme-77")
    exa.on_search(structured_response(company([listed_elsewhere], verified=False)))
    http.get(listed_elsewhere["url"], status=200)

    out = research.research_company("Acme", "acme.com", profile)

    assert out["role_source"] == "none"
    assert out["content"]["open_roles"] == []
    assert out["third_party_roles"] == [listed_elsewhere]
    assert len(exa.requests()) == 1


# postings on third-party sites: a gateway to the real posting, never an open role -------------------


def test_a_posting_on_a_third_party_site_is_looked_for_where_the_company_posts_its_jobs(exa, http, profile):
    on_ashby = role("Metadata Analyst", "https://jobs.ashbyhq.com/acme/1111")
    gateway = role("Lead Growth Engineer", "https://rockethunt.ai/en/vacancies/1dc282ff")  # an account to see where to apply
    part_of_a_word = role("Data Analyst", "https://hirify.me/jobs/2")  # not the Metadata Analyst posting
    other_level = role("Senior Metadata Analyst", "https://hirify.me/jobs/4")  # nor is this one
    behind_it = role("Lead growth engineer (Remote, Europe)", "https://job-boards.eu.greenhouse.io/acme/jobs/222")
    listed = [on_ashby, gateway, part_of_a_word, other_level]
    main = structured_response(company(listed), [grounded("open_roles[0].url", on_ashby["url"])])
    fallback = structured_response(
        {
            "open_roles": [
                {**behind_it, "company_verified": True},
                {**on_ashby, "url": on_ashby["url"] + "/application", "company_verified": True},  # listed already
                {**role("Data Engineer", "https://hirify.me/jobs/3"), "company_verified": True},  # never fetched
            ]
        },
        [grounded("open_roles[0].url", behind_it["url"], confidence="medium")],
    )
    exa.on_search(lambda body: fallback if "includeDomains" in body else main)
    exa.on_contents(contents_response())
    ashby_board(http, "acme", "1111")
    for page in (gateway, part_of_a_word, other_level, behind_it):
        http.get(page["url"], status=200)

    out = research.research_company("Acme", "acme.com", profile)

    assert [(r["url"], r["link_status"]) for r in out["content"]["open_roles"]] == [
        (on_ashby["url"], "live"),
        (behind_it["url"], "live"),
    ]
    # found: a posting's title holds the listing's, word for word
    assert out["third_party_roles"] == [part_of_a_word, other_level]
    assert out["role_source"] == "main+ats_fallback"
    assert out["dead_roles"] == []
    assert out["grounding"]["open_roles"] == {"confidence": "medium", "urls": [on_ashby["url"], behind_it["url"]]}
    assert "look for each of them: Lead Growth Engineer; Data Analyst; Senior Metadata Analyst." in exa.requests()[1]["query"]


def test_a_third_party_posting_not_found_at_the_company_is_a_lead_and_never_an_open_role(exa, http, profile):
    gateway = role("Head of Data Science", "https://hirify.me/jobs/1347972-head-data-science")
    expired = role("Senior Data Analytics Engineer", "https://www.eurojobs.com/jobs/30987655/senior-data-analytics-engineer")
    gone = role("Data Engineer", "https://remoteboard.io/jobs/77")
    main = structured_response(company([gateway, expired, gone, gateway]))
    exa.on_search(lambda body: structured_response({"open_roles": []}) if "includeDomains" in body else main)
    http.get(gateway["url"], status=200, body="<h1>Head of Data Science</h1><button>Log in to apply</button>")
    http.get(expired["url"], status=200, body="<title>This job is no longer available | Eurojobs</title>")
    http.get(gone["url"], status=404)

    out = research.research_company("Acme", "acme.com", profile)

    assert out["content"]["open_roles"] == []
    assert out["role_source"] == "none"
    assert out["third_party_roles"] == [gateway]
    assert out["dead_roles"] == [{"title": r["title"], "url": r["url"]} for r in (expired, gone)]
    assert "look for each of them: Head of Data Science." in exa.requests()[1]["query"]
    assert exa.requests(CONTENTS_URL) == []  # no paid fetch settles a lead: it is never offered as a posting


def test_the_fallbacks_live_postings_come_before_an_unverified_role_and_the_cap_holds(exa, http, profile):
    careers_page = role("Growth Analyst", "https://acme.com/careers")  # no posting to confirm: unverified
    gateway = role("Lead Growth Engineer " + "x" * 200, "https://rockethunt.ai/en/vacancies/1dc282ff")
    found = [role(f"Analytics Engineer {n}", f"https://jobs.ashbyhq.com/acme/{n}") for n in range(research.MAX_ROLES)]
    main = structured_response(company([careers_page, gateway]))
    fallback = structured_response({"open_roles": [{**posting, "company_verified": True} for posting in found]})
    exa.on_search(lambda body: fallback if "includeDomains" in body else main)
    exa.on_contents(contents_response())
    ashby_board(http, "acme", *(str(n) for n in range(research.MAX_ROLES)))
    http.get(gateway["url"], status=200)

    out = research.research_company("Acme", "acme.com", profile)

    assert [(r["url"], r["link_status"]) for r in out["content"]["open_roles"]] == [(r["url"], "live") for r in found]
    assert out["role_source"] == "ats_fallback"  # none of the main search's roles is left
    assert out["third_party_roles"] == [gateway]
    query = exa.requests()[1]["query"]  # a listing's title is the web's text: only its start is quoted
    assert query.endswith(f"look for each of them: {gateway['title'][: research.SEEN_TITLE_CHARS]}.")


def test_a_careers_site_under_the_companys_name_on_another_domain_is_its_own(exa, http, profile):
    sibling = role("Lead Analytics Engineer", "https://www.acme-jobs.com/jobs/8252905-lead-analytics-engineer")
    other_suffix = role("Senior Data Engineer", "https://careers.acme.eu/jobs/8223624-senior-data-engineer")
    regional_board = role("Data Engineer", "https://acme.jobs.personio.com/job/2737184")
    name_as_a_subdomain = role("Growth Engineer", "https://acme.freehost.org/remote-jobs/growth-engineer")
    name_with_another_word = role("Analytics Lead", "https://acme-talent-partners.com/jobs/analytics-lead")
    listed = [sibling, other_suffix, regional_board, name_as_a_subdomain, name_with_another_word]
    exa.on_search(lambda body: structured_response({"open_roles": []} if "includeDomains" in body else company(listed)))
    exa.on_contents(contents_response())
    for posting in listed:
        http.get(posting["url"], status=200)

    out = research.research_company("Acme", "acme.com", profile)

    assert [r["url"] for r in out["content"]["open_roles"]] == [r["url"] for r in (sibling, other_suffix, regional_board)]
    assert out["third_party_roles"] == [name_as_a_subdomain, name_with_another_word]


def test_a_link_written_to_read_like_the_companys_is_neither_a_role_nor_a_lead(exa, profile):
    # never fetched either: the http fixture fails the test on a request nothing mocks
    lookalikes = [role("Data Engineer", "https://acme.com@evil.io/jobs/1"), role("Analyst", "https://evil.io\\.acme.com/jobs/2")]
    no_link = role("Growth Engineer", "N/A")
    fallback = structured_response({"open_roles": [{**role("BI Lead", "https://acme.com@hirify.me/jobs/3"), "company_verified": True}]})
    exa.on_search(lambda body: fallback if "includeDomains" in body else structured_response(company([*lookalikes, no_link])))

    out = research.research_company("Acme", "acme.com", profile)

    assert (out["content"]["open_roles"], out["third_party_roles"], out["dead_roles"]) == ([], [], [])
    assert out["role_source"] == "none"


def test_a_posting_open_under_one_link_is_not_dead_for_its_other_link(exa, http, profile):
    live = role("Data Engineer", "https://acme.com/jobs/7")
    same_posting = role("Data Engineer", "https://www.acme.com/jobs/7/")
    closed = role("Analyst", "https://acme.com/jobs/8")
    exa.on_search(structured_response(company([same_posting, live, closed])))
    exa.on_contents(contents_response())
    http.get(live["url"], status=200)
    http.get(same_posting["url"], status=404)
    http.get(closed["url"], status=404)

    out = research.research_company("Acme", "acme.com", profile)

    assert [r["url"] for r in out["content"]["open_roles"]] == [live["url"]]
    assert out["dead_roles"] == [{"title": "Analyst", "url": closed["url"]}]


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
        "https://job-boards.eu.greenhouse.io/acme/jobs/6",
        "https://acme-jobs.com/jobs/7",  # a careers site registered under the company's name
        "https://careers.acme.co.uk/jobs/8",
        "https://boards.eu.greenhouse.io/acme/jobs/9",
        "https://jobs.eu.lever.co/acme/10",
        "https://jobs.workable.com/view/hZsm/remote-data-engineer-11",
        "https://acme.jobs.personio.com/job/12",
        "https://acme.pinpointhq.com/postings/13",
        "https://my_team.recruitee.com/o/14",
    ]
    left_out = [
        "https://agraph.ai/jobs/j_acme_1",  # an aggregator's copy
        "https://notacme.com/jobs/2",
        "https://acmejobs.com/jobs/2",  # the name alone, or with a careers word after a hyphen
        "https://acme-labs.com/jobs/2",
        "https://remote-acme.io/jobs/2",
        "https://acme.someboard.io/jobs/2",  # ... as the name the site is registered under, not a subdomain of another's
        "https://someone.acme.site/jobs/2",  # nor a page anyone can put up on a platform of that name
        "https://acme.co.evil.io/jobs/2",
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
        (
            lambda http: (
                http.get(
                    "https://rockethunt.ai/en/vacancies/1dc2",
                    status=307,
                    headers={"Location": "https://rockethunt.ai/.within.website/?redir=https%3A%2F%2Frockethunt.ai%2Fen%2Fvacancies%2F1dc2"},
                ),
                http.get("https://rockethunt.ai/.within.website/", status=200, body="<title>Checking your browser</title>"),
            ),
            "https://rockethunt.ai/en/vacancies/1dc2",
            "unknown",
        ),
    ],
    ids=[
        "ashby-board-missing", "ashby-api-error", "ashby-board-root", "redirect-keeps-id", "server-error", "network-error",
        "sent-to-a-bot-check",
    ],
)
def test_check_posting(http, setup, url, expected):
    setup(http)

    assert research.check_posting(url) == expected


@pytest.mark.parametrize(
    "page",
    [
        "<h1>Data Engineer</h1><h2>This position is no longer active</h2><p>Either the position was filled, or the ad has expired.</p>",
        "<title>Data Analyst (Growth): this job is closed | Jobboard</title><h1>Data Analyst</h1>",
        '<div class="job"><p><strong>We&#39;re sorry, this position has been <b>filled</b>.&nbsp;</strong><br>See our other vacancies.</p></div>',
        "<p>Ooops... The position you were looking for is no longer available, please look at the job list.</p>",
        "<h2>Acme Group is no longer taking applications for this job</h2>",
        # what is hidden ends with its element, and a class that only clips is not one that hides
        '<div hidden><div>Cookie settings</div><br></div><div class="overflow-hidden"><span>Job listing has expired</span></div>',
        # a hidden form field has no end tag: it hides nothing after it
        '<form><input type="hidden" name="token" value="x"></form><h2>This position is no longer active</h2>',
    ],
    ids=[
        "notice", "in-the-title", "after-sorry-across-markup", "then-a-comma", "no-longer-taking-applications",
        "after-a-hidden-element", "after-a-hidden-form-field",
    ],
)
def test_a_page_that_answers_200_and_says_its_posting_has_closed_is_dead(http, page):
    http.get("https://careers.acme.com/jobs/8-data-engineer", status=200, body=page)

    assert research.check_posting("https://careers.acme.com/jobs/8-data-engineer") == "dead"


NOTICE = "<p>This job is no longer available</p>"
NOT_SHOWN = {
    "hidden-attribute": f'<div class="notice" hidden>{NOTICE}</div>',
    "inline-style": f'<div style="display: none">{NOTICE}</div>',
    "utility-class": f'<div class="alert d-none">{NOTICE}</div>',
    "shown-by-a-script": f'<div x-show="closed" x-cloak>{NOTICE}</div><p v-if="job.closed">This job has expired</p>',
    "aria-hidden": f'<div class="modal" aria-hidden="true">{NOTICE}</div>',
    "dialog": f"<dialog>{NOTICE}</dialog>",
    "template-and-noscript": f"<template>{NOTICE}</template><noscript>{NOTICE}</noscript>",
    "form-control": "<select><option>Job expired</option></select>",
    "tag-with-a-bracket-in-a-value": f'<div class="[&>p]:m-0 hidden">{NOTICE}</div>',
    "inside-a-nested-element": f"<div hidden><div>Other postings</div>{NOTICE}</div>",
    "comment-script-and-style": (
        f"<!-- {NOTICE} --><script>const closed = '<p>This job has expired</p>'</script>"
        '<style>.gone::after { content: "Job expired" }</style>'
    ),
    "script-never-closed": "<script>// <p>This job has expired</p>",
    "said-of-something-else": (
        "<p>We review applications until <span>the position has been filled</span>.</p>"
        "<p>Apply before<br>this role is closed.</p>"
        "<p>Note: the position has been filled in other offices but remains open in Belgrade.</p>"
        "<p>This role is no longer available as a fully remote position, but hybrid in Berlin.</p>"
        "<p>We are no longer accepting applications from agencies.</p><li>Analyst - job expired</li>"
    ),
}


@pytest.mark.parametrize("markup", NOT_SHOWN.values(), ids=NOT_SHOWN.keys())
def test_a_closed_notice_the_page_does_not_show_or_says_of_something_else_leaves_the_posting_live(http, markup):
    page = f"<h1>Data Engineer</h1><p>Apply now</p>{markup}"
    http.get("https://careers.acme.com/jobs/8-data-engineer", status=200, body=page)

    assert research.check_posting("https://careers.acme.com/jobs/8-data-engineer") == "live"


HOSTILE = {
    "sentence-marks": "<p>" + "." * 30_000 + "x</p>",
    "comments-never-closed": "<!--" * 25_000,
    "angle-brackets": "<" * 100_000,
    "tags-never-closed": "<a" * 50_000,
    "quotes-never-closed": '<a href="' * 11_000,
    "scripts-never-closed": "<script " * 12_500,
    "closing-tags-without-opening": "<div>" * 10_000 + "</span>" * 7_000,
    "the-wording-over-and-over": "<p>" + "this job is no longer " * 4_500,
}


@pytest.mark.parametrize("markup", HOSTILE.values(), ids=HOSTILE.keys())
def test_a_posting_page_is_read_in_one_pass_whatever_its_markup(http, markup):
    # the page comes from any site; a parser or a pattern that rescans takes from seconds to minutes for these
    http.get("https://careers.acme.com/jobs/8-data-engineer", status=200, body=markup)

    started = time.perf_counter()
    assert research.check_posting("https://careers.acme.com/jobs/8-data-engineer") == "live"
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    ("domain", "url"),
    [
        ("x.com", "https://x.io/jobs/1"),
        ("acme.github.io", "https://evil.github.io/jobs/1"),
        ("data.io", "https://big-data.com/jobs/1"),
    ],
    ids=["a-name-too-short-to-tell-apart", "a-company-site-under-anothers-name", "the-name-as-one-word-of-anothers"],
)
def test_a_site_is_not_the_companys_own_for_sharing_a_short_or_borrowed_name(exa, profile, domain, url):
    exa.on_agent(agent_run({"open_roles": [role("Data Engineer", url)]}))

    roles, _ = research.find_roles_on_fiber("Acme", domain, profile)

    assert roles == []


def test_check_posting_keeps_a_live_url_with_non_ascii_or_spaces(http):
    http.get("https://acme.com/jobs/ing%C3%A9nieur-donn%C3%A9es%20senior", status=200)

    assert research.check_posting("https://acme.com/jobs/ingénieur-données senior") == "live"


def test_check_posting_still_sees_a_redirect_away_from_an_encoded_url_as_dead(http):
    http.get("https://acme.com/jobs/ing%C3%A9nieur", status=302, headers={"Location": "https://acme.com/careers"})
    http.get("https://acme.com/careers", status=200)

    assert research.check_posting("https://acme.com/jobs/ingénieur") == "dead"
