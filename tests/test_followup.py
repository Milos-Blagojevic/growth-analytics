import argparse
import json
from datetime import datetime, timedelta

import pytest

import hunt
from boards_mock import GREENHOUSE, greenhouse, greenhouse_job, teamtailor_feed
from dashboard import db, email_updates, ingest, tracking
from dashboard_data import fit, mail_update, result, write_mail, write_run
from inbox import followup

TELEGRAM_API = "https://api.telegram.org/bottok/sendMessage"
ACME_URL = "https://acme.com/careers/analytics-engineer"
BETA_URL = "https://beta.io/jobs/1"
DELTA_URL = "https://delta.ai/jobs/7"
ZETA_JOB = "https://job-boards.greenhouse.io/zeta/jobs/111"
PIPELINE_LINE = "Pipeline: http://127.0.0.1:8000/pipeline"


def days_ago(days):
    """A local time that many days back, as the dashboard and `hunt.py mail` save times."""
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def track(folder, results, items):
    """hunt.db as the dashboard leaves it: a saved run with `results`, read into the database, and `items`
    ((kind, ref, days ago)) marked applied that long ago."""
    if results:
        write_run(folder, "20261001-090000", results)
    conn = db.connect(folder)
    ingest.ingest(conn, folder)
    for kind, ref, days in items:
        tracking.update(conn, kind, ref, status="applied", applied_at=days_ago(days))
    conn.close()


def add_from_email(folder, *updates):
    """Applications added to Pipeline from their confirmation emails, as the owner's click there does."""
    write_mail(folder, "20261101-090000", list(updates))
    conn = db.connect(folder)
    ingest.ingest(conn, folder)
    for update in updates:
        email_updates.decide(conn, update["id"], "add")
    conn.close()


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A data folder with four applications: Acme's and Beta's postings, 12 and 10 days ago, Gamma without a posting,
    9 days ago, and Delta's posting, 3 days ago."""
    monkeypatch.chdir(tmp_path)
    track(tmp_path, [
        result("Acme", "acme.com", fit=fit(82, "apply", "Analytics Engineer", ACME_URL)),
        result("Beta", "beta.io", fit=fit(78, "apply", "Data Engineer", BETA_URL)),
        result("Gamma", "gamma.dev", fit=fit(60, "check_careers")),
        result("Delta", "delta.ai", fit=fit(70, "consider", "Growth Engineer", DELTA_URL)),
    ], [("posting", ACME_URL, 12), ("posting", BETA_URL, 10), ("company", "gamma.dev", 9), ("posting", DELTA_URL, 3)])
    return tmp_path


@pytest.fixture
def pages(http):
    """Acme's posting is still up, Beta's is gone. Delta's is not registered: a request for it fails the test."""
    http.get(ACME_URL, body="<h1>Analytics Engineer</h1><p>Apply now</p>")
    http.get(BETA_URL, status=404)


