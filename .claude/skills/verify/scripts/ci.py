# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic-ai-harness[coder,anthropic]==0.66.0"]
# [tool.uv]
# exclude-newer = "2026-11-09T00:00:00Z"  # fixes the versions of the unpinned transitive dependencies too
# ///
"""Run the verify skill unattended, for CI: a Pydantic AI Coder agent on a copy of the checkout in a Docker container.

    uv run .claude/skills/verify/scripts/ci.py [--intent TEXT] [--base REF] [--model MODEL] [--max-requests N]

Paid: one Claude session per run (ANTHROPIC_API_KEY), at most --max-requests model requests. Needs Docker; the
container is removed afterwards, the `verify-sandbox` image (tools and requirements-dev.txt) is kept for the next run.

The container holds HEAD's history and the working tree's tracked and untracked files, never git-ignored ones (.env,
the token files, hunt.db, ...). It has no network and none of this machine's environment, so nothing the agent runs
can reach Exa, Anthropic or Telegram or read the API key, and it works on its own copy, so this checkout is never
changed. The prompt is SKILL.md with --intent and `--base <commit>` (default: git merge-base HEAD origin/main) in
place of $ARGUMENTS. The report goes to stdout and, in GitHub Actions, to the job summary; the exit status is 0 for
SHIP and 1 for BLOCK or FIX BEFORE MERGE.

The dependencies are pinned here, not in requirements*.txt: pydantic-ai-harness needs a newer pydantic-ai-slim than
the one inbox/chat.py runs on.
"""

import argparse
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[4]
SKILL = REPO / ".claude/skills/verify/SKILL.md"
MODEL = "anthropic:claude-opus-5-5"
MAX_REQUESTS = 150  # a full verify takes roughly 50-120; Pydantic AI's default limit of 50 cuts it short
IMAGE = "verify-sandbox"

# The checkout is copied into /workspace at the start of a run, next to the .venv built here.
SANDBOX_DOCKERFILE = """FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep curl sqlite3 \\
    && rm -rf /var/lib/apt/lists/* && git config --system --add safe.directory '*'
COPY requirements.txt requirements-dev.txt /tmp/requirements/
RUN python -m venv /workspace/.venv \\
    && /workspace/.venv/bin/pip install --no-cache-dir -r /tmp/requirements/requirements-dev.txt
WORKDIR /workspace
"""

CI_INSTRUCTIONS = """\
You are the verifier of the verify skill, running unattended in CI. /workspace is a copy of the checkout in a Docker \
container without network access: no private files, no API keys, no `origin/main` (the base commit is the `--base` \
in the arguments) and no `docker` (report the Docker check SKIPPED). `.venv` already holds requirements-dev.txt on \
Python 3.13; Python 3.11 is not installed (CI's pytest job runs the suite on 3.11 and 3.13). The skill's hard limits \
still apply. Report, never fix. Your final output is the step 7 report as structured data: the runner derives the \
verdict from the severities."""

Severity = Literal["S0", "S1", "S2", "S3"]


class Risk(BaseModel):
    severity: Severity
    risk: str
    evidence: str = Field(description="file:line, a command and its output, or a failing probe test")
    repro: str
    fix_direction: str


class Unverified(BaseModel):
    concern: str
    settle: str = Field(description="what would confirm or rule it out")


class Check(BaseModel):
    check: str
    command: str
    result: str = Field(description="the outcome, or SKIPPED with the reason")


class Report(BaseModel):
    """The step 7 report without its verdict line, which follows from the severities."""

    change: str = Field(description="what the diff does, in one or two lines; where it differs from the claim")
    risks: list[Risk] = Field(description="highest impact first")
    unverified: list[Unverified]
    checks: list[Check]


