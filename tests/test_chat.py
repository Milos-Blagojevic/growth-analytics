import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import anthropic
import httpx
import pytest
import requests
import responses
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from clients import jev
from exa_mock import error, result, search_response
from inbox import chat, gmail, outlook
from pipeline import news, scoring

AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
INBOX = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"


def scripted(*steps):
    """A FunctionModel that answers each model call with the next step: a list of (tool, args) calls or a text.
    Returns (model, seen): `seen` collects the last ModelRequest the model saw at each call, for assertions."""
    queue, seen = list(steps), []

    def model(messages, info):
        seen.append(messages[-1])
        step = queue.pop(0)
        if isinstance(step, str):
            return ModelResponse(parts=[TextPart(step)])
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args) for name, args in step])

    return FunctionModel(model), seen


def returned(request):
    """The tool results in a ModelRequest, in call order."""
    return [p.content for p in request.parts if isinstance(p, ToolReturnPart)]


def message(n, **extra):
    return {"id": f"id-{n}", "received_at": f"2026-10-18T09:{n:02d}:00", "from_name": "Ana", "from_address": "ana@acme.com",
            "to": ["me@example.com"], "subject": f"Subject {n}", "preview": f"Preview {n}", "web_link": f"https://mail/{n}",
            **extra}


@pytest.fixture
def mailboxes(monkeypatch):
    """Both accounts signed in, with tokens that need no refresh."""
    boxes = chat.Mailboxes(outlook_token="outlook_token.json", gmail_token="gmail_token.json")
    monkeypatch.setattr(boxes, "token", lambda account: f"at-{account}")
    return boxes


def run(steps, deps, text="hello"):
    model, seen = scripted(*steps)
    conversation = chat.Conversation(chat.build_agent(model), deps)
    return conversation.answer(text), seen, conversation


def test_list_emails_formats_entries_with_handles_per_account(mailboxes, monkeypatch):
    searches = []

    def search(module):
        def fake(token, query="", *, days=None, limit=20, folder="inbox"):
            searches.append((module, token, query, days, limit, folder))
            return [message(1, preview="  Hi   there\nlong " + "x" * 200), message(2, from_name="")]
        return fake

    monkeypatch.setattr(outlook, "search", search("outlook"))
    monkeypatch.setattr(gmail, "search", search("gmail"))

    answer, seen, conversation = run([
        [("list_emails", {"account": "outlook", "days": 2, "folder": "Junk"}),
         ("list_emails", {"account": "gmail", "query": "acme", "days": 0, "limit": 500})],
        "done",
    ], mailboxes)

    assert answer == "done"
    # Pydantic AI runs the two calls concurrently, so the searches can happen in either order.
    assert sorted(searches) == [("gmail", "at-gmail", "acme", None, 50, "inbox"), ("outlook", "at-outlook", "", 2, 20, "junkemail")]
    outlook_list, gmail_list = returned(seen[1])
    assert outlook_list.startswith("o1 · Sun 18 Oct 09:01 · Ana <ana@acme.com>\n  Subject: Subject 1\n  Hi there long xxx")
    assert outlook_list.endswith("…\no2 · Sun 18 Oct 09:02 · ana@acme.com\n  Subject: Subject 2\n  Preview 2")
    assert gmail_list.startswith("g1 · ") and "\ng2 · " in gmail_list
    assert mailboxes.refs == {"o1": ("outlook", "id-1"), "o2": ("outlook", "id-2"), "g1": ("gmail", "id-1"), "g2": ("gmail", "id-2")}

    # Listing again hands out the same handles for the same messages.
    assert mailboxes.ref("outlook", "id-2") == "o2" and mailboxes.ref("gmail", "id-3") == "g3"


def test_no_matches_and_errors_are_told_to_the_model_as_text(mailboxes, monkeypatch, caplog):
    monkeypatch.setattr(outlook, "search", lambda *a, **k: [])

    def refused(*a, **k):
        raise gmail.AuthError("the Gmail sign-in no longer works: run `python hunt.py gmail-login`")

    monkeypatch.setattr(gmail, "search", refused)

    with caplog.at_level(logging.WARNING, logger="chat"):
        answer, seen, _ = run([
            [("list_emails", {"account": "outlook", "query": "acme", "days": 7}), ("list_emails", {"account": "gmail"})],
            "sorry",
        ], mailboxes)

    assert returned(seen[1]) == ["No emails in outlook inbox matching 'acme' in the last 7 day(s).",
                            "Error (gmail): the Gmail sign-in no longer works: run `python hunt.py gmail-login`"]
    assert "gmail tool failed: the Gmail sign-in no longer works" in caplog.text


