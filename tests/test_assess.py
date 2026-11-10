import pytest

from prospect_data import SETTINGS, assessment, figure, freeze_today, research
from prospects import assess

LISTED = "Agency that builds apps for clients"


@pytest.fixture(autouse=True)
def today(monkeypatch):
    freeze_today(monkeypatch)


def test_assess_gives_claude_the_seller_and_the_research_and_returns_its_assessment(fake_claude, monkeypatch):
    # two revenue figures that disagree: both come back, and value.pick settles it later
    fields = assessment(
        disqualifier=LISTED,
        revenue_figures=[figure(500e6, "2025-09", "company"), figure(865e6, "2026-07", "third party")],
    )
    fake = fake_claude(fields)
    monkeypatch.setenv("SCORING_MODEL", "claude-test")

    result, usage = assess.assess(research(), SETTINGS, client=fake)

    assert result.model_dump() == fields
    assert usage == {"input_tokens": 1200, "output_tokens": 300}
    (call,) = fake.calls
    prompt = call["messages"][0]["content"]
    for expected in (
        "Consumer mobile apps that sell subscriptions.",  # the ideal customer
        LISTED,  # a disqualifier to pick
        "in-app subscription revenue",  # the pricing basis to look for a figure of
        "Sells subscriptions on iOS and Android.",  # site research
        "Series A, $12M in March 2026",  # open-web research
        "https://acme.com/pricing",  # grounding
    ):
        assert expected in prompt
    assert "0.01" not in prompt  # the take rate stays out: Claude copies figures, value.py does the arithmetic
    # Structured output can only return a listed disqualifier or none: a reworded one would not drop the prospect.
    schema = call["output_format"].model_json_schema()
    assert schema["properties"]["disqualifier"]["enum"] == ["", LISTED]
    assert schema["$defs"]["Figure"]["properties"]["stated_by"]["enum"] == ["company", "third party"]
    assert call["model"] == "claude-test"
    assert "tools" not in call and "temperature" not in call  # web text gets no tools; the model rejects temperature


def test_assess_ignores_a_disqualifier_when_the_seller_lists_none(fake_claude, caplog):
    settings = {**SETTINGS, "icp": {**SETTINGS["icp"], "disqualifiers": []}}

    result, _ = assess.assess(research(), settings, client=fake_claude(assessment(disqualifier="Too small")))

    assert result.disqualifier == ""
    assert "ignoring a disqualifier that is not in [icp].disqualifiers: 'Too small'" in caplog.text


def test_assess_holds_the_output_to_what_the_rating_relies_on(fake_claude):
    fake = fake_claude(assessment(
        fit_score=140,
        fit_reasons=["a", "b", "c", "d"],
        fit_concerns=["e", "f", "g", "h"],
        value_signals=["1", "2", "3", "4", "5", "6"],
        basis_figures=[figure(-5.0), figure(3e6, "2026-10-15"), figure(1e21)],
        revenue_figures=[figure(0.0), figure(9e6, "last year"), figure(8e6, " 2025 "), figure(8e6, "2025")],
        last_round_usd=1e21,
        total_funding_usd=float("inf"),
    ))  # fmt: skip

    result, _ = assess.assess(research(), SETTINGS, client=fake)

    assert result.fit_score == 100
    assert (result.fit_reasons, result.fit_concerns) == (["a", "b", "c"], ["e", "f", "g"])
    assert result.value_signals == ["1", "2", "3", "4", "5"]
    # Zero, negative or beyond any company is "not published": never a figure the value estimate could run on.
    assert (result.last_round_usd, result.total_funding_usd) == (None, None)
    assert [(f.amount_usd, f.as_of) for f in result.basis_figures] == [(3e6, "2026-10")]
    # Each figure once, with a period value.pick can order; one it cannot read counts as no period.
    assert [(f.amount_usd, f.as_of) for f in result.revenue_figures] == [(9e6, ""), (8e6, "2025")]


