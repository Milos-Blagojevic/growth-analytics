# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pydantic-ai-harness[coder,anthropic]==0.66.0",
#     "requests==2.34.2",
#     # clients/posthog_client.py, at the versions requirements.txt pins:
#     "posthog==8.0.2",
#     "opentelemetry-api==1.45.2",
#     "opentelemetry-sdk==1.45.2",
#     "opentelemetry-exporter-otlp-proto-http==1.45.2",
# ]
# [tool.uv]
# exclude-newer = "2026-11-09T00:00:00Z"  # fixes the versions of the unpinned transitive dependencies too
# exclude-newer-package = { posthog = "2026-11-19T00:00:00Z" }  # 8.0.2 came out after that day
# ///
"""The factory worker: one code task, from an instruction to a pull request. Run by the Factory workflow.

    uv run factory/worker.py --repo OWNER/NAME --base BRANCH --task-id YYYYMMDD-HHMMSS --task TEXT
                             [--model MODEL] [--max-requests N] [--max-dollars N]

Paid: one Claude session per run (ANTHROPIC_API_KEY), stopped at --max-requests model requests or --max-dollars,
whichever comes first. It pushes a branch, opens a pull request and sends a Telegram message, so it needs
FACTORY_PUSH_TOKEN (contents and pull requests on the repository, nothing else), TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID, and Docker.

What happens, and where:

1. The job clones the repository at --base and builds the `factory-sandbox` image (tools and requirements-dev.txt).
2. A Pydantic AI Coder agent works on a copy of the clone in a container without network access. The container
   holds no token and none of this machine's environment: the agent cannot push, and nothing it runs can reach
   Anthropic, GitHub or Telegram.
3. What the agent changed leaves the container as one patch. The job applies it to its own clone, commits it,
   pushes the branch factory/<task id> and opens the pull request (a draft when the agent says the task is not
   finished). Git never runs on the job in a folder the agent wrote to, so a hook or a git setting planted in the
   container runs nowhere. A change under .github/workflows is refused.
4. The pull request's overview goes to Telegram and the job summary. So does a failure, with what the run used.

The exit status is 0 for a pull request or for "nothing needed changing", and 1 for a failure.

What the agent writes is text, wherever it lands. On GitHub (the commit message, the pull request) it cannot mention
people or refer to an issue (`inert`). In Telegram its report, and a failure's reason, are quoted line by line and
each file, check and unverified item is one list entry, so a line that is neither is the job's own (`quoted`,
`entry`). In the job's log, the runner's commands are off while the agent runs and while a message is printed
(`commands_off`).

With POSTHOG_PROJECT_TOKEN and POSTHOG_HOST set, PostHog hears that a task ran, for how long and how it ended, and
the run's model, token counts and seconds (clients/posthog_client.py). Never the task or anything the agent read
or wrote.

The dependencies are pinned here, not in requirements*.txt: pydantic-ai-harness needs a newer pydantic-ai-slim than
the one inbox/chat.py runs on. The chat bot imports this module for its limits and names (factory/intake.py), so
everything from the harness is imported inside the functions that need it.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))  # run as a script, the folder on the path is factory/: clients/ is one level up

from clients import github, posthog_client, telegram  # noqa: E402

WORKFLOW = "factory.yml"
REMOTE = "https://github.com/{repo}.git"
MODEL = "anthropic:claude-opus-5-5"
MAX_REQUESTS = 150  # the verify runs on this repository take 50-120; Pydantic AI's default of 50 cuts a task short
MAX_DOLLARS = 10  # enforced only while Pydantic AI knows the model's price (it does for MODEL); the request limit always is
MAX_TASK_CHARS = 2000
MAX_PATCH_BYTES = 2_000_000
# Telegram splits a message of more than telegram.MAX_MESSAGE_CHARS at a line break, and inside a line longer than
# that. Both limits below keep every line of the agent's words shorter, so each stays whole and keeps its mark.
REPORT_CHARS = 3000  # of the agent's report, or of a failure's reason, shown in Telegram; the pull request and the log hold the rest
LINE_CHARS = 300  # of each part of a list entry in Telegram: a file's name, a check's command and its result, an unverified item
LIST_ENTRIES = 30  # files, checks or unverified items listed in Telegram
TASK_ID = re.compile(r"\d{8}-\d{6}")
IMAGE = "factory-sandbox"
PROTECTED = ".github/workflows"  # the push token has no workflow permission either: GitHub would refuse the push
AUTHOR = ("factory", "factory@users.noreply.github.com")

# The clone is copied into /workspace at the start of a run. The virtualenv is outside it, so it is never part of
# the change, whatever the repository's .gitignore says.
SANDBOX_DOCKERFILE = """FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep curl sqlite3 \\
    && rm -rf /var/lib/apt/lists/* && git config --system --add safe.directory '*'
COPY requirements.txt requirements-dev.txt /tmp/requirements/
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements/requirements-dev.txt
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /workspace
"""

WORKER_INSTRUCTIONS = """\
You are the factory worker, running unattended: nobody can answer a question, so choose sensible defaults and \
finish. /workspace is a clone of the repository at the tip of its default branch, in a Docker container without \
network access and without credentials: you cannot install packages, reach any service, push or open a pull request. \
Python 3.13 with requirements-dev.txt installed is first on PATH (`python -m pytest` runs the suite).

Do the task the owner gives you and nothing else. Follow the repository's instructions (AGENTS.md), except for steps \
that need the network, a paid service, the owner or another agent: skip those and report them as unverified. Do not \
commit. When you finish, the job takes every change in the working tree (git-ignored files stay behind), commits it \
on a new branch and opens the pull request from your report; it refuses a change under .github/workflows. Run the \
tests that cover what you changed and report each command with its real outcome.

Your final output is the report. Set `done` to false when the task is not finished or a check fails: the pull \
request is then opened as a draft. When nothing needed changing, leave the tree as it is and say why in the summary."""

# Run in the container. Committed or not, everything since the base commit goes into one patch in a fixed format
# (a git setting the agent changed must not make it unreadable); the last line printed is the patch's size.
EXPORT = ("git add -A && git diff --cached --binary --full-index --no-color --no-ext-diff --no-textconv --no-renames "
          '--src-prefix=a/ --dst-prefix=b/ "$1" > .git/factory.patch && wc -c < .git/factory.patch')


class TaskError(Exception):
    """The task failed for a reason the owner can read as it is."""


class Check(BaseModel):
    command: str
    result: str = Field(description="the outcome, or SKIPPED with the reason")


class Report(BaseModel):
    """What the agent hands back. The job writes the commit, the pull request and the Telegram message from it."""

    title: str = Field(min_length=1, max_length=72, pattern=r"^[^\r\n]+$",
                       description="the pull request's title, on one line: what the change does, imperative mood")
    summary: str = Field(description="what changed and why, in a few plain sentences for the repository's owner")
    done: bool = Field(description="false when the task is not finished or a check fails; the summary says what is missing")
    checks: list[Check] = Field(description="the commands you ran to check the change, each with its real outcome")
    unverified: list[str] = Field(description="what you could not check here, and how the owner can")


def branch(task_id):
    return f"factory/{task_id}"


def run_name(task_id):
    """The run-name factory.yml gives a task's run, which is how the chat bot finds it again."""
    return f"factory {task_id}"


def git(*args, cwd, env=None):
    """Runs git on the job, never in the container, with hooks off: nothing a repository holds runs here."""
    done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if done.returncode:
        raise TaskError(f"git {args[0]} failed: {done.stderr.strip()[-500:]}")
    return done.stdout


def auth(token):
    """The environment for the git commands that talk to GitHub. The token travels in a request header set through
    the environment, so it is never written into the clone, which the container gets a copy of."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader", "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


def clone(url, base, dest, env):
    """Clones the branch `base` into dest and returns its commit."""
    git("clone", "-q", "--single-branch", "--branch", base, url, str(dest), cwd=dest.parent, env=env)
    return git("rev-parse", "HEAD", cwd=dest).strip()


def build_image(checkout, context):
    context.mkdir()
    for name in ("requirements.txt", "requirements-dev.txt"):
        shutil.copy(checkout / name, context / name)
    (context / "Dockerfile").write_text(SANDBOX_DOCKERFILE)
    subprocess.run(["docker", "build", "--quiet", "--tag", IMAGE, str(context)], check=True, stdout=subprocess.DEVNULL)


async def log_tool_calls(ctx, events):
    """Prints each tool call, so the job's log shows progress during a long run."""
    from pydantic_ai.messages import FunctionToolCallEvent

    async for event in events:
        if isinstance(event, FunctionToolCallEvent):
            args = event.part.args_as_dict()
            detail = args.get("command") or args.get("path") or args.get("pattern") or ""
            print(f"> {event.part.tool_name} {' '.join(str(detail).split())[:200]}", flush=True)


def make_agent(model):
    from pydantic_ai import Agent
    from pydantic_ai.output import ToolOutput
    from pydantic_ai_harness.coder import Coder

    # Extra output retries: a malformed report at the end would otherwise throw away the whole paid session.
    return Agent(model, output_type=ToolOutput(Report, max_retries=3),
                 capabilities=[Coder(sub_agents=False, instructions=WORKER_INSTRUCTIONS)])


async def export(workspace, base):
    """What the agent changed since the commit `base`, as one patch (b"" for nothing). The container's content is
    untrusted, so this is all that leaves it, and only as data."""
    done = await workspace.run(["sh", "-c", EXPORT, "export", base], timeout=300)
    try:
        size = int(done.stdout.split()[-1]) if done.exit_code == 0 else None
    except (ValueError, IndexError):
        size = None
    if size is None:
        raise TaskError(f"reading the change out of the container failed: {done.stderr.strip()[-500:]}")
    if size > MAX_PATCH_BYTES:
        raise TaskError(f"the change is {size:,} bytes, over the limit of {MAX_PATCH_BYTES:,}")
    patch = await workspace.read_bytes(".git/factory.patch")
    if len(patch) > MAX_PATCH_BYTES:  # the size above is what the container said; this is what it handed over
        raise TaskError(f"the change is {len(patch):,} bytes, over the limit of {MAX_PATCH_BYTES:,}")
    return patch


async def run_agent(agent, checkout, base, task, limits, usage):
    """Runs the agent in a new container holding a copy of checkout and returns (its report, the patch of what it
    changed). The container is removed afterwards, also on SIGTERM (GitHub's cancel). usage accumulates the run's
    usage, also when the run fails."""
    from pydantic_ai.workspaces import Workspace
    from pydantic_ai_harness.docker_sandbox import DockerSandbox, DockerSandboxBackend

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    backend = DockerSandboxBackend(image=IMAGE, allow_internet_access=False,
                                   docker_args=["--volume", f"{checkout}:/src:ro"])
    try:
        workspace = Workspace(backend)
        copy = await workspace.run(["cp", "-R", "/src/.", "/workspace/"], timeout=300)
        if copy.exit_code:
            raise TaskError(f"copying the clone into the container failed: {copy.stderr.strip()[-500:]}")
        result = await agent.run(task, workspace=backend, event_stream_handler=log_tool_calls, usage=usage,
                                 usage_limits=limits)
        return result.output, await export(workspace, base)
    finally:
        if backend.ref is not None:
            await DockerSandbox().destroy(backend.ref)


def apply(checkout, patch):
    """Applies the container's patch to the job's own clone and stages it. Returns the files it changes as
    (added, deleted, path), "-" for a binary file's counts. git refuses a patch that reaches outside the working
    tree or into .git, and this refuses one that touches a workflow."""
    file = checkout.parent / "change.patch"
    file.write_bytes(patch)
    git("apply", "--index", str(file), cwd=checkout)
    stat = git("diff", "--cached", "--numstat", "--no-renames", "-z", cwd=checkout)
    files = [tuple(entry.split("\t", 2)) for entry in stat.split("\0") if entry]
    workflows = [path for _, _, path in files if path == PROTECTED or path.startswith(PROTECTED + "/")]
    if workflows:
        raise TaskError(f"the change touches {', '.join(workflows)}: the factory never changes workflows, "
                        "so that part is yours to make")
    return files


def push(checkout, name, message, env):
    git("-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", message, cwd=checkout)
    git("push", "-q", "origin", f"HEAD:refs/heads/{name}", cwd=checkout, env=env)  # never forced: an existing branch stays


def cell(text):
    return inert(text).replace("|", "\\|").replace("\n", "<br>")


def inert(text):
    """The agent's words for GitHub, which mentions people and links or closes issues on the say of whoever wrote a
    pull request's text. A zero-width space after each @, each # and each `GH-` or `/issues/` or `/pull/` that a
    number follows leaves no mention and no issue reference in any form GitHub reads."""
    return re.sub(r"[@#]|GH-(?=\d)|/(?:issues|pull)/(?=\d)", lambda found: found.group() + "\u200b", text, flags=re.IGNORECASE)


def quoted(text):
    """Text that may hold the agent's words (its report, or a failure's reason with git's or the container's
    output), for Telegram: at most REPORT_CHARS of it, every line marked as a quote. A line without the mark is
    therefore the job's own, in whichever message Telegram's size limit puts it."""
    if len(text) > REPORT_CHARS:
        text = f"{text[:REPORT_CHARS]}\n[cut: {len(text) - REPORT_CHARS:,} more characters]"
    return "\n".join(f"> {line}" for line in text.splitlines())


def entry(text):
    """Text that may hold the agent's words as part of one list entry in Telegram: one line, at most LINE_CHARS."""
    text = " ".join(text.split())
    return text if len(text) <= LINE_CHARS else f"{text[:LINE_CHARS]} [cut]"


def listed(label, entries):
    more = [f"- and {len(entries) - LIST_ENTRIES} more"] if len(entries) > LIST_ENTRIES else []
    return "\n".join([label, *(f"- {item}" for item in entries[:LIST_ENTRIES]), *more])


def spend(usage):
    text = f"{usage.requests} model requests, {usage.input_tokens:,} input and {usage.output_tokens:,} output tokens"
    return text if usage.cost is None else f"{text}, ${usage.cost:.2f}"


def pull_body(report, task_id, task, usage):
    lines = [inert(report.summary), "", f"Task `{task_id}`:", "", *(f"> {line}" for line in task.splitlines()), ""]
    if report.checks:
        lines += ["Checks the agent ran:", "", "| Command | Result |", "|---|---|"]
        lines += [f"| {cell(c.command)} | {cell(c.result)} |" for c in report.checks]
    else:
        lines += ["The agent ran no checks."]
    if report.unverified:
        lines += ["", "Not verified:", "", *(f"- {cell(item)}" for item in report.unverified)]
    return "\n".join([*lines, "", f"Written by the factory worker (`factory/worker.py`). Usage: {spend(usage)}."])


def overview(heading, report, files, usage):
    """The Telegram message, as blocks (telegram.pack_blocks keeps each whole where it can): plain text. `heading`
    and the labels are the job's; what the agent wrote is quoted or is one list entry. With a pull request
    (`files`) the agent's title leads its summary."""
    words = f"{report.title}\n\n{report.summary}" if files else report.summary
    blocks = [heading, f"The agent's report:\n{quoted(words)}"]
    if files:  # a file's name can hold a newline: shown as JSON would write it
        blocks.append(listed("Files:", [
            f"{entry(json.dumps(path, ensure_ascii=False)[1:-1])} ({'binary' if added == '-' else f'+{added} -{deleted}'})"
            for added, deleted, path in files]))
    if report.checks:
        blocks.append(listed("Checks:", [f"{entry(c.command)}: {entry(c.result)}" for c in report.checks]))
    if report.unverified:
        blocks.append(listed("Not verified:", [entry(item) for item in report.unverified]))
    return [*blocks, f"Usage: {spend(usage)}"]


def run_reported(agent, checkout, base, prompt, limits, usage, model):
    """run_agent, as one PostHog trace holding one generation: the provider and model of `model`, the token counts
    and the seconds of the whole run. A run that failed is reported too: it was paid for."""
    started = time.monotonic()
    with posthog_client.agent_turn(posthog_client.new_session_id()), commands_off():  # the tool calls are printed
        try:
            return asyncio.run(run_agent(agent, checkout, base, prompt, limits, usage))
        finally:
            if usage.requests:
                provider, _, name = model.partition(":")
                posthog_client.generation(provider, name, input_tokens=usage.input_tokens,
                                          output_tokens=usage.output_tokens, latency=time.monotonic() - started)


def work(args, task, usage):
    """The task from clone to pull request. Returns the blocks of the Telegram message; raises TaskError."""
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    token = os.environ.get("FACTORY_PUSH_TOKEN")
    if not token:
        raise TaskError("FACTORY_PUSH_TOKEN is not set (an Actions secret; see factory/README.md)")
    agent = make_agent(args.model)
    limits = UsageLimits(request_limit=args.max_requests, cost_limit=Decimal(str(args.max_dollars)))
    prompt = f"Task {args.task_id}, from the repository's owner:\n\n{task}"
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp).resolve() / "clone"
        base = clone(REMOTE.format(repo=args.repo), args.base, checkout, auth(token))
        build_image(checkout, Path(tmp) / "image")
        try:
            report, patch = run_reported(agent, checkout, base, prompt, limits, usage, args.model)
        except UsageLimitExceeded as error:  # the container is gone and so is the unfinished work
            raise TaskError(f"the run stopped at its limit before the agent finished: {error}") from None
        files = apply(checkout, patch) if patch.strip() else []
        if not files:
            return overview(f"Code task {args.task_id}: nothing changed, so there is no pull request.", report, files, usage)
        name = branch(args.task_id)
        push(checkout, name, inert(f"{report.title}\n\n{report.summary}") + f"\n\nFactory task {args.task_id}", auth(token))
    try:
        pull = github.open_pull(args.repo, token, head=name, base=args.base, title=inert(report.title),
                                body=pull_body(report, args.task_id, task, usage), draft=not report.done)
    except RuntimeError as error:
        raise TaskError(f"the change is on the branch {name}, but opening the pull request failed: {error}") from None
    state = "pull request opened" if report.done else "draft pull request opened (the agent says it is not finished)"
    return overview(f"Code task {args.task_id}: {state}\n{pull['html_url']}", report, files, usage)


@contextmanager
def commands_off():
    """While this is open, the GitHub Actions runner takes nothing the job prints as a command to it (`::error::`,
    `::add-mask::`, `##[...]`): what is printed then holds the agent's words. Outside Actions it does nothing."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        yield
        return
    resume = secrets.token_hex(16)
    print(f"::stop-commands::{resume}", flush=True)
    try:
        yield
    finally:
        print(f"::{resume}::", flush=True)


def announce(blocks):
    """Prints the message, adds it to the job summary in GitHub Actions and sends it to Telegram. Returns whether
    Telegram took it."""
    text = "\n\n".join(blocks)
    with commands_off():
        print(text, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as out:
            out.write(text + "\n")
    try:
        for message in telegram.pack_blocks(blocks):
            telegram.send(message)
    except RuntimeError as error:
        print(f"Telegram did not take the message: {error}", file=sys.stderr)
        return False
    return True


def main():
    from pydantic_ai.usage import RunUsage

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="OWNER/NAME on GitHub")
    parser.add_argument("--base", required=True, help="the branch to work from and open the pull request into")
    parser.add_argument("--task-id", required=True, help="YYYYMMDD-HHMMSS: names the branch factory/<task id>")
    parser.add_argument("--task", required=True, help="what to change, as a self-contained instruction")
    parser.add_argument("--model", default=MODEL, help="PROVIDER:MODEL, as Pydantic AI names it")
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--max-dollars", type=float, default=MAX_DOLLARS)
    args = parser.parse_args()
    posthog_client.configure(os.environ.get("POSTHOG_PROJECT_TOKEN"), os.environ.get("POSTHOG_HOST"), service="factory")
    with posthog_client.command("task"):  # PostHog: that a task ran, for how long and with which exit code
        task = args.task.strip()
        if not TASK_ID.fullmatch(args.task_id):
            sys.exit(f"--task-id must look like 20261118-143000, not {args.task_id!r}")
        if not task or len(task) > MAX_TASK_CHARS:
            sys.exit(f"--task must be 1 to {MAX_TASK_CHARS} characters, not {len(task)}")
        if not telegram.configured():
            sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set: the task's outcome would reach nobody")
        usage = RunUsage()
        try:
            blocks = work(args, task, usage)
        except BaseException as error:  # also a cancel or timeout: a paid run without a pull request still says what it used
            reason = str(error) if isinstance(error, TaskError) else repr(error)  # may quote git or the container
            announce([f"Code task {args.task_id} failed.", f"Why:\n{quoted(reason)}", f"Usage: {spend(usage)}"])
            if isinstance(error, TaskError):
                sys.exit(1)
            raise
        if not announce(blocks):
            sys.exit(1)


if __name__ == "__main__":
    main()
