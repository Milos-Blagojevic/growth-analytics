import argparse
import json
from datetime import datetime

import pytest
import requests

import hunt
from clients import exa_client
from exa_mock import NEXT_RUN, contents_response, error, grounded, monitor_run, result
from pipeline import monitor, research, runs, scoring

PROFILE_TOML = """
[search]
roles = ["analytics engineer", "growth engineer"]
seniority = ["senior", "lead"]
work_mode = "Prefer fully remote roles."

[candidate]
location = "Belgrade, Serbia"
experience = "8 years of analytics engineering (dbt, Snowflake)."
"""
MONITORS_CSV = "# name,domain\nAcme,https://www.Acme.com/\n\nBeta,beta.io\n"
COMPANIES = [("Acme", "acme.com"), ("Beta", "beta.io")]
WEBHOOK = "https://hunt.example.org"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/acme"
TELEGRAM_API = "https://api.telegram.org/bottok/sendMessage"
AE = "https://jobs.ashbyhq.com/acme/1111"
SALES = "https://jobs.ashbyhq.com/acme/2222"


def posting(title, url, *, verified=True):
    """A posting as a run lists it (research.ATS_SCHEMA)."""
    return {"title": title, "url": url, "location": "Remote", "remote_or_relocation": "Remote (EU)",
            "company_verified": verified}


def found_run(run_id, *postings, texts=None):
    """A completed run that lists `postings`, with each posting's own page among its pages (text from `texts`)."""
    pages = [result(p["url"], title=p["title"], text=(texts or {}).get(p["url"], f"{p['title']}: the posting.")) for p in postings]
    return monitor_run(run_id, results=pages, content={"open_roles": list(postings)},
                       grounding=[grounded("open_roles[0].url", postings[0]["url"])])


@pytest.fixture
def account(exa):
    return exa.monitors()


@pytest.fixture
def state(account, profile):
    """Acme's monitor (mon_1) and Beta's (mon_2), as a sync leaves them."""
    state = {"monitors": {}}
    monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)
    return state


# Config ------------------------------------------------------------------------------------------------------


def test_companies_are_spelled_as_in_companies_csv_and_no_file_is_no_companies(tmp_path):
    (tmp_path / "monitors.csv").write_text("\ufeff# name,domain\n Acme ,https://www.Acme.com/,extra\n\nBeta,beta.io\n")

    assert runs.read_monitors(tmp_path / "monitors.csv") == COMPANIES
    assert runs.read_monitors(tmp_path / "missing.csv") == []


@pytest.mark.parametrize(
    "text, message",
    [
        ("Acme\n", "needs a name and a domain"),
        (",acme.com\n", "needs a name and a domain"),
        ("Acme,acme.com/careers\n", "needs a name and a domain"),
        ("Acme,localhost\n", "needs a name and a domain"),
        ("Acme,acme .com\n", "needs a name and a domain"),
        ("Acme,acme.com\nAcme EU,www.acme.com\n", "acme.com is listed twice"),
        ("Acme," + "x" * 200_000 + ".com\n", "cannot be read"),  # beyond csv's field limit
    ],
)
def test_a_row_that_cannot_name_a_monitor_is_refused_not_skipped(tmp_path, text, message):
    """monitor-sync deletes the monitor of every company the list leaves out: a skipped row would lose one."""
    (tmp_path / "monitors.csv").write_text(text)

    with pytest.raises(ValueError, match=message):
        runs.read_monitors(tmp_path / "monitors.csv")


def test_add_monitor_appends_one_row_that_reads_back_and_refuses_one_listed_already(tmp_path):
    path = tmp_path / "monitors.csv"
    assert runs.add_monitor(path, "Acme, Inc.", "https://www.Acme.com/") == ("Acme, Inc.", "acme.com")  # created
    path.write_text(path.read_text().rstrip("\n"))  # an editor that leaves no newline at the end

    assert runs.add_monitor(path, "Beta", "beta.io") == ("Beta", "beta.io")
    assert runs.read_monitors(path) == [("Acme, Inc.", "acme.com"), ("Beta", "beta.io")]
    with pytest.raises(ValueError, match="acme.com is monitored already"):
        runs.add_monitor(path, "Acme EU", "acme.com")
    assert runs.read_monitors(path) == [("Acme, Inc.", "acme.com"), ("Beta", "beta.io")]


# The owner's own file: a byte-order mark, comments, a quoted name with a comma and one across two lines, an extra
# column, Windows line endings and no newline at the end.
HAND_WRITTEN = ('\ufeff# name,domain,notes\r\n"Acme, Inc.",acme.com,keep this\r\n\r\n"Beta\r\nLabs",beta.io\r\n'
                "# paused: Gamma,gamma.dev\r\nDelta,delta.ai")


def test_edit_monitor_changes_one_row_and_keeps_every_other_line_as_written(tmp_path):
    path = tmp_path / "monitors.csv"
    path.write_bytes(HAND_WRITTEN.encode())

    assert runs.edit_monitor(path, "acme.com", " Acme  EU ", "https://www.Acme.eu/") == ("Acme EU", "acme.eu")
    assert runs.edit_monitor(path, "https://delta.ai", "Delta AI", "delta.ai") == ("Delta AI", "delta.ai")

    assert path.read_bytes().decode() == ('# name,domain,notes\r\nAcme EU,acme.eu,keep this\r\n\r\n"Beta\r\nLabs",beta.io\r\n'
                                          "# paused: Gamma,gamma.dev\r\nDelta AI,delta.ai")
    assert runs.read_monitors(path) == [("Acme EU", "acme.eu"), ("Beta Labs", "beta.io"), ("Delta AI", "delta.ai")]
    assert [p.name for p in tmp_path.iterdir()] == ["monitors.csv"]  # written whole, through a file of its own