@pytest.fixture
def telegram_api(http, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    return http.post(TELEGRAM_API, json={"ok": True})


def followup_args(**overrides):
    return argparse.Namespace(**{"state": followup.STATE, "db": followup.DB, "dry_run": False, **overrides})


def sent(telegram_api):
    return [json.loads(call.request.body)["text"] for call in telegram_api.calls]


def printed(capsys):
    """The lines a dry run printed: the message."""
    return capsys.readouterr().out.splitlines()


# The command ---------------------------------------------------------------------------------------------------


def test_followup_sends_each_waiting_application_once_by_whether_its_posting_is_open(folder, http, pages, telegram_api,
                                                                                    capsys):
    assert hunt.cmd_followup(followup_args()) == 0

    assert sent(telegram_api) == [
        "Follow-ups: 3 application(s) without a reply for 8+ days\n"
        "\n"
        "Still open, worth a follow-up:\n"
        "- Acme: Analytics Engineer (applied 12 days ago)\n"
        f"{ACME_URL}\n"
        "\n"
        "Posting closed, move on:\n"
        "- Beta: Data Engineer (applied 10 days ago)\n"
        f"{BETA_URL}\n"
        "\n"
        "Could not check:\n"
        "- Gamma: Check careers page (applied 9 days ago)\n"
        "\n"
        f"{PIPELINE_LINE}"
    ]  # Delta has waited 3 days: not listed, and its posting is not looked at
    state = json.loads((folder / followup.STATE).read_text())
    assert set(state["nudged"]) == {f"posting:{ACME_URL}", f"posting:{BETA_URL}", "company:gamma.dev"}
    assert "Sent 3 follow-up(s) to Telegram." in capsys.readouterr().out

    requests_so_far = len(http.calls)
    assert hunt.cmd_followup(followup_args()) == 0  # each application once: nothing to report, nothing looked at

    assert "No application has waited 8 days for a reply." in capsys.readouterr().out
    assert len(http.calls) == requests_so_far and len(telegram_api.calls) == 1


def test_followup_dry_run_prints_the_message_and_saves_nothing(folder, pages, capsys):
    database = (folder / "hunt.db").read_bytes()

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0  # Telegram is neither set up nor asked

    lines = printed(capsys)
    assert lines[0] == "Follow-ups: 3 application(s) without a reply for 8+ days" and lines[-1] == PIPELINE_LINE
    assert "- Acme: Analytics Engineer (applied 12 days ago)" in lines
    assert not (folder / followup.STATE).exists()
    assert (folder / "hunt.db").read_bytes() == database  # read, never written


def test_followup_saves_nothing_when_telegram_refuses_the_message(folder, http, pages, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    http.post(TELEGRAM_API, json={"ok": False, "error_code": 400, "description": "chat not found"})

    with pytest.raises(RuntimeError, match="chat not found"):
        hunt.cmd_followup(followup_args())

    assert not (folder / followup.STATE).exists()
    capsys.readouterr()
    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0  # so the next check reports the same three
    assert printed(capsys)[0] == "Follow-ups: 3 application(s) without a reply for 8+ days"


def test_followup_without_a_dashboard_database_has_nothing_to_report(tmp_path, monkeypatch, http, capsys):
    monkeypatch.chdir(tmp_path)

    assert hunt.cmd_followup(followup_args()) == 0  # before Telegram is asked for: there is nothing to send

    assert "hunt.db: nothing is tracked yet" in capsys.readouterr().out
    assert len(http.calls) == 0 and list(tmp_path.iterdir()) == []  # no database is created, no state saved


def test_followup_needs_telegram_and_a_state_file_it_can_read_before_any_request(folder, http, monkeypatch):
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set"):
        hunt.cmd_followup(followup_args())

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    (folder / followup.STATE).write_text('{"nudged": ["posting:x"]}')
    with pytest.raises(SystemExit, match="followup_state.json: expected"):
        hunt.cmd_followup(followup_args())
    assert len(http.calls) == 0 and (folder / followup.STATE).read_text() == '{"nudged": ["posting:x"]}'


def test_followup_stops_on_a_database_it_cannot_read(tmp_path, monkeypatch, http):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hunt.db").write_text("not a database, and long enough to be read as a broken one " * 4)

    with pytest.raises(SystemExit, match="hunt.db: cannot be read"):
        hunt.cmd_followup(followup_args(dry_run=True))
    assert len(http.calls) == 0


def test_a_reply_since_the_application_takes_it_off_the_list(folder, http, capsys):
    # The files of `hunt.py mail` are read themselves: the dashboard has not copied these into hunt.db.
    write_mail(folder, "20261120-090000", [
        mail_update(1, "rejected", "Acme Inc.", received_at=days_ago(2)),
        mail_update(2, "interview", "", sender="ana@beta.io", received_at=days_ago(1)),  # Beta, by the sender's domain
        mail_update(3, "rejected", "Gamma", received_at=days_ago(20)),  # before this application was sent
        mail_update(4, "applied", "Gamma", received_at=days_ago(1)),  # a confirmation is no reply
        mail_update(5, "rejected", "Omega Labs", received_at=days_ago(1)),  # another company
    ])
    (folder / "mail" / "20261121-090000.json").write_text("{")  # a file that cannot be read is skipped

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys) == [
        "Follow-ups: 1 application(s) without a reply for 8+ days",
        "",
        "Could not check:",
        "- Gamma: Check careers page (applied 9 days ago)",
        "",
        PIPELINE_LINE,
    ]
    assert len(http.calls) == 0  # an answered application's posting is not looked at


def test_a_title_from_a_web_page_cannot_forge_lines_of_the_message(tmp_path, monkeypatch, http, capsys):
    monkeypatch.chdir(tmp_path)
    forged = "Analytics Engineer\n\nPosting closed, move on:\n- Acme: Chief of Everything (applied 99 days ago)"
    track(tmp_path, [result("Acme\r\nInc", "acme.com", fit=fit(82, "apply", forged, ACME_URL))],
          [("posting", ACME_URL, 12)])
    http.get(ACME_URL, body="<h1>Analytics Engineer</h1>")

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys) == [
        "Follow-ups: 1 application(s) without a reply for 8+ days",
        "",
        "Still open, worth a follow-up:",
        "- Acme Inc: Analytics Engineer Posting closed, move on: - Acme: Chief of Everything (applied 99 days ago)"
        " (applied 12 days ago)",
        ACME_URL,
        "",
        PIPELINE_LINE,
    ]


