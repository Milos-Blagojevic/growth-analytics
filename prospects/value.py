"""What a prospect is worth to you a year and where it goes: fixed arithmetic and rules on the figures Claude copied
from the research and on prospects.toml. No model call, so `python -m prospects load` re-rates every saved prospect
for free after a change to the pricing, the tiers or these rules.

The estimate is a range, not a forecast: nothing here is fitted to closed deals.
"""

import math

DROP = "drop"
OUT = "out"  # tier of a dropped prospect
UNKNOWN = "unknown"  # value method, and tier, of a prospect whose value cannot be estimated


def pick(figures):
    """The figure to value a prospect on when the research states several (None when it states none): from the most
    recent year; within that year one the company stated rather than a third party's estimate; then the latest
    month; then the lower amount. A figure with no period ranks below every dated one, and an amount that is not
    above zero is never picked.

    `figures`: [{"amount_usd", "as_of": "YYYY-MM" | "YYYY" | "", "stated_by": "company" | "third party", ...}].
    assess.py keeps targets and forecasts out of the list: the most recent figure has to be one the company reached.
    """

    def rank(figure):
        year, _, month = figure["as_of"].partition("-")
        return int(year) if year else -1, figure["stated_by"] == "company", int(month or 0), -figure["amount_usd"]

    return max((figure for figure in figures if figure["amount_usd"] > 0), key=rank, default=None)


def estimate(assessment, config):
    """Yearly value in dollars: {"low", "high", "method", "figure"}; low and high are None when the method is
    "unknown", and "figure" is the published figure the estimate ran on (None for "headcount" and "unknown").

    The best evidence wins: "basis" (a published figure for the pricing basis, see pick) x take rate; else
    "revenue" (published revenue x the share of it that is the basis) x take rate; else "headcount" (employees x
    [pricing].revenue_per_employee, when that is set, x the same share) x take rate.
    """
    pricing = config["pricing"]
    rate, share, per_employee = pricing["take_rate"], pricing["basis_share_of_revenue"], pricing["revenue_per_employee"]
    basis, revenue = pick(assessment["basis_figures"]), pick(assessment["revenue_figures"])
    if basis:
        low = high = basis["amount_usd"] * rate
        method, figure = "basis", basis
    elif revenue:
        low = high = revenue["amount_usd"] * share * rate
        method, figure = "revenue", revenue
    elif assessment["headcount_low"] and per_employee:
        low = assessment["headcount_low"] * per_employee[0] * share * rate
        high = assessment["headcount_high"] * per_employee[1] * share * rate
        method, figure = "headcount", None
    else:
        return {"low": None, "high": None, "method": UNKNOWN, "figure": None}
    return {"low": round(low), "high": round(high), "method": method, "figure": figure}


def midpoint(value):
    """The geometric middle of the range (headcount ranges span a multiple, not a difference); None when unknown."""
    return None if value["method"] == UNKNOWN else math.sqrt(value["low"] * value["high"])


def describe(value):
    if value["method"] == UNKNOWN:
        return "value unknown"
    low, high = value["low"], value["high"]
    amount = f"${low:,.0f}" if low == high else f"${low:,.0f}-${high:,.0f}"
    return f"{amount} a year ({value['method']})"


def route(assessment, value, config):
    """{"tier", "route", "reason"}: dropped for a disqualifier or a low fit, [routing].unknown_value when the value
    is unknown, otherwise the first tier whose min_value the estimate's midpoint reaches (dropped below the last)."""
    routing = config["routing"]
    if assessment["disqualifier"]:
        return {"tier": OUT, "route": DROP, "reason": f"disqualified: {assessment['disqualifier']}"}
    if assessment["fit_score"] < routing["min_fit"]:
        return {"tier": OUT, "route": DROP, "reason": f"fit {assessment['fit_score']} is below {routing['min_fit']}"}
    middle = midpoint(value)
    if middle is None:
        return {"tier": UNKNOWN, "route": routing["unknown_value"], "reason": "no published figure or headcount to value it from"}
    for tier in routing["tiers"]:
        if middle >= tier["min_value"]:
            return {"tier": tier["name"], "route": tier["route"], "reason": f"about ${middle:,.0f} a year"}
    return {"tier": OUT, "route": DROP, "reason": f"about ${middle:,.0f} a year is below the lowest tier"}