@pytest.mark.parametrize(
    "current, name, domain, message",
    [
        ("gamma.dev", "Gamma", "gamma.dev", "gamma.dev is not listed"),  # in a comment only
        ("acme.com", "Acme", "https://beta.io", "beta.io is monitored already"),
        ("acme.com", " Acme,  Inc. ", "www.acme.com", "is listed so already"),
        ("acme.com", "Acme", "acme.com/careers", "needs a name and a domain"),
        ("acme.com", "# Acme", "acme.com", "needs a name and a domain"),  # the file would read a comment
    ],
)
def test_edit_monitor_refuses_a_change_it_cannot_make_and_leaves_the_file_as_it_was(tmp_path, current, name, domain,
                                                                                   message):
    path = tmp_path / "monitors.csv"
    path.write_bytes(HAND_WRITTEN.encode())

    with pytest.raises(ValueError, match=message):
        runs.edit_monitor(path, current, name, domain)
    assert path.read_bytes() == HAND_WRITTEN.encode()


def test_remove_monitors_deletes_their_rows_and_keeps_every_other_line_as_written(tmp_path):
    path = tmp_path / "monitors.csv"
    path.write_bytes(HAND_WRITTEN.encode())

    assert runs.remove_monitors(path, ["https://www.Delta.ai/", "beta.io"]) == [("Beta Labs", "beta.io"), ("Delta", "delta.ai")]
    assert path.read_bytes().decode() == '# name,domain,notes\r\n"Acme, Inc.",acme.com,keep this\r\n\r\n# paused: Gamma,gamma.dev\r\n'

    for domains, message in ((["acme.com", "gamma.dev"], "not listed: gamma.dev"), (["", " "], "no company to remove")):
        with pytest.raises(ValueError, match=message):
            runs.remove_monitors(path, domains)
    assert runs.read_monitors(path) == [("Acme, Inc.", "acme.com")]

    assert runs.remove_monitors(path, ["acme.com"]) == [("Acme, Inc.", "acme.com")]
    assert runs.read_monitors(path) == []  # comments only: the sync deletes every monitor
    assert [p.name for p in tmp_path.iterdir()] == ["monitors.csv"]


@pytest.mark.parametrize("address", ["http://127.0.0.1:8000", "https://localhost:8000", "http://hunt.example.org", "hunt.example.org"])
def test_an_address_exa_would_refuse_for_a_monitor_is_refused_before_asking(address):
    with pytest.raises(ValueError, match="DASHBOARD_URL"):
        monitor.webhook_url(address)
    assert monitor.webhook_url(WEBHOOK) == WEBHOOK


# Sync --------------------------------------------------------------------------------------------------------


def test_sync_creates_a_monitor_per_company_that_exa_runs_by_itself(account, profile):
    state, saved = {"monitors": {}}, []

    lines, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: saved.append(list(state["monitors"])))

    acme, beta = account.calls("POST")
    assert acme["search"]["query"] == "New job openings at Acme (acme.com) for any of: analytics engineer, growth engineer."
    assert acme["search"]["includeDomains"][:3] == ["acme.com", "*.acme.com", "jobs.ashbyhq.com"]
    assert acme["search"]["contents"] == {"text": {"maxCharacters": research.DESCRIPTION_CHARS}}
    assert acme["trigger"] == {"type": "interval", "period": "12h"}
    assert acme["outputSchema"] == research.ATS_SCHEMA
    assert acme["webhook"] == {"url": WEBHOOK, "events": ["monitor.deleted"]}  # nothing is pushed while it runs
    assert "Beta (beta.io)" in beta["search"]["query"]
    assert acme["metadata"] == {"source": "hunt.py", "folder": state["folder"], "domain": "acme.com"}
    assert [state["monitors"][domain]["id"] for domain in ("acme.com", "beta.io")] == ["mon_1", "mon_2"]
    # the folder's id before any create, then after each create: an interrupted sync loses track of none
    assert saved == [[], ["acme.com"], ["acme.com", "beta.io"]]
    next_run = f"{datetime.fromisoformat(NEXT_RUN).astimezone():%Y-%m-%d %H:%M}"
    assert all("created" in line and next_run in line for line in lines) and len(lines) == 2
    assert problems == []


def test_sync_leaves_a_matching_monitor_alone_and_sends_only_what_changed(account, profile, state):
    monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)
    assert len(account.calls("POST")) == 2 and account.calls("PATCH", "/mon_1") == []

    profile["search"]["roles"] = ["data engineer"]
    lines, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)

    (changed,) = account.calls("PATCH", "/mon_1")
    assert list(changed) == ["search"]  # the trigger is not sent again: the monitor keeps its cycle
    assert account.monitors["mon_1"]["search"]["query"].endswith("for any of: data engineer.")
    assert state["monitors"]["acme.com"]["params"]["search"]["query"].endswith("for any of: data engineer.")
    assert len(account.calls("POST")) == 2 and problems == [] and "updated (search)" in lines[0]


