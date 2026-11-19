# PostHog Audit Report

## Summary

This audit covered the Python server and CLI integration. Event naming and single-runtime initialization are sound, but the SDK is one major version behind, runtime credentials are not configured in a real environment file, anonymous capture prevents stable identity, and PostHog currently reports no live web events.

**Counts**

- **Errors**: 5 (must fix)
- **Warnings**: 3 (should fix)
- **Suggestions**: 2 (nice to have)
- **Passes**: 7

**Problematic items** _(only `error`, `warning`, `suggestion` — no passes)_

| Severity | Area | Check | File | Details |
|----------|------|-------|------|---------|
| `error` | Installation | SDK version up to date | `requirements.txt:9` | Installed 7.91.4; latest is 8.0.2. |
| `error` | Installation | Initialization is correct | `clients/posthog_client.py:74` | Initialization is env-sourced, but the token and host are not set in a real `.env` file. |
| `error` | Identification | Stable distinct_id (not session UUID) | `clients/posthog_client.py:150` | Product events intentionally receive a new random identity per event. |
| `error` | Identification | reset() called on logout / account switch | `dashboard/app.py:226` | Logout clears the session cookie without clearing PostHog identity state. |
| `error` | Live Data | No live web events | — | No `$pageview` or `$screen` events were seen in the last 30 days. |
| `warning` | Identification | identify() called before captures / flag evals | `clients/posthog_client.py:164` | Captures occur without `identify_context()` or `set()`. |
| `warning` | Live Data | Authorized URLs not configured | — | No owned production or staging domains are allowlisted. |
| `warning` | Live Data | Source-map check computing | — | The recommendation has not yet produced frame-resolution statistics. |
| `suggestion` | Live Data | Error alerts not wired | — | Created, reopened, and spiking issue alerts are not wired. |
| `suggestion` | Live Data | Rate-limit check computing | — | The recommendation has not yet produced result details. |

## Recommended actions

