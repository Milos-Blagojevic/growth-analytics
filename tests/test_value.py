import pytest

from prospect_data import SETTINGS, assessment, figure
from prospects import value


def without_headcount(**fields):
    return assessment(headcount_low=None, headcount_high=None, **fields)


def with_revenue(amount_usd, **fields):
    return without_headcount(revenue_figures=[figure(amount_usd)], **fields)


@pytest.mark.parametrize(
    ("figures", "picked"),
    [
        ([figure(1e9, "2025"), figure(2e9, "2026-10")], 2e9),
        ([figure(500e6, "2025-09", "company"), figure(865e6, "2026-07", "third party")], 865e6),
        ([figure(865e6, "2026-07", "third party"), figure(700e6, "2026-01", "company")], 700e6),
        ([figure(600e6, "2026-03"), figure(700e6, "2026-09"), figure(650e6, "2026")], 700e6),
        ([figure(900e6, "2026", "third party"), figure(800e6, "2026", "third party")], 800e6),
        ([figure(5e9, "", "company"), figure(1e9, "2019", "third party")], 1e9),
        ([figure(5e9, "", "third party"), figure(4e9, "", "company")], 4e9),
        ([figure(2e9, "", "company"), figure(1e9, "0000", "third party")], 1e9),
        ([figure(-5e6, "2026"), figure(0.0, "2026"), figure(3e6, "2024")], 3e6),
        ([figure(-5e6, "2026")], None),
        ([], None),
    ],
    ids=["latest-year", "newer-estimate-beats-older-company-figure", "same-year-the-companys-own", "then-latest-month",
         "then-the-lower-amount", "undated-ranks-last", "undated-the-companys-own", "any-period-beats-none",
         "never-an-amount-not-above-zero", "nothing-above-zero", "none-published"],
)  # fmt: skip
def test_pick_settles_conflicting_figures_the_same_way_whatever_their_order(figures, picked):
    for order in (figures, figures[::-1]):
        assert (value.pick(order) or {}).get("amount_usd") == picked


@pytest.mark.parametrize(
    ("fields", "low", "high", "method"),
    [
        # take rate 1%; half of revenue is the basis; $50,000-$200,000 of revenue per employee
        (assessment(basis_figures=[figure(3_000_000.0)], revenue_figures=[figure(9_000_000.0)]), 30000, 30000, "basis"),
        (assessment(revenue_figures=[figure(1_000_000.0, "2025"), figure(9_000_000.0)]), 45000, 45000, "revenue"),
        (assessment(headcount_low=51, headcount_high=200), 12750, 200000, "headcount"),
        (without_headcount(), None, None, "unknown"),
    ],
    ids=["basis-beats-revenue", "revenue-beats-headcount", "headcount", "nothing-published"],
)
def test_estimate_runs_on_the_best_published_figure(fields, low, high, method):
    worth = value.estimate(fields, SETTINGS)

    assert (worth["low"], worth["high"], worth["method"]) == (low, high, method)
    # the figure it ran on: the picked one of that kind, none for a headcount guess
    assert worth["figure"] == (value.pick(fields[f"{method}_figures"]) if method in ("basis", "revenue") else None)


def test_estimate_never_runs_on_headcount_without_a_per_employee_range():
    settings = {**SETTINGS, "pricing": {**SETTINGS["pricing"], "revenue_per_employee": None}}

    assert value.estimate(assessment(), settings)["method"] == "unknown"


@pytest.mark.parametrize(
    ("fields", "tier", "route", "reason"),
    [
        # 40 employees: $10,000-$40,000, whose midpoint is exactly tier A's $20,000
        (assessment(), "A", "founder outreach", "about $20,000 a year"),
        (with_revenue(3_999_800.0), "B", "sales sequence", "about $19,999 a year"),
        (with_revenue(1_000_000.0), "B", "sales sequence", "about $5,000 a year"),
        (with_revenue(999_800.0), "out", "drop", "about $4,999 a year is below the lowest tier"),
        (without_headcount(), "unknown", "research manually", "no published figure or headcount to value it from"),
        (assessment(fit_score=50), "A", "founder outreach", "about $20,000 a year"),
        (assessment(fit_score=49), "out", "drop", "fit 49 is below 50"),
        (
            assessment(fit_score=95, basis_figures=[figure(90_000_000.0)], disqualifier="Agency that builds apps for clients"),
            "out", "drop", "disqualified: Agency that builds apps for clients",
        ),
    ],
    ids=["tier-floor", "just-below-a-tier", "lowest-tier-floor", "below-every-tier", "unknown-value", "fit-floor",
         "low-fit", "disqualified-despite-fit-and-value"],
)  # fmt: skip
def test_route_follows_the_rules_in_order(fields, tier, route, reason):
    where = value.route(fields, value.estimate(fields, SETTINGS), SETTINGS)

    assert where == {"tier": tier, "route": route, "reason": reason}


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        (assessment(), "$10,000-$40,000 a year (headcount)"),
        (with_revenue(9_000_000.0), "$45,000 a year (revenue)"),
        (without_headcount(), "value unknown"),
    ],
)
def test_describe_shows_a_range_only_when_there_is_one(fields, text):
    assert value.describe(value.estimate(fields, SETTINGS)) == text
