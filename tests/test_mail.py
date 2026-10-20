import argparse
import json
from datetime import datetime, timezone

import pytest

import hunt
import mail
import mail_labels
import outlook
import scoring

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
INBOX = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
JUNK = "https://graph.microsoft.com/v1.0/me/mailFolders/junkemail/messages"
TELEGRAM = "https://api.telegram.org/bottok/sendMessage"


def graph_message(n, sender, subject, body):
    return {"id": f"graph-{n}", "internetMessageId": f"<m{n}@mail.example>", "subject": subject,
            "from": {"emailAddress": {"name": sender.split("@")[0], "address": sender}},
            "receivedDateTime": f"2026-10-1{n}T09:00:00Z", "body": {"contentType": "text", "content": body},
            "webLink": f"https://outlook.live.com/{n}"}


REJECTION = graph_message(1, "no-reply@us.greenhouse-mail.io", "Your application to Acme",
                          "Thank you for your interest. Unfortunately we decided to move forward with other candidates. "
                          + "x" * 3000 + " TAIL-BEYOND-LIMIT")
NEWSLETTER = graph_message(2, "news@substack.com", "This week in dbt", "Ten new macros worth knowing.")
INVITE = graph_message(3, "ana@beta.io", "Next steps at Beta", "We'd love to schedule a 30 minute call with you.")
AGENDA = graph_message(4, "no-reply@microsoft.com", "Today: Interview with Beta",  # Outlook's daily agenda
                       "Your agenda for today. 12:00 Interview with Beta (Zoom).")

LABELS = {"emails": [
    {"number": 1, "outcome": "rejected", "company": "Acme", "role": "Analytics Engineer", "summary": "Acme declined."},
    {"number": 2, "outcome": "interview", "company": "Beta", "role": "", "summary": "Beta asks for a call."},
]}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var, value in {"OUTLOOK_CLIENT_ID": "app-123", "ANTHROPIC_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "tok",
                       "TELEGRAM_CHAT_ID": "42"}.items():
        monkeypatch.setenv(var, value)
    (tmp_path / "outlook_token.json").write_text(json.dumps({"refresh_token": "rt-1"}))
    return tmp_path


@pytest.fixture
def mailbox(http):
    http.post(TOKEN_URL, json={"access_token": "at", "refresh_token": "rt-2"})
    http.get(INBOX, json={"value": [REJECTION, NEWSLETTER, INVITE, AGENDA]})
    http.get(JUNK, json={"value": []})


@pytest.fixture
def fake(fake_claude, monkeypatch):
    fake = fake_claude(LABELS)
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    return fake


def mail_args(**overrides):
    return argparse.Namespace(**{"token": "outlook_token.json", "state": "mail_state.json", "days": 14,
                                 "dry_run": False, **overrides})


def test_mail_labels_application_email_saves_updates_and_tells_telegram(workdir, mailbox, fake, http):
    telegram_api = http.post(TELEGRAM, json={"ok": True})

    assert hunt.cmd_mail(mail_args()) == 0

    (call,) = fake.calls
    sent_to_claude = call["messages"][0]["content"]
    assert "Your application to Acme" in sent_to_claude and "Next steps at Beta" in sent_to_claude
    assert "This week in dbt" not in sent_to_claude  # no application wording, not an ATS: stays local
    assert "Today: Interview with Beta" not in sent_to_claude  # Outlook's calendar reminder repeats the invite
    assert "TAIL-BEYOND-LIMIT" not in sent_to_claude  # only the first BODY_CHARS characters
    (text,) = [json.loads(c.request.body)["text"] for c in telegram_api.calls]
    assert "Rejected: Acme · Analytics Engineer\nAcme declined." in text
    assert "Interview invite: Beta\nBeta asks for a call." in text
    assert text.endswith("http://127.0.0.1:8000/pipeline")
    (saved,) = mail.update_files(workdir)
    updates = json.loads(saved.read_text())["updates"]
    assert [(u["id"], u["outcome"], u["from"]) for u in updates] == [
        ("<m1@mail.example>", "rejected", "no-reply <no-reply@us.greenhouse-mail.io>"),
        ("<m3@mail.example>", "interview", "ana <ana@beta.io>"),
    ]
    state = json.loads((workdir / "mail_state.json").read_text())
    assert set(state["seen"]) == {"<m1@mail.example>", "<m3@mail.example>"}
    assert json.loads((workdir / "outlook_token.json").read_text())["refresh_token"] == "rt-2"

    # The next check re-reads the overlap: already labelled email is neither paid for nor sent again.
    assert hunt.cmd_mail(mail_args()) == 0
    assert len(fake.calls) == 1 and len(telegram_api.calls) == 1
    assert len(mail.update_files(workdir)) == 1


def test_mail_reads_an_email_claude_left_unlabelled_again(workdir, mailbox, fake_claude, monkeypatch, http):
    http.post(TELEGRAM, json={"ok": True})
    only_first = fake_claude({"emails": LABELS["emails"][:1]})  # 2 candidates, 1 label
    monkeypatch.setattr(scoring, "_client", lambda: only_first)

    assert hunt.cmd_mail(mail_args()) == 0

    state = json.loads((workdir / "mail_state.json").read_text())
    assert set(state["seen"]) == {"<m1@mail.example>"}
    # The next check starts at the missed email (received 2026-10-13T09:00Z), however long ago that was.
    assert datetime.fromisoformat(state["checked_until"]) - mail.OVERLAP == datetime(2026, 10, 13, 9, tzinfo=timezone.utc)

    hunt.cmd_mail(mail_args())
    retried = only_first.calls[1]["messages"][0]["content"]
    assert "Next steps at Beta" in retried and "Your application to Acme" not in retried
    assert set(json.loads((workdir / "mail_state.json").read_text())["seen"]) == {"<m1@mail.example>", "<m3@mail.example>"}