1. **Installation · SDK version up to date** — The Python SDK is installed at 7.91.4 while 8.0.2 is current. _Why it matters:_ A major-version gap can change event behavior, supported options, and bug fixes relied on by downstream analytics. _Fix:_ Upgrade the `posthog` pin at `requirements.txt:9`, review the v8 migration notes, and run the test suite. See [Python SDK docs](https://posthog.com/docs/libraries/python).
2. **Installation · Initialization is correct** — The shared server initializer is valid, but `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` are absent from real environment files. _Why it matters:_ Every event silently becomes a no-op when deployed without both values, leaving funnels and operational telemetry incomplete. _Fix:_ Configure both variables in the deployment environment used by the entry points that call `clients/posthog_client.py:74`. See [Python installation docs](https://posthog.com/docs/libraries/python).
3. **Identification · Stable distinct_id (not session UUID)** — Product captures use a new random identity for every event instead of a stable owner ID. _Why it matters:_ Person counts inflate and multi-step funnels and retention cannot connect actions by the same owner. _Fix:_ At `clients/posthog_client.py:150`, establish a stable non-PII owner identifier with the Python context/person API before capturing events. See [identifying users](https://posthog.com/docs/product-analytics/identify).
4. **Identification · reset() called on logout / account switch** — The logout route clears its session cookie without clearing analytics identity state. _Why it matters:_ If stable identification is added, consecutive users on one device can be merged into the same person. _Fix:_ Update the logout path at `dashboard/app.py:226` to end the relevant PostHog identity context when the session is cleared. See [identifying users](https://posthog.com/docs/product-analytics/identify).
5. **Live Data · No live web events** — PostHog has seen no `$pageview` or `$screen` events in the last 30 days. _Why it matters:_ Web analytics, navigation funnels, and page-level conversion analysis have no recent input. _Fix:_ Confirm whether this server-only project is expected to send web events; if it is, install and initialize the browser SDK with pageview capture enabled, then verify Activity → Live events. See [web analytics installation](https://posthog.com/docs/web-analytics/installation).
6. **Identification · identify() called before captures / flag evals** — Events are captured before any stable identity is established. _Why it matters:_ Early activity remains anonymous and cannot reliably join user-level funnels, retention, or feature-flag evaluation. _Fix:_ Establish `identify_context()` or person properties before the capture path at `clients/posthog_client.py:164`. See [identifying users](https://posthog.com/docs/product-analytics/identify).
7. **Live Data · Authorized URLs not configured** — The project has no authorized production or staging domains. _Why it matters:_ Toolbar redirects and URL-sensitive filters can fail or behave inconsistently. _Fix:_ In Project settings, add only domains the team confirms it owns; do not trust event-derived hosts without confirmation. See [authorized URLs guidance](https://posthog.com/docs/toolbar#authorized-urls).
8. **Live Data · Source-map check computing** — PostHog has not finished computing frame-resolution statistics. _Why it matters:_ Until the check completes, unresolved stack frames and fragmented error grouping cannot be ruled out. _Fix:_ Revisit Error tracking recommendations after computation finishes and upload source maps if unresolved frames are reported. See [source maps](https://posthog.com/docs/error-tracking/upload-source-maps).
9. **Live Data · Error alerts not wired** — No alerts are configured for newly created, reopened, or spiking issues. _Why it matters:_ Errors can be recorded correctly but remain unnoticed long enough to increase user impact. _Fix:_ Configure the three recommended triggers under Error tracking → Alerts. See [error tracking alerts](https://posthog.com/docs/error-tracking/alerts).
10. **Live Data · Rate-limit check computing** — PostHog has not finished computing the ingestion rate-limit recommendation. _Why it matters:_ Without a completed result, unexpected exception volume may create avoidable ingestion cost or noise. _Fix:_ Revisit Error tracking settings after computation finishes and configure project and per-issue limits if recommended. See [error tracking settings](https://posthog.com/docs/error-tracking).

## Full audit

### Installation

Whether the PostHog SDK is present, current, and initialized the way the framework expects. Everything downstream depends on this: a missing or stale SDK silently changes which events exist and which config options are honoured.

| Check | Status | File | Details |
|-------|--------|------|---------|
| PostHog SDK installed | `pass` | `requirements.txt:9` | `posthog@7.91.4` |
| SDK version up to date | `error` | `requirements.txt:9` | Installed 7.91.4, latest 8.0.2. |
| Initialization is correct | `error` | `clients/posthog_client.py:74` | The constructor is server-appropriate and env-sourced, but the token and host are not set in a real `.env` file. |
| One initialization per runtime | `pass` | `clients/posthog_client.py:74` | One shared server constructor is reused by all entry points. |

#### Assumptions and blind spots

Static review confirmed the shared constructor and known Python entry points, but did not execute every deployment mode. Environment values may be injected by hosting infrastructure rather than a local `.env`, which would explain the missing local configuration. Verify the deployed process has both variables and that a test event arrives in the intended PostHog project.

### Identification

Whether the same human maps to one stable `distinct_id` across sessions, runtimes, and logins. Identity defects are the most expensive kind to fix after the fact, because they corrupt person counts, funnels, and retention retroactively rather than going forward.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Stable distinct_id (not session UUID) | `error` | `clients/posthog_client.py:150` | Product captures omit a stable authenticated distinct ID and intentionally create a random ID per event. |
| identify() called before captures / flag evals | `warning` | `clients/posthog_client.py:164` | Captures run without `identify_context()` or `set()`. |
| Same distinct_id across client and server | `pass` | `clients/posthog_client.py:74` | Single runtime: only Python server/CLI code initializes PostHog. |
| reset() called on logout / account switch | `error` | `dashboard/app.py:226` | Logout clears the session cookie without clearing PostHog identity state. |

#### Assumptions and blind spots

The audit treated the dashboard as a single-owner application and found no browser PostHog runtime. The current anonymous-per-event design means logout state may not persist today, but it becomes a real merge risk when stable identification is introduced. Confirm in PostHog that product events are intentionally anonymous; if user-level analysis is required, inspect distinct IDs on recent events before changing the scheme.

### Event Capture

Whether events are named consistently, reach PostHog reliably, and cover the moments the business actually reasons about. Gaps here don't break anything visibly; they just leave the questions you want to ask unanswerable.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Event names are static and consistent | `pass` | `clients/posthog_client.py:154` | Wrapper callers use static string literals. |
| Captures route through a reverse proxy | `pass` | `clients/posthog_client.py:74` | Server-only SDK; no browser reverse proxy is required. |
| Key activation events captured | `pass` | `dashboard/auth.py:3` | No signup or billing paths were detected, and activation actions are explicitly captured. |

#### Assumptions and blind spots

_No findings to qualify; the standard checks for this area passed cleanly._

### Live Data

What PostHog itself has already flagged for this project, read from its recommendations and health checks. These come from ingested data rather than source code, so they catch problems no static scan can see — stack frames that never resolve, syncs that keep failing, alerts nobody wired up.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Open findings in PostHog | `pass` | — | 5 open findings from PostHog. |
| No live web events | `error` | — | No `$pageview` or `$screen` events were observed in the last 30 days. |
| Authorized URLs not configured | `warning` | — | Add each confirmed owned domain under Project settings → Authorized URLs. |
| Error alerts not wired | `suggestion` | — | Created, reopened, and spiking issue alerts are not wired. |
| Rate-limit check computing | `suggestion` | — | The recommendation has not yet produced result details. |
| Source-map check computing | `warning` | — | The recommendation has not yet produced frame-resolution statistics. |

#### Assumptions and blind spots

These findings reflect PostHog's recent lookback window, so a project that has just started sending data, or findings that have not been recomputed yet, can appear cleaner or worse than the current state. The missing web events may be expected for a server-only integration rather than evidence that server events are absent. Verify recent event names in Activity and revisit the two computing recommendations after PostHog finishes.

### Write report

This workflow check verifies that the local markdown audit report was created.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Create posthog-audit-report.md | `pending` | — | The row was pending when the report source ledger was read. |

#### Assumptions and blind spots

The report file itself confirms completion; the ledger row is resolved immediately after the document is written.

### Upload notebook

This workflow check verifies that the local report is mirrored into a shareable PostHog notebook.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Write the report into a PostHog notebook | `pending` | — | The row was pending when the report source ledger was read. |

#### Assumptions and blind spots

Notebook completeness is verified by reading the cloud copy back after all sections are appended.

## About this audit

The PostHog wizard runs this audit in stages: SDK installation → init correctness → identification → event capture → live data → this report. The early stages resolve checks against the project's source tree. The live-data stage reads what PostHog already computed from your ingested data — things no source scan can see, like whether stack frames actually resolve. Every result, pass or otherwise, is recorded in the ledger this report was generated from.

- `error` items break correctness now (events lost, identity broken). Fix first.
- `warning` items work today but cause subtle data-quality bugs. Fix when convenient.
- `suggestion` items are best-practice improvements with measurable upside.

Re-run `posthog-wizard audit` after applying fixes to refresh the ledger.

## Project changes and next steps

- **Changes made:** No source code, dependencies, configuration, or environment files were changed. The run only audited the integration, read live PostHog findings, generated this report, and mirrored it to a PostHog notebook.
- **Files modified or created:** Created `posthog-audit-report.md`; no existing project files were modified.
- **Manual steps:** Upgrade `posthog` from 7.91.4 to 8.0.2 after reviewing migration notes; ensure `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` are configured in every deployed runtime; decide whether user-level analytics are intended and, if so, implement a stable non-PII distinct ID before capture and clear identity on logout; confirm whether web events are expected; configure authorized URLs and error alerts; revisit the rate-limit and source-map recommendations after computation finishes.
