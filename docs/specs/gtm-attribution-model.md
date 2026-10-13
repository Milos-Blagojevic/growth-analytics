# Spec: multi-touch attribution, CAC and payback-by-channel model on synthetic data

Status: ready to implement, except the report layer (see [Report layer: open decision](#report-layer-open-decision)).
Owner: Milos Blagojevic. Implementer: a coding agent working from this document alone.

## 1. Goal

Publish a public, reproducible project that models a B2B SaaS company's go-to-market funnel. It must:

1. assign credit for conversions to marketing channels with several attribution models,
2. compute customer acquisition cost (CAC), LTV:CAC and CAC payback by channel and acquisition cohort, under each model,
3. compare every model with the **true** causal effect of each channel, which the synthetic data generator knows because it planted it,
4. document every assumption and show how the main conclusions change when key assumptions change.

It is a portfolio piece for senior analytics engineering roles on growth and go-to-market teams: GTM data model, attribution, CAC/payback, funnel modelling, on dbt with a warehouse, product analytics events, a CRM and billing data. A reader (hiring manager, analytics lead) should come away convinced that the author can:

- build a clean, tested GTM data model from messy source-shaped data,
- implement and explain attribution models,
- define CAC and payback precisely,
- knows that attribution assigns credit and does not measure incrementality, and can show the size of that gap.

## 2. Decisions already made

| Topic | Decision |
|---|---|
| Business | Fictional product-led B2B SaaS (data tooling) with a self-serve path and a sales-assisted enterprise path |
| Stack | Python (generator, Markov model, evaluation) + dbt with DuckDB (all SQL modelling) |
| Attribution | Six rule-based models in SQL plus a data-driven Markov chain model; no Shapley in this scope |
| Data | Fully synthetic, seeded, generated in CI; no real or scraped data |
| Location | A new public GitHub repository under the owner's account (proposed name `gtm-attribution-model`; confirm with the owner before creating it) |
| Report | **Not decided**: a richer report site than README + dbt docs is wanted; options and constraints in section 12 |

## 3. Non-goals

- Marketing mix modelling (MMM), geo-lift or holdout experiments: mention in the write-up as next steps only.
- Shapley value attribution.
- A live app, a server or a database other than the DuckDB file.
- Real company data, real ad accounts or any API calls.
- Anything about the owner's job search, CV or employers in the public repository.

## 4. Repository layout

```
gtm-attribution-model/
  README.md                 # write-up: question, data, methods, results, truth comparison, limits, reproduce
  ASSUMPTIONS.md            # numbered assumptions (section 9)
  LICENSE                   # MIT
  Makefile                  # data, build, eval, report, all, clean
  pyproject.toml / requirements*.txt   # pinned versions
  config/
    generator.yml           # every generator parameter (section 5.2)
  generator/                # Python package: simulate people, touches, conversions, revenue, truth
  warehouse/                # DuckDB file is written here (git-ignored)
  dbt/                      # dbt project (dbt-duckdb): sources, seeds, staging, intermediate, marts, tests, docs
  evaluation/               # Python: truth comparison, sensitivity runs, chart data exports
  report/                   # report layer, chosen later (section 12)
  tests/                    # pytest for the generator and evaluation code
  .github/workflows/ci.yml  # build, test, publish
```

Make targets:

| Target | Does |
|---|---|
| `make data` | Runs the generator from `config/generator.yml`, writes raw and truth tables into `warehouse/gtm.duckdb` (schemas `raw` and `truth`), prints the calibration summary (section 5.6) |
| `make build` | `dbt build` (models + tests) |
| `make eval` | Truth comparison and sensitivity scenarios; writes tables to schema `evaluation` |
| `make report` | Builds the report (after section 12 is decided) |
| `make all` | All of the above from a clean state |

Everything runs offline after dependency install. One command (`make all`) reproduces every number in the README.

## 5. Synthetic data generator

### 5.1 Business shape (defaults; all configurable)

- Period: 24 months, 2024-01-01 to 2025-12-31.
- Company: fictional "Brightline Data", a data pipeline tool. Self-serve plans (Team, Pro) and an Enterprise plan sold by sales.
- Funnel: visit → signup → activated (meaningful product use within 14 days) → paid. Enterprise-fit accounts (about 8% of signups) can instead become sales leads → opportunity → won deal.
- Channels:

| channel | Paid | Source shape | Role in the simulation |
|---|---|---|---|
| `paid_search_brand` | yes | Google Ads | Mostly captures demand created elsewhere; small true effect |
| `paid_search_nonbrand` | yes | Google Ads | Moderate true effect |
| `paid_social_linkedin` | yes | LinkedIn Ads | Strong for enterprise-fit; expensive |
| `paid_social_meta` | yes | Meta Ads | Weak to moderate; many view-through impressions |
| `display` | yes | Programmatic | Mostly impressions, few clicks; real but small effect |
| `events_webinars` | yes | CRM campaign members + finance costs | Offline touches without sessions; strong for enterprise |
| `email` | no media spend | Email clicks with UTMs | Nurtures known leads; affects activation/paid more than signup |
| `organic_search` | no | Web sessions | Baseline demand plus some halo from paid |
| `organic_social` | no | Web sessions | Small |
| `referral` | no | Web sessions | Small, steady |
| `direct` | no | Web sessions | Inflated by missing UTMs and demand capture |

- Calibration targets (the generator is tuned so a default run lands inside these ranges):

| Metric over 24 months | Target range |
|---|---|
| Unique people with at least one session | 120k–180k |
| Signups | 20k–30k |
| Paid customers (self-serve + enterprise) | 1.5k–2.5k |
| Enterprise deals won | 150–350 |
| Total media spend | $2.0M–$3.0M |
| Blended media CAC | $900–$1,800 |
| Self-serve monthly churn | 2.5%–4.5% |

### 5.2 Configuration

`config/generator.yml` holds every parameter, with comments: seed, period, people volume, segment shares, per-channel spend plan (monthly budget, seasonality, pauses), diminishing-returns curve, CTR, view-through share, adstock half-life, true effect per channel on signup (`beta`) and channel quality on downstream conversion, plan prices, churn, expansion, gross margin %, data-quality rates (section 5.4). Changing the seed changes the data and nothing else.

### 5.3 How behaviour is simulated (and how truth is known)

- **People.** Each person `p` has an arrival date, a segment (`self_serve` or `enterprise_fit`), a baseline propensity `b_p` (lognormal), one or two devices (about 30% have two) and a pre-drawn threshold `E_p ~ Exp(1)`.
- **Exposures.**
  - Paid channels produce impressions from daily spend with diminishing returns (`impressions = k_c * spend^alpha_c`, `alpha_c < 1`), spread over people in the channel's audience. Clicks come from a CTR; clicks create web sessions.
  - Unpaid channels create sessions from baseline demand.
  - **Demand capture:** after any paid exposure, a person's chance of later arriving through `paid_search_brand`, `organic_search` or `direct` rises (the "halo"). These channels then often hold the last touch without having caused much.
  - `events_webinars` creates offline touches (CRM campaign members) for leads only. `email` creates clicks for known leads only.
- **Signup.** Discrete daily hazard. Each person accumulates `H_p(t) = sum over days of (lambda0 * b_p + sum_c beta_c * A_pc(t))`, where `A_pc` is the person's adstocked exposure to channel `c` (geometric decay with the channel's half-life). The person signs up on the first day `H_p(t) >= E_p`.
  - The signup happens in a session on that day. If the person has no session that day, the generator creates one through a demand-capture channel (brand search, organic search or direct, by configured weights). This is realistic and makes last-touch models over-credit those channels.
- **Downstream.** Activation, paid conversion, enterprise lead → opportunity → won (sales cycle 30–120 days, configurable win rate), monthly churn and enterprise expansion are drawn per person with pre-drawn uniforms (common random numbers), with probabilities depending on segment and on the quality of the channels that touched the person.
- **Truth by counterfactual.** For each channel `c`, rerun the signup and downstream simulation with `beta_c = 0` and channel `c`'s touches removed, **using the same `E_p` and the same pre-drawn uniforms**. The true incremental effect of `c` is the difference from the full run. Also run with every channel removed: the result is the baseline (conversions that would have happened with no marketing). Removal effects do not sum to the total because channels interact; report that sum next to the total and say so in the write-up.

### 5.4 Planted data-quality issues (each rate configurable)

- 15–25% of paid clicks arrive with no UTMs (they look like direct or referral).
- Inconsistent UTM values: case (`LinkedIn`/`linkedin`), synonyms (`cpc`/`paid`/`ppc`, `fb`/`facebook`/`meta`), stray spaces.
- Campaign naming that encodes the channel only sometimes.
- Cross-device: sessions on a person's second device before signup are linkable to the user only if that device later logs in (about 50%).
- Ad platforms report their own conversions (with 1-day view and 7- or 28-day click windows), which add up to well over the real number. Keep them in the raw ad tables so the write-up can contrast platform-reported and modelled numbers.
- A three-week LinkedIn pause in one quarter (a natural experiment worth pointing at in the write-up).
- Late-arriving cost data for events (booked in the month after the event).

### 5.5 Output tables

Schema `raw` (shaped like real sources; column names in the source's style):

| Table | Grain | Key columns |
|---|---|---|
| `google_ads__campaign_daily` | campaign × day | date, campaign_id, campaign_name, network (search), spend, impressions, clicks, platform_conversions |
| `linkedin_ads__campaign_daily` | campaign × day | same shape, LinkedIn naming |
| `meta_ads__campaign_daily` | campaign × day | same shape, includes view-through conversions |
| `display__campaign_daily` | campaign × day | same shape |
| `events__pages` | page view | anonymous_id, user_id (null before identify), timestamp, url, referrer, utm_source, utm_medium, utm_campaign, utm_content, gclid, device_type |
| `events__identifies` | identify call | anonymous_id, user_id, timestamp |
| `events__tracks` | product event | user_id, event (`signed_up`, `pipeline_created`, `pipeline_run_succeeded`, ...), timestamp |
| `crm__leads` | lead | lead_id, email_hash, user_id (nullable), created_at, lead_source, is_enterprise_fit |
| `crm__campaign_members` | lead × campaign | lead_id, campaign_id, campaign_type (`webinar`, `event`, `email`), status, responded_at |
| `crm__opportunities` | opportunity | opportunity_id, lead_id, account_id, created_at, close_date, stage, amount_arr, is_won |
| `billing__subscriptions` | subscription | subscription_id, user_id or account_id, plan, started_at, canceled_at, mrr_start |
| `billing__invoices` | invoice | invoice_id, subscription_id, period_start, period_end, amount, status |
| `finance__marketing_costs` | cost line × month | month, category (`events`, `tools`, `agency`, `salaries`), channel (nullable), amount |

Schema `truth` (read only by `evaluation/`, never by dbt models; dbt must not declare it as a source):

| Table | Content |
|---|---|
| `person` | person id ↔ anonymous ids ↔ user_id, segment, real exposure counts per channel |
| `channel_effect` | per channel: removal-effect signups, activations, paid customers, enterprise wins, and incremental gross margin over the period |
| `baseline` | the same counts with all marketing removed |
| `parameters` | the effective generator parameters for the run |

### 5.6 Generator checks

- `make data` prints a calibration summary: every section 5.1 target with its value and in or out of range.
- pytest: same seed gives identical output (row counts and a checksum per table); every target is inside its range for the default seed; removing a channel never increases conversions; the planted UTM-loss rate is within ±2 points of the configured rate.

## 6. dbt project (dbt-duckdb)

Naming: `stg_<source>__<entity>`, `int_<entity>`, `fct_` and `dim_` marts. Every model has a description and column docs; every primary key has `unique` and `not_null` tests.

### 6.1 Seeds

- `channel_mapping`: ordered rules (source pattern, medium pattern, campaign pattern, has gclid, referrer pattern) → channel. First matching rule wins; unmatched sessions with no referrer are `direct`.
- `channel_attributes`: channel, is_paid, cost source, display name, colour for charts.

### 6.2 Staging

One model per raw table: rename to consistent snake_case, cast types, normalise UTMs (lowercase, trim, synonym map), nothing else.

### 6.3 Intermediate

| Model | Grain | Content |
|---|---|---|
| `int_identity_map` | anonymous_id | user_id from identifies (first identify wins); unmatched anonymous ids stay anonymous |
| `int_sessions` | session | page views sessionised (30-minute inactivity), landing UTMs/referrer, channel from `channel_mapping`, user_id via identity map |
| `int_touchpoints` | touchpoint | sessions plus offline touches (campaign members, email clicks) in one table: touch_id, user_id, timestamp, channel, touch_type (`click`, `view`, `offline`) |
| `int_conversions` | conversion | user_id or account_id, conversion_type (`signup`, `activation`, `first_paid`, `enterprise_won`), timestamp, first-month MRR |
| `int_journeys` | conversion × eligible touch | touches before the conversion inside the lookback window, ordered, with position, days before conversion, and eligibility under the touch-type setting |
| `int_spend_daily` | channel × day | ad spend from all platforms plus channel-attributed costs from finance, mapped to channels |
| `int_customer_monthly_margin` | customer × month | MRR from invoices, gross margin (MRR × gross-margin %), active flag, churn month |

### 6.4 Attribution models

All produce rows in `fct_attribution_credit` (grain: conversion × touch × model) with `credit` between 0 and 1. For every model and conversion, credits sum to exactly 1 (a dbt test, tolerance 1e-9). A conversion with no eligible touches gets one synthetic touch on channel `unattributed`.

| Model id | Rule |
|---|---|
| `first_touch` | 100% to the first eligible touch |
| `last_touch` | 100% to the last eligible touch |
| `last_non_direct` | 100% to the last touch that is not `direct`; if all are direct, to the last direct touch |
| `linear` | Equal split |
| `time_decay` | Weight `2^(-days_before / half_life)`, normalised; half-life is a dbt var (default 7 days) |
| `u_shaped` | 40% first, 40% last, 20% split over the middle; one touch gets 100%, two touches 50/50 |
| `markov` | Removal effect from a first-order Markov chain over channel paths (6.5) |

dbt vars (all documented in `ASSUMPTIONS.md`): `lookback_days` (default 30), `include_view_through` (default false), `time_decay_half_life_days` (7), `conversion_type_for_attribution` (default `first_paid`; signup is also built), `session_timeout_minutes` (30).

### 6.5 Markov model

- Implement as a dbt Python model on DuckDB if dbt-duckdb's Python model support is enough; otherwise a Python step that reads `int_journeys` and writes `markov_channel_weights`, declared as a source. Document which was used.
- Paths: ordered channel sequences per conversion, plus non-converting paths of people with touches and no conversion inside the period (needed for the null state). States: `start`, channels, `conversion`, `null`.
- Removal effect per channel = 1 − P(conversion with the channel removed) / P(conversion). Normalise the effects to weights summing to 1, then give each conversion's credit to its touches by the channel weights of the channels present (renormalised per conversion).
- Unit tests in pytest on a tiny hand-computed example.

### 6.6 Marts

| Model | Grain | Content |
|---|---|---|
| `fct_attribution_credit` | conversion × touch × model | credit, channel, conversion type, conversion month |
| `fct_attributed_conversions` | model × channel × month × conversion type | summed credit |
| `fct_channel_cac` | model × channel × month (and a whole-period rollup) | spend (media-only and fully loaded), attributed new paid customers, CAC both ways; organic channels report customers and no CAC |
| `fct_cohort_margin` | model × channel × acquisition month × months since acquisition | attributed customers, cumulative gross margin per attributed customer (customer margin weighted by the customer's credit for that channel) |
| `fct_channel_payback` | model × channel (and × cohort) | CAC, payback month = first month where cumulative gross margin per customer ≥ CAC, or "not within data" if never |
| `fct_channel_ltv_cac` | model × channel | LTV = monthly gross margin per customer ÷ monthly churn, capped at 36 months; LTV:CAC |
| `dim_channel` | channel | attributes from the seed |

Definitions to state in the model docs and in `ASSUMPTIONS.md`:

- **Blended CAC** = all media spend ÷ all new paid customers in the period.
- **Paid CAC** = spend on paid channels ÷ customers credited to paid channels.
- **Attributed CAC (channel, model)** = channel spend ÷ customers credited to the channel under the model.
- **Fully loaded CAC** adds the finance cost lines (events, tools, agency, a configured share of salaries) allocated by channel where the cost names one, otherwise by spend share.

## 7. Evaluation (`evaluation/`, writes schema `evaluation`)

Reads dbt marts and `truth.*`. Produces:

1. `truth_vs_attribution`: per conversion type, model and channel, the attributed conversions, true removal-effect conversions, the ratio (over- or under-credit factor) and the absolute error.
2. `model_scorecard`: per model, the mean absolute percentage error over channels, Spearman rank correlation between the attributed and true channel ranking, and the share of credit given to demand-capture channels (`paid_search_brand`, `direct`, `organic_search`) against their true share.
3. `cac_truth`: per channel, **true incremental CAC** (channel spend ÷ true incremental paid customers) next to each model's attributed CAC, and the payback month each implies.
4. `sensitivity`: the scenarios in section 8; for each, payback month and CAC per channel under each model.
5. Chart-ready exports (Parquet) for the report layer.

pytest for the evaluation code on small fixtures.

## 8. Sensitivity scenarios

Run by re-invoking `dbt build --vars ...` into a separate DuckDB schema per scenario (or the same file with a scenario column), then `evaluation/`. At minimum:

| Scenario | Change from default |
|---|---|
| Short window | `lookback_days` 7 |
| Long window | `lookback_days` 90 |
| View-through counted | `include_view_through` true |
| Signup as conversion | `conversion_type_for_attribution` signup |
| Fully loaded CAC | CAC includes finance costs |
| Lower margin | gross margin 70% instead of 80% |
| No cross-device stitching | identity map ignores second-device logins |

The README's sensitivity table shows which channel rankings and payback conclusions survive every scenario and which flip.

## 9. `ASSUMPTIONS.md`

One entry per assumption, numbered and stable (`A01`, `A02`, ...). Each entry: the assumption, the default, why, the alternatives, where it is set (dbt var or config key) and whether a sensitivity scenario covers it. Start with at least:

| Id | Assumption | Default |
|---|---|---|
| A01 | Conversion event for CAC and payback | First paid invoice (self-serve) or won opportunity (enterprise) |
| A02 | Attribution lookback window | 30 days before conversion |
| A03 | Touch types eligible for credit | Clicks and offline touches; views excluded |
| A04 | Session definition | 30 minutes of inactivity |
| A05 | Channel classification | First matching rule in `channel_mapping`; unmatched with no referrer = direct |
| A06 | Identity resolution | Anonymous ids linked via identify; no probabilistic matching |
| A07 | Time-decay half-life | 7 days |
| A08 | U-shaped weights | 40 / 20 / 40 |
| A09 | CAC scope | Media spend only; fully loaded as a scenario |
| A10 | Cost timing | Spend counted on the day it is spent, not lagged to conversions |
| A11 | Organic channels | Get credit and customers but no CAC |
| A12 | Gross margin | 80% of MRR |
| A13 | LTV horizon | Margin ÷ churn, capped at 36 months |
| A14 | Payback definition | First month cumulative gross margin per customer ≥ CAC; cohorts by acquisition month |
| A15 | Currency and tax | USD, net of tax, no FX |
| A16 | Enterprise revenue | ARR from the won opportunity ÷ 12 as MRR from close date |

## 10. README write-up outline

1. The question (one paragraph) and the headline finding (one chart).
2. The company and data: funnel, channels, what is planted, why synthetic.
3. The data model: lineage diagram (from dbt docs), the main marts.
4. Attribution models: one line each, and the credit by channel per model.
5. CAC, payback and LTV:CAC by channel; how much they move between models.
6. Attribution versus truth: scorecard; which channels each model over- or under-credits and why (demand capture, view-through, cross-device).
7. Sensitivity: what survives every scenario.
8. Limitations and next steps: attribution is not incrementality; MMM, geo-lift and holdout tests; Shapley.
9. Reproduce: `make all`; repository map.

Tone: plain and precise; numbers come from the pipeline, never typed by hand. Charts are regenerated by `make`.

## 11. CI

GitHub Actions on push and pull request: install pinned dependencies, `make all`, pytest, `dbt build` tests, fail on any calibration target out of range. On `main`: publish dbt docs and the report site to GitHub Pages. No secrets are needed.

## 12. Report layer: open decision

The owner wants a richer report than README + dbt docs and has not chosen a tool. **The implementer must not choose.** Build phases 1–6 so the report layer is replaceable: it may read only the dbt marts, the `evaluation` schema and the chart-ready Parquet exports, never raw or intermediate tables. Before phase 7, ask the owner to pick, presenting this comparison:

| Option | How it works | Strengths | Costs |
|---|---|---|---|
| Evidence.dev | Markdown pages with SQL blocks against the DuckDB file; built to a static site | Looks like a BI tool (close to Looker/Omni work); SQL-first, which suits an analytics engineering portfolio; DuckDB support | Adds Node to the toolchain; less room for long methods text with maths |
| Quarto | Markdown or notebooks with Python (Plotly or Altair); rendered to a static site | Best for a narrative report with methods and formulas; stays in Python | Feels like a paper rather than a product; charts are less interactive unless built in Plotly |
| Observable Framework | Static site with data loaders (Python/SQL) and JavaScript charts | Most polished, custom interactive visuals | Most frontend work; JavaScript to maintain |

Whatever is chosen: a static site on GitHub Pages, built in CI, with pages for overview, attribution by model, CAC and payback, truth comparison, sensitivity and assumptions (rendered from `ASSUMPTIONS.md`).

## 13. Phases and acceptance criteria

| Phase | Done when |
|---|---|
| 1. Repository and scaffolding | Owner confirmed the repo name; repo created with layout, MIT licence, pinned dependencies, Makefile, empty dbt project that builds, CI running |
| 2. Generator | `make data` produces every section 5.5 table; calibration summary inside every range for the default seed; generator tests pass; truth tables written |
| 3. Staging and intermediate | All staging and intermediate models with tests; conversion counts in `int_conversions` match counts computed directly from the raw tables (signed-up track events, first paid invoices, won opportunities) in a dbt test; share of sessions classified `direct` reported |
| 4. Attribution | Six SQL models and Markov; credit-sums-to-one test passes for every model; Markov unit test passes |
| 5. CAC, payback, LTV:CAC | All section 6.6 marts with docs and tests; blended CAC equals spend ÷ customers recomputed independently in a test |
| 6. Evaluation and sensitivity | `evaluation` tables and all section 8 scenarios produced by `make eval`; chart exports written |
| 7. Report | Owner chose the tool (section 12); site builds in CI and publishes to Pages |
| 8. Write-up and publish | README per section 10 with real numbers and charts; `ASSUMPTIONS.md` complete; dbt docs and report live; repo link added to the side project in the owner's job-hunt dashboard |

Work in small pull requests, one phase or less each, each with its acceptance criteria shown passing.

## 14. Rules for the implementer

- Ask the owner before creating the public repository, before publishing to GitHub Pages, and before phase 7.
- Pin every dependency; record versions in the README.
- Never hard-code a number that appears in the README or report; generate it.
- Keep `truth` out of dbt: only `evaluation/` reads it, so the models cannot cheat.
- No personal data about the owner in the repository.
