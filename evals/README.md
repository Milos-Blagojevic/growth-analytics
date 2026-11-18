# AI evaluations

A versioned exam of real model behavior, separate from the free software tests. The first exam measures whether the production Jev role screen keeps or drops a job posting. It calls `pipeline.scoring.screen_roles` and `clients.jev`, not a reimplementation.

## Files

| Path | Purpose |
|---|---|
| `data/roles.json` | Nine fictional postings, public fictional candidate, owner-reviewed labels and reasons |
| `roles.py` | Dataset checks, production-screen execution, grading and report rendering |
| `__main__.py` | Free check by default; explicit paid execution and report saving |
| `results/<run>/results.json` | A live run's machine-readable evidence, suitable for committing after review |
| `results/<run>/README.md` | The same run's readable results and limitations |

## What the exam measures

**Keep** means pass the posting to Claude for inspection, not recommend applying. **Drop** means confidently fails a hard requirement. Missing information should survive the screen.

The fictional candidate lives in Belgrade, Serbia, and targets senior-or-above analytics engineering and growth engineering. An overseas on-site or hybrid role stays in consideration when it offers relocation support. No CV, private profile, saved research or mailbox data is read.

The owner labeled all nine examples interactively before model execution: four keeps and five drops. A–F are six calibration cases; G–I are three separately labeled holdout cases. All are invented, not sampled from real job traffic. There was no production prompt or threshold change in adding this exam.

- **False drop:** an expected keep was discarded. This can hide an opportunity before Claude sees it.
- **False keep:** an expected drop passed through. This wastes downstream inspection and possibly Claude spend.
- **Error:** no valid model decision. An outage is never counted as a correct keep. A run with errors is incomplete.

The runner publishes only requested, validated probability evidence. Unsolicited provider fields are excluded. Unexpected screening exceptions or non-finite numeric evidence make a case unanswered instead of crashing the run or becoming a fallback keep. Error classes are retained, but raw provider bodies and diagnostics are not published.

Reports show counts for each split, not a claim of general accuracy. Nine hand-picked cases cannot establish real-world performance or probability calibration. The live run does not call Claude, Exa or Telegram, and does not change job-hunt data or scoring rules.

## Recorded baseline

The [2026-11-18 baseline report](results/baseline/README.md) records a real TypeSafe run requesting `jev-1.14.0`, not an HTTP-mocked software test. All nine owner-labeled decisions matched, including the three holdout cases. The [machine-readable evidence](results/baseline/results.json) preserves the exact dataset, probabilities, fingerprints and reported usage. This result is limited to the small synthetic exam above.

## Free dataset check

From the repository root, with the normal requirements installed:

```bash
python -m evals roles
python -m evals roles --split holdout
```

These validate and list the expected decisions. They make no model calls, write no results, and measure no model quality. `python -m pytest tests/test_evals.py` checks the runner using the existing HTTP-level Jev fake, not the real model. Ordinary CI remains free.

## Run the real model

Ask the owner before running: this spends money on TypeSafe. Export `TYPESAFE_API_KEY` in your shell first; unlike `hunt.py`, this command deliberately does **not** load `.env`. It needs no other service keys or private files. Never paste or commit a key in a dataset, command example or report.

```bash
python -m evals roles --live --output evals/results/baseline
```

`--output` must name a new directory. Existing reports are never overwritten. For the bundled dataset this makes one screen request per selected posting: nine when `--split all` (the default), six for `calibration`, three for `holdout`. The existing Jev client may make up to four HTTP attempts per posting on retryable failures; an outage can stop later requests. There is no automatic schedule, prompt tuning, extra search or downstream scoring.

The existing `JEV_MODEL` override applies; otherwise the client pins `jev-1.14.0`. The report records the requested model, not an independently verified provider deployment. Cost uses the existing client's $0.042 per million reported input tokens; missing usage or failed requests can leave uncounted charges. It is an estimate, not a hard spending cap.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Free check valid, or every selected live decision matched its label |
| `1` | Live run completed but at least one decision was wrong; reports are still saved |
| `2` | Invalid invocation/data, missing credentials, report-write failure, or incomplete live evaluation |

A live run writes:

- `results.json`: exact dataset and labels, selected case rows, expected/actual decisions, probabilities, rejection reasons, split counts, errors, timestamps, requested model, questions, thresholds, dataset/configuration/source hashes, Python version and reported usage.
- `README.md`: readable case results, label rationales, per-question probability evidence, hard-rule failures, summary counts, all recorded source fingerprints, rerun command, cost estimate and limitations. Provider error bodies are not copied into public artifacts.

## Publish and compare

1. Review both reports, including failures and limitations. Do not replace a disappointing baseline with a nicer run or change its labels after seeing the answers.
2. Commit the runner, dataset, docs and selected **real** result directories together. Do not commit simulated reports from software tests. No live result is claimed until a paid run has actually completed.
3. To compare a prompt, threshold or model change, keep the answer key fixed and rerun the same split into a new output directory. Check dataset hashes match and inspect which cases changed, especially false drops. Keep the matching repository revision and pinned requirements with each published run; source hashes identify the screen, client and runner.
4. Tune using calibration cases only. Inspect the holdout after choosing the change. Once holdout results influence tuning, those cases become regression data: create fresh independently labeled holdout cases before making another held-out claim.
5. Expand toward sanitized, representative real-world examples before drawing product-quality conclusions. Review their suitability for publication and confirm labels before running them. Never copy private research into this public fixture without review.

Hosted model responses can change even with the same requested version. Repeat runs when investigating instability, with explicit spend approval; do not imply one small run proves reliability.
