import argparse
import json

import pytest

import hunt
import news
import scoring
from exa_mock import error, result, search_response

TELEGRAM = "https://api.telegram.org/bottok/sendMessage"
PROFILE_TOML = """
[search]
roles = ["growth engineer"]
seniority = ["senior"]
work_mode = "remote"

[news]
topics = ["growth engineering", "data analytics job market"]
"""
SHARED = "https://news.example.com/hiring-rebounds"


def article(url, title, published="2026-10-18T07:00:00.000Z"):
    return result(url, title=title, highlights=[f"{title}: the details."]) | {"publishedDate": published}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var, value in {"ANTHROPIC_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}.items():
        monkeypatch.setenv(var, value)
    (tmp_path / "profile.toml").write_text(PROFILE_TOML)
    (tmp_path / "news_state.json").write_text(json.dumps({"https://old.example.com/sent": "2026-10-10T08:00:00"}))
    return tmp_path


@pytest.fixture
def searches(exa):
    def handler(body):
        if body["query"] == "growth engineering":
            return search_response([article("https://blog.example.com/experiments", "Experiment platforms in 2026"),
                                    article(SHARED, "Analytics hiring rebounds")])
        return search_response([article(SHARED, "Analytics hiring rebounds"),
                                article("https://old.example.com/sent", "Already sent")])

    exa.on_search(handler)
    return exa


def news_args(**overrides):
    return argparse.Namespace(**{"profile": "profile.toml", "state": "news_state.json", "days": 7, "dry_run": False,
                                 **overrides})


def test_news_sends_picked_articles_once(workdir, searches, fake_claude, monkeypatch, http):
    fake = fake_claude({"overview": "Hiring for analytics is picking up.",
                        "picks": [{"number": 2, "takeaway": "Demand for your roles is up."},
                                  {"number": 7, "takeaway": "invented"}, {"number": 2, "takeaway": "again"}]})
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    telegram_api = http.post(TELEGRAM, json={"ok": True})

    assert hunt.cmd_news(news_args()) == 0

    bodies = searches.requests()
    assert [b["category"] for b in bodies] == ["news", "news"] and all("startPublishedDate" in b for b in bodies)
    content = fake.calls[0]["messages"][0]["content"]
    assert "1. Experiment platforms in 2026 (blog.example.com, 2026-10-18)" in content
    assert content.count("Analytics hiring rebounds (") == 1  # found by both topics, given once
    assert "Already sent" not in content
    (text,) = [json.loads(c.request.body)["text"] for c in telegram_api.calls]
    assert "Hiring for analytics is picking up." in text
    assert f"• Analytics hiring rebounds (news.example.com)\nDemand for your roles is up.\n{SHARED}" in text
    assert "invented" not in text and "again" not in text
    state = json.loads((workdir / "news_state.json").read_text())
    assert set(state) == {"https://old.example.com/sent", "https://blog.example.com/experiments", SHARED}

    # Nothing new next time: no Claude call, no message.
    assert hunt.cmd_news(news_args()) == 0
    assert len(fake.calls) == 1 and len(telegram_api.calls) == 1


def test_jev_drops_irrelevant_articles_and_claude_reads_the_rest(workdir, searches, fake_claude, monkeypatch, http,
                                                                jev_api):
    relevance = {"Experiment platforms in 2026": 0.1, "Analytics hiring rebounds": news.RELEVANT_MIN}
    jev_api.on(lambda state, key, question: relevance[state["article"]["title"]])
    fake = fake_claude({"overview": "o", "picks": [{"number": 1, "takeaway": "Demand is up."}]})
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    telegram_api = http.post(TELEGRAM, json={"ok": True})

    assert hunt.cmd_news(news_args()) == 0

    assert jev_api.requests[0]["state"]["candidate"] == {"target_roles": ["growth engineer"],
                                                         "interests": ["growth engineering", "data analytics job market"]}
    content = fake.calls[0]["messages"][0]["content"]
    assert "1. Analytics hiring rebounds" in content and "Experiment platforms" not in content
    (text,) = [json.loads(c.request.body)["text"] for c in telegram_api.calls]
    assert "Analytics hiring rebounds (news.example.com)\nDemand is up." in text
    state = json.loads((workdir / "news_state.json").read_text())
    assert {"https://blog.example.com/experiments", SHARED} <= set(state)  # dropped articles are not screened again


def test_no_claude_call_when_jev_finds_nothing_relevant(workdir, searches, fake_claude, monkeypatch, jev_api, capsys):
    jev_api.on(lambda state, key, question: 0.05)
    fake = fake_claude({"overview": "o", "picks": []})
    monkeypatch.setattr(scoring, "_client", lambda: fake)

    assert hunt.cmd_news(news_args()) == 0

    assert fake.calls == []
    assert "None of 2 new article(s) was worth sending." in capsys.readouterr().out


def test_claude_reads_every_article_when_jev_gives_no_usable_answer(workdir, searches, fake_claude, monkeypatch,
                                                                    jev_api):
    jev_api.on(lambda state, key, question: None)  # 200 with a null probability
    fake = fake_claude({"overview": "o", "picks": []})
    monkeypatch.setattr(scoring, "_client", lambda: fake)

    assert hunt.cmd_news(news_args(dry_run=True)) == 0

    assert len(jev_api.requests) == 1  # the first failure stops the screen
    content = fake.calls[0]["messages"][0]["content"]
    assert "Experiment platforms in 2026" in content and "Analytics hiring rebounds" in content


def test_news_reports_a_failed_topic_and_keeps_the_rest(workdir, exa, fake_claude, monkeypatch, capsys):
    exa.on_search(lambda body: error(400, "bad query") if body["query"] == "growth engineering"
                  else search_response([article(SHARED, "Analytics hiring rebounds")]))
    fake = fake_claude({"overview": "o", "picks": [{"number": 1, "takeaway": "t"}]})
    monkeypatch.setattr(scoring, "_client", lambda: fake)

    assert hunt.cmd_news(news_args(dry_run=True)) == 1

    out = capsys.readouterr().out
    assert "Analytics hiring rebounds" in out and "Could not search:\n- growth engineering: ValueError" in out
    assert json.loads((workdir / "news_state.json").read_text()) == {"https://old.example.com/sent": "2026-10-10T08:00:00"}


def test_news_needs_topics(workdir, http):
    (workdir / "profile.toml").write_text(PROFILE_TOML.split("[news]")[0])

    with pytest.raises(SystemExit, match=r"\[news\].topics is empty"):
        hunt.cmd_news(news_args())
    assert len(http.calls) == 0
