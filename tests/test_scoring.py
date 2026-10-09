import pytest

import scoring

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
