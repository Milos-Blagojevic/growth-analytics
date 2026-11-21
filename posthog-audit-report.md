# PostHog Audit Report

## Summary

This audit covered the Python server and CLI integration. The current SDK, centralized event naming, single-runtime identity, and server-only routing checks passed; the main correctness risks are missing runtime configuration and an uncleared logout identity state, while PostHog also reports one project-setting warning and two error-tracking improvements.

**Counts**

- **Errors**: 2 (must fix)
- **Warnings**: 1 (should fix)
- **Suggestions**: 2 (nice to have)
- **Passes**: 10

**Problematic items** _(only `error`, `warning`, `suggestion` — no passes)_

| Severity | Area | Check | File | Details |
|----------|------|-------|------|---------|
| `error` | Installation | Initialization is correct | `clients/posthog_client.py:74` | Initialization is runtime-appropriate and env-sourced, but `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` are absent from project env files, so every runtime silently disables PostHog when neither is set. |
| `error` | Identification | reset() called on logout / account switch | `dashboard/app.py:282` | The detected logout flow clears the authentication cookie but does not clear PostHog state with `reset()`. |
| `warning` | Live Data | Authorized URLs not configured | — | Add each confirmed owned production and staging domain under Project settings → Authorized URLs; never populate this security allowlist from event data without explicit confirmation. |
| `suggestion` | Live Data | Error alerts not wired | — | No alert is wired for created, reopened, or spiking error-tracking issues. |
| `suggestion` | Live Data | Error rate limits not set | — | No project or per-issue ingestion rate limit is configured. |

## Recommended actions