def test_account_that_is_not_set_up_is_reported():
    boxes = chat.Mailboxes(outlook_token="outlook_token.json")
    answer, seen, _ = run([[("list_emails", {"account": "gmail"})], "ok"], boxes)
    assert returned(seen[1]) == ["Error (gmail): gmail is not set up here; the owner can use: outlook"]


def test_a_token_file_that_is_not_a_sign_in_names_the_login_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "gid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "gsecret")
    (tmp_path / "gmail_token.json").write_text("{}")
    boxes = chat.Mailboxes(gmail_token=str(tmp_path / "gmail_token.json"))
    answer, seen, _ = run([[("list_emails", {"account": "gmail"})], "ok"], boxes)
    assert returned(seen[1]) == [f"Error (gmail): {tmp_path / 'gmail_token.json'} is not a saved sign-in: run `python hunt.py gmail-login`"]


def test_read_email_resolves_handles_and_caps_the_body(mailboxes, monkeypatch):
    reads = []

    def read(token, message_id):
        reads.append((token, message_id))
        return message(1, body="  Dear me,\n" + "y" * 20_000)

    monkeypatch.setattr(gmail, "read", read)
    mailboxes.refs["g1"] = ("gmail", "id-1")

    answer, seen, _ = run([[("read_email", {"handle": "G1 "})], [("read_email", {"handle": "o9"})], "ok"], mailboxes)

    assert reads == [("at-gmail", "id-1")]
    (text,) = returned(seen[1])
    assert text.startswith("g1 · Sun 18 Oct 09:01 · From: Ana <ana@acme.com> · To: me@example.com\nSubject: Subject 1\n"
                           "Link: https://mail/1\n\nDear me,\n")
    assert text.endswith("y\n[… 8009 more characters not shown]") and len(text) < chat.BODY_CHARS + 300
    assert returned(seen[2]) == ["unknown email handle 'o9'; use one that list_emails returned, like o1 or g2"]


def test_drafts_are_created_never_sent_and_reported_with_their_link(mailboxes, monkeypatch, caplog):
    created = []

    def create_draft(token, *, to, subject, body):
        created.append(("new", token, to, subject, body))
        return {"id": "d1", "web_link": "https://outlook.live.com/draft/1", "subject": subject, "to": to}

    def create_reply_draft(token, message_id, body):
        created.append(("reply", token, message_id, body))
        return {"id": "d2", "web_link": "https://mail.google.com/mail/#drafts", "subject": "Re: Subject 1", "to": ["ana@acme.com"]}

    monkeypatch.setattr(outlook, "create_draft", create_draft)
    monkeypatch.setattr(gmail, "create_reply_draft", create_reply_draft)
    mailboxes.refs["g1"] = ("gmail", "id-1")

    with caplog.at_level(logging.INFO, logger="chat"):
        answer, seen, _ = run([
            [("draft_email", {"account": "outlook", "to": [" ana@acme.com ", "bo@acme.com"], "subject": "Invoice",
                              "body": "Hi Ana,\n\nAttached.\n\nBest"})],
            [("draft_reply", {"handle": "g1", "body": "Tuesday 10:00 works.\n\nBest"})],
            [("draft_email", {"account": "gmail", "to": ["nobody"], "subject": "x", "body": "y"})],
            "Two drafts are waiting.",
        ], mailboxes)

    assert created == [("new", "at-outlook", ["ana@acme.com", "bo@acme.com"], "Invoice", "Hi Ana,\n\nAttached.\n\nBest"),
                       ("reply", "at-gmail", "id-1", "Tuesday 10:00 works.\n\nBest")]
    assert returned(seen[1]) == [("Draft created (not sent): to ana@acme.com, bo@acme.com, subject Invoice. "
                                                "Open it: https://outlook.live.com/draft/1")]
    assert returned(seen[2]) == [("Draft created (not sent): to ana@acme.com, subject Re: Subject 1. "
                                                "Open it: https://mail.google.com/mail/#drafts")]
    assert returned(seen[3]) == ["every recipient must be an email address"]
    assert "Hi Ana" not in caplog.text and "ana@acme.com" not in caplog.text  # drafts are private; the log counts only
    assert "outlook draft created to 2 recipient(s)" in caplog.text