def test_sync_moves_monitors_made_with_another_period_to_this_one(account, profile, monkeypatch):
    """A monitor keeps the period it was created with until a sync sends PERIOD: the first sync after a change of
    PERIOD moves every monitor to it (Exa bills each run), keeping the monitors and what they judged."""
    period = monitor.PERIOD
    monkeypatch.setattr(monitor, "PERIOD", "7d" if period != "7d" else "1d")  # monitors made before PERIOD changed
    state = {"monitors": {}}
    monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)
    monkeypatch.setattr(monitor, "PERIOD", period)
    judged = {"last_run": "run_9", "seen": [{"title": "Senior Analytics Engineer", "url": AE, "verdict": "apply",
                                             "score": 82, "first_seen": "2026-11-20T08:00:00"}]}
    state["monitors"]["acme.com"].update(judged)

    lines, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)

    assert [account.calls("PATCH", f"/{mid}") for mid in ("mon_1", "mon_2")] == [
        [{"trigger": {"type": "interval", "period": period}}]] * 2
    assert account.monitors["mon_1"]["trigger"] == {"type": "interval", "period": period}
    acme = state["monitors"]["acme.com"]
    assert [entry["params"]["trigger"]["period"] for entry in state["monitors"].values()] == [period] * 2
    assert (acme["id"], {key: acme[key] for key in judged}) == ("mon_1", judged)
    assert len(account.calls("POST")) == 2 and problems == []  # updated, not created again
    assert all("updated (trigger)" in line for line in lines)


def test_sync_deletes_the_monitor_of_a_company_no_longer_listed_and_keeps_it_while_exa_fails(account, profile, state):
    account.failures["delete"] = [error(400, "not now")]
    _, problems = monitor.sync(COMPANIES[:1], profile, WEBHOOK, state, lambda: None)
    assert "Beta (beta.io)" in problems[0] and "beta.io" in state["monitors"]  # still known: the next sync tries again

    lines, problems = monitor.sync(COMPANIES[:1], profile, WEBHOOK, state, lambda: None)
    assert list(account.monitors) == ["mon_1"] and list(state["monitors"]) == ["acme.com"]
    assert lines[0] == "Beta (beta.io): deleted" and problems == []


def test_sync_creates_a_monitor_again_when_exa_no_longer_has_it_and_keeps_what_was_judged(account, profile, state):
    judged = [{"title": "Analytics Engineer", "url": AE}]
    state["monitors"]["acme.com"] |= {"last_run": "run_9", "seen": judged}
    del account.monitors["mon_1"]  # deleted on Exa's dashboard

    monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)

    entry = state["monitors"]["acme.com"]
    assert entry["id"] == "mon_3" and entry["seen"] == judged
    assert "last_run" not in entry  # the new monitor has runs of its own


def test_sync_goes_on_after_a_company_it_could_not_create_a_monitor_for(account, profile):
    account.failures["create"] = [requests.Timeout("slow")]
    state = {"monitors": {}}

    lines, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)

    assert list(state["monitors"]) == ["beta.io"] and "Beta (beta.io): created" in lines[0]
    assert "Acme (acme.com)" in problems[0] and "may have been created" in problems[0]
    assert len(account.calls("POST")) == 2  # one each: the create that timed out is not sent again


def test_sync_reports_a_monitor_exa_stopped_running_and_leaves_what_is_not_this_folders_alone(account, profile, state):
    account.monitors["mon_2"]["status"] = "paused"
    stray = exa_client.create_monitor(monitor.params("Gone", "gone.dev", profile, WEBHOOK, "a-lost-state-files-id"))
    theirs = exa_client.create_monitor({**monitor.params("Lead", "lead.dev", profile, WEBHOOK, "x"), "metadata": {"team": "sales"}})

    lines, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)

    assert any("Beta (beta.io)" in problem and "paused" in problem for problem in problems)
    assert any(stray.id in problem for problem in problems)  # billed, and only the owner can tell whose it is
    assert not any(theirs.id in text for text in lines + problems)  # not this program's
    assert set(account.monitors) == {"mon_1", "mon_2", stray.id, theirs.id}  # reported, never deleted


def test_sync_takes_over_a_monitor_of_its_own_that_it_lost_track_of_and_leaves_none_unaccounted_for(account, profile, state):
    # the reply to a create was lost, or the state file is an older copy: this folder's monitors run on Exa unlisted
    folder = state["folder"]
    lost = state["monitors"].pop("beta.io")["id"]
    second = exa_client.create_monitor(monitor.params("Acme", "acme.com", profile, WEBHOOK, folder)).id
    dropped = exa_client.create_monitor(monitor.params("Gone", "gone.dev", profile, WEBHOOK, folder)).id
    profile["search"]["roles"] = ["data engineer"]

    lines, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)

    assert state["monitors"]["beta.io"]["id"] == lost and "taken over" in lines[1]  # not a second monitor for Beta
    (changed,) = account.calls("PATCH", f"/{lost}")
    assert list(changed) == ["search"]  # brought up to date with what differs on Exa, nothing else
    assert set(account.monitors) == {"mon_1", lost}  # Acme's second one and the one nothing lists are deleted
    assert all(any(gone in line and "deleted" in line for line in lines) for gone in (second, dropped))
    assert len(account.calls("POST")) == 4 and problems == []  # the fixture's two and the two above: none created


