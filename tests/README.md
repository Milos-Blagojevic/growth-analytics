# Tests

The test suite, flat in this folder. It needs no keys and makes no network calls.

## Files

| File | Purpose |
|---|---|
| `test_*.py` | Tests, one file per module or feature |
| `exa_mock.py` | The Exa API mock |
| `jev_mock.py` | The TypeSafe (Jev) API mock |
| `dashboard_data.py` | Writes pipeline files to a temporary directory for dashboard tests |

## Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests need no keys and make no network calls. Every HTTP request made through `requests` is intercepted with [`responses`](https://github.com/getsentry/responses); a request without a registered mock fails the test. Exa is mocked at the HTTP level with payloads in Exa's wire format (`tests/exa_mock.py`), so the real `exa-py` SDK builds the requests and parses the responses. Ashby, posting pages, Telegram, Microsoft Graph and the Gmail API are mocked the same way, and TypeSafe in its wire format (`tests/jev_mock.py`); Claude is replaced by a fake client, and the chat bot's model by a scripted Pydantic AI `FunctionModel`. Dashboard tests write pipeline files to a temporary directory (`tests/dashboard_data.py`) and call the app in-process with FastAPI's `TestClient`; job and chat-bot tests replace `hunt.py` with a small stub script, so no paid command runs.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11 and 3.13 for every pull request and every push to `main`.

## Verify in CI

The Verify workflow (`.github/workflows/verify.yml`) runs the `verify` skill (see [Files](../README.md#files)) without Claude Code. `.claude/skills/verify/scripts/ci.py` gives the skill to a [Pydantic AI Coder](https://pydantic.dev/docs/ai/harness/coder/) agent on `claude-opus-5-5`, with the pull request's title and description as what the change should do. The agent works on a copy of the branch in a Docker container without network access. The copy holds the git history and the tracked and untracked files, never git-ignored ones (`.env`, the token files, `hunt.db`, ...), and the container gets none of the runner's environment, so the agent's commands cannot read the API key or reach Exa, Anthropic or Telegram. The report goes to the job summary, with the model requests, tokens and dollars it took, and the check fails on BLOCK and FIX BEFORE MERGE. The skill's Docker image check cannot run inside the container and is reported as skipped.

Each run is a paid Claude session of up to 150 model requests (`--max-requests`), so it starts only when you add the `verify` label to a pull request (remove it and add it again to verify new commits), or from **Actions → Verify → Run workflow**. Setup: create the `verify` label and add `ANTHROPIC_API_KEY` as a repository secret (**Settings → Secrets and variables → Actions**). To run it on the laptop, with Docker running and `ANTHROPIC_API_KEY` exported (it never reads `.env`): `uv run .claude/skills/verify/scripts/ci.py --intent "<what the change should do>"`.
