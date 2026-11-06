"""The CI verify runner (.claude/skills/verify/scripts/ci.py): what reaches the container, the prompt, the verdict,
the report and the workflow that starts it."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic_ai.exceptions import UsageLimitExceeded

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("verify_ci", REPO / ".claude" / "skills" / "verify" / "scripts" / "ci.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def test_copy_has_the_history_and_working_tree_but_no_ignored_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text(".env\nhunt.db*\n")
    (repo / "keep.py").write_text("a = 1\n")
    (repo / "gone.py").write_text("bye\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    base = git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / "keep.py").write_text("a = 2\n")
    git("commit", "-q", "-am", "change", cwd=repo)
    (repo / "keep.py").write_text("a = 3\n")
    (repo / "gone.py").unlink()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new.py").write_text("b = 1\n")
    (repo / ".env").write_text("ANTHROPIC_API_KEY=secret\n")
    (repo / "hunt.db-wal").write_text("owner data\n")
    (repo / ".gitignore").write_text("hunt.db*\n")  # the branch drops .env; base's rules still apply
    (repo / "vendor").mkdir()
    git("init", "-q", cwd=repo / "vendor")
    (repo / "vendor" / "tool.py").write_text("c = 1\n")

    copy = tmp_path / "copy"
    ci.stage(repo, copy, base)

    assert git("rev-parse", "HEAD", cwd=copy) == git("rev-parse", "HEAD", cwd=repo)
    assert git("log", "--format=%s", f"{base}..HEAD", cwd=copy).split() == ["change"]
    assert sorted(git("status", "--porcelain", cwd=copy).splitlines()) == [
        " D gone.py", " M .gitignore", " M keep.py", "?? pkg/"]
    assert (copy / "keep.py").read_text() == "a = 3\n"
    assert not (copy / ".env").exists() and not (copy / "hunt.db-wal").exists() and not (copy / "vendor").exists()
    assert git("remote", cwd=copy) == ""


def test_prompt_is_the_skill_body_with_the_arguments():
    arguments = "Adds a --verbose flag to watch\n\n--base 0123abc"
    text = ci.prompt(arguments)

    assert arguments in text
    assert "$ARGUMENTS" not in text and "allowed-tools" not in text


@pytest.mark.parametrize("severities, expected", [
    ([], "SHIP"),
    (["S3", "S3"], "SHIP"),
    (["S3", "S2"], "FIX BEFORE MERGE"),
    (["S2", "S1"], "BLOCK"),
    (["S3", "S0"], "BLOCK"),
])
def test_verdict_follows_the_worst_severity(severities, expected):
    risks = [ci.Risk(severity=s, risk="r", evidence="e", repro="p", fix_direction="f") for s in severities]

    assert ci.verdict(risks) == expected


def report(*severities):
    return ci.Report(change="c", unverified=[], checks=[ci.Check(check="pytest", command="pytest", result="pass")],
                     risks=[ci.Risk(severity=s, risk=f"risk {s}", evidence="a | b\nc", repro="r", fix_direction="f")
                            for s in severities])


def test_report_lists_the_worst_risks_first_and_keeps_the_tables_intact():
    lines = ci.render(report("S3", "S2"), "1 model requests").splitlines()

    assert lines[0] == "Verdict: FIX BEFORE MERGE"
    rows = [line for line in lines if line.startswith(("| 1 ", "| 2 "))]
    assert rows == ["| 1 | S2 | risk S2 | a \\| b<br>c | r | f |", "| 2 | S3 | risk S3 | a \\| b<br>c | r | f |"]


@pytest.mark.parametrize("outcome, raised, code, first_line", [
    (report("S3"), SystemExit, 0, "Verdict: SHIP"),
    (report("S2"), SystemExit, 1, "Verdict: FIX BEFORE MERGE"),
    (UsageLimitExceeded("request_limit of 150"), UsageLimitExceeded, None,
     "Verdict: none, the run stopped before its report: request_limit of 150"),
])
def test_main_fails_the_check_unless_ship_and_writes_the_job_summary(tmp_path, monkeypatch, outcome, raised, code,
                                                                     first_line):
    async def verify(agent, checkout, arguments, max_requests, usage):
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(output=outcome, usage=usage, all_messages=list)

    monkeypatch.setattr(ci, "make_agent", lambda model: object())
    monkeypatch.setattr(ci, "stage", lambda repo, dest, base: None)
    monkeypatch.setattr(ci, "build_image", lambda checkout, context: None)
    monkeypatch.setattr(ci, "verify", verify)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--base", "HEAD"])
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    with pytest.raises(raised) as error:
        ci.main()

    assert getattr(error.value, "code", None) == code
    assert (tmp_path / "summary.md").read_text().splitlines()[0].startswith(first_line)


def test_workflow_starts_only_on_the_label_or_by_hand_with_a_read_only_token():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "verify.yml").read_text())
    triggers = workflow[True]  # YAML 1.1 reads the key `on` as true

    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert triggers["pull_request"] == {"types": ["labeled"]}
    assert workflow["permissions"] == {"contents": "read"}
