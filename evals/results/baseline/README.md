# Jev role-screen evaluation

Status: **PASS**. Requested model: `jev-1.14.0`.
Started: 2026-11-18T18:46:17.489553+00:00. Finished: 2026-11-18T18:46:19.913490+00:00.

Nine-case synthetic smoke exam (or the selected subset), labeled by the owner before execution. This is not an estimate of real-world accuracy. Keep means inspect with Claude, not apply.

False drops are wanted inspections incorrectly discarded; false keeps are clear mismatches passed through. Errors are unanswered cases, never successful keeps. An incomplete run supports no overall quality claim.

| Split | Cases | Evaluated | Correct | False drops | False keeps | Errors |
|---|---:|---:|---:|---:|---:|---:|
| all selected | 9 | 9 | 9 | 0 | 0 | 0 |
| calibration | 6 | 6 | 6 | 0 | 0 | 0 |
| holdout | 3 | 3 | 3 | 0 | 0 | 0 |

| Case | Split | Expected | Actual | Result |
|---|---|---|---|---|
| a_us_only | calibration | drop | drop | PASS |
| b_remote_unspecified | calibration | keep | keep | PASS |
| c_berlin_relocation | calibration | keep | keep | PASS |
| d_junior_analyst | calibration | drop | drop | PASS |
| e_bi_engineering | calibration | keep | keep | PASS |
| f_berlin_no_relocation | calibration | drop | drop | PASS |
| g_us_preferred | holdout | keep | keep | PASS |
| h_office_manager | holdout | drop | drop | PASS |
| i_mid_level | holdout | drop | drop | PASS |

## Decision evidence

### a_us_only

Label reason: US residency is required, excludes Serbia, and relocation is not offered.

Hard-rule failures: limited to regions that exclude Belgrade, Serbia, no relocation

| Question | Probability evidence |
|---|---|
| target_role | `0.97` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.99` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.02` |
| relocation | `0.02` |

### b_remote_unspecified

Label reason: Missing country eligibility is uncertainty, not evidence for a confident rejection.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.98` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.09` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.13` |
| relocation | `0.07` |

### c_berlin_relocation

Label reason: Relocation support keeps an otherwise relevant overseas hybrid role in consideration.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.97` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.65` |
| onsite_or_hybrid_only | `0.98` |
| office_in_candidate_country | `0.01` |
| relocation | `0.98` |

### d_junior_analyst

Label reason: An explicitly entry-level role fails the senior-or-above requirement.

Hard-rule failures: below senior level

| Question | Probability evidence |
|---|---|
| target_role | `0.58` |
| seniority | `{"probabilities": {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0}, "score": 0.0}` |
| region_excludes_candidate | `0.04` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.09` |
| relocation | `0.05` |

### e_bi_engineering

Label reason: Senior analytics-engineering duties count despite the BI Developer title.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.87` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.03` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.08` |
| relocation | `0.06` |

### f_berlin_no_relocation

Label reason: Overseas mandatory office attendance without relocation support fails the location rule.

Hard-rule failures: limited to regions that exclude Belgrade, Serbia, no relocation

| Question | Probability evidence |
|---|---|
| target_role | `0.97` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.95` |
| onsite_or_hybrid_only | `0.98` |
| office_in_candidate_country | `0.01` |
| relocation | `0.01` |

### g_us_preferred

Label reason: A US time-zone preference is not a residency restriction; worldwide applicants are explicitly welcome.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.96` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.03` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.06` |
| relocation | `0.06` |

### h_office_manager

Label reason: Administrative office-management duties do not overlap the target engineering roles.

Hard-rule failures: not one of your target roles

| Question | Probability evidence |
|---|---|
| target_role | `0.02` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.03` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.12` |
| relocation | `0.05` |

### i_mid_level

Label reason: An explicitly mid-level role fails the senior-or-above requirement despite relevant tools.

Hard-rule failures: below senior level

| Question | Probability evidence |
|---|---|
| target_role | `0.98` |
| seniority | `{"probabilities": {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0}, "score": 1.0}` |
| region_excludes_candidate | `0.03` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.08` |
| relocation | `0.06` |


## Reproduction and limits

- Dataset SHA-256: `48b3fdb87cb0a75b94b40fbd0d5df9959a4546e5dc2447b631f9938ffcc2ec07`.
- Questions and thresholds SHA-256: `78088455a43d6b7057e7e199c91e77c05160ec64fb5998165d886060086dc2b3`.
- Screen source SHA-256: `1c8dcaa5002149534df5d5f9c57d43855af2c129911a4ecc86069d849f6d6fae`.
- Client source SHA-256: `1a4b9a35f42ba43cb3063523137f83d68610318122db824a338a70a96fc4f837`.
- Runner source SHA-256: `16f7e81a3e770a7953f2d1d6a6cb230262c6fe6f26d028e2286880add481b8f0`.
- Rerun: `python -m evals roles --live --split all --output NEW_DIRECTORY`.
- Client-tallied responses: 9; reported input tokens: 5087; estimated cost: $0.000214.
- Cost uses the existing client's token rate, not the provider's bill. Failed requests without reported usage are not included; this is not a spending cap.
- `results.json` contains the exact dataset and labels, questions, thresholds, source hashes, per-case probabilities, rejection reasons, and split counts.
- Calibration and holdout counts are separate. Do not tune on the holdout: once its results influence tuning, treat it as regression data and add a fresh holdout.
- Reuse the matching repository revision and pinned requirements; the runner does not guarantee identical answers from a hosted model on a later date.
