import json
import sys
import time
from pathlib import Path

import pytest

from dashboard import bot as chatbot
from dashboard import jobs
from dashboard.bot import Bot

# Stands in for hunt.py chat: records its arguments and working directory, prints a line, can sleep, exits as told.
STUB = """
import json, os, sys, time
with open("calls.jsonl", "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
print("chat bot up", flush=True)
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("STUB_SLEEP", raising=False)
    monkeypatch.delenv("STUB_EXIT", raising=False)
    (tmp_path / "stub_hunt.py").write_text(STUB)
    return tmp_path


def make(root, **kwargs):
    """A Bot on the stub with short delays; `kwargs["sleep"]` records the waits instead of sleeping them."""
    kwargs.setdefault("delays", (0.05, 0.2))
    return Bot(root, [sys.executable, str(root / "stub_hunt.py")], **kwargs)


def calls(root):
    path = root / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_starts_hunt_chat_in_the_root_and_restarts_it_with_growing_delays(root, monkeypatch):
    monkeypatch.setenv("STUB_EXIT", "3")
    sleeps = []  # the waits the bot asked for; recorded after each exit, so three of them mean three finished children
    bot = make(root, sleep=sleeps.append)
    bot.start()
    until(lambda: len(sleeps) >= 3)
    bot.stop()

    assert len(calls(root)) >= 3
    assert {c["args"][-1] for c in calls(root)} == {"chat"}
    assert {Path(c["cwd"]).resolve() for c in calls(root)} == {root.resolve()}
    # exit code 0 or not, the bot restarts; every exit here is a fast one, so the delay climbs and then stays
    assert sleeps[:3] == [0.05, 0.2, 0.2]
    status = bot.status()
    assert (status["running"], status["since"]) == (False, None) and status["starts"] >= 3
    log = bot.read_log()
    assert log.count("chat bot up") >= 3
    assert "[dashboard] hunt.py chat exited with code 3; restarting in 0.05 s" in log
    assert "[dashboard] hunt.py chat exited with code 3; restarting in 0.2 s" in log


def test_a_child_that_ran_long_enough_resets_the_backoff(root, monkeypatch):
    monkeypatch.setattr(chatbot, "FAST_EXIT_SECONDS", 0)  # every exit counts as a long run
    sleeps = []
    bot = make(root, sleep=sleeps.append)
    bot.start()
    until(lambda: len(sleeps) >= 3)
    bot.stop()
    assert sleeps[:3] == [0.05, 0.05, 0.05]


def test_stop_terminates_the_child_and_does_not_restart_it(root, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    (root / "jobs").mkdir()
    (root / "jobs" / "chat.log").write_text("from the previous dashboard process\n")
    sleeps = []
    bot = make(root, sleep=sleeps.append)
    bot.start()
    until(lambda: bot.status()["running"] and "chat bot up" in bot.read_log())

    status = bot.status()
    assert status["since"] and status["starts"] == 1 and status["last_exit"] is None and status["next_start"] is None
    assert "previous dashboard" not in bot.read_log()  # truncated at start
    assert not jobs.Runner(root).busy()  # the bot is not a job and takes no run lock

    started = time.monotonic()
    bot.stop()
    assert time.monotonic() - started < 5
    time.sleep(0.3)
    status = bot.status()
    assert (status["running"], status["starts"], status["last_exit"]) == (False, 1, -15)  # SIGTERM
    assert sleeps == [] and len(calls(root)) == 1
    assert not bot._thread.is_alive()


def test_stop_cuts_the_wait_before_a_restart_short(root, monkeypatch):
    monkeypatch.setenv("STUB_EXIT", "1")
    bot = make(root, delays=(30,))
    bot.start()
    until(lambda: bot.status()["next_start"] is not None)
    started = time.monotonic()
    bot.stop()
    assert time.monotonic() - started < 5
    assert bot.status()["starts"] == 1 and len(calls(root)) == 1


def test_a_command_that_cannot_start_is_retried_with_backoff_and_logged(tmp_path):
    sleeps = []
    bot = Bot(tmp_path, [str(tmp_path / "missing-python")], delays=(0.01, 0.02),
              sleep=lambda seconds: (sleeps.append(seconds), time.sleep(0.01)))
    bot.start()
    until(lambda: len(sleeps) >= 3)
    bot.stop()
    assert sleeps[:3] == [0.01, 0.02, 0.02]
    assert bot.status()["starts"] == 0 and not bot.status()["running"]
    assert "[dashboard] Could not start hunt.py chat:" in bot.read_log()


def test_read_log_returns_the_tail_of_a_long_log(root):
    bot = make(root)
    (root / "jobs").mkdir()
    bot.log_path.write_text("first line\n" + "x" * jobs.LOG_TAIL_BYTES + "\nlast line\n")
    tail = bot.read_log()
    assert tail.startswith("…\n") and tail.endswith("last line\n") and "first line" not in tail
    assert len(tail) <= jobs.LOG_TAIL_BYTES + 2