1. **Installation · Initialization is correct** — The initializer is valid and environment-sourced, but neither required PostHog variable is configured in project environment files. _Why it matters:_ Every capture becomes a silent no-op when the deployed process lacks either value, leaving funnels and operational telemetry incomplete. _Fix:_ Configure `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` for every deployed entry point that reaches `clients/posthog_client.py:74`. See [Python SDK docs](https://posthog.com/docs/libraries/python).
2. **Identification · reset() called on logout / account switch** — The logout flow clears authentication state without clearing PostHog state. _Why it matters:_ Consecutive authenticated users can be merged if identity state survives an account change, contaminating person counts, funnels, and retention. _Fix:_ Clear the relevant PostHog identity state when the logout path at `dashboard/app.py:282` clears the session. See [identifying users](https://posthog.com/docs/product-analytics/identify).
3. **Live Data · Authorized URLs not configured** — PostHog has no authorized production or staging domains configured. _Why it matters:_ Toolbar redirects and URL-sensitive filters can fail or behave inconsistently. _Fix:_ Add only team-confirmed owned domains under Project settings → Authorized URLs. See [authorized URLs guidance](https://posthog.com/docs/toolbar#authorized-urls).
4. **Live Data · Error alerts not wired** — Created, reopened, and spiking issue alerts are not wired. _Why it matters:_ Correctly recorded errors can remain unnoticed long enough to increase user impact. _Fix:_ Configure the three recommended triggers under Error tracking → Alerts. See [error tracking alerts](https://posthog.com/docs/error-tracking/alerts).
5. **Live Data · Error rate limits not set** — Neither project-wide nor per-issue ingestion rate limits are configured. _Why it matters:_ An exception burst can create avoidable ingestion cost and noise. _Fix:_ Configure project and per-issue limits under Error tracking → Settings. See [error tracking settings](https://posthog.com/docs/error-tracking).

## Full audit

### Installation

Whether the PostHog SDK is present, current, and initialized the way the framework expects. Everything downstream depends on this: a missing or stale SDK silently changes which events exist and which config options are honoured.

| Check | Status | File | Details |
|-------|--------|------|---------|
| PostHog SDK installed | `pass` | `requirements.txt:9` | `posthog@8.0.2` |
| SDK version up to date | `pass` | `requirements.txt:9` | Installed 8.0.2, latest 8.0.2. |
| Initialization is correct | `error` | `clients/posthog_client.py:74` | Initialization is runtime-appropriate and env-sourced, but `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` are absent from project env files, so every runtime silently disables PostHog when neither is set. |
| One initialization per runtime | `pass` | `clients/posthog_client.py:84` | One shared `Posthog` constructor; configure calls belong to separate dashboard, hunt, prospects, evals, and test processes. |

#### Assumptions and blind spots

Static review confirmed the shared constructor and known Python entry points, but did not execute every deployment mode. Environment values may be injected by hosting infrastructure rather than a project `.env`, which would explain the missing local configuration. Verify each deployed process has both variables and that a test event reaches project 185214.

### Identification

Whether the same human maps to one stable `distinct_id` across sessions, runtimes, and logins. Identity defects are the most expensive kind to fix after the fact, because they corrupt person counts, funnels, and retention retroactively rather than going forward.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Stable distinct_id (not session UUID) | `pass` | `clients/posthog_client.py:55` | No `identify()` calls; all server events are deliberately assigned the stable `growth-analytics-owner` distinct ID. |
| identify() called before captures / flag evals | `pass` | `clients/posthog_client.py:101` | No identify flow exists; the stable owner distinct ID is applied in `before_send` to every event. |
| Same distinct_id across client and server | `pass` | `clients/posthog_client.py:84` | Single runtime: only the Python server SDK initializes PostHog, so there is no client/server identity handoff. |
| reset() called on logout / account switch | `error` | `dashboard/app.py:282` | The detected logout flow clears the authentication cookie but does not clear PostHog state with `reset()`. |

#### Assumptions and blind spots

The audit treated the dashboard as a single-owner application and found no browser PostHog runtime. The fixed owner ID is appropriate only while that single-owner assumption remains true; a future multi-user model would require authenticated IDs and a revised logout strategy. Verify recent events in PostHog consistently use the intended owner ID and confirm whether the server SDK retains any identity context across logout requests.

### Event Capture

Whether events are named consistently, reach PostHog reliably, and cover the moments the business actually reasons about. Gaps here don't break anything visibly; they just leave the questions you want to ask unanswerable.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Event names are static and consistent | `pass` | `dashboard/app.py:365` | All production calls through the centralized `posthog_client.capture` wrapper use static string-literal event names. |
| Captures route through a reverse proxy | `pass` | `clients/posthog_client.py:84` | Server-only SDK: no browser runtime initializes PostHog, so a reverse proxy is not required. |
| Key activation events captured | `pass` | `dashboard/auth.py:3` | No auth/billing paths detected: the password gate has no user accounts and there are no signup, onboarding, checkout, purchase, or subscription surfaces. |

#### Assumptions and blind spots

_No findings to qualify; the standard checks for this area passed cleanly._

### Live Data

What PostHog itself has already flagged for this project, read from its recommendations and health checks. These come from ingested data rather than source code, so they catch problems no static scan can see — stack frames that never resolve, syncs that keep failing, alerts nobody wired up.

| Check | Status | File | Details |
|-------|--------|------|---------|
| Open findings in PostHog | `pass` | — | 3 open findings from PostHog. |
| Error alerts not wired | `suggestion` | — | No alert is wired for: error-tracking-issue-created, error-tracking-issue-reopened, error-tracking-issue-spiking. Set them up in Error tracking → Alerts. |
| Error rate limits not set | `suggestion` | — | No ingestion rate limit set for: project, per_issue. Set them in Error tracking → Settings. |
| Authorized URLs not configured | `warning` | — | Add each confirmed owned production and staging domain under Project settings → Authorized URLs; never populate this security allowlist from event data without explicit confirmation. |

#### Assumptions and blind spots

These findings reflect what PostHog has observed in the recent lookback window, so a project that has just started sending data, or one whose findings have not been recomputed yet, can show a clean area without being clean. Recommendation rows were still marked as computing even though their actionable metadata was present. Verify the settings directly in PostHog after making changes.

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

- **Changes made:** No source code, dependencies, environment variables, or PostHog project settings were changed. The run only audited source and live PostHog findings, updated this report, and mirrors it to a PostHog notebook.
- **Files modified or created:** Modified only `posthog-audit-report.md`; no project source files were changed.
- **Manual steps:** Configure `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` in every deployed runtime; clear PostHog identity state on logout; add only confirmed owned domains to Authorized URLs; configure error-tracking alerts and project/per-issue ingestion limits.