def test_posthog_hears_that_followup_ran_and_nothing_about_the_applications(folder, pages, monkeypatch, posthog_api,
                                                                            capsys):
    monkeypatch.setattr("sys.argv", ["hunt.py", "followup", "--dry-run"])

    with pytest.raises(SystemExit) as exit_info:
        hunt.main()

    assert exit_info.value.code == 0 and "- Acme: Analytics Engineer (applied 12 days ago)" in capsys.readouterr().out
    started, ended = posthog_api.records()
    assert started == {"name": "hunt_command_started", "level": "INFO", "service": "hunt", "command": "followup"}
    assert ended.pop("seconds") >= 0
    assert ended == {"name": "hunt_command_finished", "level": "INFO", "service": "hunt", "command": "followup",
                     "exit_code": 0}
    assert posthog_api.events() == []  # no model is asked
    told = json.dumps(posthog_api.records()).lower()
    for private in ("acme", "beta", "gamma", "analytics engineer", "careers"):
        assert private not in told


# Applications added from a confirmation email --------------------------------------------------------------------


@pytest.fixture
def zeta(tmp_path, monkeypatch):
    """An application to Zeta that the dashboard knows from its confirmation email only, sent by Greenhouse 11 days
    ago. A saved run researched Zeta and found a posting on its Greenhouse board."""
    monkeypatch.chdir(tmp_path)
    track(tmp_path, [result("Zeta", "zeta.io", roles=[("Growth Analyst", ZETA_JOB, "live")])], [])
    add_from_email(tmp_path, mail_update(1, "applied", "Zeta Inc.", role="Senior Data Analyst",
                                         received_at=days_ago(11))
                   | {"from": "Zeta Hiring Team <no-reply@us.greenhouse-mail.io>"})
    return tmp_path


