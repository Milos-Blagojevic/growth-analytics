from types import SimpleNamespace

import pytest
import requests
import responses

import exa_client
import research
import scoring
from exa_mock import ExaMock


@pytest.fixture(autouse=True)
def http():
    """Intercepts every HTTP request made through `requests` (Exa SDK, Ashby, posting checks, Telegram).

    Nothing reaches the network. A request with no registered mock fails the test, even when the code
    under test swallows the resulting ConnectionError.
    """
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock
        unmocked = [
            call.request.url
            for call in mock.calls
            if isinstance(call.response, requests.ConnectionError)
            and "Connection refused by Responses" in str(call.response)
        ]
        assert not unmocked, f"unmocked HTTP request(s): {unmocked}"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fake Exa key, no other credentials (a local .env must not leak in), no retry sleeps, fresh caches."""
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    for var in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SCORING_MODEL", "DASHBOARD_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(exa_client, "BACKOFF_BASE_SECONDS", 0)
    exa_client.reset_costs()
    research._ashby_board.cache_clear()


@pytest.fixture
def exa(http):
    return ExaMock(http)


@pytest.fixture
def profile():
    """Profile as returned by hunt.load_profile (defaults filled in)."""
    return {
        "search": {
            "roles": ["analytics engineer", "growth engineer"],
            "seniority": ["senior", "lead"],
            "work_mode": "Prefer fully remote roles.",
            "exclude_domains": [],
        },
        "candidate": {
            "location": "Belgrade, Serbia",
            "experience": "8 years of analytics engineering (dbt, Snowflake).",
            "notes": "",
            "gaps": "",
            "weak_spots": "",
        },
    }


class FakeClaude:
    """Stands in for anthropic.Anthropic: messages.parse returns a fresh Fit built from `fit_fields`."""

    def __init__(self, fit_fields):
        self.fit_fields = fit_fields
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        fit = None if self.fit_fields is None else scoring.Fit(**self.fit_fields)
        usage = SimpleNamespace(input_tokens=1200, output_tokens=300)
        return SimpleNamespace(parsed_output=fit, stop_reason="end_turn", usage=usage)


@pytest.fixture
def claude():
    """claude(**overrides) -> FakeClaude returning a Fit with sensible defaults; claude(None) -> unparsable."""

    def make(fit_fields=..., **overrides):
        if fit_fields is None:
            return FakeClaude(None)
        defaults = {
            "score": 80,
            "verdict": "apply",
            "best_role_title": "",
            "best_role_url": "",
            "reasons": ["Strong dbt match."],
            "concerns": [],
            "location_assessment": "Remote in Europe: fits.",
            "next_step": "Apply.",
            "gap_note": "",
            "proof_ideas": [],
            "scope_note": "",
        }
        return FakeClaude({**defaults, **overrides})

    return make
