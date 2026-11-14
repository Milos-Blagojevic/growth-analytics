import pytest

from pipeline import scoring

LIVE = {"title": "Senior Analytics Engineer", "url": "https://jobs.ashbyhq.com/acme/1", "link_status": "live"}
UNVERIFIED = {"title": "Lead Growth Engineer", "url": "https://acme.com/careers", "link_status": "unverified"}


def research(roles):
    return {"name": "Acme", "content": {"identity_verified": True, "open_roles": roles}, "grounding": {}}


@pytest.mark.parametrize(
    ("roles", "model_verdict", "best_url", "expected"),
    [
        ([], "apply", "", "check_careers"),
        ([], "stretch", "", "check_careers"),
        ([], "skip", "", "skip"),
        ([LIVE, UNVERIFIED], "apply", UNVERIFIED["url"], "consider"),
        ([LIVE, UNVERIFIED], "apply", LIVE["url"], "apply"),
        ([LIVE], "stretch", LIVE["url"], "stretch"),
    ],
    ids=["no-roles-apply", "no-roles-stretch", "no-roles-skip", "apply-unverified", "apply-live", "stretch-live"],
)
def test_verdict_guards(profile, claude, roles, model_verdict, best_url, expected):
    fit, usage = scoring.score_company(
        research(roles), profile, client=claude(verdict=model_verdict, best_role_url=best_url)
    )

    assert fit.verdict == expected
    assert usage == {"input_tokens": 1200, "output_tokens": 300}


def test_unparsable_output_raises(profile, claude):
    with pytest.raises(RuntimeError, match="no parsable output for Acme"):
        scoring.score_company(research([LIVE]), profile, client=claude(None))


LISTING = {"title": "Head of Data Science", "url": "https://hirify.me/jobs/1347972", "location": "Remote",
           "remote_or_relocation": "Remote (Europe)"}


@pytest.mark.parametrize(
    ("roles", "model_verdict", "best_url", "expected"),
    [
        ([LIVE], "apply", LISTING["url"], ("check_careers", "")),  # Claude takes the listing for a posting
        ([LIVE], "stretch", "http://www.hirify.me/jobs/1347972/?utm_source=x", ("check_careers", "")),
        ([LIVE], "apply", "https://www.linkedin.com/company/acme/jobs", ("check_careers", "")),  # a page it was cited
        ([LIVE], "consider", "https://jobs.ashbyhq.com/othercompany/1", ("check_careers", "")),
        ([], "skip", LISTING["url"], ("skip", "")),
        ([], "check_careers", "", ("check_careers", "")),
        ([LIVE], "apply", LIVE["url"], ("apply", LIVE["url"])),  # a listing next to a real posting changes nothing
        ([LIVE], "apply", "https://JOBS.ashbyhq.com/acme/1/application?utm_source=x", ("apply", "https://JOBS.ashbyhq.com/acme/1/application?utm_source=x")),
        ([UNVERIFIED], "apply", "https://www.acme.com/careers/", ("consider", "https://www.acme.com/careers/")),
    ],
    ids=[
        "listing-as-best-role", "listing-under-another-spelling", "source-page-as-best-role", "another-companys-posting",
        "listing-url-on-a-skip", "listing-as-a-lead", "posting-as-best-role", "posting-under-another-spelling",
        "unverified-posting-under-another-spelling",
    ],
)
def test_the_role_to_apply_to_is_one_of_the_open_roles_never_a_third_party_listing(profile, claude, roles, model_verdict,
                                                                                  best_url, expected):
    fake = claude(verdict=model_verdict, best_role_title=LISTING["title"], best_role_url=best_url)

    fit, _ = scoring.score_company({**research(roles), "third_party_roles": [LISTING]}, profile, client=fake)

    assert (fit.verdict, fit.best_role_url) == expected
    assert fit.best_role_title == LISTING["title"]  # what to look for on the careers page
    sent = fake.calls[0]["messages"][0]["content"]
    assert '"third_party_roles"' in sent and LISTING["url"] in sent  # Claude is shown the listing, as a lead


NEUTRAL = {"target_role": 0.9, "region_excludes_candidate": 0.05, "onsite_or_hybrid_only": 0.1,
           "office_in_candidate_country": 0.1, "relocation": 0.2}
SENIOR = [0.0, 0.05, 0.8, 0.15]
ROLES = {  # title -> what Jev answers about it, on top of NEUTRAL
    "Senior Analytics Engineer": {},
    "Analytics Engineer (US only)": {"region_excludes_candidate": 0.97, "relocation": 0.03},
    "Analytics Engineer (US preferred)": {"region_excludes_candidate": 0.85, "relocation": 0.03},  # not confident
    "Analytics Engineer (Berlin office)": {"onsite_or_hybrid_only": 0.95, "office_in_candidate_country": 0.02,
                                           "relocation": 0.03},
    "Analytics Engineer (Berlin, relocation)": {"onsite_or_hybrid_only": 0.95, "office_in_candidate_country": 0.02,
                                                "relocation": 0.4},  # relocation possible: Claude decides
    "Junior Data Analyst": {"seniority": [0.6, 0.35, 0.05, 0.0]},
    "Office Manager": {"target_role": 0.02},
}


