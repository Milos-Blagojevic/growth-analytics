import argparse
import json
from datetime import datetime, timezone

import pytest

import hunt
import mail
import mail_labels
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
    http.get(INBOX, json={"value": [REJECTION, NEWSLETTER, INVITE]})
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