def test_a_company_whose_monitor_could_not_be_looked_at_keeps_its_untracked_one(account, profile, state):
    # Acme has a second monitor of this folder's, and Exa does not answer for its first: which is to stay is not known
    second = exa_client.create_monitor(monitor.params("Acme", "acme.com", profile, WEBHOOK, state["folder"])).id
    account.failures["get"] = [error(500)] * exa_client.MAX_ATTEMPTS

    _, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)
    assert second in account.monitors and len(problems) == 1 and "Acme (acme.com)" in problems[0]

    _, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)  # Exa answers: mon_1 is there
    assert set(account.monitors) == {"mon_1", "mon_2"} and problems == []


def test_sync_never_touches_another_data_folders_monitor_of_the_same_company(account, profile, state):
    # a trial on the laptop, then Railway: one Exa account, two state files
    other, there = {"monitors": {}}, "https://other.example.org"

    lines, problems = monitor.sync(COMPANIES[:1], profile, there, other, lambda: None)

    assert other["monitors"]["acme.com"]["id"] == "mon_3" and "created" in lines[0]  # its own: mon_1 is not taken over
    assert account.monitors["mon_1"]["webhook"]["url"] == WEBHOOK and account.calls("PATCH", "/mon_1") == []
    assert len(problems) == 2 and "mon_1" in problems[0] and "mon_2" in problems[1]  # told about them, since they are billed

    monitor.sync([], profile, None, other, lambda: None)  # the other folder drops the company
    assert set(account.monitors) == {"mon_1", "mon_2"}

    _, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)
    assert problems == [] and len(account.calls("POST")) == 3  # this folder's two are as they were


def test_sync_changes_nothing_when_it_cannot_see_which_monitors_exist(account, profile):
    account.failures["list"] = [error(400, "not now")]
    state = {"monitors": {}}

    lines, problems = monitor.sync(COMPANIES, profile, WEBHOOK, state, lambda: None)

    assert (lines, state["monitors"]) == ([], {}) and "nothing was changed" in problems[0]
    assert account.calls("POST") == []  # a create could give a company a second monitor


# Findings ----------------------------------------------------------------------------------------------------


def test_findings_are_the_new_postings_of_the_runs_since_the_last_check(account, http, state):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}, {"id": "2222"}]})
    account.add_run("mon_1", found_run("run_1", posting("Old posting", "https://jobs.ashbyhq.com/acme/0000")))
    account.add_run("mon_1", monitor_run("run_2"))  # found nothing new
    account.add_run("mon_1", found_run(
        "run_3",
        posting("Senior Analytics Engineer", AE),
        posting("Data Engineer", "https://jobs.ashbyhq.com/other/9999", verified=False),  # another company's
        posting("Growth Engineer", "https://aggregator.io/jobs/7"),  # a third-party copy
        posting("Account Executive", SALES),  # judged by an earlier check
        texts={AE: "dbt, Snowflake, Looker."},
    ))
    account.add_run("mon_1", found_run("run_4", posting("Senior Analytics Engineer", "https://jobs.ashbyhq.com/Acme/1111/application")))
    account.add_run("mon_1", monitor_run("run_5", status="running"))
    entry = state["monitors"]["acme.com"] | {"last_run": "run_1", "seen": [{"title": "Account Executive", "url": SALES}]}

    found, dead, progress, problems = monitor.findings("acme.com", entry)

    (one,) = found  # run_4 lists the same posting under another form of its link
    assert one["content"]["open_roles"] == [{
        "title": "Senior Analytics Engineer", "url": AE, "location": "Remote", "remote_or_relocation": "Remote (EU)",
        "description": "dbt, Snowflake, Looker.", "link_status": "live"}]
    assert (one["name"], one["domain"], one["content"]["identity_verified"]) == ("Acme", "acme.com", True)
    assert one["grounding"] == {"open_roles": {"confidence": "high", "urls": [AE]}}
    assert (dead, problems) == ([], [])
    assert progress == {"last_run": "run_4", "unread": {}}  # run_5 is still going: the next check reads it
    assert account.calls("GET", "/mon_1/runs/run_2") == []  # the list says it found nothing: not read
    assert len(account.calls("GET", "/mon_1/runs/run_3")) == 1


def test_findings_leave_out_closed_postings_fetch_a_text_no_run_has_and_report_runs_that_went_wrong(account, exa, http, state):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}]})
    careers = result("https://acme.com/careers", title="Careers at Acme", text="All our openings.")  # not a posting's page
    account.add_run("mon_1", monitor_run("run_1", results=[careers], content={
        "open_roles": [posting("Senior Analytics Engineer", AE), posting("Closed role", "https://jobs.ashbyhq.com/acme/0000")]}))
    account.add_run("mon_1", monitor_run("run_2", status="failed", fail_reason="insufficient_credits"))
    account.add_run("mon_1", monitor_run("run_3", results=[careers], content="Acme is hiring."))  # not the schema's answer
    exa.on_contents(contents_response([result(AE, text="Fetched posting text.")]))

    found, dead, progress, problems = monitor.findings("acme.com", state["monitors"]["acme.com"])

    assert [one["content"]["open_roles"][0]["description"] for one in found] == ["Fetched posting text."]
    assert found[0]["grounding"] == {}  # the run cited nothing
    assert dead == [{"title": "Closed role", "url": "https://jobs.ashbyhq.com/acme/0000"}]
    assert progress["last_run"] == "run_3"
    assert len(problems) == 2 and "insufficient_credits" in problems[0] and "run_3" in problems[1]