def test_mail_gives_up_on_an_email_claude_keeps_skipping(workdir, mailbox, fake_claude, monkeypatch, http):
    no_labels = fake_claude({"emails": []})
    monkeypatch.setattr(scoring, "_client", lambda: no_labels)

    for _ in range(mail.MAX_TRIES + 2):
        assert hunt.cmd_mail(mail_args()) == 0

    assert len(no_labels.calls) == mail.MAX_TRIES  # paid for at most MAX_TRIES times, then left alone
    state = json.loads((workdir / "mail_state.json").read_text())
    assert set(state["seen"]) == {"<m1@mail.example>", "<m3@mail.example>"} and state["tries"] == {}
    assert datetime.fromisoformat(state["checked_until"]) > datetime(2026, 10, 14, tzinfo=timezone.utc)


def test_mail_continues_where_a_capped_check_stopped(workdir, mailbox, fake, http, monkeypatch):
    monkeypatch.setattr(outlook, "MAX_MESSAGES", 2)  # the inbox holds 3: the invite (newest) is not read
    http.post(TELEGRAM, json={"ok": True})

    assert hunt.cmd_mail(mail_args()) == 0

    assert "Next steps at Beta" not in fake.calls[0]["messages"][0]["content"]
    state = json.loads((workdir / "mail_state.json").read_text())
    # The next check starts at the newest email read (the newsletter, 2026-10-12T09:00Z), not at this check's time.
    assert datetime.fromisoformat(state["checked_until"]) - mail.OVERLAP == datetime(2026, 10, 12, 9, tzinfo=timezone.utc)



def test_mail_failed_send_keeps_the_email_for_the_next_check(workdir, mailbox, fake, http):
    http.post(TELEGRAM, json={"ok": False, "error_code": 400, "description": "chat not found"})

    with pytest.raises(RuntimeError, match="chat not found"):
        hunt.cmd_mail(mail_args())
    assert not (workdir / "mail_state.json").exists()


def test_mail_dry_run_prints_labels_only(workdir, mailbox, fake, capsys):
    assert hunt.cmd_mail(mail_args(dry_run=True)) == 0

    out = capsys.readouterr().out
    assert "- rejected: Acme · Analytics Engineer" in out and "Rejected: Acme" in out
    assert not (workdir / "mail_state.json").exists() and mail.update_files(workdir) == []


def update(n, outcome, company, role="", summary=""):
    return {"id": f"<{n}>", "received_at": f"2026-10-2{n}T09:00:00", "from": "ana <ana@x.io>", "from_address": "ana@x.io",
            "subject": "s", "web_link": "", "outcome": outcome, "company": company, "role": role, "summary": summary}


def test_telegram_shows_one_entry_per_company_and_outcome():
    text = mail.format_message([
        update(1, "interview", "SE Ranking", summary="Invite to a hiring manager interview."),
        update(0, "rejected", "Acme", "Analytics Engineer", "Acme declined."),
        update(2, "interview", "se ranking", "Senior Data Analyst", "Interview confirmed for Oct 22."),
    ], "http://dash")

    assert text.startswith("Email: 2 application update(s)")
    assert "Interview invite: se ranking · Senior Data Analyst (2 emails)\nInterview confirmed for Oct 22." in text
    assert "Invite to a hiring manager interview." not in text
    assert "Rejected: Acme · Analytics Engineer\nAcme declined." in text


@pytest.mark.parametrize(
    ("sender", "subject", "likely"),
    [
        ("no-reply@microsoft.com", "Today: Interview with Rho", False),  # Outlook's daily agenda
        ("no-reply@microsoft.com", "Your application to Microsoft: next steps", True),
        ("calendar-notification@google.com", "Invitation: Interview with Beta", True),  # a real invite, for Claude
    ],
)
def test_only_outlooks_daily_agenda_is_skipped_locally(sender, subject, likely):
    message = {"from_address": sender, "subject": subject, "body": ""}
    assert mail.likely(message) is likely


def test_mail_labels_survive_bad_numbers_from_claude(fake_claude):
    messages = [{"id": f"<{n}>", "received_at": "2026-10-18T09:00:00", "from_name": "", "from_address": "a@b.io",
                 "subject": "s", "body": "b", "web_link": ""} for n in range(2)]
    label = {"outcome": "other", "company": "", "role": "", "summary": "s"}
    fake = fake_claude({"emails": [{"number": 2, **label}, {"number": 9, **label}, {"number": 2, **label, "outcome": "rejected"}]})

    events, usage = mail_labels.label(messages, client=fake)

    assert [(e["id"], e["outcome"], e["from"]) for e in events] == [("<1>", "other", "a@b.io")]
    assert usage == {"input_tokens": 1200, "output_tokens": 300}


@pytest.mark.parametrize(
    ("remove", "message"),
    [("OUTLOOK_CLIENT_ID", "OUTLOOK_CLIENT_ID is not set"), ("token", "outlook-login")],
)
def test_mail_needs_the_outlook_setup_before_anything_paid(workdir, http, monkeypatch, remove, message):
    if remove == "token":
        (workdir / "outlook_token.json").unlink()
    else:
        monkeypatch.delenv(remove)

    with pytest.raises(SystemExit, match=message):
        hunt.cmd_mail(mail_args())
    assert len(http.calls) == 0