def role(title):
    return {"title": title, "url": f"https://acme.com/jobs/{title.replace(' ', '-')}", "location": "",
            "remote_or_relocation": "", "link_status": "live"}


def answer_by_title(state, key, question):
    overrides = ROLES[state["role"]["title"]]
    return overrides.get(key, SENIOR if key == "seniority" else NEUTRAL[key])


def test_screen_drops_only_roles_jev_is_sure_fail_and_claude_sees_the_rest(profile, claude, jev_api):
    jev_api.on(answer_by_title)
    acme = research([role(title) for title in ROLES])

    screen = scoring.screen_roles(acme, profile)

    assert {r["title"]: r["failed"] for r in screen["roles"]} == {
        "Senior Analytics Engineer": [],
        "Analytics Engineer (US only)": ["limited to regions that exclude Belgrade, Serbia, no relocation"],
        "Analytics Engineer (US preferred)": [],
        "Analytics Engineer (Berlin office)": ["on-site or hybrid in another country than Belgrade, Serbia, no relocation"],
        "Analytics Engineer (Berlin, relocation)": [],
        "Junior Data Analyst": ["below senior level"],
        "Office Manager": ["not one of your target roles"],
    }
    assert jev_api.requests[0]["state"] == {
        "candidate": {"target_roles": profile["search"]["roles"], "location": "Belgrade, Serbia"},
        "role": {"title": "Senior Analytics Engineer"},  # empty fields are left out
    }
    fake = claude()
    scoring.score_company(acme, profile, screen=screen, client=fake)
    sent = fake.calls[0]["messages"][0]["content"]
    assert sent.count('"title"') == 3 and "Office Manager" not in sent and "(US only)" not in sent


def test_a_company_whose_roles_all_fail_is_skipped_without_claude(profile, jev_api):
    jev_api.on(answer_by_title)
    acme = research([role("Office Manager"), role("Junior Data Analyst")])

    fit, usage = scoring.score_company(acme, profile, screen=scoring.screen_roles(acme, profile))  # no Claude key set

    assert (fit.score, fit.verdict, fit.best_role_url) == (0, "skip", "")
    assert fit.concerns == ["Office Manager: not one of your target roles", "Junior Data Analyst: below senior level"]
    assert usage == {"input_tokens": 0, "output_tokens": 0}


def test_a_screened_out_skip_keeps_at_most_three_concerns(profile):
    titles = ["A", "B", "C", "D"]
    screen = {"roles": [{"title": t, "url": t, "passed": False, "failed": ["below senior level"]} for t in titles]}

    fit, _ = scoring.score_company(research([role(t) for t in titles]), profile, screen=screen)

    assert fit.concerns == ["A: below senior level", "B: below senior level",
                            "2 more open roles also failed the screen (see screen in out/)"]


@pytest.mark.parametrize(
    ("model_verdict", "best_url", "expected"),
    [
        ("check_careers", "", "check_careers"),
        ("apply", "https://acme.com/jobs/Office-Manager", "check_careers"),  # Claude overrules the screen: not to apply
        ("skip", "", "skip"),
    ],
    ids=["listing-as-a-lead", "the-dropped-role-as-best-role", "nothing-fits"],
)
def test_a_third_party_listing_sends_a_company_whose_roles_all_fail_to_claude_with_those_roles(profile, claude, jev_api,
                                                                                               model_verdict, best_url, expected):
    jev_api.on(answer_by_title)
    acme = {**research([role("Office Manager")]), "third_party_roles": [LISTING]}
    fake = claude(score=40, verdict=model_verdict, best_role_title=LISTING["title"], best_role_url=best_url)

    fit, usage = scoring.score_company(acme, profile, screen=scoring.screen_roles(acme, profile), client=fake)

    assert (fit.score, fit.verdict, fit.best_role_url) == (40, expected, "")
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Office Manager" in sent and LISTING["title"] in sent  # the role Jev dropped stays in view
    assert usage == {"input_tokens": 1200, "output_tokens": 300}


def test_without_a_location_jev_is_not_asked_about_it(profile, jev_api):
    jev_api.on(answer_by_title)
    profile["candidate"]["location"] = ""

    screen = scoring.screen_roles(research([role("Analytics Engineer (US only)")]), profile)

    assert screen["roles"][0]["passed"]
    assert set(jev_api.requests[0]["questions"]) == {"target_role", "seniority"}
