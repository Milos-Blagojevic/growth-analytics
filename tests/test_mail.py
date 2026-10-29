import argparse
import json
from datetime import datetime, timezone

import anthropic
import httpx
import pytest

import hunt
import jev
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


@pytest.mark.parametrize(
    ("fields", "code"),
    [({"emails": []}, 0), (None, 1)],  # Claude skips every email / returns nothing parsable: a problem
)
def test_mail_gives_up_on_an_email_claude_keeps_skipping(workdir, mailbox, fake_claude, monkeypatch, http, fields, code):
    no_labels = fake_claude(fields)
    monkeypatch.setattr(scoring, "_client", lambda: no_labels)

    for i in range(1, mail.MAX_TRIES + 1):
        assert hunt.cmd_mail(mail_args()) == code
        tries = json.loads((workdir / "mail_state.json").read_text())["tries"]  # saved after each check, failed or not
        assert tries == ({"<m1@mail.example>": i, "<m3@mail.example>": i} if i < mail.MAX_TRIES else {})
    for _ in range(2):
        assert hunt.cmd_mail(mail_args()) == 0  # given up on: nothing left for Claude to fail on

    assert len(no_labels.calls) == mail.MAX_TRIES  # paid for at most MAX_TRIES times, then left alone
    state = json.loads((workdir / "mail_state.json").read_text())
    assert set(state["seen"]) == {"<m1@mail.example>", "<m3@mail.example>"} and state["tries"] == {}
    assert datetime.fromisoformat(state["checked_until"]) > datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert not mail.update_files(workdir)


def test_mail_keeps_the_other_batches_when_claude_fails_on_one(workdir, fake_claude, monkeypatch, http, capsys):
    ats = [graph_message(n, "no-reply@us.greenhouse-mail.io", f"Your application to Co{n}", "Unfortunately no.")
           for n in range(10, 35)]  # 25 candidates: a batch of 20 and one of 5
    for n, m in enumerate(ats):
        m["receivedDateTime"] = f"2026-10-20T09:{n:02d}:00Z"
    http.post(TOKEN_URL, json={"access_token": "at", "refresh_token": "rt-2"})
    http.get(INBOX, json={"value": ats})
    http.get(JUNK, json={"value": []})
    telegram_api = http.post(TELEGRAM, json={"ok": True})

    class Flaky(fake_claude):
        def parse(self, **kwargs):
            if self.calls:  # the second batch
                self.calls.append(kwargs)
                raise overloaded()
            return super().parse(**kwargs)

    flaky = Flaky({"emails": [{"number": n, "outcome": "rejected", "company": f"Co{n + 9}", "role": "",
                               "summary": f"Co{n + 9} declined."} for n in range(1, mail_labels.BATCH + 1)]})
    monkeypatch.setattr(scoring, "_client", lambda: flaky)

    assert hunt.cmd_mail(mail_args()) == 1

    assert len(flaky.calls) == 2
    out = capsys.readouterr().out
    assert "Claude failed on emails 21-25 of 25: APIStatusError" in out
    (saved,) = mail.update_files(workdir)
    updates = json.loads(saved.read_text())["updates"]
    assert [u["company"] for u in updates] == [f"Co{n}" for n in range(10, 30)]  # the first batch, saved and sent
    (text,) = [json.loads(c.request.body)["text"] for c in telegram_api.calls]
    assert "Rejected: Co10" in text and "Rejected: Co29" in text and "Co30" not in text
    state = json.loads((workdir / "mail_state.json").read_text())
    assert set(state["seen"]) == {f"<m{n}@mail.example>" for n in range(10, 30)}
    assert state["tries"] == {}  # the failed call cost nothing: the batch is not a try closer to being given up on
    # The next check starts early enough to read the failed batch again: an hour before its oldest email (09:20Z).
    assert datetime.fromisoformat(state["checked_until"]) == datetime(2026, 10, 20, 10, 20, tzinfo=timezone.utc)


