"""A public, human-labeled exam of the production Jev role screen, not a second screen."""

import hashlib
import json
import math
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from clients import jev
from pipeline import scoring

DATASET = Path(__file__).parent / "data" / "roles.json"


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    location: str
    remote_or_relocation: str
    description: str = Field(min_length=1)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    captured_at: datetime
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    split: Literal["calibration", "holdout"]
    expected: Literal["keep", "drop"]
    label_reason: str = Field(min_length=1)
    role: Role
    source: Source | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1)
    target_roles: list[str] = Field(min_length=1)


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    name: str
    kind: Literal["synthetic", "real"]
    label_source: str
    candidate: Candidate
    cases: list[Case] = Field(min_length=1)


def load(path=DATASET):
    """Validate the answer key before any paid request; duplicate cases must not inflate results."""
    raw = Path(path).read_bytes()
    dataset = Dataset.model_validate_json(raw)
    ids = [case.id for case in dataset.cases]
    if len(set(ids)) != len(ids):
        raise ValueError("The dataset contains duplicate case ids")
    if any(not role.strip() for role in dataset.candidate.target_roles):
        raise ValueError("Candidate target roles must not be blank")
    for case in dataset.cases:
        if dataset.kind == "real" and case.source is None:
            raise ValueError(f"Real posting {case.id} must record source provenance")
        if case.source and hashlib.sha256(case.role.description.encode()).hexdigest() != case.source.text_sha256:
            raise ValueError(f"Source text hash mismatch for {case.id}")
    return dataset, hashlib.sha256(raw).hexdigest()


def select(dataset, split):
    cases = [case for case in dataset.cases if split == "all" or case.split == split]
    if not cases:
        raise ValueError(f"No cases in split {split}")
    return cases


def metrics(rows):
    evaluated = [row for row in rows if row["actual"] is not None]
    return {
        "cases": len(rows),
        "expected_keep": sum(row["expected"] == "keep" for row in rows),
        "expected_drop": sum(row["expected"] == "drop" for row in rows),
        "evaluated": len(evaluated),
        "correct": sum(row["expected"] == row["actual"] for row in evaluated),
        "false_drops": sum(row["expected"] == "keep" and row["actual"] == "drop" for row in evaluated),
        "false_keeps": sum(row["expected"] == "drop" and row["actual"] == "keep" for row in evaluated),
        "errors": len(rows) - len(evaluated),
    }


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("Decision evidence must contain finite numbers")
    return value


def _probability(value):
    value = _number(value)
    if not 0 <= value <= 1:
        raise ValueError("Decision probabilities must be between zero and one")
    return value


def _evidence(answers, questions):
    """Publish only requested, typed decision evidence, never unsolicited provider diagnostics."""
    clean = {}
    for name, question in questions.items():
        answer = answers[name]
        if question["type"] == "noul":
            clean[name] = _probability(answer)
        elif question["type"] == "score":
            clean[name] = {
                "score": _number(answer["score"]),
                "probabilities": {
                    str(i): _probability(answer["probabilities"][str(i)])
                    for i in range(len(question["criteria"]))
                },
            }
        else:
            raise ValueError("Unsupported role-screen evidence type")
    return clean


def run(dataset, dataset_sha256, cases):
    """Make one production screen call per case. Provider failures are errors, never fallback passes."""
    jev.reset()
    questions = scoring.ROLE_QUESTIONS | scoring.LOCATION_QUESTIONS
    profile = {
        "search": {"roles": dataset.candidate.target_roles},
        "candidate": {"location": dataset.candidate.location},
    }
    report = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model_requested": jev.model(),
        "python": platform.python_version(),
        "dataset_sha256": dataset_sha256,
        "dataset": dataset.model_dump(mode="json"),
        "source_sha256": {
            name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for name, module in (("screen", scoring), ("client", jev))
        } | {"runner": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "screen_configuration": {
            "questions": questions,
            "thresholds": {
                "SCREEN_YES": scoring.SCREEN_YES,
                "SCREEN_NO": scoring.SCREEN_NO,
                "BELOW_SENIOR_MIN": scoring.BELOW_SENIOR_MIN,
            },
        },
        "rows": [],
    }
    encoded = json.dumps(report["screen_configuration"], sort_keys=True).encode()
    report["screen_configuration_sha256"] = hashlib.sha256(encoded).hexdigest()
    for case in cases:
        url = str(case.source.url) if case.source else f"https://example.invalid/jobs/{case.id}"
        posting = case.role.model_dump() | {"url": url}
        row = {"id": case.id, "split": case.split, "expected": case.expected, "actual": None}
        try:
            result = scoring.screen_roles({"content": {"open_roles": [posting]}}, profile)["roles"][0]
            evidence = _evidence(result["answers"], questions)
        except Exception as exc:
            # The eval boundary preserves errors without publishing provider bodies or treating them as keeps.
            row["error"] = f"{type(exc).__name__}: no usable model decision; provider or evaluation failure"
        else:
            row.update(actual="keep" if result["passed"] else "drop", failed=result["failed"], answers=evidence)
        report["rows"].append(row)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["metrics"] = metrics(report["rows"])
    report["by_split"] = {
        split: metrics([row for row in report["rows"] if row["split"] == split])
        for split in ("calibration", "holdout") if any(row["split"] == split for row in report["rows"])
    }
    report["usage"] = jev.usage_summary()
    report["status"] = (
        "incomplete" if report["metrics"]["errors"] else
        "fail" if report["metrics"]["false_drops"] or report["metrics"]["false_keeps"] else "pass"
    )
    return report


