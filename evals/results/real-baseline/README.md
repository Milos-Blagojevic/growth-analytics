# Jev role-screen evaluation

Status: **FAIL**. Requested model: `jev-1.14.0`.
Started: 2026-11-19T08:59:02.967866+00:00. Finished: 2026-11-19T08:59:05.431279+00:00.

Exam: **Jev role screen: nine owner-labeled real postings (2026-11-18)**. 9 selected real cases, labeled before model execution. This curated set is not an estimate of real-world accuracy. Keep means inspect with Claude, not apply.

False drops are wanted inspections incorrectly discarded; false keeps are clear mismatches passed through. Errors are unanswered cases, never successful keeps. An incomplete run supports no overall quality claim.

| Split | Cases | Evaluated | Correct | False drops | False keeps | Errors |
|---|---:|---:|---:|---:|---:|---:|
| all selected | 9 | 9 | 5 | 0 | 4 | 0 |
| calibration | 6 | 6 | 3 | 0 | 3 | 0 |
| holdout | 3 | 3 | 2 | 0 | 1 | 0 |

| Case | Split | Expected | Actual | Result |
|---|---|---|---|---|
| r01_paddle_hubs | calibration | drop | keep | FAIL |
| r02_circle_member_growth | calibration | keep | keep | PASS |
| r03_ostrom_power_trading | calibration | keep | keep | PASS |
| r04_airalo_supported_locations | calibration | drop | drop | PASS |
| r05_engine_us_growth | calibration | drop | keep | FAIL |
| r06_pergolux_berlin_onsite | calibration | drop | keep | FAIL |
| r07_alt_remote_growth | holdout | keep | keep | PASS |
| r08_immersive_uk_remote | holdout | drop | keep | FAIL |
| r09_sword_portugal_remote | holdout | keep | keep | PASS |

## Decision evidence

### r01_paddle_hubs

Label reason: Owner decision: drop. The posting lists UK, Portugal and Ireland and says hiring is focused within company hubs.

Source: https://jobs.ashbyhq.com/paddle/938c6fbe-3167-4264-bca5-3b4ad104adee/

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `b778480847ca28685bade1e8ecdccfc878bfb19b568ee1e652f6183379706295`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.96` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.92` |
| onsite_or_hybrid_only | `0.06` |
| office_in_candidate_country | `0.02` |
| relocation | `0.15` |

### r02_circle_member_growth

Label reason: Owner decision: keep. The posting combines a global remote lead role with analytics infrastructure and member-growth engineering.

Source: https://jobs.ashbyhq.com/circle/194a7b5d-df8d-4bb2-936d-7344d4501e11/application

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `a3fb12dbe2aa8b52359a3023742ec1c76e5e0ba75155dc05c405879ed3f8492b`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.95` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0}, "score": 3.0}` |
| region_excludes_candidate | `0.03` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.13` |
| relocation | `0.08` |

### r03_ostrom_power_trading

Label reason: Owner resolved an initially unclear case as keep under the conservative-screening rule. Seniority and country eligibility remain uncertain, not confidently disqualifying.

Source: https://jobs.ashbyhq.com/ostrom/0e1820f2-157a-41d4-a168-131f0388e088

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `61ff66df3b2658ac0d7501511fded0f95366e757eaef3a48b70e02e1d29dd295`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.97` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.41, "2": 0.59, "3": 0.0}, "score": 1.59}` |
| region_excludes_candidate | `0.13` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.02` |
| relocation | `0.14` |

### r04_airalo_supported_locations

Label reason: Owner decision: drop. The header names Spain and the UK; later text also names Germany and supported European locations. The original ambiguity is retained.

Source: https://jobs.lever.co/airalo/5cd064bf-0d97-45d3-920b-64efc9ed40bd/

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `72ed3eb47e3e0ae1f63fc438a634af950176745d584a20883cb9178ef8a2c720`.

Hard-rule failures: limited to regions that exclude Belgrade, Serbia, no relocation

| Question | Probability evidence |
|---|---|
| target_role | `0.95` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.97` |
| onsite_or_hybrid_only | `0.01` |
| office_in_candidate_country | `0.01` |
| relocation | `0.06` |

### r05_engine_us_growth

Label reason: Owner decision: drop. The header says Remote - US. A sponsorship question is present, but the posting does not explicitly offer relocation.

Source: https://job-boards.greenhouse.io/engine/jobs/7997352003

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `d324cbe245ea3b8e96a4096f67ef79054fb331544e54defef9753158b40182e3`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.89` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.89` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.03` |
| relocation | `0.15` |