def test_traces_carry_timings_tokens_and_tool_names_but_no_text(mailboxes, monkeypatch, capfire):
    """With LOGFIRE_TOKEN, hunt.py chat sends these traces to Logfire, which never scrubs model messages: no message,
    email, search, draft or answer text may be in them, only what shows where an answer went wrong or slow."""
    found = message(1, subject="PRIVATE-SUBJECT", from_name="PRIVATE-SENDER", preview="PRIVATE-PREVIEW")
    monkeypatch.setattr(outlook, "search", lambda *a, **k: [found])
    monkeypatch.setattr(outlook, "read", lambda token, message_id: {**found, "body": "PRIVATE-BODY"})
    monkeypatch.setattr(outlook, "create_reply_draft", lambda token, message_id, body: {
        "id": "d1", "web_link": "https://outlook.live.com/draft/1", "subject": "Re: PRIVATE-SUBJECT", "to": ["ana@acme.com"]})
    monkeypatch.setattr(gmail, "create_draft", lambda token, *, to, subject, body: {
        "id": "d2", "web_link": "https://mail.google.com/mail/#drafts", "subject": subject, "to": to})

    answer, _, _ = run([
        [("list_emails", {"account": "outlook", "query": "PRIVATE-QUERY"})],
        [("read_email", {"handle": "o1"})],
        [("draft_reply", {"handle": "o1", "body": "PRIVATE-REPLY"}),
         ("draft_email", {"account": "gmail", "to": ["PRIVATE-RECIPIENT@acme.com"], "subject": "PRIVATE-DRAFT-SUBJECT",
                          "body": "PRIVATE-DRAFT-BODY"})],
        "PRIVATE-ANSWER",
    ], mailboxes, text="PRIVATE-QUESTION")

    assert answer == "PRIVATE-ANSWER"
    spans = capfire.exporter.exported_spans_as_dict()
    assert {s["attributes"].get("gen_ai.tool.name") for s in spans} - {None} == {
        "list_emails", "read_email", "draft_reply", "draft_email"}
    assert sum(s["attributes"].get("gen_ai.usage.input_tokens", 0) for s in spans) > 0
    exported = json.dumps([spans, capfire.log_exporter.exported_logs_as_dicts()], default=str)
    assert "PRIVATE" not in exported and "ana@acme.com" not in exported


def test_traces_of_failed_answers_carry_no_text_either(mailboxes, monkeypatch, capfire):
    """A rejected tool call echoes its input back to the model, and an error's message can quote mail: neither may
    reach the traces. The failure itself shows, by its type."""
    def search(*a, **k):
        raise KeyError("PRIVATE-ERROR")

    monkeypatch.setattr(outlook, "search", search)
    model, _ = scripted([("list_emails", {"account": "PRIVATE-ACCOUNT"})], [("list_emails", {"account": "outlook"})])
    reply = chat.handle(chat.Conversation(chat.build_agent(model), mailboxes), "PRIVATE-QUESTION")

    assert reply == "Sorry, that failed: KeyError: 'PRIVATE-ERROR'"
    spans = capfire.exporter.exported_spans_as_dict()
    assert any(e["attributes"].get("exception.type") == "KeyError" for s in spans for e in s.get("events", []))
    exported = json.dumps([spans, capfire.log_exporter.exported_logs_as_dicts()], default=str)
    assert "PRIVATE" not in exported



def test_outlook_tools_refresh_a_write_scoped_token_once_and_search_graph(http, monkeypatch, tmp_path):
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "app-123")
    token_requests = []

    def token(request):
        token_requests.append({k: v[0] for k, v in parse_qs(request.body).items()})
        return 200, {}, json.dumps({"access_token": "at-1", "refresh_token": "rt-2"})

    http.add_callback(responses.POST, AUTH, callback=token, content_type="application/json")
    graph = http.get(INBOX, json={"value": [
        {"id": "AAMk1", "subject": "Hello", "receivedDateTime": "2026-10-18T09:12:00Z", "bodyPreview": "Hi",
         "from": {"emailAddress": {"name": "Ana", "address": "Ana@Acme.com"}}, "toRecipients": [], "webLink": "https://o/1"},
    ]})
    path = tmp_path / "outlook_token.json"
    path.write_text(json.dumps({"refresh_token": "rt-1"}))
    boxes = chat.Mailboxes(outlook_token=str(path))

    answer, seen, _ = run([[("list_emails", {"account": "outlook"})], [("list_emails", {"account": "outlook", "days": 0})], "ok"],
                          boxes)

    assert len(token_requests) == 1 and "Mail.ReadWrite" in token_requests[0]["scope"]
    assert len(graph.calls) == 2 and graph.calls[0].request.headers["Authorization"] == "Bearer at-1"
    assert returned(seen[1])[0].startswith("o1 · ")
    assert "Ana <ana@acme.com>" in returned(seen[1])[0]


