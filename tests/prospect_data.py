"""Sample prospects.toml, Exa replies, research and assessment values for the prospects tests."""

import json
from datetime import date

from exa_mock import grounded, structured_response
from prospects import assess

CONFIG_TOML = """
[product]
description = "Subscription billing for mobile apps."

[icp]
description = "Consumer mobile apps that sell subscriptions."
look_for = "whether it sells in-app subscriptions"
disqualifiers = ["Agency that builds apps for clients"]

[pricing]
basis = "in-app subscription revenue"
take_rate = 0.01
basis_share_of_revenue = 0.5
revenue_per_employee = [50000, 200000]

[routing]
min_fit = 50

[[routing.tiers]]
name = "A"
min_value = 20000
route = "founder outreach"

[[routing.tiers]]
name = "B"
min_value = 5000
route = "sales sequence"
"""

# CONFIG_TOML as config.load returns it.
SETTINGS = {
    "product": {"description": "Subscription billing for mobile apps."},
    "icp": {
        "description": "Consumer mobile apps that sell subscriptions.",
        "look_for": "whether it sells in-app subscriptions",
        "disqualifiers": ["Agency that builds apps for clients"],
    },
    "pricing": {
        "basis": "in-app subscription revenue",
        "take_rate": 0.01,
        "basis_share_of_revenue": 0.5,
        "revenue_per_employee": [50000, 200000],
    },
    "routing": {
        "min_fit": 50,
        "unknown_value": "research manually",
        "tiers": [
            {"name": "A", "min_value": 20000, "route": "founder outreach"},
            {"name": "B", "min_value": 5000, "route": "sales sequence"},
        ],
    },
}

SITE = {
    "business_summary": "Acme sells a meditation app to consumers.",
    "business_model": "Monthly and yearly in-app subscriptions.",
    "pricing": "$12.99 a month or $69.99 a year.",
    "customers": "Consumers; 2 million downloads.",
    "scale": "unknown",
    "fit_evidence": "Sells subscriptions on iOS and Android.",
}
NEWS = {
    "title": "Acme raises $12M",
    "url": "https://news.example/acme-series-a",
    "date": "2026-03-04",
    "summary": "Acme closed a Series A.",
}
MONEY = {
    "funding": "Series A, $12M in March 2026 led by Index; $15M raised in total.",
    "revenue": "unknown",
    "headcount": "About 40 employees (LinkedIn).",
    "growth_signals": "Hiring two growth marketers.",
    "news": [NEWS],
}


def site_reply():
    """Exa's answer to the search of the prospect's own site."""
    return structured_response(
        SITE,
        [grounded("business_summary", "https://acme.com/", "https://acme.com/about"),
         grounded("pricing", "https://acme.com/pricing", confidence="medium")],
    )  # fmt: skip


def money_reply(*, identity_verified=True, news=MONEY["news"]):
    """Exa's answer to the open-web search for funding, size and news."""
    return structured_response(
        {"identity_verified": identity_verified, **MONEY, "news": news},
        [grounded("identity_verified", "https://acme.com/"),
         grounded("funding", NEWS["url"]),
         grounded("news[0].url", NEWS["url"])],
    )  # fmt: skip


def exa_replies(site=None, money=None):
    """An ExaMock search handler: the search restricted to the prospect's domain gets `site`, the other `money`."""
    site, money = site or site_reply(), money or money_reply()
    return lambda body: site if "includeDomains" in body else money


def research(name="Acme", domain="acme.com", *, money=MONEY):
    """A prospect's research as enrich.research returns it for site_reply() and money_reply()."""
    return {
        "name": name,
        "domain": domain,
        "site": SITE,
        "money": money,
        "grounding": {
            "site": {
                "business_summary": {"confidence": "high", "urls": ["https://acme.com/", "https://acme.com/about"]},
                "pricing": {"confidence": "medium", "urls": ["https://acme.com/pricing"]},
            },
            "money": {
                "funding": {"confidence": "high", "urls": [NEWS["url"]]},
                "news": {"confidence": "high", "urls": [NEWS["url"]]},
            } if money else {},
        },
        "warnings": [],
    }  # fmt: skip


def freeze_today(monkeypatch):
    """Makes it 10 November 2026 for assess.py, which drops figures for periods that have not come yet."""

    class Frozen(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 10)

    monkeypatch.setattr(assess, "date", Frozen)


def figure(amount_usd, as_of="2026", stated_by="company", evidence=None):
    """One published figure as an assessment lists it."""
    return {
        "amount_usd": amount_usd,
        "as_of": as_of,
        "stated_by": stated_by,
        "evidence": evidence or f"${amount_usd:,.0f} in {as_of or 'an unstated year'}",
    }


def assessment(**overrides):
    """Assessment fields. With SETTINGS: 40 employees are worth $10,000-$40,000 a year, midpoint $20,000 (tier A)."""
    return {
        "fit_score": 82,
        "fit_reasons": ["Sells in-app subscriptions."],
        "fit_concerns": [],
        "disqualifier": "",
        "basis_figures": [],
        "revenue_figures": [],
        "headcount_low": 40,
        "headcount_high": 40,
        "funding_stage": "series A",
        "last_round_usd": 12_000_000.0,
        "last_round_date": "2026-03",
        "total_funding_usd": 15_000_000.0,
        "value_signals": ["Raised a Series A in March 2026."],
        "brief": "Acme sells a meditation app on subscription.",
        **overrides,
    }


def write_run(root, run_id, results, *, manifest=None):
    """Saves a run under root/prospect_runs/<run_id>/ as `python -m prospects run` does; returns its folder."""
    run_dir = root / "prospect_runs" / run_id
    run_dir.mkdir(parents=True)
    for result in results:
        (run_dir / f"{result['domain']}.json").write_text(json.dumps(result))
    if manifest is not None:
        (run_dir / "_run.json").write_text(json.dumps(manifest))
    return run_dir