@pytest.mark.parametrize(
    ("as_of", "read"),
    [("2026-07", "2026-07"), ("2026-07-15", "2026-07"), ("2026", "2026"), ("FY2026", "2026"), ("July 2026", "2026"),
     ("Q3 2026", "2026"), ("2025-2026", "2026"), ("2026-13", "2026"), ("2025-12 (reported)", "2025-12"),
     ("a 2024 figure, restated in 2025-03", "2025-03"), ("2025, first given in 2024", "2025"),
     ("20261015", ""), ("0000", ""), ("last year", ""), ("", "")],
)  # fmt: skip
def test_assess_reads_a_figures_period_as_the_latest_month_or_year_it_names(fake_claude, as_of, read):
    result, _ = assess.assess(research(), SETTINGS, client=fake_claude(assessment(revenue_figures=[figure(9e6, as_of)])))

    assert [f.as_of for f in result.revenue_figures] == [read]


def test_assess_drops_figures_for_periods_that_have_not_come_yet_and_says_so(fake_claude, caplog):
    # It is 10 November 2026. A target is not revenue the company has, and as the most recent figure it would win
    # the pick: a prospect at $1.5M with a "$10M by 2028" plan would be valued at $10M.
    fake = fake_claude(assessment(
        revenue_figures=[
            figure(1.5e6, "2026-06"), figure(2e6, "2026-11"), figure(3e6, "2026"), figure(5e6, "2026-12"),
            figure(10e6, "2028", evidence="targets $10M ARR\n by 2028"), figure(12e6, "2027-01"),
            figure(4e6, "a 2030 target, set in 2026"),
        ],
        basis_figures=[figure(6e6, "2027")],
    ))  # fmt: skip

    result, _ = assess.assess(research(), SETTINGS, client=fake)

    assert [(f.amount_usd, f.as_of) for f in result.revenue_figures] == [(1.5e6, "2026-06"), (2e6, "2026-11"), (3e6, "2026")]
    assert result.basis_figures == []
    assert "Acme: left out a figure for a period that has not come yet (2028): targets $10M ARR by 2028" in caplog.text
    assert caplog.text.count("left out a figure") == 5


def test_assess_keeps_a_figure_listed_twice_once_and_every_figure_that_differs(fake_claude):
    listed = [
        figure(8e6, "2025", evidence="$8M in 2025 (pricing page)"),
        figure(8e6, "2025", evidence="$8M in 2025 (press release)"),  # the same figure from another page
        figure(7e6, "2025"),
        figure(8e6, "2024"),
        figure(8e6, "2025", "third party"),
    ]

    result, _ = assess.assess(research(), SETTINGS, client=fake_claude(assessment(revenue_figures=listed)))

    assert [(f.amount_usd, f.as_of, f.stated_by, f.evidence) for f in result.revenue_figures] == [
        (8e6, "2025", "company", "$8M in 2025 (pricing page)"),
        (7e6, "2025", "company", "$7,000,000 in 2025"),
        (8e6, "2024", "company", "$8,000,000 in 2024"),
        (8e6, "2025", "third party", "$8,000,000 in 2025"),
    ]


@pytest.mark.parametrize(
    ("given", "kept"),
    [((51, 200), (51, 200)), ((200, 51), (51, 200)), ((None, 120), (120, 120)), ((120, None), (120, 120)),
     ((0, 0), (None, None)), ((-3, 40), (40, 40)), ((40, 3_000_000_000), (40, 40))],
)  # fmt: skip
def test_assess_returns_headcount_as_an_ordered_plausible_range_or_nothing(fake_claude, given, kept):
    fake = fake_claude(assessment(headcount_low=given[0], headcount_high=given[1]))

    result, _ = assess.assess(research(), SETTINGS, client=fake)

    assert (result.headcount_low, result.headcount_high) == kept


def test_assess_raises_when_claude_returns_nothing_parsable(fake_claude):
    with pytest.raises(RuntimeError, match="no parsable output for Acme"):
        assess.assess(research(), SETTINGS, client=fake_claude(None))