@pytest.mark.parametrize(
    ("status", "titles", "heading"),
    [
        (200, ["Account Executive", "Senior Data Analyst (Remote, EU)"], "Still open, worth a follow-up:"),
        (200, ["Account Executive", "Growth Analyst"], "Posting closed, move on:"),
        (200, ["Data Analyst"], "Could not check:"),  # may be the role under a shorter title
        (200, [], "Could not check:"),  # a board that lists nothing does not say that one posting closed
        (503, [], "Could not check:"),
        (404, [], "Could not check:"),
    ],
    ids=["listed", "not-listed", "a-title-alike", "empty-board", "board-down", "board-gone"],
)
def test_an_application_from_an_email_is_looked_up_on_the_board_saved_research_knows(zeta, http, capsys, status, titles,
                                                                                    heading):
    jobs = [greenhouse_job(100 + n, title, board="zeta") for n, title in enumerate(titles)]
    http.get(GREENHOUSE.format("zeta"), status=status, json={"jobs": jobs, "meta": {"total": len(jobs)}})

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys) == [
        "Follow-ups: 1 application(s) without a reply for 8+ days",
        "",
        heading,
        "- Zeta Inc.: Senior Data Analyst (applied 11 days ago)",  # applied when the email came; no link to a posting
        "",
        PIPELINE_LINE,
    ]


def test_a_saved_run_that_did_not_verify_the_company_names_no_board_to_call_an_application_closed(tmp_path, monkeypatch,
                                                                                                 http, capsys):
    # such a run is where a namesake's postings are saved: its board, which does not list the role, says nothing
    monkeypatch.chdir(tmp_path)
    unverified = result("Zeta", "zeta.io", roles=[("Growth Analyst", ZETA_JOB, "live")])
    unverified["research"]["content"]["identity_verified"] = False
    track(tmp_path, [unverified], [])
    add_from_email(tmp_path, mail_update(1, "applied", "Zeta Inc.", role="Senior Data Analyst", received_at=days_ago(11))
                   | {"from": "Zeta Hiring Team <no-reply@us.greenhouse-mail.io>"})
    for guess in ("zetainc", "zeta-inc"):  # the board is guessed from the email instead; the one named zeta is not asked
        http.get(GREENHOUSE.format(guess), status=404)

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys)[2:4] == ["Could not check:", "- Zeta Inc.: Senior Data Analyst (applied 11 days ago)"]


@pytest.fixture
def eta(tmp_path, monkeypatch):
    """An application to Eta Labs, known from its confirmation email only (Greenhouse, 11 days ago). No saved run
    researched the company."""
    monkeypatch.chdir(tmp_path)
    track(tmp_path, [], [])
    add_from_email(tmp_path, mail_update(1, "applied", "Eta Labs", role="Data Analyst", received_at=days_ago(11)))
    return tmp_path


def test_nothing_is_called_closed_from_a_guessed_board(eta, http, capsys):
    # A board named like the company answers, without the role: it may be another company's board.
    greenhouse(http, "etalabs", greenhouse_job(222, "Account Executive", board="etalabs"))
    http.get(GREENHOUSE.format("eta-labs"), status=404)

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys)[2:4] == ["Could not check:", "- Eta Labs: Data Analyst (applied 11 days ago)"]
    assert len(http.calls) == 2  # both spellings of the name were tried


def test_a_guessed_board_that_lists_the_role_shows_the_application_as_open(eta, http, capsys):
    http.get(GREENHOUSE.format("etalabs"), status=404)
    greenhouse(http, "eta-labs", greenhouse_job(111, "Data Analyst, Growth", board="eta-labs"))

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys)[2:4] == ["Still open, worth a follow-up:", "- Eta Labs: Data Analyst (applied 11 days ago)"]


def test_an_email_without_a_role_or_from_no_job_board_is_not_looked_up(tmp_path, monkeypatch, http, capsys):
    monkeypatch.chdir(tmp_path)
    track(tmp_path, [], [])
    add_from_email(
        tmp_path,
        mail_update(1, "applied", "Eta Labs", received_at=days_ago(11)),  # names no role: nothing to look for
        mail_update(2, "applied", "Theta", role="Data Analyst", sender="ana@theta.io", received_at=days_ago(9)),
        mail_update(3, "applied", "Iota", role="Data Analyst\n\nStill open, worth a follow-up:", sender="ana@iota.io",
                    received_at=days_ago(8)),
    )

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys)[2:6] == [
        "Could not check:",
        "- Eta Labs: Role not named in the email (applied 11 days ago)",
        "- Theta: Data Analyst (applied 9 days ago)",
        "- Iota: Data Analyst Still open, worth a follow-up: (applied 8 days ago)",  # an email's words, on one line
    ]
    assert len(http.calls) == 0


