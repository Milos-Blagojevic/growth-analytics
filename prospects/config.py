"""prospects.toml: the product, the ideal customer, the pricing and the routing rules `python -m prospects` works from."""

import math
import tomllib

from prospects import value

DEFAULT_MIN_FIT = 50
DEFAULT_UNKNOWN_VALUE = "research manually"

# Every key a section may hold. A misspelt optional key would otherwise fall back to its default without a word.
KEYS = {
    "product": {"description"},
    "icp": {"description", "look_for", "disqualifiers"},
    "pricing": {"basis", "take_rate", "basis_share_of_revenue", "revenue_per_employee"},
    "routing": {"min_fit", "unknown_value", "tiers"},
}
TIER_KEYS = {"name", "min_value", "route"}


def _number(number):
    """A finite TOML number (TOML has inf and nan; true and false are not numbers here)."""
    return isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number)


def _table(raw, section):
    table = raw.get(section, {})
    if not isinstance(table, dict):
        raise ValueError(f"[{section}] must be a table")
    unknown = sorted(set(table) - KEYS[section])
    if unknown:
        raise ValueError(f"[{section}].{unknown[0]} is not a key this file has; see prospects.example.toml")
    return table


def _text(table, section, key, default=None):
    text = table.get(key, default)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"[{section}].{key} is required")
    return text.strip()


def _share(pricing, key, default=None):
    share = pricing.get(key, default)
    if not _number(share) or not 0 < share <= 1:
        raise ValueError(f"[pricing].{key} must be a number above 0 and at most 1 (0.01 is 1%)")
    return share


def _tiers(routing):
    tiers = routing.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("[[routing.tiers]] needs at least one tier")
    for tier in tiers:
        named = isinstance(tier, dict) and all(isinstance(tier.get(k), str) and tier[k].strip() for k in ("name", "route"))
        if not named or set(tier) != TIER_KEYS or not _number(tier["min_value"]) or tier["min_value"] < 0:
            raise ValueError(f"[[routing.tiers]]: each tier needs name, route and min_value (0 or more); got {tier!r}")
        if tier["name"].strip() in (value.OUT, value.UNKNOWN):
            raise ValueError(
                f"[[routing.tiers]]: {value.OUT!r} and {value.UNKNOWN!r} are the tiers of dropped and unvalued prospects"
            )
    values = [tier["min_value"] for tier in tiers]
    if any(above <= below for above, below in zip(values, values[1:])):
        # route() takes the first tier the value reaches, so any other order would hide the tiers below it
        raise ValueError("[[routing.tiers]] must be listed from the highest min_value to the lowest")
    return [{"name": t["name"].strip(), "min_value": t["min_value"], "route": t["route"].strip()} for t in tiers]


def load(path):
    """prospects.toml as a dict with defaults filled in. Raises ValueError naming the key that is missing, invalid
    or unknown.

    Returns {"product": {"description"}, "icp": {"description", "look_for", "disqualifiers"},
    "pricing": {"basis", "take_rate", "basis_share_of_revenue", "revenue_per_employee": [low, high] or None},
    "routing": {"min_fit", "unknown_value", "tiers": [{"name", "min_value", "route"}, ...]}}, tiers highest first.
    """
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        raise ValueError(f"[{unknown[0]}] is not a section this file has; see prospects.example.toml")
    product, icp, pricing, routing = (_table(raw, section) for section in KEYS)

    disqualifiers = icp.get("disqualifiers", [])
    if not isinstance(disqualifiers, list) or not all(isinstance(d, str) and d.strip() for d in disqualifiers):
        raise ValueError("[icp].disqualifiers must be a list of non-empty strings")

    per_employee = pricing.get("revenue_per_employee")
    if per_employee is not None:
        pair = isinstance(per_employee, list) and len(per_employee) == 2 and all(_number(v) for v in per_employee)
        if not pair or not 0 < per_employee[0] <= per_employee[1]:
            raise ValueError("[pricing].revenue_per_employee must be [low, high] dollars a year, e.g. [50000, 200000]")

    min_fit = routing.get("min_fit", DEFAULT_MIN_FIT)
    if not _number(min_fit) or not 0 <= min_fit <= 100:
        raise ValueError("[routing].min_fit must be a number from 0 to 100")

    return {
        "product": {"description": _text(product, "product", "description")},
        "icp": {
            "description": _text(icp, "icp", "description"),
            "look_for": _text(icp, "icp", "look_for"),
            "disqualifiers": [d.strip() for d in disqualifiers],
        },
        "pricing": {
            "basis": _text(pricing, "pricing", "basis"),
            "take_rate": _share(pricing, "take_rate"),
            "basis_share_of_revenue": _share(pricing, "basis_share_of_revenue", 1.0),
            "revenue_per_employee": per_employee,
        },
        "routing": {
            "min_fit": min_fit,
            "unknown_value": _text(routing, "routing", "unknown_value", DEFAULT_UNKNOWN_VALUE),
            "tiers": _tiers(routing),
        },
    }