def verdict(risks):
    """The skill's rule: BLOCK for any S0 or S1, FIX BEFORE MERGE for any S2, otherwise SHIP."""
    severities = {risk.severity for risk in risks}
    if severities & {"S0", "S1"}:
        return "BLOCK"
    return "FIX BEFORE MERGE" if "S2" in severities else "SHIP"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def stage(repo, dest, base):
    """Copies HEAD's history and repo's tracked and untracked files to dest, uncommitted changes included; dest gets
    no remote. A file HEAD does not have (untracked, or staged as with `git add -f .env`) is copied only if both the
    working tree's ignore rules and base's root .gitignore on its own allow it, so a branch can neither drop nor
    negate (`!.env`) a rule of base's. Left out: files reached through a symlinked folder, nested repositories and
    submodules."""
    bundle, base_rules = dest.with_suffix(".bundle"), dest.with_suffix(".gitignore")
    git("bundle", "create", "-q", str(bundle), "HEAD", cwd=repo)
    git("-c", "advice.detachedHead=false", "clone", "-q", str(bundle), str(dest), cwd=repo)
    bundle.unlink()
    git("remote", "remove", "origin", cwd=dest)
    for name in filter(None, git("ls-files", "-z", cwd=dest).split("\0")):
        if not (dest / name).is_dir():  # a submodule stays an empty folder
            (dest / name).unlink()
    rules = subprocess.run(["git", "show", f"{base}:.gitignore"], cwd=repo, capture_output=True, text=True)
    base_rules.write_text(rules.stdout if rules.returncode == 0 else "")  # a base without .gitignore adds no rules

    def listed(*args):
        return set(filter(None, git("ls-files", "-z", *args, cwd=repo).split("\0")))

    in_head = set(filter(None, git("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=repo).split("\0")))
    staged = listed("--cached") - (listed("--cached", "--ignored", f"--exclude-from={base_rules}") - in_head)
    untracked = listed("--others", "--exclude-standard") & listed("--others", f"--exclude-from={base_rules}")
    base_rules.unlink()
    real_repo = repo.resolve()
    for name in sorted(staged | untracked):
        source = repo / name
        if source.is_dir() and not source.is_symlink() or name.endswith("/"):  # submodule or nested repository
            continue
        if source.parent.resolve() != real_repo / Path(name).parent:  # reached through a symlinked folder
            continue
        if source.exists() or source.is_symlink():  # tracked files deleted in the working tree stay deleted
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / name, follow_symlinks=False)


def build_image(checkout, context):
    context.mkdir()
    for name in ("requirements.txt", "requirements-dev.txt"):
        shutil.copy(checkout / name, context / name)
    (context / "Dockerfile").write_text(SANDBOX_DOCKERFILE)
    subprocess.run(["docker", "build", "--quiet", "--tag", IMAGE, str(context)], check=True, stdout=subprocess.DEVNULL)


def prompt(arguments):
    _, _, body = SKILL.read_text().split("---", 2)  # drops the frontmatter
    return body.strip().replace("$ARGUMENTS", arguments)


async def log_tool_calls(ctx, events):
    """Prints each tool call, so the CI log shows progress during a long run."""
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
                 capabilities=[Coder(sub_agents=False, instructions=CI_INSTRUCTIONS)])


async def verify(agent, checkout, arguments, max_requests, usage):
    """Runs the agent in a new container holding a copy of checkout, and removes the container afterwards, also on
    SIGTERM (GitHub's cancel). usage accumulates the run's usage, also when the run fails."""
    from pydantic_ai.usage import UsageLimits
    from pydantic_ai.workspaces import Workspace
    from pydantic_ai_harness.docker_sandbox import DockerSandbox, DockerSandboxBackend

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    backend = DockerSandboxBackend(image=IMAGE, allow_internet_access=False,
                                   docker_args=["--volume", f"{checkout}:/src:ro"])
    try:
        copy = await Workspace(backend).run(["cp", "-R", "/src/.", "/workspace/"], timeout=300)
        if copy.exit_code:
            raise RuntimeError(f"copying the checkout into the container failed: {copy.stderr}")
        return await agent.run(prompt(arguments), workspace=backend, event_stream_handler=log_tool_calls,
                               usage=usage, usage_limits=UsageLimits(request_limit=max_requests))
    finally:
        if backend.ref is not None:
            await DockerSandbox().destroy(backend.ref)


def cell(text):
    return text.replace("|", "\\|").replace("\n", "<br>")


def tokens(usage):
    return f"{usage.requests} model requests, {usage.input_tokens:,} input and {usage.output_tokens:,} output tokens"


def spend(result):
    from pydantic_ai.messages import ModelResponse

    try:
        dollars = sum(m.cost().total_price for m in result.all_messages() if isinstance(m, ModelResponse))
    except Exception:  # pricing is best effort (unknown model, unmeasured usage); the report must still go out
        return tokens(result.usage)
    return f"{tokens(result.usage)}, ${dollars:.2f}"


def render(report, usage):
    risks = sorted(report.risks, key=lambda risk: risk.severity)  # stable: keeps the likelihood order within one
    lines = [f"Verdict: {verdict(risks)}", "", f"Change: {report.change}", "", "Risks, highest impact first", "",
             "| # | Sev | Risk | Evidence | Repro | Fix direction |", "|---|---|---|---|---|---|"]
    lines += [f"| {n} | {r.severity} | {cell(r.risk)} | {cell(r.evidence)} | {cell(r.repro)} | {cell(r.fix_direction)} |"
              for n, r in enumerate(risks, 1)]
    lines += ["", "Unverified", ""] + [f"- {cell(u.concern)}: {cell(u.settle)}" for u in report.unverified]
    lines += ["", "Checks run", "", "| Check | Command | Result |", "|---|---|---|"]
    lines += [f"| {cell(c.check)} | {cell(c.command)} | {cell(c.result)} |" for c in report.checks]
    return "\n".join([*lines, "", f"Usage: {usage}"])


def publish(text):
    """Prints text and, in GitHub Actions, adds it to the job summary."""
    print(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as out:
            out.write(text + "\n")


def main():
    from pydantic_ai.usage import RunUsage

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--intent", default="", help="what the change should do (empty: inferred from the diff)")
    parser.add_argument("--base", help="base commit (default: git merge-base HEAD origin/main)")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS)
    args = parser.parse_args()
    ref = args.base or git("merge-base", "HEAD", "origin/main", cwd=REPO).strip()
    base = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=REPO).strip()
    if subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"], cwd=REPO).returncode:
        sys.exit(f"--base {ref} is not an ancestor of HEAD: the container gets HEAD's history only")
    agent = make_agent(args.model)  # fails here, before the image build, on a missing API key
    usage = RunUsage()
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp).resolve() / "checkout"
        stage(REPO, checkout, base)
        build_image(checkout, Path(tmp) / "image")
        try:
            result = asyncio.run(verify(agent, checkout, f"{args.intent.strip()}\n\n--base {base}".strip(),
                                        args.max_requests, usage))
        except BaseException as error:  # also a cancel or timeout: a paid run without a report still shows its cost
            publish(f"Verdict: none, the run stopped before its report: {error!r}\n\nUsage: {tokens(usage)}")
            raise
    publish(render(result.output, spend(result)))
    sys.exit(0 if verdict(result.output.risks) == "SHIP" else 1)


if __name__ == "__main__":
    main()