def markdown(report):
    """The readable companion to the machine-readable, per-case evidence."""
    lines = [
        "# Jev role-screen evaluation", "",
        f"Status: **{report['status'].upper()}**. Requested model: `{report['model_requested']}`.",
        f"Started: {report['started_at']}. Finished: {report['finished_at']}.", "",
        f"Exam: **{report['dataset']['name']}**. "
        f"{len(report['rows'])} selected {report['dataset']['kind']} cases, labeled before model execution. "
        "This curated set is not an estimate of real-world accuracy. Keep means inspect with Claude, not apply.", "",
        "False drops are wanted inspections incorrectly discarded; false keeps are clear mismatches passed through. "
        "Errors are unanswered cases, never successful keeps. An incomplete run supports no overall quality claim.", "",
        "| Split | Cases | Evaluated | Correct | False drops | False keeps | Errors |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for split, counts in {"all selected": report["metrics"], **report["by_split"]}.items():
        lines.append("| " + " | ".join(str(value) for value in (
            split, counts["cases"], counts["evaluated"], counts["correct"],
            counts["false_drops"], counts["false_keeps"], counts["errors"],
        )) + " |")
    lines.extend(["", "| Case | Split | Expected | Actual | Result |", "|---|---|---|---|---|"])
    for row in report["rows"]:
        outcome = "ERROR" if row["actual"] is None else "PASS" if row["expected"] == row["actual"] else "FAIL"
        lines.append(f"| {row['id']} | {row['split']} | {row['expected']} | {row['actual'] or 'unanswered'} | {outcome} |")
    lines.extend(["", "## Decision evidence", ""])
    cases = {case["id"]: case for case in report["dataset"]["cases"]}
    for row in report["rows"]:
        case = cases[row["id"]]
        lines.extend([f"### {row['id']}", "", f"Label reason: {case['label_reason']}", ""])
        if source := case.get("source"):
            lines.extend([
                f"Source: {source['url']}", "",
                f"Snapshot: {source['captured_at']}; text SHA-256: `{source['text_sha256']}`.", "",
            ])
        if row["actual"] is None:
            lines.extend([f"Error: {row['error']}. No decision or probability evidence is credited.", ""])
            continue
        failures = "; ".join(row["failed"]) or "None: no confident hard-rule failure."
        lines.extend([
            f"Hard-rule failures: {failures}", "",
            "| Question | Probability evidence |", "|---|---|",
        ])
        for name, answer in row["answers"].items():
            lines.append(f"| {name} | `{json.dumps(answer, sort_keys=True, allow_nan=False)}` |")
        lines.append("")
    selected_splits = {row["split"] for row in report["rows"]}
    split = next(iter(selected_splits)) if len(selected_splits) == 1 else "all"
    lines.extend([
        "", "## Reproduction and limits", "",
        f"- Dataset SHA-256: `{report['dataset_sha256']}`.",
        f"- Questions and thresholds SHA-256: `{report['screen_configuration_sha256']}`.",
        *(f"- {name.title()} source SHA-256: `{digest}`." for name, digest in report["source_sha256"].items()),
        f"- Rerun with the matching frozen dataset file: "
        f"`python -m evals roles --dataset DATASET_JSON --live --split {split} --output NEW_DIRECTORY`.",
        f"- Client-tallied responses: {report['usage']['requests']}; "
        f"reported input tokens: {report['usage']['input_tokens']}; "
        f"estimated cost: ${report['usage']['dollars']:.6f}.",
        "- Cost uses the existing client's token rate, not the provider's bill. "
        "Failed requests without reported usage are not included; this is not a spending cap.",
        "- `results.json` contains the exact dataset and labels, questions, thresholds, source hashes, "
        "per-case probabilities, rejection reasons, and split counts.",
        "- Calibration and holdout counts are separate. Do not tune on the holdout: "
        "once its results influence tuning, treat it as regression data and add a fresh holdout.",
        "- Reuse the matching repository revision and pinned requirements; "
        "the runner does not guarantee identical answers from a hosted model on a later date.", "",
    ])
    return "\n".join(lines)
