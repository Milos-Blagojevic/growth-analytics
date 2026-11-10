import pytest

from exa_mock import error
from prospect_data import SETTINGS, SITE, exa_replies, money_reply, research
from prospects import enrich


def test_research_reads_the_prospects_own_site_then_the_open_web(exa):
    exa.on_search(exa_replies())

    found = enrich.research("Acme", "acme.com", SETTINGS)

    assert found == research()  # identity_verified is checked and dropped; grounding is merged per field
    site_search, money_search = exa.requests()
    assert site_search["includeDomains"] == ["acme.com"]  # the site fields come from the prospect's pages only
    assert "includeDomains" not in money_search
    assert "whether it sells in-app subscriptions" in site_search["outputSchema"]["properties"]["fit_evidence"]["description"]
    assert "in-app subscription revenue" in money_search["outputSchema"]["properties"]["revenue"]["description"]
    assert (site_search["systemPrompt"], money_search["systemPrompt"]) == (enrich.SITE_PROMPT, enrich.MONEY_PROMPT)


def test_research_drops_funding_findings_about_another_company(exa):
    exa.on_search(exa_replies(money=money_reply(identity_verified=False)))

    found = enrich.research("Acme", "acme.com", SETTINGS)

    assert found["site"] == SITE
    assert found["money"] is None and found["grounding"]["money"] == {}
    assert found["warnings"] == ["Funding and news search described a different company; its findings were dropped"]


@pytest.mark.parametrize("news", [None, "none found", 7, [None, "a headline"]])
def test_research_saves_news_as_a_list_of_items_whatever_exa_sends(exa, news):
    exa.on_search(exa_replies(money=money_reply(news=news)))

    assert enrich.research("Acme", "acme.com", SETTINGS)["money"]["news"] == []


def test_research_keeps_the_site_research_when_the_funding_search_fails(exa):
    exa.on_search(exa_replies(money=error(400)))

    found = enrich.research("Acme", "acme.com", SETTINGS)

    assert found["site"] == SITE and found["money"] is None
    (warning,) = found["warnings"]
    assert warning.startswith("Funding and news search failed: ValueError: Request failed with status code 400")


def test_research_fails_without_paying_for_the_funding_search_when_the_site_search_fails(exa):
    exa.on_search(exa_replies(site=error(400)))

    with pytest.raises(ValueError, match="status code 400"):
        enrich.research("Acme", "acme.com", SETTINGS)
    assert len(exa.requests()) == 1