def test_mail_keeps_trying_until_claude_answers(workdir, mailbox, fake_claude, monkeypatch, http):
    class Down(fake_claude):
        def parse(self, **kwargs):
            if len(self.calls) < mail.MAX_TRIES:  # key or billing problem for the first checks: no answer at all
                self.calls.append(kwargs)
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
            return super().parse(**kwargs)

    down = Down(LABELS)
    monkeypatch.setattr(scoring, "_client", lambda: down)

    for _ in range(mail.MAX_TRIES):
        assert hunt.cmd_mail(mail_args()) == 1
        state = json.loads((workdir / "mail_state.json").read_text())
        assert state["seen"] == {} and state["tries"] == {}  # nothing was billed, so nothing counts against them
        assert datetime.fromisoformat(state["checked_until"]) - mail.OVERLAP == datetime(2026, 10, 11, 9, tzinfo=timezone.utc)
    telegram_api = http.post(TELEGRAM, json={"ok": True})

    assert hunt.cmd_mail(mail_args()) == 0  # Claude is back: labelled, saved and sent on this check

    assert len(down.calls) == mail.MAX_TRIES + 1
    (saved,) = mail.update_files(workdir)
    assert [u["company"] for u in json.loads(saved.read_text())["updates"]] == ["Acme", "Beta"]
    (text,) = [json.loads(c.request.body)["text"] for c in telegram_api.calls]
    assert "Rejected: Acme" in text and "Beta" in text
    state = json.loads((workdir / "mail_state.json").read_text())
    assert set(state["seen"]) == {"<m1@mail.example>", "<m3@mail.example>"} and state["tries"] == {}


def test_mail_counts_an_unparsable_claude_reply_as_a_try(workdir, mailbox, fake_claude, monkeypatch, http):
    nothing = fake_claude(None)
    monkeypatch.setattr(scoring, "_client", lambda: nothing)

    assert hunt.cmd_mail(mail_args()) == 1

    assert len(nothing.calls) == 1 and not mail.update_files(workdir)  # nothing saved, nothing sent (Telegram unmocked)
    state = json.loads((workdir / "mail_state.json").read_text())
    assert state["seen"] == {} and state["tries"] == {"<m1@mail.example>": 1, "<m3@mail.example>": 1}


def test_mail_counts_an_answer_the_sdk_cannot_validate_as_a_try(workdir, mailbox, fake_claude, monkeypatch, http):
    """Claude answered (billed) but its JSON was cut off: the SDK raises before parsed_output exists."""
    class CutOff(fake_claude):
        def parse(self, **kwargs):
            self.calls.append(kwargs)
            mail_labels.Labels.model_validate_json('{"emails": [{"number": 1, "outcome": "rej')

    cut_off = CutOff(LABELS)
    monkeypatch.setattr(scoring, "_client", lambda: cut_off)

    for n in range(1, mail.MAX_TRIES + 1):
        assert hunt.cmd_mail(mail_args()) == 1
        state = json.loads((workdir / "mail_state.json").read_text())
        if n < mail.MAX_TRIES:
            assert state["tries"] == {"<m1@mail.example>": n, "<m3@mail.example>": n} and state["seen"] == {}
    assert state["tries"] == {} and set(state["seen"]) == {"<m1@mail.example>", "<m3@mail.example>"}  # given up
    assert len(cut_off.calls) == mail.MAX_TRIES
    assert hunt.cmd_mail(mail_args()) == 0 and len(cut_off.calls) == mail.MAX_TRIES  # not paid for again


