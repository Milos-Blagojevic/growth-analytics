# Tests

The test suite, flat in this folder. It needs no keys and makes no network calls.

## Files

| File | Purpose |
|---|---|
| `test_*.py` | Tests, one file per module or feature |
| `exa_mock.py` | The Exa API mock |
| `jev_mock.py` | The TypeSafe (Jev) API mock |
| `posthog_mock.py` | The PostHog ingestion mock: events and log records |
| `dashboard_data.py` | Writes pipeline files to a temporary directory for dashboard tests |
| `prospect_data.py` | Sample `prospects.toml`, Exa replies, research and saved runs for the prospects tests |

## Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests need no keys and make no network calls. Every HTTP request made through `requests` is intercepted with [`responses`](https://github.com/getsentry/responses); a request without a registered mock fails the test. Exa is mocked at the HTTP level with payloads in Exa's wire format (`tests/exa_mock.py`, which also holds an in-memory fake of Exa's Monitors API), so the real `exa-py` SDK builds the requests and parses the responses. Ashby, posting pages, Telegram, Microsoft Graph and the Gmail API are mocked the same way, and TypeSafe in its wire format (`tests/jev_mock.py`); Claude is replaced by a fake client, and the chat bot's model by a scripted Pydantic AI `FunctionModel`. Dashboard tests write pipeline files to a temporary directory (`tests/dashboard_data.py`) and call the app in-process with FastAPI's `TestClient`; job and chat-bot tests replace `hunt.py` with a small stub script, so no paid command runs.

PostHog is off in every test unless the test asks for the `posthog_api` fixture, which turns it on against `tests/posthog_mock.py` (capture v1's `/i/v1/analytics/events` and `/i/v1/ai/events` endpoints and the OTLP logs endpoint in their wire formats) and returns what was sent: `posthog_api.events()` and `posthog_api.records()`. A test of something that reports to PostHog checks there that none of the text it handled was sent.

The factory's tests mock the GitHub API the same way and run real `git` against a local repository in a temporary folder. The coding agent and its Docker container are replaced by a function that edits a copy of the clone, so the suite needs no Docker (`tests/test_worker.py`).

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11 and 3.13 for every pull request and every push to `main`.

## Eval runner versus model quality

`test_evals.py` checks grading, split isolation, explicit dataset selection, source-provenance/hash validation before spending, explicit paid-call opt-in, error accounting, public-report safety and preservation of existing baselines using the HTTP-level Jev fake. A fresh-process probe rejects private configuration and network access from the free command; malformed and unsolicited provider fields exercise safe reporting. These software tests do not measure the real model's judgment. The separate [AI eval runner](../evals/README.md) uses public human-labeled cases; its default command is free, and only explicit `--live` calls TypeSafe. Do not run live evals as part of the ordinary test suite.


## Independent verification

Behavior changes get one full review by an agent that did not implement them, followed, when needed, by one focused independent check of the repairs. Prefer the original reviewer for follow-up; a replacement receives the intent, full report, reviewed snapshot, repair diff and check evidence. The follow-up closes each original finding and checks regression risks from the fixes, not another unrestricted audit. Newly discovered serious defects still block.

Findings retain F1, F2, ... IDs and open/resolved/withdrawn dispositions with evidence. Missing meaningful regression coverage can block; supporting cases passing on the old code and optional refactors do not force another round. Repeat expensive upgrade/Docker checks when fixes can affect those surfaces, not merely because a follow-up started. The resulting code still needs the applicable test suite and smoke evidence.

After the full review and focused follow-up, unresolved blockers or incomplete evidence require the owner to approve further review or a scope split. Preserve attempted stages across sessions, branches and replacement reviewers; an interrupted attempt is INCOMPLETE, not an automatic retry. Keep reports, source snapshots, check evidence and remaining cleanup in the handoff or draft PR. This limit is agent policy, not a runtime lock.

Checkpoint commits and clearly marked draft PRs may precede verification. Only a completed review with no blockers is merge-ready, subject to CI; draft publication grants no merge, deployment or paid-call permission. Standalone docs/test tasks skip review, but a test-only repair still needs independent closure of its blocker.

Run `/verify <intent>` for the full review. For follow-up, pass `/verify <intent> --follow-up <prior report and reviewed snapshot>` with the actual report and repair/check evidence, not just its verdict. Source changes after a review, including upstream integration, need an impact assessment and affected checks before reusing approval.

## Verify in CI

The Verify workflow (`.github/workflows/verify.yml`) runs one full `verify` review without Claude Code; it never launches repairs or another review automatically. `.claude/skills/verify/scripts/ci.py` gives the skill to a [Pydantic AI Coder](https://pydantic.dev/docs/ai/harness/coder/) agent on `claude-opus-5-5`, with the pull request's title and description as intent. The agent works in a network-isolated Docker copy with no runner credentials. The copy includes history and allowed tracked/untracked source, never git-ignored private files. The runner force-stages only that sanitized copy to freeze its exact `HEAD:tree` identity, including uncommitted changes and newly staged files that became ignored; private-file exclusions are applied before indexing, and the owner's index and worktree are untouched.

The structured report records mode, revision, completeness, basis, stable finding IDs/statuses, and each check's kind (`suite`, `smoke`, `other`), required flag, PASS/FAIL/SKIPPED status and nonblank name, command and result evidence. CI requires evidence for both the full test suite and changed-entry-point smoke; a generic successful status check cannot replace either. It passes only a complete full report matching the expected snapshot, with no essential check skipped, blockers or failed checks. Missing state is rejected; empty evidence, stale snapshots, and standalone follow-ups cannot pass. Interrupted review runs publish INCOMPLETE and available usage before failing. Python 3.11 is delegated to ordinary CI and Docker cannot run inside this container; these are optional SKIPPED checks with reasons, not hidden failures. Essential checks without replacement evidence remain required and make verification INCOMPLETE.

The report and model requests, tokens and dollars appear in the job summary. BLOCK, FIX BEFORE MERGE and INCOMPLETE fail the check. Local focused follow-up can close the referenced full review plus repairs; the CI runner deliberately accepts only a single full review and has no focused-review CLI mode.

Each run is a paid Claude session of up to 150 model requests (`--max-requests`), so it starts only when you add the `verify` label to a pull request (remove it and add it again to verify new commits), or from **Actions → Verify → Run workflow**. Setup: create the `verify` label and add `ANTHROPIC_API_KEY` as a repository secret (**Settings → Secrets and variables → Actions**). To run it on the laptop, with Docker running and `ANTHROPIC_API_KEY` exported (it never reads `.env`): `uv run .claude/skills/verify/scripts/ci.py --intent "<what the change should do>"`.