def test_trim_keeps_the_last_turns_whole_within_the_size_budget():
    def turn(n, result="x"):
        return [ModelRequest(parts=[UserPromptPart(f"q{n}")]),
                ModelResponse(parts=[ToolCallPart(tool_name="list_emails", args={}, tool_call_id=f"c{n}")]),
                ModelRequest(parts=[ToolReturnPart(tool_name="list_emails", content=result, tool_call_id=f"c{n}")]),
                ModelResponse(parts=[TextPart(f"a{n}")])]

    history = turn(1) + turn(2) + turn(3)
    assert chat.trim(history, turns=2) == history[4:]
    assert chat.trim(history, turns=5) == history and chat.trim([], turns=2) == []

    heavy = turn(1, "y" * 500) + turn(2, "y" * 500) + turn(3, "y" * 500)
    assert chat.trim(heavy, turns=5, chars=1_100) == heavy[4:]  # two full emails fit, three do not
    assert chat.trim(heavy, turns=5, chars=10) == heavy[8:]  # the newest exchange always stays, whatever its size


def test_conversation_continues_from_its_history_and_new_resets_handles(mailboxes, monkeypatch):
    monkeypatch.setattr(outlook, "search", lambda *a, **k: [message(1)])
    model, seen = scripted([("list_emails", {"account": "outlook"})], "one email", "still here")
    conversation = chat.Conversation(chat.build_agent(model), mailboxes)

    assert chat.handle(conversation, "what's new?") == "one email"
    assert chat.handle(conversation, "thanks") == "still here"
    user_prompts = [p.content for p in seen[2].parts if isinstance(p, UserPromptPart)]
    assert user_prompts == ["thanks"] and len(conversation.history) == 6  # both turns are context for the next answer

    assert chat.handle(conversation, "/new") == "Fresh start. What do you want to know?"
    assert conversation.history == [] and mailboxes.refs == {}
    assert chat.handle(conversation, "/start") == chat.HELP == chat.handle(conversation, "/help")


def test_a_failing_answer_becomes_a_short_reply(mailboxes, caplog):
    def explode(messages, info):
        raise ValueError("model down")

    conversation = chat.Conversation(chat.build_agent(FunctionModel(explode)), mailboxes)
    with caplog.at_level(logging.ERROR, logger="chat"):
        reply = chat.handle(conversation, "hi")
    assert reply == "Sorry, that failed: ValueError: model down"
    assert "answer failed" in caplog.text


def test_tool_loops_stop_at_the_request_limit(mailboxes, monkeypatch):
    monkeypatch.setattr(outlook, "search", lambda *a, **k: [])
    model, seen = scripted(*([[("list_emails", {"account": "outlook"})]] * 30))
    conversation = chat.Conversation(chat.build_agent(model), mailboxes)
    reply = chat.handle(conversation, "loop")
    assert reply.startswith("Sorry, that failed: UsageLimitExceeded")
    assert len(seen) == chat.MAX_REQUESTS