def overloaded():
    response = httpx.Response(529, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.APIStatusError("overloaded", response=response, body=None)


def test_next_state_keeps_the_tries_of_emails_claude_never_answered_about():
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    older = {"id": "<a>", "received_at": "2026-10-20T09:00:00"}  # already missed by Claude's answers, twice
    newer = {"id": "<b>", "received_at": "2026-10-20T11:00:00"}

    state, given_up = mail.next_state({"tries": {"<a>": mail.MAX_TRIES - 1}}, now, [], unanswered=[older, newer])

    assert given_up == [] and state["seen"] == {}
    assert state["tries"] == {"<a>": mail.MAX_TRIES - 1}  # not a try more, and none for the email never answered about
    assert datetime.fromisoformat(state["checked_until"]) == datetime.fromisoformat(older["received_at"]).astimezone() + mail.OVERLAP


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


RECEIVED = graph_message(5, "no-reply@greenhouse-mail.io", "Thanks for applying to Gamma",
                         "We received your application and will review it.")
JEV_SORTS = {  # subject -> Jev's (outcome, confidence)
    "Your application to Acme": ("rejected", 0.9),
    "Thanks for applying to Gamma": ("other", 0.95),  # clear: dropped before Claude
    "Next steps at Beta": ("other", 0.6),  # not sure enough: Claude decides
}


def test_jev_drops_clear_non_updates_and_claude_labels_the_rest(workdir, fake, http, jev_api):
    http.post(TOKEN_URL, json={"access_token": "at", "refresh_token": "rt-2"})
    http.get(INBOX, json={"value": [REJECTION, INVITE, RECEIVED]})
    http.get(JUNK, json={"value": []})
    http.post(TELEGRAM, json={"ok": True})
    jev_api.on(lambda state, key, question: JEV_SORTS[state["subject"]])

    assert hunt.cmd_mail(mail_args()) == 0

    assert len(jev_api.requests) == 3
    sent_to_claude = fake.calls[0]["messages"][0]["content"]
    assert "Your application to Acme" in sent_to_claude and "Next steps at Beta" in sent_to_claude
    assert "Thanks for applying to Gamma" not in sent_to_claude
    (saved,) = mail.update_files(workdir)
    assert json.loads(saved.read_text())["screened_out"] == 1
    # The dropped email counts as checked: neither Jev nor Claude reads it again.
    assert set(json.loads((workdir / "mail_state.json").read_text())["seen"]) == {
        "<m1@mail.example>", "<m3@mail.example>", "<m5@mail.example>"}


def test_mail_sends_every_application_email_to_claude_when_jev_is_down(workdir, mailbox, fake, http, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
    typesafe = http.post(jev.API_URL, status=529)
    http.post(TELEGRAM, json={"ok": True})

    assert hunt.cmd_mail(mail_args()) == 0

    assert len(typesafe.calls) == jev.MAX_ATTEMPTS  # tried once, with retries, then the rest skip Jev
    sent_to_claude = fake.calls[0]["messages"][0]["content"]
    assert "Your application to Acme" in sent_to_claude and "Next steps at Beta" in sent_to_claude
    assert len(mail.update_files(workdir)) == 1


def update(n, outcome, company, role="", summary=""):
    return {"id": f"<{n}>", "received_at": f"2026-10-2{n}T09:00:00", "from": "ana <ana@x.io>", "from_address": "ana@x.io",
            "subject": "s", "web_link": "", "outcome": outcome, "company": company, "role": role, "summary": summary}


def test_telegram_merges_emails_about_the_same_application_only():
    text = mail.format_message([
        update(1, "interview", "SE Ranking", summary="Invite to a hiring manager interview."),
        update(0, "rejected", "Acme", "Analytics Engineer", "Acme declined."),
        update(2, "interview", "se ranking", "Senior Data Analyst", "Interview confirmed for Oct 22."),
        update(3, "interview", "", summary="Gamma invites."),  # no company named: a shared ATS sender
        update(4, "interview", "", summary="Delta invites."),
        update(5, "rejected", "Acme", "Data Analyst", "Acme declined the second role."),
    ], "http://dash")

    assert text.startswith("Email: 5 application update(s) from 6 emails")
    assert "Interview invite: se ranking · Senior Data Analyst (2 emails)\nInterview confirmed for Oct 22." in text
    assert "Invite to a hiring manager interview." not in text
    assert "Gamma invites." in text and "Delta invites." in text
    assert "Rejected: Acme · Analytics Engineer\nAcme declined." in text
    assert "Rejected: Acme · Data Analyst\nAcme declined the second role." in text


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


def test_local_filter_reads_the_same_text_the_models_get():
    # Enough blank lines to fill the raw 2,000-character window; the wording only shows once whitespace is collapsed.
    body = "\n \n" * 800 + "Unfortunately we will not move forward with your application."
    message = {"from_address": "ana@beta.io", "subject": "Update", "body": body}

    assert mail.likely(message) is True
    assert "unfortunately" in mail.excerpt(body).lower()


def test_mail_labels_survive_bad_numbers_from_claude(fake_claude):
    messages = [{"id": f"<{n}>", "received_at": "2026-10-18T09:00:00", "from_name": "", "from_address": "a@b.io",
                 "subject": "s", "body": "b", "web_link": ""} for n in range(2)]
    label = {"outcome": "other", "company": "", "role": "", "summary": "s"}
    fake = fake_claude({"emails": [{"number": 2, **label}, {"number": 9, **label}, {"number": 2, **label, "outcome": "rejected"}]})

    events, usage, problems, unanswered = mail_labels.label(messages, client=fake)

    assert [(e["id"], e["outcome"], e["from"]) for e in events] == [("<1>", "other", "a@b.io")]
    assert usage == {"input_tokens": 1200, "output_tokens": 300} and problems == [] and unanswered == []


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