def test_a_check_after_a_long_pause_reads_the_latest_runs_only(account, state):
    for n in range(monitor.MAX_RUNS + 5):
        account.add_run("mon_1", monitor_run(f"run_{n}"))

    found, _, progress, _ = monitor.findings("acme.com", state["monitors"]["acme.com"])

    assert (found, progress["last_run"]) == ([], f"run_{monitor.MAX_RUNS + 4}")
    assert len(account.calls("GET", "/mon_1/runs")) == monitor.MAX_RUNS // exa_client.MONITORS_PAGE  # no page more


def test_a_run_that_cannot_be_read_is_tried_again_then_given_up_and_never_blocks_for_good(account, http, state):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}, {"id": "2222"}]})
    account.add_run("mon_1", found_run("run_1", posting("Senior Analytics Engineer", AE)))
    account.add_run("mon_1", found_run("run_2", posting("Never read", "https://jobs.ashbyhq.com/acme/0000")))
    account.add_run("mon_1", found_run("run_3", posting("Lead Growth Engineer", SALES)))
    account.unreadable.add("run_2")  # Exa answers 500 for it, however often it is asked
    entry = state["monitors"]["acme.com"]

    def check():
        found, _, progress, problems = monitor.findings("acme.com", entry)
        monitor.advance(entry, progress)
        return [one["content"]["open_roles"][0]["title"] for one in found], problems

    titles, problems = check()
    assert titles == ["Senior Analytics Engineer"] and "tries again" in problems[0]  # run_1's is not held back
    assert (entry["last_run"], entry["unread"]) == ("run_1", {"run_2": 1})  # run_3 waits its turn
    for failed_checks in range(2, monitor.MAX_TRIES):
        assert check()[0] == [] and entry["unread"] == {"run_2": failed_checks}

    titles, problems = check()

    assert titles == ["Lead Growth Engineer"] and "given up" in problems[0]  # run_3, at last
    assert entry["last_run"] == "run_3" and "unread" not in entry


def test_a_run_still_going_behind_a_later_one_is_reported_and_not_waited_for(account, http, state):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}]})
    account.add_run("mon_1", monitor_run("run_1", status="running"))  # Exa never ended it
    account.add_run("mon_1", found_run("run_2", posting("Senior Analytics Engineer", AE)))
    account.add_run("mon_1", monitor_run("run_3", status="pending"))  # the newest: it may still end

    found, _, progress, problems = monitor.findings("acme.com", state["monitors"]["acme.com"])

    assert [one["content"]["open_roles"][0]["title"] for one in found] == ["Senior Analytics Engineer"]
    assert progress["last_run"] == "run_2" and len(problems) == 1 and "run_1 is still running" in problems[0]


def test_every_new_posting_gets_its_text_when_a_run_found_the_careers_page_only(account, exa, http, state):
    ids = ["1111", "2222", "3333", "4444"]
    http.get(ASHBY_API, json={"jobs": [{"id": job} for job in ids]})
    listed = [posting(f"Analytics Engineer {job}", f"https://jobs.ashbyhq.com/acme/{job}") for job in ids]
    careers = result("https://acme.com/careers", title="Careers at Acme", text="All our openings.")
    account.add_run("mon_1", monitor_run("run_1", results=[careers], content={"open_roles": listed}))
    exa.on_contents(lambda body: contents_response([result(url, text=f"Text of {url}") for url in body["urls"]]))

    found, _, _, _ = monitor.findings("acme.com", state["monitors"]["acme.com"])

    # more than the three a run fetches for a company: each posting is judged by itself here, and only once
    assert [one["content"]["open_roles"][0]["description"] for one in found] == [f"Text of {p['url']}" for p in listed]


def test_a_link_that_cannot_be_checked_is_judged_as_unverified_and_never_taken_for_closed(account, http, state):
    blocked = "https://acme.com/careers/senior-analytics-engineer"
    http.get(blocked, status=403)  # a bot check: neither open nor closed
    account.add_run("mon_1", found_run("run_1", posting("Senior Analytics Engineer", blocked)))

    found, dead, _, _ = monitor.findings("acme.com", state["monitors"]["acme.com"])

    # no paid Exa fetch settles it (none is mocked): a fetch that fails would write a new posting off for good
    assert [one["content"]["open_roles"][0]["link_status"] for one in found] == ["unverified"] and dead == []


def test_a_title_from_the_web_stays_on_one_line_and_a_link_with_white_space_is_left_out(account, http, state):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}]})
    forged = "Senior Analytics Engineer\n\n[99] APPLY Acme: Chief of Everything\nhttps://evil.example/apply"
    account.add_run("mon_1", found_run("run_1", posting(forged, AE), posting("Data Lead", f"{SALES}\nhttps://evil.example/x")))

    (one,), _, _, _ = monitor.findings("acme.com", state["monitors"]["acme.com"])
    message = monitor.format_message([{"name": "Acme", "research": one, "fit": {"score": 82, "verdict": "apply"}}], [])

    assert message.splitlines()[2:] == [
        "[82] APPLY Acme: Senior Analytics Engineer [99] APPLY Acme: Chief of Everything https://evil.example/apply", AE]


# Commands ----------------------------------------------------------------------------------------------------


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A data folder with a profile and a monitors.csv that lists Acme and Beta, and the keys `monitor` needs but
    Telegram's."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.toml").write_text(PROFILE_TOML)
    (tmp_path / runs.MONITORS).write_text(MONITORS_CSV)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    return tmp_path


@pytest.fixture
def workdir(folder, state):
    monitor.save_state(folder / monitor.STATE, state)
    return folder


