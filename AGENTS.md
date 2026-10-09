# AGENTS.md

Python 3.11+ CLI tools: an Exa search client (`exa_client.py`) and a job-hunt pipeline built on it (`hunt.py`). `README.md` is the user documentation and describes behavior in detail; read the relevant section before changing a feature.

## Layout

| Module | Role |
|---|---|
| `hunt.py` | CLI entry point: `discover`, `run`, `send`, `watch` |
| `research.py` | Per-company Exa research, posting link checks, posting text fetch |
| `scoring.py` | Claude fit scoring: `SYSTEM_PROMPT` rubric plus the `Fit` pydantic model |
| `exa_client.py` | The only place that talks to Exa: retries, cost logging, cost tally |
| `ashby.py` | Ashby public job-board API (link checks and watch list) |
| `watch.py` | Watch list: new matching postings from Ashby boards |
| `telegram.py` | Telegram delivery |

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest
```

CI runs the same suite on Python 3.11 and 3.13 for every pull request and push to `main`. Keep code 3.11-compatible.

Do not run these without asking: they spend money or send real messages.

- `python exa_client.py`, `hunt.py discover`, `hunt.py run` (any mode): paid Exa calls; `run` without `--research-only` also pays for Claude.
- `hunt.py run` without `--dry-run`, `hunt.py send`, `hunt.py watch` without `--dry-run`: send to Telegram. `watch` also updates `watch_state.json`.

## Private files

`.env`, `profile.toml`, `cv.md`, `companies.csv`, `discovered.csv`, `watch_state.json`, `watch.log` and `out/` hold the owner's secrets and personal data. They are git-ignored; never commit them, and do not read `.env`. When a config or CSV format changes, update the matching template (`.env.example`, `profile.example.toml`, `cv.example.md`, `companies.example.csv`) and the README.

## Conventions

- All Exa requests go through the `exa_client` module functions so retries and cost tracking apply. Never construct `Exa` elsewhere.
- Other HTTP goes through `requests` with an explicit timeout (existing calls use `timeout=15`). Tests intercept `requests` only, so another HTTP library would bypass the mocks.
- Exa structured output (`search_structured`) allows nesting depth 2 and at most 10 properties in total, array item properties included. `research.RESEARCH_SCHEMA` is at 10: adding a field means removing one.
- `scoring.SYSTEM_PROMPT` and the `Fit` model must agree. Verdict names also appear in `hunt.VERDICTS` and the digest code; change them together.
- The scoring model rejects `temperature`; do not add it.
- One failing company must not abort a run: `hunt.process_company` never raises and returns errors in its result.
- Dependencies are pinned in `requirements.txt` (runtime) and `requirements-dev.txt` (tests).

## Tests

- Tests need no keys and make no network calls. The autouse `http` fixture in `tests/conftest.py` fails any test that makes an unmocked request.
- Mock Exa at the HTTP level with the `exa` fixture (`tests/exa_mock.py`, Exa wire format), so the real SDK builds and parses requests. Do not patch `exa_client` functions.
- Mock Ashby, posting pages and Telegram through `responses`; use the `claude` fixture (`FakeClaude`) instead of the Anthropic client.
- New behavior gets a test in the matching `tests/test_<module>.py`.

## Docs

Update `README.md` in the same change when behavior, commands, config keys, costs or files change.