def test_serve_answers_the_owner_only_and_splits_long_replies(mailboxes, monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    long_answer = "line\n" * 1000
    model, seen = scripted(long_answer, "second")
    stop = threading.Event()
    batches = [
        [{"update_id": 1, "message": {"chat": {"id": 7}, "text": "who are you?"}},
         {"update_id": 2, "message": {"chat": {"id": 42}, "text": "what's new?"}},
         {"update_id": 3, "message": {"chat": {"id": 42}, "photo": [{"file_id": "x"}]}}],
        [{"update_id": 4, "message": {"chat": {"id": 42}, "text": "thanks"}}],
    ]
    polls, sent = [], []

    def poll(offset, timeout=None):
        polls.append((offset, timeout))
        if timeout == 0:  # the confirmation right after a batch: nothing new yet
            return []
        if len(batches) == 1:
            stop.set()
        return batches.pop(0)

    with caplog.at_level(logging.INFO, logger="chat"):
        chat.serve(chat.build_agent(model), mailboxes, poll=poll, send=sent.append, stop=stop)

    assert polls == [(None, None), (4, 0), (4, None), (5, 0)]  # each batch is confirmed before it is answered
    assert len(sent) > 3 and all(len(m) <= 4096 for m in sent) and "\n".join(sent[:-2]) == long_answer
    assert sent[-2:] == ["I only read text messages.", "second"]
    assert "ignored a message from chat 7" in caplog.text and "who are you?" not in caplog.text
    assert "message received (11 chars)" in caplog.text and "what's new?" not in caplog.text


def test_serve_keeps_polling_after_telegram_errors(mailboxes, monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    stop = threading.Event()
    sleeps, sent = [], []
    attempts = iter([RuntimeError("Telegram request failed: ConnectionError"),
                     [{"update_id": 9, "message": {"chat": {"id": 42}, "text": "hi"}}]])

    def poll(offset, timeout=None):
        if timeout == 0:
            raise RuntimeError("Telegram request failed: ReadTimeout")
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        stop.set()
        return outcome

    def send(text):
        sent.append(text)
        raise RuntimeError("Telegram sendMessage failed: Bad Request (code 400)")

    model, _ = scripted("hello")
    with caplog.at_level(logging.WARNING, logger="chat"):
        chat.serve(chat.build_agent(model), mailboxes, poll=poll, send=send, sleep=sleeps.append, stop=stop)

    assert sleeps == [chat.RETRY_SECONDS] and sent == ["hello"]  # a failed confirmation does not drop the message
    assert "Telegram request failed: ConnectionError; polling again in 5s" in caplog.text
    assert "could not confirm the messages yet (Telegram request failed: ReadTimeout); the next poll will" in caplog.text
    assert "could not send the answer: Telegram sendMessage failed" in caplog.text


PROFILE = {"search": {"roles": ["growth engineer"]},
           "news": {"topics": ["growth engineering", "data analytics job market"]}}
EXPERIMENTS = "https://blog.example.com/experiments"
HIRING = "https://news.example.com/hiring-rebounds"
SENT = "https://old.example.com/sent"  # in news_state.json: an earlier digest considered it


def article(url, title):
    return result(url, title=title, highlights=[f"{title}: the details."]) | {"publishedDate": "2026-10-18T07:00:00.000Z"}


@pytest.fixture
def newsroom(mailboxes, exa, fake_claude, monkeypatch, tmp_path):
    """News digests set up on `mailboxes`. "growth engineering" finds EXPERIMENTS and HIRING; every other search
    finds HIRING and SENT. Claude picks the second article it is given."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    state = tmp_path / "news_state.json"
    state.write_text(json.dumps({SENT: datetime.now().isoformat(timespec="seconds")}))
    mailboxes.news = chat.News(profile=PROFILE, state=str(state))
    exa.on_search(lambda body: search_response(
        [article(EXPERIMENTS, "Experiment platforms in 2026"), article(HIRING, "Analytics hiring rebounds")]
        if body["query"] == "growth engineering"
        else [article(HIRING, "Analytics hiring rebounds"), article(SENT, "Already sent")]))
    claude = fake_claude({"overview": "Hiring is picking up.",
                          "picks": [{"number": 2, "takeaway": "Demand for your roles is up."}]})
    monkeypatch.setattr(scoring, "_client", lambda: claude)
    return SimpleNamespace(exa=exa, claude=claude, state=state)


def chatting(model, deps, *texts, send=None):
    """Runs chat.serve over the owner's `texts`, one message per poll. Returns the replies sent."""
    stop, sent = threading.Event(), []
    batches = [[{"update_id": n, "message": {"chat": {"id": 42}, "text": text}}] for n, text in enumerate(texts, 1)]

    def poll(offset, timeout=None):
        if timeout == 0:
            return []
        if len(batches) == 1:
            stop.set()
        return batches.pop(0)

    chat.serve(chat.build_agent(model), deps, poll=poll, send=send or sent.append, stop=stop)
    return sent


def test_a_digest_on_the_configured_topics_skips_and_records_what_digests_considered(mailboxes, newsroom):
    model, seen = scripted([("news_digest", {})], "Here it is.", [("news_digest", {})], "Nothing new.")

    assert chatting(model, mailboxes, "any news?", "and now?") == ["Here it is.", "Nothing new."]

    assert [b["query"] for b in newsroom.exa.requests()] == ["growth engineering", "data analytics job market"] * 2
    digest = returned(seen[1])[0]
    assert digest.startswith("Searched: growth engineering; data analytics job market (published in the last 7 day(s))\n")
    assert "Hiring is picking up." in digest
    assert f"• Analytics hiring rebounds (news.example.com)\nDemand for your roles is up.\n{HIRING}" in digest
    content = newsroom.claude.calls[0]["messages"][0]["content"]
    assert content.count("Analytics hiring rebounds (") == 1 and "Already sent" not in content
    # Recorded once the first answer was sent, so the second digest has nothing new and pays for no Claude call.
    assert set(json.loads(newsroom.state.read_text())) == {SENT, EXPERIMENTS, HIRING}
    assert "No new articles" in returned(seen[3])[0] and len(newsroom.claude.calls) == 1


def test_a_named_topic_is_searched_alone_and_neither_reads_nor_writes_the_state(mailboxes, newsroom):
    model, seen = scripted([("news_digest", {"topics": [" "]})],
                           [("news_digest", {"topics": [" DuckDB 2.0 ", ""], "days": 2})], "Here.")
    before = newsroom.state.read_text()

    assert chatting(model, mailboxes, "Give me the recent news about DuckDB 2.0") == ["Here."]

    # Blank topics are not a request for the digest on the configured ones, which would write the state.
    assert returned(seen[1]) == ["name a topic, or leave topics out for the owner's configured ones"]
    (body,) = newsroom.exa.requests()
    assert body["query"] == "DuckDB 2.0" and body["category"] == "news"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(body["startPublishedDate"].replace("Z", "+00:00"))
    assert timedelta(days=2) <= age < timedelta(days=2, minutes=5)
    digest = returned(seen[2])[0]
    assert digest.startswith("Searched: DuckDB 2.0 (published in the last 2 day(s))\n")
    # What an earlier digest considered is not left out of a one-off lookup.
    assert f"• Already sent (old.example.com)\nDemand for your roles is up.\n{SENT}" in digest
    content = newsroom.claude.calls[0]["messages"][0]["content"]
    assert '"DuckDB 2.0"' in content and "growth engineering" not in content
    assert newsroom.state.read_text() == before


def test_a_digest_that_never_reached_the_owner_is_not_recorded(mailboxes, newsroom):
    before = newsroom.state.read_text()

    def down_after_the_digest(messages, info):
        if returned(messages[-1]):
            raise ValueError("model down")
        return ModelResponse(parts=[ToolCallPart(tool_name="news_digest", args={})])

    assert chatting(FunctionModel(down_after_the_digest), mailboxes, "any news?") == [
        "Sorry, that failed: ValueError: model down"]
    assert newsroom.state.read_text() == before

    tried = []

    def refused_once(text):
        tried.append(text)
        if len(tried) == 1:
            raise RuntimeError("Telegram sendMessage failed: Bad Request (code 400)")

    model, _ = scripted([("news_digest", {})], "Here it is.")
    chatting(model, mailboxes, "any news?", "/help", send=refused_once)
    assert tried == ["Here it is.", chat.HELP]
    assert newsroom.state.read_text() == before  # the reply that did arrive was not the digest's

    def unreachable(**kwargs):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    def cut_off(**kwargs):  # pydantic's message quotes the output it rejects: only its first line is passed on
        TypeAdapter(news.Digest).validate_json('{"overview": "CUT-OFF-OVERVIEW", "picks": [{"number": 1, "takeaway": "De')

    # Claude returns no digest, cannot be reached or is cut off: the tool says so, and the articles are found again.
    newsroom.claude.fields = None
    for parse, told in ((newsroom.claude.parse, "RuntimeError: Claude returned no parsable news digest "
                                                "(stop_reason=end_turn)"),
                        (unreachable, "APIConnectionError: Connection error."),
                        (cut_off, "ValidationError: 1 validation error for Digest")):
        newsroom.claude.parse = parse
        model, seen = scripted([("news_digest", {})], "No digest today.")
        assert chatting(model, mailboxes, "any news?") == ["No digest today."]
        assert returned(seen[1]) == [f"Error (news): {told}"]
        assert newsroom.state.read_text() == before


def test_a_bug_inside_a_digest_fails_the_answer_with_its_traceback_in_the_log(mailboxes, newsroom, caplog):
    def bug(**kwargs):
        raise KeyError("search")

    newsroom.claude.parse = bug
    before = newsroom.state.read_text()
    model, _ = scripted([("news_digest", {})], "never said")

    with caplog.at_level(logging.ERROR, logger="chat"):
        assert chatting(model, mailboxes, "any news?") == ["Sorry, that failed: KeyError: 'search'"]

    assert "answer failed" in caplog.text and "Traceback (most recent call last)" in caplog.text
    assert newsroom.state.read_text() == before


def test_articles_jev_drops_from_a_digest_never_reach_claude_and_are_recorded(mailboxes, newsroom, jev_api):
    jev_api.on(lambda state, key, question: 0.1 if state["article"]["title"].startswith("Experiment") else 0.9)
    newsroom.claude.fields["picks"][0]["number"] = 1
    model, seen = scripted([("news_digest", {})], "Here it is.")

    chatting(model, mailboxes, "any news?")

    assert "Experiment platforms" not in newsroom.claude.calls[0]["messages"][0]["content"]
    assert f"• Analytics hiring rebounds (news.example.com)\nDemand for your roles is up.\n{HIRING}" in returned(seen[1])[0]
    assert set(json.loads(newsroom.state.read_text())) == {SENT, EXPERIMENTS, HIRING}  # not screened again next time


def test_a_digest_without_picks_says_so_and_still_records_its_articles(mailboxes, newsroom):
    newsroom.claude.fields["picks"] = []
    model, seen = scripted([("news_digest", {})], "Nothing worth your time.")

    chatting(model, mailboxes, "any news?")

    assert returned(seen[1]) == ["Searched: growth engineering; data analytics job market (published in the last 7 "
                                 "day(s))\nNone of the 2 article(s) found was worth a pick."]
    assert set(json.loads(newsroom.state.read_text())) == {SENT, EXPERIMENTS, HIRING}  # not read by Claude again


def test_a_topic_whose_search_fails_is_named_to_the_model(mailboxes, exa, tmp_path):
    mailboxes.news = chat.News(profile=PROFILE, state=str(tmp_path / "news_state.json"))
    exa.on_search(lambda body: error(400, "bad query"))

    _, seen, _ = run([[("news_digest", {"topics": ["dbt"]})], "ok"], mailboxes)

    digest = returned(seen[1])[0]
    assert digest.startswith("Searched: dbt (published in the last 7 day(s))\nNews digest ")
    assert digest.endswith(": 0 pick(s)\n\nCould not search:\n- dbt: ValueError")


def test_news_digest_says_what_is_missing_and_bounds_what_one_answer_spends(mailboxes, newsroom):
    _, seen, _ = run([[("news_digest", {})], "ok"], chat.Mailboxes(outlook_token="outlook_token.json"))
    assert returned(seen[1]) == ["Error (news): news digests are off: they need EXA_API_KEY and profile.toml"]

    mailboxes.news.profile = {"search": {"roles": ["growth engineer"]}}
    _, seen, _ = run([[("news_digest", {})], "ok"], mailboxes)
    assert returned(seen[1]) == ["Error (news): ValueError: [news].topics is empty"]

    mailboxes.news.profile = PROFILE
    newsroom.state.write_text("[]")  # found before the digest is paid for, not when its articles are recorded
    _, seen, _ = run([[("news_digest", {})], "ok"], mailboxes)
    assert returned(seen[1]) == [f"Error (news): ValueError: {newsroom.state} does not map article URLs to times"]

    mailboxes.news.state = str(newsroom.state.parent)  # a folder
    _, seen, _ = run([[("news_digest", {})], "ok"], mailboxes)
    assert returned(seen[1])[0].startswith("Error (news): IsADirectoryError: ")
    assert newsroom.exa.requests() == []

    digests = [[("news_digest", args)] for args in (
        {"topics": ["dbt"], "days": 4_000_000}, {"topics": ["dbt"], "days": 0},
        {"topics": ["a", "b", "c", "d", "x" * 100]}, {"topics": ["dbt"]})]
    _, seen, _ = run([[("news_digest", {"topics": list("abcdef")})], [("news_digest", {"topics": ["x" * 101]})],
                      *digests, "ok"], mailboxes)
    too_much = ["at most 5 topics per digest, each a few words (at most 100 characters)"]
    assert returned(seen[1]) == too_much and returned(seen[2]) == too_much
    assert [returned(seen[n])[0].split("\n")[0] for n in (3, 4)] == [
        "Searched: dbt (published in the last 90 day(s))", "Searched: dbt (published in the last 1 day(s))"]
    assert returned(seen[6]) == ["Error (news): at most 3 news digests per answer; the owner can ask again for more."]
    assert [b["query"] for b in newsroom.exa.requests()] == ["dbt", "dbt", "a", "b", "c", "d", "x" * 100]
    assert len(newsroom.claude.calls) == 3


def test_neither_the_log_nor_the_traces_carry_the_text_of_a_news_digest(mailboxes, newsroom, capfire, caplog):
    with caplog.at_level(logging.INFO):
        answer, _, _ = run([[("news_digest", {"topics": ["PRIVATE-TOPIC"]})], "PRIVATE-ANSWER"], mailboxes,
                           text="PRIVATE-QUESTION")

    assert answer == "PRIVATE-ANSWER"
    spans = capfire.exporter.exported_spans_as_dict()
    assert "news_digest" in {s["attributes"].get("gen_ai.tool.name") for s in spans}
    exported = json.dumps([spans, capfire.log_exporter.exported_logs_as_dicts()], default=str)
    for text in ("PRIVATE", "Analytics hiring rebounds", "Already sent", "Demand for your roles", HIRING):
        assert text not in exported and text not in caplog.text
    assert "news digest: 1 topic(s), 2 article(s), 1 pick(s), 1200 in / 300 out tokens" in caplog.text


def test_posthog_gets_an_answer_as_one_trace_and_a_conversation_as_one_session_without_any_text(
        mailboxes, newsroom, jev_api, posthog_api, monkeypatch):
    """PostHog gets no more than the Logfire traces hold: the model calls of an answer as one generation, a span per
    tool call, and the Jev and Claude calls a news digest made, all in the answer's trace."""
    monkeypatch.setattr(outlook, "search", lambda *a, **k: [message(1, subject="PRIVATE-SUBJECT", from_name="PRIVATE-SENDER")])
    jev_api.on(lambda state, key, question: 0.9)
    model, _ = scripted([("list_emails", {"account": "outlook", "query": "PRIVATE-QUERY"}),
                         ("news_digest", {"topics": ["PRIVATE-TOPIC"]})], "PRIVATE-ANSWER", "PRIVATE-SECOND", "PRIVATE-THIRD")
    conversation = chat.Conversation(chat.build_agent(model), mailboxes)

    assert chat.handle(conversation, "PRIVATE-QUESTION") == "PRIVATE-ANSWER"
    assert chat.handle(conversation, "and?") == "PRIVATE-SECOND"
    chat.handle(conversation, "/new")
    assert chat.handle(conversation, "again") == "PRIVATE-THIRD"

    events = posthog_api.events()
    traces = {}
    for event in events:
        properties = event["properties"]
        traces.setdefault((properties["$ai_session_id"], properties["$ai_trace_id"]), []).append(
            (event["event"], properties.get("$ai_provider") or properties["$ai_span_name"]))
    (first_session, _), (second_session, _), (third_session, _) = traces  # an answer is a trace
    assert first_session == second_session != third_session  # a conversation is a session: /new starts another
    first, second, third = traces.values()
    assert sorted(first) == [("$ai_generation", "anthropic"),  # the digest's picks
                             ("$ai_generation", "function"),  # the answer's own model calls, here the scripted model
                             ("$ai_generation", "typesafe"), ("$ai_generation", "typesafe"),  # the digest's two articles
                             ("$ai_span", "list_emails"), ("$ai_span", "news_digest")]
    assert second == third == [("$ai_generation", "function")]
    answered = next(e["properties"] for e in events if e["properties"].get("$ai_provider") == "function")
    assert answered["$ai_input_tokens"] > 0 and answered["$ai_output_tokens"] > 0 and answered["$ai_latency"] >= 0
    told = json.dumps(events)
    for text in ("PRIVATE", "Analytics hiring rebounds", "Already sent", "Demand for your roles", HIRING, "ana@acme.com"):
        assert text not in told


def test_a_typesafe_outage_during_one_digest_does_not_end_the_screen_for_later_ones(mailboxes, newsroom, jev_api):
    outage = [True]

    def relevance(state, key, question):
        if outage:
            raise requests.ConnectionError("refused")
        return 0.9

    jev_api.on(relevance)
    dbt = [("news_digest", {"topics": ["dbt"]})]
    model, _ = scripted(dbt, "One.", dbt, "Two.")
    conversation = chat.Conversation(chat.build_agent(model), mailboxes)

    conversation.answer("news about dbt?")  # Jev is down: Claude reads both articles
    outage.clear()
    conversation.answer("and again?")

    assert len(jev_api.requests) == jev.MAX_ATTEMPTS + 2  # the bot runs for weeks: the second digest is screened again


def test_a_state_file_that_cannot_be_updated_does_not_stop_the_bot(mailboxes, newsroom, tmp_path, caplog):
    sent = []

    def spoiled_meanwhile(text):  # between the digest and its answer the file stops being a state file
        newsroom.state.write_text("{not json")
        sent.append(text)

    model, _ = scripted([("news_digest", {})], "Here it is.", "You are welcome.")
    with caplog.at_level(logging.WARNING, logger="chat"):
        chatting(model, mailboxes, "any news?", "thanks", send=spoiled_meanwhile)
    assert sent == ["Here it is.", "You are welcome."] and "could not record the digest's articles" in caplog.text

    caplog.clear()
    mailboxes.news.state = str(tmp_path / "gone" / "news_state.json")  # a folder that does not exist
    model, _ = scripted([("news_digest", {})], "Here it is.", "You are welcome.")
    with caplog.at_level(logging.WARNING, logger="chat"):
        assert chatting(model, mailboxes, "any news?", "thanks") == ["Here it is.", "You are welcome."]
    assert "could not record the digest's articles" in caplog.text