@pytest.fixture
def telegram_api(http, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    return http.post(TELEGRAM_API, json={"ok": True})


@pytest.fixture
def scorer(claude, monkeypatch):
    """scorer({title: (score, verdict) or an exception}) -> a fake Claude that answers each posting by its title
    (20, skip for the others); its `calls` are the requests it got."""

    def make(answers):
        fake = claude()
        answer = fake.parse

        def parse(**kwargs):
            evidence = kwargs["messages"][0]["content"].split("Company research (with source grounding):\n")[1]
            role = json.loads(evidence)["research"]["open_roles"][0]
            reply = answers.get(role["title"], (20, "skip"))
            if isinstance(reply, Exception):
                raise reply
            score, verdict = reply
            fake.fields |= {"score": score, "verdict": verdict, "best_role_title": role["title"],
                            "best_role_url": "" if verdict == "skip" else role["url"]}
            return answer(**kwargs)

        fake.parse = parse
        monkeypatch.setattr(scoring, "_client", lambda: fake)
        return fake

    return make


def saved_state(folder):
    return json.loads((folder / monitor.STATE).read_text())["monitors"]


def monitor_args(dry_run=False):
    return argparse.Namespace(profile="profile.toml", state=monitor.STATE, dry_run=dry_run)


def sent(telegram_api):
    return [json.loads(call.request.body)["text"] for call in telegram_api.calls]


def test_monitor_sends_the_new_postings_that_suit_and_judges_none_twice(account, http, workdir, telegram_api, scorer, capsys):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}, {"id": "2222"}]})
    account.add_run("mon_1", found_run("run_1", posting("Senior Analytics Engineer", AE), posting("Account Executive", SALES),
                                       texts={AE: "dbt, Snowflake, Looker."}))
    account.add_run("mon_2", monitor_run("run_1"))  # Beta: nothing new
    fake = scorer({"Senior Analytics Engineer": (82, "apply")})

    assert hunt.cmd_monitor(monitor_args()) == 0

    (text,) = sent(telegram_api)
    assert f"[82] APPLY Acme: Senior Analytics Engineer\n{AE}" in text and "Account Executive" not in text
    assert "dbt, Snowflake, Looker." in fake.calls[0]["messages"][0]["content"]  # the posting's text reaches the scorer
    assert len(fake.calls) == 2  # one call per new posting
    saved = saved_state(workdir)
    assert [(s["title"], s["verdict"], s["score"]) for s in saved["acme.com"]["seen"]] == [
        ("Senior Analytics Engineer", "apply", 82), ("Account Executive", "skip", 20)]
    assert (saved["acme.com"]["last_run"], saved["beta.io"]["last_run"]) == ("run_1", "run_1")
    assert "1 of 2 new posting(s) from 2/2 monitor(s) are worth a look" in capsys.readouterr().out

    # a later run lists the same postings again, one under another form of its link
    account.add_run("mon_1", found_run("run_2", posting("Senior Analytics Engineer", "https://jobs.ashbyhq.com/Acme/1111/application"),
                                       posting("Account Executive", SALES)))
    assert hunt.cmd_monitor(monitor_args()) == 0
    assert len(fake.calls) == 2 and len(telegram_api.calls) == 1
    assert saved_state(workdir)["acme.com"]["last_run"] == "run_2"


def test_monitor_dry_run_prints_what_it_would_send_and_changes_nothing(account, http, workdir, scorer, capsys):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}]})
    account.add_run("mon_1", found_run("run_1", posting("Senior Analytics Engineer", AE)))
    scorer({"Senior Analytics Engineer": (82, "apply")})
    before = (workdir / monitor.STATE).read_text()

    assert hunt.cmd_monitor(monitor_args(dry_run=True)) == 0  # no Telegram keys, and no request to Telegram

    assert f"[82] APPLY Acme: Senior Analytics Engineer\n{AE}" in capsys.readouterr().out
    assert (workdir / monitor.STATE).read_text() == before


def test_a_posting_that_could_not_be_scored_is_read_again_and_the_others_are_not(account, http, workdir, telegram_api, scorer):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}, {"id": "2222"}]})
    account.add_run("mon_1", found_run("run_1", posting("Senior Analytics Engineer", AE), posting("Lead Growth Engineer", SALES)))
    answers = {"Senior Analytics Engineer": (82, "apply"), "Lead Growth Engineer": RuntimeError("overloaded")}
    fake = scorer(answers)

    assert hunt.cmd_monitor(monitor_args()) == 1

    (text,) = sent(telegram_api)
    assert "Senior Analytics Engineer" in text and "Could not check:\n- Acme: Lead Growth Engineer: RuntimeError: overloaded" in text
    entry = saved_state(workdir)["acme.com"]
    assert [s["title"] for s in entry["seen"]] == ["Senior Analytics Engineer"] and "last_run" not in entry

    answers["Lead Growth Engineer"] = (64, "consider")
    assert hunt.cmd_monitor(monitor_args()) == 0

    assert f"[64] CONSIDER Acme: Lead Growth Engineer\n{SALES}" in sent(telegram_api)[1]
    assert "Senior Analytics Engineer" not in sent(telegram_api)[1] and len(fake.calls) == 2
    entry = saved_state(workdir)["acme.com"]
    assert entry["last_run"] == "run_1" and "unscored" not in entry


