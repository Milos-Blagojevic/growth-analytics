"""The CI verify runner (.claude/skills/verify/scripts/ci.py): what reaches the container, the prompt, the verdict,
the report and the workflow that starts it."""

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic import ValidationError

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
    (repo / ".gitignore").write_text(".env\nhunt.db*\nout/\n")
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
    (repo / "out").mkdir()
    (repo / "out" / "r.json").write_text("{}\n")
    (repo / ".gitignore").write_text("hunt.db*\n!.env\n")  # the branch drops out/ and negates .env; base's rules win
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
    for private in (".env", "hunt.db-wal", "out", "vendor"):
        assert not (copy / private).exists(), private
    assert git("remote", cwd=copy) == ""


def test_copy_leaves_out_staged_private_files_symlinked_folders_and_submodules(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "vendor").mkdir()
    git("init", "-q", cwd=repo)
    git("init", "-q", cwd=repo / "vendor")
    (repo / "vendor" / "tool.py").write_text("c = 1\n")
    git("add", ".", cwd=repo / "vendor")
    git("commit", "-q", "-m", "tool", cwd=repo / "vendor")
    (repo / ".gitignore").write_text(".env\nout/\n")
    (repo / "docs" / "a.md").write_text("doc\n")
    git("add", ".", cwd=repo)  # vendor becomes a submodule entry
    git("commit", "-q", "-m", "base", cwd=repo)
    base = git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / ".env").write_text("ANTHROPIC_API_KEY=secret\n")
    git("add", "-f", ".env", cwd=repo)
    (repo / "out").mkdir()
    (repo / "out" / "a.md").write_text("owner data\n")
    (repo / "docs" / "a.md").unlink()
    (repo / "docs").rmdir()
    (repo / "docs").symlink_to("out")  # docs/a.md now reads out/a.md

    copy = tmp_path / "copy"
    ci.stage(repo, copy, base)

    assert not (copy / ".env").exists()
    assert not (copy / "docs" / "a.md").exists() and not (copy / "out").exists()
    assert (copy / ".gitignore").exists() and not any((copy / "vendor").iterdir())


REVISION = "reviewed-head:reviewed-tree"


def check(status="PASS", *, kind="suite", required=True):
    return ci.Check(check=f"{kind} evidence", kind=kind, required=required, status=status,
                    command=f"run-{kind}", result="observed outcome")


def report(*severities, **changes):
    data = dict(mode="full", revision=REVISION, complete=True, basis="base commit; changed behavior",
                change="c", unverified=[], checks=[check(), check(kind="smoke")],
                risks=[ci.Risk(id=f"F{n}", status="open", severity=s, risk=f"risk {s}",
                               evidence="a | b\nc", repro="r", fix_direction="f")
                       for n, s in enumerate(severities, 1)])
    data.update(changes)
    return ci.Report(**data)


@pytest.mark.parametrize("severities, expected", [
    ([], "SHIP"),
    (["S3"], "SHIP"),
    (["S3", "S2"], "FIX BEFORE MERGE"),
    (["S2", "S1"], "BLOCK"),
    (["S3", "S0"], "BLOCK"),
])
def test_completed_review_verdict_follows_the_worst_severity(severities, expected):
    assert ci.verdict(report(*severities), REVISION) == expected


@pytest.mark.parametrize("changes", [
    {"complete": False},
    {"checks": []},
    {"checks": [check(required=False), check(kind="smoke")]},
    {"checks": [check()]},
    {"checks": [check(kind="smoke")]},
    {"checks": [check(kind="other")]},
    {"checks": [check("SKIPPED"), check(kind="smoke")]},
    {"mode": "follow-up", "basis": "prior full review; repairs"},
    {"revision": "an-earlier-head:an-earlier-tree"},
    {"risks": [ci.Risk(id="F1", status="resolved", severity="S1", risk="hidden blocker",
                       evidence="claimed fixed", repro="r", fix_direction="f")]},
])
def test_incomplete_or_unbound_evidence_cannot_approve_ci(changes):
    review = report(**changes)
    assert ci.verdict(review, REVISION) == "INCOMPLETE"
    assert ci.incomplete_reasons(review, REVISION)


def test_optional_environment_skip_does_not_hide_a_required_failure():
    delegated = check("SKIPPED", kind="other", required=False)
    smoke = check(kind="smoke")
    assert ci.verdict(report(checks=[check(), smoke, delegated]), REVISION) == "SHIP"
    assert ci.verdict(report(checks=[check("FAIL"), smoke, delegated]), REVISION) == "FIX BEFORE MERGE"
    assert ci.verdict(report("S1", checks=[check("FAIL"), smoke, delegated]), REVISION) == "BLOCK"


@pytest.mark.parametrize("missing", ["mode", "revision", "complete"])
def test_reports_without_explicit_review_state_are_rejected(missing):
    data = report().model_dump()
    del data[missing]
    with pytest.raises(ValidationError):
        ci.Report.model_validate(data)