def test_teamtailor_names_the_board_in_the_address_it_sends_from(tmp_path, monkeypatch, http, capsys):
    monkeypatch.chdir(tmp_path)
    track(tmp_path, [], [])
    add_from_email(tmp_path, mail_update(1, "applied", "Theta Group AB", role="Data Analyst", received_at=days_ago(9),
                                         sender="no-reply@theta.teamtailor-mail.com"))
    http.get("https://theta.teamtailor.com/jobs.rss", body=teamtailor_feed(
        ("https://theta.teamtailor.com/jobs/123-data-analyst", "Data Analyst", "hybrid", "Stockholm", "<p>SQL</p>")))

    assert hunt.cmd_followup(followup_args(dry_run=True)) == 0

    assert printed(capsys)[2:4] == ["Still open, worth a follow-up:",
                                    "- Theta Group AB: Data Analyst (applied 9 days ago)"]


# The module ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "address", "expected"),
    [
        ("Eta Labs", "no-reply@us.greenhouse-mail.io",
         [("greenhouse", "etalabs", "job-boards.greenhouse.io"), ("greenhouse", "eta-labs", "job-boards.greenhouse.io")]),
        ("Acme", "no-reply@greenhouse.io", [("greenhouse", "acme", "job-boards.greenhouse.io")]),
        ("Acme", "no-reply@ashbyhq.com", [("ashby", "acme", "jobs.ashbyhq.com")]),
        ("Acme", "no-reply@hire.lever.co", [("lever", "acme", "jobs.lever.co")]),
        ("Acme", "noreply@candidates.workablemail.com", [("workable", "acme", "apply.workable.com")]),
        ("Acme", "jobs@workable.com", [("workable", "acme", "apply.workable.com")]),
        ("Acme", "notification@smartrecruiters.com", [("smartrecruiters", "acme", "jobs.smartrecruiters.com")]),
        ("Acme", "no-reply@recruitee.com", [("recruitee", "acme", "acme.recruitee.com")]),
        ("Whatever AB", "no-reply@theta.teamtailor-mail.com", [("teamtailor", "theta", "theta.teamtailor.com")]),
        ("Acme", "no-reply@teamtailor-mail.com", [("teamtailor", "acme", "acme.teamtailor.com")]),
        ("Acme", "recruiting@personio.de", [("personio", "acme", "acme.jobs.personio.de")]),
        ("Acme", "recruiting@mail.personio.com", [("personio", "acme", "acme.jobs.personio.de")]),
        ("Acme", "no-reply@pinpointhq.com", [("pinpoint", "acme", "acme.pinpointhq.com")]),
        ("Acme", "ana@acme.com", []),  # the company's own mail: no job board to guess on
        ("Acme", "no-reply@notgreenhouse.io", []),
        ("", "no-reply@us.greenhouse-mail.io", []),  # no name to go by
        ("Jobs", "no-reply@greenhouse.io", []),  # not a board's name
        # an address that is none: what stands before the job board's domain never becomes a host
        ("Acme", "x@evil.example/a#.teamtailor-mail.com", [("teamtailor", "acme", "acme.teamtailor.com")]),
    ],
)
def test_the_board_is_guessed_from_the_job_board_that_sent_the_email_and_the_companys_name(company, address, expected):
    boards = followup.guessed_boards(company, address)

    assert [(board["ats"], board["slug"], board["host"]) for board in boards] == expected


