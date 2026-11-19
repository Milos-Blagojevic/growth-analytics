# Real-posting labeling session — 2026-11-18

PR #40 (the synthetic exam and its real baseline) is merged. This session starts a separate, boundary-focused review of real public job postings. The synthetic answer key and baseline are unchanged.

## Evidence

| File | Contents |
|---|---|
| `sources.json` | Exa-extracted source text, source URLs, text hashes, queries, capture times, retrieval settings and API-reported costs |
| `link-checks.json` | Free `research.verify_roles(settle=False)` results and check time |
| `session.json` | Fictional candidate, review rules, review cards, owner labels and unresolved questions |
| `batch-2.json` | The next three source-backed review cards and their owner-labeling state |
| `holdout.json` | Three separately labeled holdout cards, not used for prompt or threshold tuning |

Three successful Exa searches returned 12 sources; the free link check marked 11 company postings live and one reposting dead. The dead reposting is excluded from labeling. Exa reported $0.021 for the successful searches. Failed requests without usage are not included in that tally. Source gathering used no Exa agents, Jev, Claude or Telegram calls; the separately approved Jev baseline is recorded below.

Searches were deliberately aimed at remote eligibility, growth engineering and location/relocation boundaries. This is not a random or representative sample. Text is Exa's extraction (requested up to 16,000 characters per result, with maximum cache age 24 hours), not a guarantee that every part of the source page was extracted. Job availability is only the result of the recorded link check, not a promise that a posting stays open.

## Candidate and labels

Use the same public fictional candidate: Belgrade, Serbia; senior-or-above analytics engineering or growth engineering. Overseas mandatory office attendance can be considered when relocation support is offered. No private CV or profile is used.

- **Keep:** Claude should inspect it. This does not mean apply or that every skill matches.
- **Drop:** confidently fails a hard requirement of the screen.
- **Unclear:** discuss the rule or collect additional source evidence; do not invent a label.

The first batch is Paddle's Senior Analytics Engineer, Circle's Lead Engineer, Member Growth, and Ostrom's Analytics Engineer - Power Trading. Exact source evidence, summaries, owner decisions and unresolved questions are in `session.json`; the snapshots are in `sources.json`. Unresolved cases have no expected decision and remain unscored. No model answers have been generated.

The first two batches are six calibration cases; `holdout.json` contains three separately labeled holdout cases selected before model execution. Two additional available sources remain unused. Cases are not selected based on a model's successes or failures.

## Owner answer key

| Split | Posting | Expected decision |
|---|---|---|
| Calibration | Paddle — Senior Analytics Engineer | Drop |
| Calibration | Circle — Lead Engineer, Member Growth | Keep |
| Calibration | Ostrom — Analytics Engineer, Power Trading | Keep |
| Calibration | Airalo — Senior Analytics Engineer, Finance | Drop |
| Calibration | Engine — Senior Growth Engineer | Drop |
| Calibration | PERGOLUX — Senior Analytics Engineer, Marketing & Growth | Drop |
| Holdout | Alt — Lead Engineer, Growth | Keep |
| Holdout | Immersive — Senior Analytics Engineer, UK Remote | Drop |
| Holdout | Sword — Lead Growth Engineer | Keep |

All nine decisions were supplied by the owner before model execution. Ostrom was initially unclear, then kept after discussing the conservative-screening rule; its source uncertainties are preserved. No blanket country rule is inferred from Sword's keep label. These are expected decisions, not model results.

## Observed model baseline

The owner-approved [real baseline](../../results/real-baseline/README.md) ran once on `jev-1.14.0` after all nine labels were frozen. It matched 5/9 decisions: calibration 3/6 and holdout 2/3, with no false drops, four false keeps and no provider errors. Paddle, Engine, PERGOLUX and Immersive were kept despite their expected drop labels. All four expected keeps survived.

The [machine-readable report](../../results/real-baseline/results.json) preserves per-case probabilities, source hashes, inputs, labels and configuration fingerprints. Jev reported 17,944 input tokens, estimated at $0.000754 using the existing client rate. No prompts, thresholds, model version, labels or source snapshots were changed to improve the result, and no repeat run was made. This small curated corpus does not estimate performance across all job postings.

## Continue

1. Review the cards in each batch with the owner. Record their decisions and reasons before running a model.
2. If more evidence is needed, preserve it as a new version; do not silently alter the input that was labeled.
3. Use the calibration cases for any tuning; inspect the holdout only after selecting a change. Once holdout results influence tuning, treat those cases as regression data and label a fresh holdout.
4. The owner approved one nine-case Jev baseline, including the existing client retries, after all labels were recorded. Any additional model run requires separate approval. No prompt or threshold tuning is part of the approved baseline.

The session files are the human-labeling audit trail, not model results. Their runnable exam is exported to `../../data/roles-real-2026-11-18.json`; `python -m evals roles --dataset evals/data/roles-real-2026-11-18.json` checks it for free from the repository root. The default command still uses the original nine synthetic cases. Human summaries are review aids only: the exported model input preserves the exact frozen source text and its verified hash.