def test_a_posting_that_never_scores_and_a_run_that_never_reads_are_given_up_and_hold_nothing_back(account, http, workdir,
                                                                                                  telegram_api, scorer):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}, {"id": "2222"}]})
    account.add_run("mon_1", found_run("run_1", posting("Lead Growth Engineer", SALES)))
    account.add_run("mon_1", found_run("run_2", posting("Never read", "https://jobs.ashbyhq.com/acme/0000")))
    account.add_run("mon_1", found_run("run_3", posting("Senior Analytics Engineer", AE)))
    account.unreadable.add("run_2")
    scorer({"Senior Analytics Engineer": (82, "apply"), "Lead Growth Engineer": RuntimeError("refused")})

    codes = [hunt.cmd_monitor(monitor_args()) for _ in range(monitor.MAX_TRIES + 1)]

    assert codes == [1] * monitor.MAX_TRIES + [0]  # reported while they are tried, then left alone
    (text,) = sent(telegram_api)  # run_3's posting, once the run before it was given up
    assert f"[82] APPLY Acme: Senior Analytics Engineer\n{AE}" in text
    assert f"Lead Growth Engineer: RuntimeError: refused; given up after {monitor.MAX_TRIES} checks" in text
    entry = saved_state(workdir)["acme.com"]
    assert [(s["title"], s["verdict"]) for s in entry["seen"]] == [
        ("Lead Growth Engineer", "unscored"), ("Senior Analytics Engineer", "apply")]
    assert entry["last_run"] == "run_3" and "unscored" not in entry and "unread" not in entry


def test_a_run_given_up_is_not_tried_anew_while_a_posting_behind_it_is_scored_again(account, http, workdir, telegram_api,
                                                                                     scorer):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}, {"id": "2222"}]})
    account.add_run("mon_1", found_run("run_1", posting("Never read", "https://jobs.ashbyhq.com/acme/0000")))
    account.add_run("mon_1", found_run("run_2", posting("Lead Growth Engineer", SALES)))
    account.add_run("mon_1", found_run("run_3", posting("Senior Analytics Engineer", AE)))
    account.unreadable.add("run_1")
    scorer({"Senior Analytics Engineer": (82, "apply"), "Lead Growth Engineer": RuntimeError("refused")})

    codes = [hunt.cmd_monitor(monitor_args()) for _ in range(2 * monitor.MAX_TRIES)]

    # three checks give the run up, and the posting behind it its three, the first of them in the run's last
    assert codes == [1] * (2 * monitor.MAX_TRIES - 1) + [0]
    (text,) = sent(telegram_api)  # run_3's posting goes out with the check that gave run_1 up, not after the posting's
    assert f"[82] APPLY Acme: Senior Analytics Engineer\n{AE}" in text
    assert len(account.calls("GET", "/mon_1/runs/run_1")) == monitor.MAX_TRIES * exa_client.MAX_ATTEMPTS  # then never again
    entry = saved_state(workdir)["acme.com"]
    assert entry["last_run"] == "run_3" and "unread" not in entry and "unscored" not in entry


def test_a_posting_that_failed_to_score_and_has_closed_since_is_no_longer_counted():
    entry = {"unscored": {SALES: 1, AE: 2}}

    monitor.remember(entry, [], [{"title": "Lead Growth Engineer", "url": SALES}])

    assert entry["unscored"] == {AE: 2} and entry["seen"][0]["verdict"] == "closed"


def test_a_monitor_that_cannot_be_read_does_not_stop_the_others(account, http, workdir, telegram_api, scorer):
    del account.monitors["mon_1"]  # Acme's was deleted on Exa's dashboard
    role = "https://beta.io/careers/senior-growth-engineer"
    http.get(role, body="<h1>Senior Growth Engineer</h1>")
    account.add_run("mon_2", found_run("run_1", posting("Senior Growth Engineer", role)))
    scorer({"Senior Growth Engineer": (77, "apply")})

    assert hunt.cmd_monitor(monitor_args()) == 1

    (text,) = sent(telegram_api)
    assert f"[77] APPLY Beta: Senior Growth Engineer\n{role}" in text and "Could not check:\n- Acme: " in text
    assert saved_state(workdir)["beta.io"]["last_run"] == "run_1"


def test_monitor_sends_nothing_for_a_posting_jev_rules_out_and_never_asks_claude(account, http, workdir, telegram_api, scorer, jev_api):
    http.get(ASHBY_API, json={"jobs": [{"id": "2222"}]})
    account.add_run("mon_1", found_run("run_1", posting("Account Executive", SALES)))
    jev_api.on(lambda state, key, question: [0, 0.05, 0.9, 0.05] if key == "seniority" else 0.03 if key == "target_role" else 0.5)
    fake = scorer({})

    assert hunt.cmd_monitor(monitor_args()) == 0

    assert fake.calls == [] and sent(telegram_api) == []
    (judged,) = saved_state(workdir)["acme.com"]["seen"]
    assert (judged["title"], judged["verdict"], judged["score"]) == ("Account Executive", "skip", 0)


def test_monitor_saves_nothing_when_telegram_refuses_the_message(account, http, workdir, monkeypatch, scorer):
    http.get(ASHBY_API, json={"jobs": [{"id": "1111"}]})
    account.add_run("mon_1", found_run("run_1", posting("Senior Analytics Engineer", AE)))
    scorer({"Senior Analytics Engineer": (82, "apply")})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    http.post(TELEGRAM_API, json={"ok": False, "error_code": 400, "description": "chat not found"})
    before = (workdir / monitor.STATE).read_text()

    with pytest.raises(RuntimeError, match="chat not found"):
        hunt.cmd_monitor(monitor_args())

    assert (workdir / monitor.STATE).read_text() == before  # the posting is judged and sent again by the next check


