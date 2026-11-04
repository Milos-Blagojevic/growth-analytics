"""The Telegram chat bot (`hunt.py chat`), kept running by the dashboard process (CHAT_BOT=1).

On Railway the dashboard is the only always-on process, so it starts `python hunt.py chat` (the hunt.py next to
this package, like dashboard jobs) as a child with the data folder as working directory, restarts it whenever it
exits (the bot never exits on its own: an exit means a crash or a lost connection) and stops it at shutdown.
Restarts back off while the child keeps dying soon after starting. Its output goes to jobs/chat.log, truncated when
the dashboard starts. The bot is not a dashboard job: it takes no run lock (it must not block `hunt.py mail` and
the Runs page), has no row in the jobs table and does not count for Runner.busy().
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from dashboard import jobs

LOG_FILE = "chat.log"
FAST_EXIT_SECONDS = 60  # an exit this soon after the start counts towards the restart backoff
STOP_TIMEOUT = 5  # seconds after SIGTERM before SIGKILL


class Bot:
    """Runs `hunt.py chat` as a child process and restarts it on exit, until stop(). Thread-safe.

    `delays` are the seconds waited before a restart, indexed by the number of consecutive exits within
    FAST_EXIT_SECONDS of the start (the last entry repeats); a child that lived longer starts over at the first.
    `sleep(seconds)` replaces that wait (tests); by default it waits on the stop event, so stop() cuts it short."""

    def __init__(self, root, command=None, *, delays=(10, 60, 300), sleep=None):
        self.root = Path(root)
        self.command = list(command or [sys.executable, str(jobs.HUNT_SCRIPT)])
        self.delays = tuple(delays)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._log = None
        self._proc = None
        self._since = None  # when the running child started
        self._starts = 0
        self._last_exit = None  # exit code of the previous child
        self._next_start = None  # when the next restart is due, while waiting

    @property
    def log_path(self):
        return self.root / jobs.JOBS_DIR / LOG_FILE

    def start(self):
        (self.root / jobs.JOBS_DIR).mkdir(exist_ok=True)
        self._log = open(self.log_path, "w")
        self._thread = threading.Thread(target=self._loop, name="hunt-chat", daemon=True)
        self._thread.start()

    def stop(self):
        """Stops the child (SIGTERM, then SIGKILL after STOP_TIMEOUT) without restarting it."""
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=STOP_TIMEOUT)
        if self._log:
            self._log.close()

    def status(self):
        """For the Runs page: running, since (ISO), starts, last_exit (the previous child's code), next_start (ISO)."""
        with self._lock:
            return {"running": self._proc is not None, "since": self._since, "starts": self._starts,
                    "last_exit": self._last_exit, "next_start": self._next_start}

    def read_log(self):
        return jobs.tail(self.log_path)

    def _note(self, text):
        self._log.write(f"[dashboard] {text}\n")
        self._log.flush()  # before the child writes through the same file

    def _loop(self):
        fast_exits = 0
        while True:
            started = time.monotonic()
            with self._lock:
                if self._stop.is_set():
                    return
                try:
                    proc = subprocess.Popen(
                        self.command + ["chat"],
                        cwd=self.root,
                        stdin=subprocess.DEVNULL,
                        stdout=self._log,
                        stderr=subprocess.STDOUT,
                        env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    )
                except OSError as exc:
                    self._note(f"Could not start hunt.py chat: {exc}")
                    proc, code = None, None
                else:
                    self._proc, self._since, self._next_start = proc, _now(), None
                    self._starts += 1
            if proc is not None:
                code = proc.wait()
                with self._lock:
                    self._proc, self._since, self._last_exit = None, None, code
            if self._stop.is_set():
                return
            fast_exits = fast_exits + 1 if time.monotonic() - started < FAST_EXIT_SECONDS else 0
            delay = self.delays[min(max(fast_exits - 1, 0), len(self.delays) - 1)]
            with self._lock:
                self._next_start = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            if proc is not None:
                self._note(f"hunt.py chat exited with code {code}; restarting in {delay} s")
            if self._sleep:
                self._sleep(delay)
            elif self._stop.wait(delay):
                return


def _now():
    return datetime.now().isoformat(timespec="seconds")
