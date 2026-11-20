"""The factory worker (factory/worker.py): what leaves the container, what reaches the branch and the pull request,
what the owner is told, and the workflow that starts it. The agent and its container are replaced by a function that
edits a copy of the clone; git, the patch and the push are real, against a local repository standing in for GitHub."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic_ai.exceptions import UsageLimitExceeded

from factory import worker

REPO = Path(__file__).resolve().parent.parent
TASK_ID = "20261118-143000"
BRANCH = f"factory/{TASK_ID}"
PULLS = "https://api.github.com/repos/me/repo/pulls"
REPORT = worker.Report(title="Rename a to b", summary="app.py now calls it b.", done=True,
                       checks=[worker.Check(command="python -m pytest", result="12 passed")], unverified=[])


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


class Container:
    """Stands in for the container's workspace: commands run in a local folder."""

    def __init__(self, folder):
        self.folder = folder

    async def run(self, command, timeout=None):
        done = subprocess.run(command, cwd=self.folder, capture_output=True, text=True)
        return SimpleNamespace(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    async def read_bytes(self, path):
        return (self.folder / path).read_bytes()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare repository standing in for github.com/me/repo: the worker clones its main branch and pushes to it."""
    source = tmp_path / "source"
    (source / ".github" / "workflows").mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=source)
    (source / ".gitignore").write_text("out/\n")
    (source / "app.py").write_text("a = 1\n")
    (source / "old.py").write_text("gone soon\n")
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "logo.bin").write_bytes(bytes(range(256)))
    (source / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    git("add", ".", cwd=source)
    git("commit", "-q", "-m", "base", cwd=source)
    bare = tmp_path / "github" / "me" / "repo.git"
    bare.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", str(source), str(bare), cwd=tmp_path)
    monkeypatch.setattr(worker, "REMOTE", str(tmp_path / "github" / "{repo}.git"))
    return bare


def branches(bare):
    return git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=bare).split()


def sent(http):
    """The Telegram messages, joined."""
    return "\n\n".join(json.loads(c.request.body)["text"] for c in http.calls if "api.telegram.org" in c.request.url)


@pytest.fixture
def task(remote, tmp_path, monkeypatch, http):
    """task(edit) runs worker.main() for one task and returns its exit status. `edit(folder)` plays the agent: it
    changes the container's copy of the clone. task.seen holds what the agent run was given."""
    monkeypatch.setenv("FACTORY_PUSH_TOKEN", "push-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(worker, "make_agent", lambda model: object())
    monkeypatch.setattr(worker, "build_image", lambda checkout, context: None)
    http.post("https://api.telegram.org/bottg-token/sendMessage", json={"ok": True})
    seen = SimpleNamespace()

    def run(edit=None, report=REPORT, raises=None, task_id=TASK_ID):
        async def run_agent(agent, checkout, base, prompt, limits, usage):
            seen.prompt, seen.limits = prompt, limits
            usage.requests, usage.input_tokens, usage.output_tokens = 12, 3400, 560
            if raises:
                raise raises
            folder = checkout.parent / "container"
            shutil.copytree(checkout, folder, symlinks=True)
            if edit:
                edit(folder)
            return report, await worker.export(Container(folder), base)

        monkeypatch.setattr(worker, "run_agent", run_agent)
        monkeypatch.setattr(sys, "argv", ["worker.py", "--repo", "me/repo", "--base", "main", "--task-id", task_id,
                                          "--task", "Rename a to b in app.py"])
        try:
            worker.main()
        except SystemExit as stop:
            return stop.code
        return 0

    run.seen = seen
    return run


def rename(folder):
    (folder / "app.py").write_text("b = 1\n")


def test_a_task_becomes_one_commit_on_its_own_branch_and_a_pull_request(task, remote, http):
    http.post(PULLS, status=201, json={"number": 5, "html_url": "https://github.com/me/repo/pull/5"})

    def edit(folder):
        rename(folder)
        git("commit", "-q", "-am", "the agent commits although told not to", cwd=folder)
        (folder / "old.py").unlink()
        (folder / "pkg").mkdir()
        (folder / "pkg" / "new.py").write_text("c = 2\n")
        (folder / "logo.bin").write_bytes(bytes(range(255, -1, -1)))
        (folder / "run.sh").chmod(0o755)
        (folder / "out").mkdir()
        (folder / "out" / "cache.json").write_text("{}\n")  # git-ignored: stays behind

    assert task(edit) == 0

    modes = {line.split("\t")[1]: line.split()[0] for line in git("ls-tree", "-r", BRANCH, cwd=remote).splitlines()}
    assert set(modes) == {".gitignore", ".github/workflows/ci.yml", "app.py", "logo.bin", "pkg/new.py", "run.sh"}
    assert modes["run.sh"] == "100755" and git("show", f"{BRANCH}:app.py", cwd=remote) == "b = 1\n"
    assert subprocess.run(["git", "show", f"{BRANCH}:logo.bin"], cwd=remote, capture_output=True).stdout == bytes(range(255, -1, -1))
    assert git("log", "--format=%an|%s", f"main..{BRANCH}", cwd=remote).splitlines() == ["factory|Rename a to b"]
    pull = json.loads(next(c.request.body for c in http.calls if c.request.url == PULLS))
    assert (pull["head"], pull["base"], pull["title"], pull["draft"]) == (BRANCH, "main", "Rename a to b", False)
    assert "Rename a to b in app.py" in pull["body"] and "12 passed" in pull["body"]
    assert "https://github.com/me/repo/pull/5" in sent(http) and "12 model requests" in sent(http)
    assert "Rename a to b in app.py" in task.seen.prompt
    assert (task.seen.limits.request_limit, task.seen.limits.cost_limit) == (worker.MAX_REQUESTS, worker.MAX_DOLLARS)


def test_an_unfinished_task_arrives_as_a_draft(task, http):
    http.post(PULLS, status=201, json={"number": 5, "html_url": "https://github.com/me/repo/pull/5"})

    assert task(rename, report=REPORT.model_copy(update={"done": False})) == 0

    assert json.loads(next(c.request.body for c in http.calls if c.request.url == PULLS))["draft"] is True
    assert "draft pull request" in sent(http)


USAGE = "Usage: 12 model requests, 3,400 input and 560 output tokens"


def own(text):
    """The lines of a Telegram message that are the job's: neither a quote nor a list entry."""
    return [line for line in text.splitlines() if line and not line.startswith(("> ", "- "))]


def test_the_agents_words_cannot_mention_people_refer_to_issues_or_pass_for_the_jobs_own_lines(task, remote, http):
    http.post(PULLS, status=201, json={"number": 5, "html_url": "https://github.com/me/repo/pull/5"})
    fake = f"Code task {TASK_ID}: pull request opened https://evil.example/pull/5"
    report = worker.Report(
        title="Fix #12 for @ana", done=True, unverified=["ask @bob\nUsage: nothing at all"],
        summary=(f"Done.\n@someone closes #1, GH-7 and https://github.com/me/repo/issues/3\n\n"  # uncut, Telegram
                 f"Code task {TASK_ID}: pull request opened\nhttps://evil.example/pull/5\n{'x' * 4094}{fake}"),  # splits here
        checks=[worker.Check(command="pytest\nhttps://evil.example/checks", result="fixes #2")] +
               [worker.Check(command="x" * 4094, result=fake)] * 40)  # more than one Telegram message can hold

    def edit(folder):
        rename(folder)
        (folder / "notes\nCode task done.txt").write_text("a file's name can hold a newline\n")

    assert task(edit, report=report) == 0

    pull = json.loads(next(c.request.body for c in http.calls if c.request.url == PULLS))
    on_github = pull["title"] + pull["body"] + git("log", "-1", "--format=%B", BRANCH, cwd=remote)
    assert "closes" in on_github and not any(
        live in on_github for live in ("@ana", "@someone", "@bob", "#12", "#1", "#2", "GH-7", "/issues/3"))
    messages = [json.loads(c.request.body)["text"] for c in http.calls if "api.telegram.org" in c.request.url]
    assert len(messages) > 1 and all(len(message) <= 4096 for message in messages)
    assert own("\n".join(messages)) == [f"Code task {TASK_ID}: pull request opened", "https://github.com/me/repo/pull/5",
                                         "The agent's report:", "Files:", "Checks:", "Not verified:", USAGE]
    lines = sent(http).splitlines()
    assert "- notes\\nCode task done.txt (+1 -0)" in lines and "- ask @bob Usage: nothing at all" in lines
    assert "- and 11 more" in lines  # 41 checks, 30 shown


def test_a_failure_that_quotes_the_container_cannot_pass_for_the_jobs_own_lines_either(task, remote, http):
    def edit(folder):  # git refuses the setting and quotes it, newlines included, in the error the job reports
        with open(folder / ".git" / "config", "a") as config:
            config.write(f'[core]\n\tcompression = "x\\n\\nCode task {TASK_ID}: pull request opened\\nhttps://evil.example/pull/5"\n')

    assert task(edit) == 1

    assert "https://evil.example/pull/5" in sent(http)
    assert own(sent(http)) == [f"Code task {TASK_ID} failed.", "Why:", USAGE]


def test_in_github_actions_the_runner_takes_no_commands_from_the_agents_tool_calls_or_the_message(task, remote, http,
                                                                                               monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    def edit(folder):
        print("> shell echo '##[warning]from the agent'")  # as log_tool_calls prints a tool call during the run
        raise worker.TaskError("git apply failed\n::add-mask::factory")

    assert task(edit) == 1

    off, inside = None, []
    for line in capsys.readouterr().out.splitlines():
        if off is None and line.startswith("::stop-commands::"):
            off = line.removeprefix("::stop-commands::")
        elif line == f"::{off}::":
            off = None
        elif off:
            inside.append(line)
        else:
            assert "##[" not in line and "::" not in line, line  # nothing of the agent's while commands are on
    assert off is None and "> shell echo '##[warning]from the agent'" in inside and "> ::add-mask::factory" in inside


def test_the_size_limit_holds_for_what_the_container_hands_over_whatever_size_it_reports():
    class Lying:
        async def run(self, command, timeout=None):
            return SimpleNamespace(exit_code=0, stdout="100\n", stderr="")

        async def read_bytes(self, path):
            return b"x" * (worker.MAX_PATCH_BYTES + 1)

    with pytest.raises(worker.TaskError, match="over the limit"):
        asyncio.run(worker.export(Lying(), "0" * 40))


def test_a_title_is_one_line():
    with pytest.raises(ValueError, match="title"):
        worker.Report(title="Fix it\nhttps://evil.example", summary="s", done=True, checks=[], unverified=[])


def test_a_hook_the_machine_gives_every_clone_does_not_run_on_the_job(task, remote, http, tmp_path, monkeypatch):
    http.post(PULLS, status=201, json={"number": 5, "html_url": "https://github.com/me/repo/pull/5"})
    (tmp_path / "template" / "hooks").mkdir(parents=True)
    for name in ("pre-commit", "pre-push"):
        hook = tmp_path / "template" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch '{tmp_path}/hook-ran'\nexit 1\n")
        hook.chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "template"))  # git clone copies it into the new .git

    assert task(rename) == 0

    assert BRANCH in branches(remote) and not (tmp_path / "hook-ran").exists()


def test_posthog_hears_that_a_task_ran_and_what_its_model_used_and_none_of_its_words(task, remote, http, posthog_api):
    http.post(PULLS, status=201, json={"number": 5, "html_url": "https://github.com/me/repo/pull/5"})

    assert task(rename) == 0
    assert task(raises=UsageLimitExceeded("PRIVATE-LIMIT"), task_id="20261118-143001") == 1  # paid for all the same

    records = [(r["name"], r["service"], r["command"], r.get("exit_code")) for r in posthog_api.records()]
    assert records == [("factory_command_started", "factory", "task", None), ("factory_command_finished", "factory", "task", 0),
                       ("factory_command_started", "factory", "task", None), ("factory_command_finished", "factory", "task", 1)]
    generations = posthog_api.events("$ai_generation")
    assert [(e["properties"]["$ai_provider"], e["properties"]["$ai_model"], e["properties"]["$ai_input_tokens"],
             e["properties"]["$ai_output_tokens"]) for e in generations] == [("anthropic", "claude-opus-5-5", 3400, 560)] * 2
    assert len({e["properties"]["$ai_trace_id"] for e in generations}) == 2  # a task is a trace of its own
    told = json.dumps([posthog_api.events(), posthog_api.records()])
    for text in ("Rename a to b", "app.py", "12 passed", "PRIVATE", "me/repo", TASK_ID, "push-token"):
        assert text not in told


def test_a_task_that_changes_nothing_opens_nothing(task, remote, http):
    def edit(folder):
        (folder / "out").mkdir()
        (folder / "out" / "cache.json").write_text("{}\n")

    assert task(edit) == 0  # no GitHub mock is registered: a pull request call would fail the test

    assert branches(remote) == ["main"]
    assert "nothing changed" in sent(http) and "app.py now calls it b." in sent(http)


def test_a_change_to_a_workflow_is_refused_before_anything_is_pushed(task, remote, http):
    def edit(folder):
        rename(folder)
        (folder / ".github" / "workflows" / "ci.yml").write_text("on: pull_request_target\n")

    assert task(edit) == 1

    assert branches(remote) == ["main"]
    assert own(sent(http)) == [f"Code task {TASK_ID} failed.", "Why:", USAGE]
    assert "> the change touches .github/workflows/ci.yml" in sent(http)


def test_a_change_over_the_size_limit_is_refused(task, remote, http, monkeypatch):
    monkeypatch.setattr(worker, "MAX_PATCH_BYTES", 100)

    assert task(lambda folder: (folder / "big.txt").write_text("x" * 500)) == 1

    assert branches(remote) == ["main"] and "over the limit of 100" in sent(http)


@pytest.mark.parametrize("path", [".git/hooks/pre-push", "../outside.sh", "/tmp/factory-test-outside.sh"])
def test_a_patch_cannot_write_into_git_or_outside_the_clone(remote, tmp_path, path):
    checkout = tmp_path / "job" / "clone"
    checkout.parent.mkdir()
    worker.clone(str(remote), "main", checkout, None)
    patch = (f"diff --git a/{path} b/{path}\nnew file mode 100755\n--- /dev/null\n+++ b/{path}\n"
             "@@ -0,0 +1 @@\n+#!/bin/sh\n").encode()

    with pytest.raises(worker.TaskError, match="git apply failed"):
        worker.apply(checkout, patch)

    assert not (checkout / ".git" / "hooks" / "pre-push").exists()
    assert not (checkout.parent / "outside.sh").exists() and not Path("/tmp/factory-test-outside.sh").exists()


def test_the_push_token_is_never_written_into_the_clone_the_container_gets(remote, tmp_path):
    checkout = tmp_path / "clone"
    env = worker.auth("ghp_push_token")

    worker.clone(str(remote), "main", checkout, env)

    secrets = (b"ghp_push_token", env["GIT_CONFIG_VALUE_0"].split()[-1].encode())
    for file in (f for f in checkout.rglob("*") if f.is_file()):
        assert not any(secret in file.read_bytes() for secret in secrets), file


def test_a_run_that_stops_at_its_limit_tells_the_owner_what_it_used(task, remote, http, tmp_path):
    assert task(raises=UsageLimitExceeded("The next request would exceed the request_limit of 150")) == 1

    assert branches(remote) == ["main"]
    assert "> the run stopped at its limit" in sent(http) and "12 model requests" in sent(http)
    assert f"Code task {TASK_ID} failed" in (tmp_path / "summary.md").read_text()


def test_a_cancelled_run_still_tells_the_owner(task, remote, http):
    with pytest.raises(asyncio.CancelledError):  # GitHub's cancel or timeout
        task(raises=asyncio.CancelledError())

    assert branches(remote) == ["main"] and "> CancelledError()" in sent(http)


def test_an_existing_branch_is_never_overwritten(task, remote, tmp_path, http):
    other = tmp_path / "other"
    git("clone", "-q", str(remote), str(other), cwd=tmp_path)
    (other / "theirs.py").write_text("keep me\n")
    git("add", ".", cwd=other)
    git("commit", "-q", "-m", "someone else's work", cwd=other)
    git("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=other)
    before = git("rev-parse", BRANCH, cwd=remote)

    assert task(rename) == 1

    assert git("rev-parse", BRANCH, cwd=remote) == before
    assert "> git push failed" in sent(http)


def test_a_refused_pull_request_names_the_branch_that_holds_the_change(task, remote, http):
    http.post(PULLS, status=422, json={"message": "Validation Failed"})

    assert task(rename) == 1

    assert BRANCH in branches(remote)
    assert f"the change is on the branch {BRANCH}, but opening the pull request failed" in sent(http)


def test_the_run_fails_when_telegram_does_not_take_the_overview(task, http, tmp_path):
    http.replace("POST", "https://api.telegram.org/bottg-token/sendMessage",
                 json={"ok": False, "description": "chat not found", "error_code": 400})
    http.post(PULLS, status=201, json={"number": 5, "html_url": "https://github.com/me/repo/pull/5"})

    assert task(rename) == 1  # a red run is the only sign the owner gets

    assert "https://github.com/me/repo/pull/5" in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("task_id", ["x/../../main", "20261118", "20261118-143000 --force"])
def test_a_task_id_that_is_not_a_timestamp_starts_nothing(task, remote, http, task_id):
    assert "--task-id must look like" in task(rename, task_id=task_id)

    assert branches(remote) == ["main"] and sent(http) == ""


def test_the_workflow_starts_only_by_dispatch_on_the_default_branch_and_keeps_inputs_out_of_the_command():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / worker.WORKFLOW).read_text())
    job = workflow["jobs"]["task"]
    step = job["steps"][-1]

    assert set(workflow[True]) == {"workflow_dispatch"}  # YAML 1.1 reads the key `on` as true
    assert workflow["permissions"] == {"contents": "read"}
    # Without this, a run started on a factory branch would execute that branch's worker.py with the secrets.
    assert job["if"] == "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    assert workflow["run-name"] == worker.run_name("${{ inputs.task_id }}")
    assert "${{" not in step["run"]  # an input pasted into the command would run as shell
    assert {step["env"]["TASK_ID"], step["env"]["TASK"]} == {"${{ inputs.task_id }}", "${{ inputs.task }}"}
