"""Prospect research via Exa: what the company's own site says, then its funding, size and news from the open web."""

import logging

from clients import exa_client

logger = logging.getLogger("enrich")

SITE_PROMPT = (
    "You research a company from its own website for a sales team. Report only what the pages state; "
    "never guess and never add outside knowledge. Say 'unknown' for anything the pages do not show."
)

MONEY_PROMPT = (
    "You research a company's funding, size and recent news for a sales team. Verify every fact against the "
    "company at the given domain; if sources describe a different company with a similar name, set "
    "identity_verified to false. Never guess: say 'unknown' for missing facts and use an empty news list when "
    "there is none. Give every figure as its source states it: amount, currency and the year it refers to."
)


# Exa limits: max nesting depth 2, max 10 total properties (array item properties count). 6 properties.
def site_schema(config):
    fields = {
        "business_summary": "What the company sells and to whom, in 1-2 sentences",
        "business_model": (
            "How it charges its customers: subscriptions, usage or transaction fees, one-off purchases, "
            "advertising, licences. Say 'unknown' if the site does not show it."
        ),
        "pricing": "Published plans and prices, or 'unknown'",
        "customers": (
            "Who its customers are: segments, named customers and any customer or user counts the site states. "
            "Say 'unknown' for what is not found."
        ),
        "scale": (
            "Figures the site states about the company's size: employees, revenue, volume handled, downloads, "
            "countries, offices. Quote each figure with its unit. Say 'unknown' if there are none."
        ),
        "fit_evidence": f"What the site shows about: {config['icp']['look_for']}. Say 'unknown' if it shows nothing.",
    }
    return {
        "type": "object",
        "properties": {name: {"type": "string", "description": text} for name, text in fields.items()},
        "required": list(fields),
    }


# Currently 10: 5 top-level scalars + news + 4 item fields. Adding a field means removing one.
def money_schema(config):
    return {
        "type": "object",
        "properties": {
            "identity_verified": {
                "type": "boolean",
                "description": "True only if the evidence is about the company at the given domain, not a similarly named one",
            },
            "funding": {
                "type": "string",
                "description": (
                    "Funding stage, the last round (amount, date, lead investors) and the total raised, or that the "
                    "company is bootstrapped, public or acquired. Say 'unknown' for what is not found."
                ),
            },
            "revenue": {
                "type": "string",
                "description": (
                    f"Published figures for revenue, ARR or {config['pricing']['basis']}: each with its amount, "
                    "currency, the year it refers to and whether the company stated it or a third party estimated "
                    "it. Say 'unknown' if there are none."
                ),
            },
            "headcount": {
                "type": "string",
                "description": "Number of employees, or a range, and where the figure comes from. Say 'unknown' if not found.",
            },
            "growth_signals": {
                "type": "string",
                "description": (
                    "From the last 12 months: hiring, launches, new markets, partnerships, layoffs. "
                    "Say 'unknown' if there are none."
                ),
            },
            "news": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "date": {"type": "string", "description": "Publication date, YYYY-MM-DD"},
                        "summary": {"type": "string", "description": "What happened, in one sentence"},
                    },
                    "required": ["title", "url", "date", "summary"],
                },
            },
        },
        "required": ["identity_verified", "funding", "revenue", "headcount", "growth_signals", "news"],
    }


def research(name, domain, config):
    """Research one prospect.

    Returns {"name", "domain", "site": <site_schema dict>, "money": <money_schema dict without identity_verified>
    or None, "grounding": {"site": {field: {confidence, urls}}, "money": {...}}, "warnings": [str]}.
    The site search reads only pages on the prospect's own domain and has to succeed. The funding search covers
    the open web; when it fails or describes a different company, "money" is None and "warnings" says why.
    """
    site, site_grounding = exa_client.grounded_answer(
        f"{name} ({domain}): what the company sells and to whom, how it charges, its published pricing, its "
        f"customers and figures about its size. Also: {config['icp']['look_for']}.",
        site_schema(config),
        system_prompt=SITE_PROMPT,
        include_domains=[domain],
    )

    money, money_grounding, warnings = None, {}, []
    try:
        found, found_grounding = exa_client.grounded_answer(
            f"{name} ({domain}): funding rounds and investors, published revenue or {config['pricing']['basis']} "
            "figures, number of employees, and company news from the last 12 months.",
            money_schema(config),
            system_prompt=MONEY_PROMPT,
        )
    except Exception as exc:  # the site research is still worth assessing
        logger.warning("%s: funding and news search failed (%s); keeping the site research", name, exc)
        warnings.append(f"Funding and news search failed: {type(exc).__name__}: {exc}")
    else:
        if found.pop("identity_verified", False):
            # Exa is asked for a list of items; keep the saved research to that whatever it sends
            news = found.get("news")
            found["news"] = [item for item in news if isinstance(item, dict)] if isinstance(news, list) else []
            money = found
            money_grounding = {field: g for field, g in found_grounding.items() if field != "identity_verified"}
        else:
            logger.warning("%s: funding and news search described a different company; dropped", name)
            warnings.append("Funding and news search described a different company; its findings were dropped")

    return {
        "name": name,
        "domain": domain,
        "site": site,
        "money": money,
        "grounding": {"site": site_grounding, "money": money_grounding},
        "warnings": warnings,
    }