### r06_pergolux_berlin_onsite

Label reason: Owner decision: drop. The posting marks Berlin and OnSite; no relocation offer is stated in the captured text.

Source: https://jobs.ashbyhq.com/pergolux/29ebe347-f064-4154-aea3-8bc0aaef4a74/application

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `07c83051b230cf84447e9fa9755886fb42e80f7f5041c22bfb1e492406380a41`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.97` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.91` |
| onsite_or_hybrid_only | `0.97` |
| office_in_candidate_country | `0.02` |
| relocation | `0.17` |

### r07_alt_remote_growth

Label reason: Owner decision: keep. The captured text says Remote without an explicit country restriction; USD compensation and the 401(k) benefit remain in the model input.

Source: https://jobs.ashbyhq.com/alt/15d0c109-9401-493f-9038-7fc7659b58aa

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `ffefb9234e42f9c09425b52401b780930794b688315aeb2607947a7269e9c994`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.86` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0}, "score": 3.0}` |
| region_excludes_candidate | `0.16` |
| onsite_or_hybrid_only | `0.02` |
| office_in_candidate_country | `0.13` |
| relocation | `0.06` |

### r08_immersive_uk_remote

Label reason: Owner decision: drop. The role title and location explicitly name UK remote work and the established UK engineering team.

Source: https://jobs.ashbyhq.com/immersivelabs/bfe3228b-cf95-4458-9aa6-f8c3246b6cb7

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `2a95da51fcf18dc26f18e1b977e53ae19d51bf19846721137d7c18e2db807f0f`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.95` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "score": 2.0}` |
| region_excludes_candidate | `0.87` |
| onsite_or_hybrid_only | `0.03` |
| office_in_candidate_country | `0.02` |
| relocation | `0.15` |

### r09_sword_portugal_remote

Label reason: Owner decision: keep. The Portugal header and country-adjusted compensation language are both retained. This case-specific label does not establish a blanket country rule.

Source: https://job-boards.greenhouse.io/swordhealth/jobs/4425692009

Snapshot: 2026-11-18T19:04:40.508290Z; text SHA-256: `62e51fd56a69055e31fc221b38f990e693a526554cabe6d515b3795d1f7a5c80`.

Hard-rule failures: None: no confident hard-rule failure.

| Question | Probability evidence |
|---|---|
| target_role | `0.8` |
| seniority | `{"probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0}, "score": 3.0}` |
| region_excludes_candidate | `0.68` |
| onsite_or_hybrid_only | `0.03` |
| office_in_candidate_country | `0.02` |
| relocation | `0.1` |


## Reproduction and limits

- Dataset SHA-256: `19689f63cf45cdf667d4686c6bb13820e9673ccb98fa1d68b7f2b46f13da93eb`.
- Questions and thresholds SHA-256: `78088455a43d6b7057e7e199c91e77c05160ec64fb5998165d886060086dc2b3`.
- Screen source SHA-256: `1c8dcaa5002149534df5d5f9c57d43855af2c129911a4ecc86069d849f6d6fae`.
- Client source SHA-256: `1a4b9a35f42ba43cb3063523137f83d68610318122db824a338a70a96fc4f837`.
- Runner source SHA-256: `aa01ee3e9524076f71991abe33b3dfdd1343fb20e667a37ce36bc77d10d292b9`.
- Rerun with the matching frozen dataset file: `python -m evals roles --dataset DATASET_JSON --live --split all --output NEW_DIRECTORY`.
- Client-tallied responses: 9; reported input tokens: 17944; estimated cost: $0.000754.
- Cost uses the existing client's token rate, not the provider's bill. Failed requests without reported usage are not included; this is not a spending cap.
- `results.json` contains the exact dataset and labels, questions, thresholds, source hashes, per-case probabilities, rejection reasons, and split counts.
- Calibration and holdout counts are separate. Do not tune on the holdout: once its results influence tuning, treat it as regression data and add a fresh holdout.
- Reuse the matching repository revision and pinned requirements; the runner does not guarantee identical answers from a hosted model on a later date.