def test_a_board_is_known_only_when_one_researched_company_is_the_emails():
    saved = {
        "acme.com": {"name": "Acme", "links": ["https://jobs.ashbyhq.com/acme/1", "https://jobs.ashbyhq.com/other/2"]},
        "zeta.io": {"name": "Zeta", "links": [ZETA_JOB]},
        "zetalabs.com": {"name": "Zeta Labs", "links": ["https://jobs.lever.co/zetalabs/1"]},
    }
    greenhouse_mail = "no-reply@us.greenhouse-mail.io"

    assert followup.known_boards("Acme Inc.", greenhouse_mail, saved) == [
        {"ats": "ashby", "slug": "acme", "host": "jobs.ashbyhq.com"}]  # only the board that carries its name
    assert followup.known_boards("", "talent@mail.acme.com", saved) == [
        {"ats": "ashby", "slug": "acme", "host": "jobs.ashbyhq.com"}]  # by the sender's domain
    assert followup.known_boards("Zeta Labs", greenhouse_mail, saved) == []  # Zeta or Zeta Labs: not known which
    assert followup.known_boards("Omega", greenhouse_mail, saved) == []


def test_only_applications_silent_for_eight_days_wait_and_each_only_until_it_was_reported():
    now = datetime(2026, 11, 25, 9, 0)

    def item(ref, applied_at, name="Acme"):
        return {"kind": "posting", "ref": ref, "applied_at": applied_at, "name": name, "domain": None, "title": "T",
                "url": ref}

    items = [item("a", "2026-11-17T09:00:00"), item("b", "2026-11-17T09:00:01"), item("c", "2026-11-01"),
             item("d", "2026-10-01T09:00:00"), item("e", None), item("f", "2026-11-10T09:00:00", name="Beta"),
             item("g", "2026-11-20T09:00:00+00:00"), item("h", "2026-10-20T09:00:00", name="Beta")]
    answers = [({"company": "Beta", "from_address": "no-reply@us.greenhouse-mail.io"}, datetime(2026, 11, 1, 9, 0))]

    waiting = followup.waiting(items, answers, {"posting:d": "2026-10-10T09:00:00"}, now)

    # Eight days to the second, the longest wait first. "d" was reported before, "e" has no date to count from and
    # "g", saved with a time zone, was sent five days ago. Beta's rejection answers "h", sent before it came, and
    # not "f", sent after it.
    assert [(i["ref"], i["days"]) for i in waiting] == [("c", 24), ("f", 15), ("a", 8)]


def test_state_is_empty_without_a_file_and_refused_in_another_shape(tmp_path):
    path = tmp_path / followup.STATE
    assert followup.load_state(path) == {"nudged": {}}

    state = {"nudged": {"posting:https://acme.com/jobs/1": "2026-11-25T09:00:00"}}
    followup.save_state(path, state)
    assert followup.load_state(path) == state and list(tmp_path.iterdir()) == [path]  # no temporary file left

    for text, message in (("{", "not valid JSON"), ("[]", "expected"), ('{"nudged": []}', "expected"),
                          ('{"nudged": {"posting:x": 5}}', "not an application")):
        path.write_text(text)
        with pytest.raises(ValueError, match=message):
            followup.load_state(path)


def test_a_link_with_white_space_is_left_out_of_the_message():
    groups = {"open": [], "closed": [], "unknown": [
        {"kind": "posting", "name": "", "title": "Data Lead", "url": "https://acme.com/jobs/1\nhttps://evil.example/x",
         "days": 9},
        {"kind": "email", "name": "Zeta", "title": "Data Analyst", "url": "https://outlook.live.com/1", "days": 8}]}

    assert followup.format_message(groups, "https://hunt.example.org/").splitlines() == [
        "Follow-ups: 2 application(s) without a reply for 8+ days",
        "",
        "Could not check:",
        "- Data Lead (applied 9 days ago)",  # no company known for this posting
        "- Zeta: Data Analyst (applied 8 days ago)",  # the link of an email is not a posting's
        "",
        "Pipeline: https://hunt.example.org/pipeline",
    ]