@pytest.mark.parametrize("field", ["check", "command", "result"])
@pytest.mark.parametrize("blank", ["", " \t\n"])
def test_check_evidence_cannot_be_empty_or_whitespace(field, blank):
    data = check().model_dump()
    data[field] = blank
    with pytest.raises(ValidationError) as error:
        ci.Check.model_validate(data)
    assert error.value.errors()[0]["loc"] == (field,)


def test_report_preserves_finding_ids_and_escapes_evidence_table_cells():
    rendered = ci.render(report("S3", "S2"), "1 model request", REVISION)
    rows = [line for line in rendered.splitlines() if line.startswith(("| F1 ", "| F2 "))]
    assert [row.split("|")[1].strip() for row in rows] == ["F2", "F1"]
    assert all(r"a \| b<br>c" in row and len(row.split(" | ")) == 7 for row in rows)


@pytest.mark.parametrize("outcome, raised, code, expected", [
    (report("S3"), SystemExit, 0, "SHIP"),
    (report("S2"), SystemExit, 1, "FIX BEFORE MERGE"),
    (report(complete=False), SystemExit, 1, "INCOMPLETE"),
    (report(checks=[check(kind="other")]), SystemExit, 1, "INCOMPLETE"),
    (UsageLimitExceeded("request_limit of 150"), UsageLimitExceeded, None, "INCOMPLETE"),
    (asyncio.CancelledError(), asyncio.CancelledError, None, "INCOMPLETE"),
])
def test_main_fails_unless_complete_and_writes_the_snapshot_and_summary(tmp_path, monkeypatch, outcome, raised,
                                                                      code, expected):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "code.py").write_text("old = True\n")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "base", cwd=repo)
    (repo / "code.py").write_text("old = False\n")
    (repo / "new.py").write_text("new = True\n")
    before = git("status", "--porcelain", cwd=repo)
    snapshots = []

    async def verify(agent, checkout, arguments, max_requests, usage):
        snapshot = f"{git('rev-parse', 'HEAD', cwd=checkout).strip()}:{git('write-tree', cwd=checkout).strip()}"
        assert f"--revision {snapshot}" in arguments
        assert snapshot.split(":")[1] != git("rev-parse", "HEAD^{tree}", cwd=checkout).strip()
        snapshots.append(snapshot)
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(output=outcome.model_copy(update={"revision": snapshot}),
                               usage=usage, all_messages=list)

    monkeypatch.setattr(ci, "REPO", repo)
    monkeypatch.setattr(ci, "make_agent", lambda model: object())
    monkeypatch.setattr(ci, "build_image", lambda checkout, context: None)
    monkeypatch.setattr(ci, "verify", verify)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--base", "HEAD"])
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    with pytest.raises(raised) as error:
        ci.main()

    assert getattr(error.value, "code", None) == code
    summary = (tmp_path / "summary.md").read_text()
    assert summary.splitlines()[0] == f"Verdict: {expected}"
    assert snapshots[0] in summary
    assert git("status", "--porcelain", cwd=repo) == before


def test_source_snapshot_covers_newly_staged_source_that_becomes_ignored(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text(".env\n")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "base", cwd=repo)
    (repo / "staged.py").write_text("value = 10\n")
    git("add", "staged.py", cwd=repo)
    (repo / ".gitignore").write_text(".env\nstaged.py\n")
    (repo / ".env").write_text("private fixture\n")
    snapshots = []

    async def verify(agent, checkout, arguments, max_requests, usage):
        assert git("show", ":staged.py", cwd=checkout) == (repo / "staged.py").read_text()
        assert not (checkout / ".env").exists()
        snapshot = f"{git('rev-parse', 'HEAD', cwd=checkout).strip()}:{git('write-tree', cwd=checkout).strip()}"
        snapshots.append(snapshot)
        # Reusing the first report after source changes must not approve the second snapshot.
        return SimpleNamespace(output=report(revision=snapshots[0]), usage=usage, all_messages=list)

    monkeypatch.setattr(ci, "REPO", repo)
    monkeypatch.setattr(ci, "make_agent", lambda model: object())
    monkeypatch.setattr(ci, "build_image", lambda checkout, context: None)
    monkeypatch.setattr(ci, "verify", verify)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--base", "HEAD"])
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    for value, code in [(10, 0), (99, 1)]:
        (repo / "staged.py").write_text(f"value = {value}\n")
        before = git("status", "--porcelain", cwd=repo)
        with pytest.raises(SystemExit) as error:
            ci.main()
        assert error.value.code == code
        assert git("status", "--porcelain", cwd=repo) == before

    assert snapshots[0] != snapshots[1]
    assert summary.read_text().splitlines().count("Verdict: INCOMPLETE") == 1


def test_workflow_starts_only_on_the_label_or_by_hand_with_a_read_only_token():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "verify.yml").read_text())
    triggers = workflow[True]  # YAML 1.1 reads the key `on` as true

    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert triggers["pull_request"] == {"types": ["labeled"]}
    assert workflow["permissions"] == {"contents": "read"}
    # Without this filter, adding any label to a pull request would start a paid run.
    assert workflow["jobs"]["verify"]["if"] == (
        "github.event_name == 'workflow_dispatch' || github.event.label.name == 'verify'")
