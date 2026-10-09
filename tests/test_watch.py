import argparse
import json

import pytest

import hunt
import watch

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{}"

PROFILE_TOML = """
[search]
roles = ["growth engineer"]
seniority = ["senior"]
work_mode = "remote"

[watch]
ashby_boards = ["Acme", "gone"]
title_keywords = ["Growth", "analytics engineer"]
title_exclude = ["marketing"]
"""


def job(job_id, title, **extra):
    return {"id": job_id, "title": title, **extra}


@pytest.fixture
def boards(http):
    http.get(
        ASHBY_API.format("acme"),
        json={
            "jobs": [
                job("j1", "Senior Growth Engineer", location="Berlin", isRemote=True, jobUrl="https://jobs.ashbyhq.com/acme/j1"),
                job("j2", "Growth Marketing Manager"),
                job("j3", "Accountant"),
                job("j4", "Analytics Engineer"),
                job("j5", "Lead Analytics Engineer"),
            ]
        },
    )
    http.get(ASHBY_API.format("gone"), status=404)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.toml").write_text(PROFILE_TOML)
    (tmp_path / "watch_state.json").write_text(json.dumps({"acme": {"j4": {"title": "Analytics Engineer", "first_seen": "x"}}}))
    return tmp_path


def watch_args(dry_run=False):
    return argparse.Namespace(profile="profile.toml", state="watch_state.json", dry_run=dry_run)


def test_check_reports_only_new_matching_postings(boards):
    state = {"acme": {"j4": {"title": "Analytics Engineer", "first_seen": "x"}}}

    new, problems, new_state = watch.check(["acme", "gone"], ["growth", "analytics engineer"], ["marketing"], state)

    assert [(m["id"], m["location"], m["remote"], m["url"]) for m in new] == [
        ("j1", "Berlin", True, "https://jobs.ashbyhq.com/acme/j1"),
        ("j5", "unknown", False, "https://jobs.ashbyhq.com/acme/j5"),
    ]
    assert problems == ["gone: no such Ashby board"]
    assert set(new_state) == {"acme"}
    assert set(new_state["acme"]) == {"j1", "j4", "j5"}
    j1 = new_state["acme"]["j1"]
    assert (j1["location"], j1["remote"], j1["url"]) == ("Berlin", True, "https://jobs.ashbyhq.com/acme/j1")
    assert set(state["acme"]) == {"j4"}  # input state is not mutated


def test_watch_saves_state_only_after_successful_send(boards, http, workdir, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    http.post("https://api.telegram.org/bottok/sendMessage", json={"ok": False, "error_code": 400, "description": "chat not found"})
    before = (workdir / "watch_state.json").read_text()

    with pytest.raises(RuntimeError, match="chat not found"):
        hunt.cmd_watch(watch_args())
    assert (workdir / "watch_state.json").read_text() == before

    http.replace("POST", "https://api.telegram.org/bottok/sendMessage", json={"ok": True})
    assert hunt.cmd_watch(watch_args()) == 1  # sent, but the "gone" board could not be read
    assert set(json.loads((workdir / "watch_state.json").read_text())["acme"]) == {"j1", "j4", "j5"}


def test_watch_dry_run_does_not_save_state(boards, workdir, capsys):
    before = (workdir / "watch_state.json").read_text()

    hunt.cmd_watch(watch_args(dry_run=True))

    assert "Senior Growth Engineer" in capsys.readouterr().out
    assert (workdir / "watch_state.json").read_text() == before