def test_monitor_needs_a_sync_and_telegram_before_it_asks_exa(http, folder, state):
    with pytest.raises(SystemExit, match="monitor-sync"):
        hunt.cmd_monitor(monitor_args())

    monitor.save_state(folder / monitor.STATE, state)
    requests_so_far = len(http.calls)
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set"):
        hunt.cmd_monitor(monitor_args())
    assert len(http.calls) == requests_so_far


def sync_args():
    return argparse.Namespace(profile="profile.toml", companies=runs.MONITORS, state=monitor.STATE)


def test_monitor_sync_refuses_a_dashboard_address_exa_would_not_take_before_any_request(http, folder):
    with pytest.raises(SystemExit, match="public HTTPS address"):  # the default dashboard address is this machine's
        hunt.cmd_monitor_sync(sync_args())
    assert len(http.calls) == 0


def test_monitor_sync_gives_the_monitors_the_railway_address_and_deletes_them_when_none_is_listed(account, folder, monkeypatch, capsys):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "hunt.up.railway.app")

    assert hunt.cmd_monitor_sync(sync_args()) == 0

    assert [body["webhook"]["url"] for body in account.calls("POST")] == ["https://hunt.up.railway.app"] * 2
    assert [entry["id"] for entry in saved_state(folder).values()] == ["mon_1", "mon_2"]
    assert "2 monitor(s)" in capsys.readouterr().out

    (folder / runs.MONITORS).write_text("# name,domain\n")  # every company taken off the list
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN")  # deleting needs no address

    assert hunt.cmd_monitor_sync(sync_args()) == 0
    assert account.monitors == {} and saved_state(folder) == {}

    # one of this folder's is left on Exa by a create whose reply was lost: tidied up even with nothing listed
    folder_id = json.loads((folder / monitor.STATE).read_text())["folder"]
    exa_client.create_monitor(monitor.params("Acme", "acme.com", {"search": {"roles": ["x"]}}, "https://hunt.example.org", folder_id))
    assert hunt.cmd_monitor_sync(sync_args()) == 0
    assert account.monitors == {} and "lists none" in capsys.readouterr().out


def test_monitor_sync_with_nothing_listed_and_nothing_ever_synced_exits_before_any_request(http, folder):
    (folder / runs.MONITORS).unlink()

    with pytest.raises(SystemExit, match="monitors.csv: no companies listed"):
        hunt.cmd_monitor_sync(sync_args())
    assert len(http.calls) == 0


def test_monitor_sync_changes_nothing_while_the_profile_still_lists_the_companies(http, folder, state):
    """[monitor].companies is no longer read: a sync on monitors.csv alone would delete the monitors it lists."""
    monitor.save_state(folder / monitor.STATE, state)
    (folder / "profile.toml").write_text(PROFILE_TOML + '[monitor]\ncompanies = [{ name = "Acme", domain = "acme.com" }]\n')
    requests_so_far, before = len(http.calls), (folder / monitor.STATE).read_text()

    with pytest.raises(SystemExit, match=r"\[monitor\].companies is no longer read. Move each company to monitors.csv"):
        hunt.cmd_monitor_sync(sync_args())

    assert len(http.calls) == requests_so_far and (folder / monitor.STATE).read_text() == before


@pytest.mark.parametrize("text", [None, "Acme,acme.com\nAcme EU,acme.com\n"])
def test_monitor_sync_changes_nothing_without_a_monitors_csv_it_can_read(http, folder, state, text):
    """A missing or broken list is not an empty one: the monitors that run on Exa are left alone."""
    monitor.save_state(folder / monitor.STATE, state)
    (folder / runs.MONITORS).unlink()
    if text:
        (folder / runs.MONITORS).write_text(text)
    requests_so_far, before = len(http.calls), (folder / monitor.STATE).read_text()

    with pytest.raises(SystemExit, match=r"not found, and 2 monitor\(s\) of this folder run on Exa" if text is None
                       else "monitors.csv: acme.com is listed twice"):
        hunt.cmd_monitor_sync(sync_args())

    assert len(http.calls) == requests_so_far and (folder / monitor.STATE).read_text() == before


@pytest.mark.parametrize(
    "text",
    [
        "{ half written",
        "[]",
        '{"monitors": []}',
        '{"monitors": {"acme.com": 5}}',
        '{"monitors": {"acme.com": {"name": "Acme", "params": {}}}}',  # no id
        '{"monitors": {"acme.com": {"id": "mon_1", "name": "Acme", "params": {}, "seen": [{"title": "AE"}]}}}',  # no url
        '{"monitors": {"acme.com": {"id": "mon_1", "name": "Acme", "params": {}, "unscored": [1]}}}',
        '{"monitors": {"acme.com": {"id": "mon_1", "name": "Acme", "params": {}, "unread": {"run_1": "twice"}}}}',
        '{"folder": null, "monitors": {}}',  # every monitor without a folder id would pass for this folder's
        '{"folder": "", "monitors": {}}',
    ],
)
def test_a_state_file_that_cannot_be_read_stops_both_commands_with_the_file_named(http, folder, text):
    (folder / monitor.STATE).write_text(text)

    for command, args in ((hunt.cmd_monitor, monitor_args()), (hunt.cmd_monitor_sync, sync_args())):
        with pytest.raises(SystemExit, match="monitor_state.json: "):
            command(args)
    assert len(http.calls) == 0 and (folder / monitor.STATE).read_text() == text
