import json
import logging
import threading
from urllib.parse import parse_qs

import pytest
import responses
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

import chat
import gmail
import outlook

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
