import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clients import jev
from evals import roles
from evals.__main__ import main


NEUTRAL = {
    "target_role": 0.99,
    "region_excludes_candidate": 0.01,
    "onsite_or_hybrid_only": 0.01,
    "office_in_candidate_country": 0.01,
    "relocation": 0.01,
}
SENIOR = [0.0, 0.0, 0.99, 0.01]


def neutral(state, key, question):
    return SENIOR if key == "seniority" else NEUTRAL[key]


def test_default_command_never_calls_a_model_or_writes_results(jev_api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert main(["roles"]) == 0

    assert jev_api.requests == []
    assert list(tmp_path.iterdir()) == []


def test_grading_distinguishes_both_mistakes_and_keeps_holdout_separate(jev_api):
    dataset, digest = roles.load()
    selected = [case for case in dataset.cases if case.id in {"a_us_only", "b_remote_unspecified", "g_us_preferred"}]

    def answer(state, key, question):
        # Deliberately reject B and keep A: two different consumer-visible mistakes.
        if state["role"]["location"] == "Remote" and key == "region_excludes_candidate":
            return 0.99
        return neutral(state, key, question)

    jev_api.on(answer)
    report = roles.run(dataset, digest, selected)

    assert report["status"] == "fail"
    assert report["metrics"] == {
        "cases": 3, "expected_keep": 2, "expected_drop": 1,
        "evaluated": 3, "correct": 1, "false_drops": 1, "false_keeps": 1, "errors": 0,
    }
    assert report["by_split"]["calibration"]["correct"] == 0
    assert report["by_split"]["holdout"]["correct"] == 1
    assert {row["id"]: row["actual"] for row in report["rows"]} == {
        "a_us_only": "keep", "b_remote_unspecified": "drop", "g_us_preferred": "keep",
    }


def test_live_command_saves_a_failed_exam_and_returns_nonzero(jev_api, tmp_path):
    jev_api.on(neutral)  # Keeping everything must fail against known negative examples.
    output = tmp_path / "baseline"

    assert main(["roles", "--live", "--split", "holdout", "--output", str(output)]) == 1

    report = json.loads((output / "results.json").read_text())
    assert report["metrics"]["false_keeps"] == 2
    assert report["metrics"]["false_drops"] == 0
    assert set(report["by_split"]) == {"holdout"}
    assert {row["id"] for row in report["rows"]} == {"g_us_preferred", "h_office_manager", "i_mid_level"}
    assert len(jev_api.requests) == 3  # No spending on the unselected calibration split.


def test_a_correct_exam_returns_zero(jev_api, tmp_path):
    def answer(state, key, question):
        title = state["role"]["title"]
        if title == "Senior Office Manager" and key == "target_role":
            return 0.01
        if title == "Analytics Engineer, Mid-Level" and key == "seniority":
            return [0.0, 0.99, 0.01, 0.0]
        return neutral(state, key, question)

    jev_api.on(answer)
    output = tmp_path / "baseline"

    assert main(["roles", "--live", "--split", "holdout", "--output", str(output)]) == 0

    report = json.loads((output / "results.json").read_text())
    assert report["status"] == "pass"
    assert report["metrics"]["correct"] == 3
    assert report["metrics"]["errors"] == 0


def test_posthog_gets_a_live_run_and_its_jev_requests_without_the_postings_and_nothing_from_the_free_check(
        jev_api, posthog_api, tmp_path):
    jev_api.on(neutral)

    assert main(["roles"]) == 0
    assert posthog_api.events() == [] and posthog_api.records() == []  # the free check stays off the network

    assert main(["roles", "--live", "--split", "holdout", "--output", str(tmp_path / "run")]) == 1

    started, ended = posthog_api.records()
    assert started == {"name": "evals_command_started", "level": "INFO", "service": "evals", "command": "roles"}
    assert ended.pop("seconds") >= 0
    assert ended == {"name": "evals_command_finished", "level": "ERROR", "service": "evals", "command": "roles",
                     "exit_code": 1}  # the exam failed: two expected drops were kept
    generations = [event["properties"] for event in posthog_api.events()]
    assert [(g["$ai_provider"], g["$ai_model"], g["$ai_input_tokens"]) for g in generations] == [
        ("typesafe", jev.DEFAULT_MODEL, 500)] * 3
    told = json.dumps([posthog_api.events(), posthog_api.records()])
    for text in ("Office Manager", "Mid-Level", "Belgrade", "example.invalid"):
        assert text not in told


def test_api_errors_are_unanswered_not_fallback_successes(http, monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
    http.post(jev.API_URL, json={"error": "not-public-response"}, status=401)
    output = tmp_path / "baseline"

    assert main(["roles", "--live", "--split", "holdout", "--output", str(output)]) == 2

    raw = (output / "results.json").read_text()
    report = json.loads(raw)
    assert report["status"] == "incomplete"
    assert report["metrics"]["evaluated"] == 0
    assert report["metrics"]["correct"] == 0
    assert report["metrics"]["errors"] == 3
    assert all(row["actual"] is None for row in report["rows"])
    assert "not-public-response" not in raw
    assert "test-jev-key" not in raw
    assert "not-public-response" not in (output / "README.md").read_text()


def test_existing_report_is_not_overwritten_or_paid_for_again(jev_api, tmp_path):
    output = tmp_path / "baseline"
    output.mkdir()
    original = output / "results.json"
    original.write_text("existing evidence\n")

    assert main(["roles", "--live", "--output", str(output)]) == 2

    assert original.read_text() == "existing evidence\n"
    assert jev_api.requests == []


def test_missing_credentials_fail_before_creating_output(tmp_path):
    output = tmp_path / "baseline"

    assert main(["roles", "--live", "--output", str(output)]) == 2

    assert not output.exists()


@pytest.mark.parametrize("arguments", [["roles", "--live"], ["roles", "--output", "unused"]])
def test_paid_mode_requires_both_explicit_live_flag_and_output(arguments):
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2


@pytest.mark.parametrize("problem", ["duplicate_id", "invalid_label"])
def test_bad_answer_keys_cannot_silently_change_the_grade(tmp_path, problem):
    fields = json.loads(roles.DATASET.read_text())
    if problem == "duplicate_id":
        fields["cases"][1]["id"] = fields["cases"][0]["id"]
    else:
        fields["cases"][0]["expected"] = "maybe"
    path = tmp_path / "exam.json"
    path.write_text(json.dumps(fields))

    with pytest.raises(ValueError):
        roles.load(path)


def test_fresh_command_cannot_load_private_configuration_or_make_network_calls():
    script = """
import dotenv
import requests

def forbidden(*args, **kwargs):
    raise AssertionError("Free eval attempted private configuration or network access")

dotenv.load_dotenv = forbidden
requests.sessions.Session.request = forbidden
from evals.__main__ import main
raise SystemExit(main(["roles"]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parent.parent,
        env={key: value for key, value in os.environ.items() if key != "TYPESAFE_API_KEY"},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def wire_answers():
    return {
        "answers": {
            **{key: {"type": "noul", "noul": probability} for key, probability in NEUTRAL.items()},
            "seniority": {
                "type": "score", "score": 2.01,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 0.99, "3": 0.01},
            },
        },
        "usage": {"input_tokens": 500},
    }


@pytest.mark.parametrize("problem", ["extra_object", "nonfinite_score"])
def test_unusable_provider_data_is_an_incomplete_report_not_a_crash(http, monkeypatch, tmp_path, problem):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
    payload = wire_answers()
    if problem == "extra_object":
        payload["answers"]["provider_metadata"] = {"diagnostic": "DO_NOT_PUBLISH"}
    else:
        payload["answers"]["seniority"]["score"] = float("inf")
    http.post(jev.API_URL, json=payload)
    output = tmp_path / "baseline"

    assert main(["roles", "--live", "--split", "holdout", "--output", str(output)]) == 2

    raw = (output / "results.json").read_text()
    report = json.loads(raw, parse_constant=lambda value: pytest.fail(f"Non-finite JSON: {value}"))
    assert report["status"] == "incomplete"
    assert report["metrics"]["evaluated"] == 0
    assert report["metrics"]["errors"] == 3
    assert all(row["actual"] is None for row in report["rows"])
    assert "DO_NOT_PUBLISH" not in raw
    assert "DO_NOT_PUBLISH" not in (output / "README.md").read_text()


def test_unsolicited_provider_diagnostics_never_enter_public_evidence(http, monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
    payload = wire_answers()
    payload["answers"]["provider_diagnostic"] = {"noul": "DO_NOT_PUBLISH"}
    http.post(jev.API_URL, json=payload)
    output = tmp_path / "baseline"

    assert main(["roles", "--live", "--split", "holdout", "--output", str(output)]) == 1

    raw = (output / "results.json").read_text()
    report = json.loads(raw)
    assert report["metrics"]["errors"] == 0
    assert report["metrics"]["false_keeps"] == 2
    assert "provider_diagnostic" not in raw
    assert "DO_NOT_PUBLISH" not in raw
    assert "DO_NOT_PUBLISH" not in (output / "README.md").read_text()


@pytest.fixture
def alternate_exam(tmp_path):
    cases = []
    for case_id, expected, title, description in (
        ("inspect", "keep", "Senior Analytics Engineer", "Own SQL models, dbt testing, and analytics infrastructure."),
        ("reject", "drop", "Office Manager", "Manage office supplies and travel bookings, with no engineering duties."),
    ):
        cases.append({
            "id": case_id, "split": "calibration", "expected": expected, "label_reason": "Human-reviewed test case.",
            "role": {"title": title, "location": "Remote worldwide", "remote_or_relocation": "Remote", "description": description},
            "source": {
                "url": f"https://example.com/jobs/{case_id}",
                "captured_at": "2026-11-18T19:00:00+00:00",
                "text_sha256": hashlib.sha256(description.encode()).hexdigest(),
            },
        })
    fields = {
        "schema_version": 1, "name": "Different answer key", "kind": "real", "label_source": "Test fixture",
        "candidate": {"location": "Belgrade, Serbia", "target_roles": ["analytics engineer"]},
        "cases": cases,
    }
    path = tmp_path / "alternate.json"
    path.write_text(json.dumps(fields))
    return path


def test_explicit_exam_is_graded_instead_of_the_default_corpus(alternate_exam, jev_api, tmp_path):
    jev_api.on(neutral)  # Keeping both is one mistake on this exam, not the default exam's five.
    output = tmp_path / "alternate-results"

    assert main(["roles", "--dataset", str(alternate_exam), "--live", "--output", str(output)]) == 1

    report = json.loads((output / "results.json").read_text())
    assert report["metrics"] == {
        "cases": 2, "expected_keep": 1, "expected_drop": 1,
        "evaluated": 2, "correct": 1, "false_drops": 0, "false_keeps": 1, "errors": 0,
    }
    assert len(jev_api.requests) == 2  # Never spend on a different, unselected exam.


@pytest.mark.parametrize("problem", ["missing_source", "changed_description", "changed_hash", "unsafe_source_url"])
def test_invalid_source_evidence_is_rejected_before_spending(alternate_exam, jev_api, tmp_path, problem):
    fields = json.loads(alternate_exam.read_text())
    case = fields["cases"][0]
    if problem == "missing_source":
        del case["source"]
    elif problem == "changed_description":
        case["role"]["description"] += " New evidence not present when the case was labeled."
    elif problem == "changed_hash":
        case["source"]["text_sha256"] = "0" * 64
    else:
        case["source"]["url"] = "javascript:alert(1)"
    alternate_exam.write_text(json.dumps(fields))
    output = tmp_path / "invalid-results"

    assert main(["roles", "--dataset", str(alternate_exam), "--live", "--output", str(output)]) == 2

    assert jev_api.requests == []
    assert not output.exists()
