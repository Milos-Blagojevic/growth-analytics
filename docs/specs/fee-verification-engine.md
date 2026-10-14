# Spec: payment fee verification engine on synthetic data

Status: ready to implement. Three decisions need the owner first (section 17); none of them blocks phases 1–5.
Owner: Milos Blagojevic. Implementer: a coding agent working from this document alone.

## 1. Motivation

This section explains why the project exists and what it has to prove. It is for the owner and the implementer. **None of it goes into the public repository:** no company names from this section, no job search, no employers.

### 1.1 The gap

The owner is an analytics/data engineer (about 7 years: Snowflake, dbt, Airflow, Terraform, Databricks; founding data hire twice; revenue and CAC modelling; cloud cost attribution). He applied for a founding Data Engineer / Analyst role at an early-stage fintech whose product checks every fee a company pays to its payment providers, and every invoice it sends to its own clients, against the contracts behind them. The posting says: *"A link to what you have built says more than a CV."*

His CV covers the data platform side of that posting well. What it does not show is **payments domain experience**, which the posting asks for directly:

- "Financial data fluency: transactions, fees, FX, billing, balances, and reconciliation-style problems, and you never present a number you cannot trace."
- Nice to have: "interchange, scheme fees, FX, provider statements, settlement files, ledgers, or billing systems" and "onboarded data from inconsistent CSVs, statements … and third-party exports".

This project closes that gap with evidence instead of claims: a public, reproducible repository where messy provider statements are reconciled to an internal ledger, every fee is recomputed from the contract, and every discrepancy is reported with the transaction, the contract clause and the arithmetic behind it.

### 1.2 What a reviewer must see

The reviewer is a fintech founder or data lead reading the README for a few minutes, then perhaps opening one finding and following it to its source. Each duty in the posting maps to something in the repository:

| The role asks for | Where the project shows it |
|---|---|
| Own the platform: warehouse, pipelines, models; lineage and documentation | dbt + DuckDB project with documented models, tests and lineage back to `source_file` / `source_row` (sections 8, 15) |
| Ingest transaction events, provider statements, FX rates, contract terms | Raw ledger, three provider export formats, an FX reference series, contracts as versioned files (section 7) |
| Onboard messy pilot data quickly and correctly | Each provider exports in its own format with planted data-quality problems that must be cleaned, not flagged as overcharges (section 7.6) |
| Data-quality, reconciliation and anomaly checks | Ledger ↔ statement reconciliation, dbt tests, an explainable anomaly detector (sections 8.3, 10) |
| Independently verify FX, fee and billing calculations | A pure-Python reference engine that recomputes every fee; a differential test requires dbt and Python to agree to the cent (section 11) |
| One metric, one definition, one source of truth | Numbered metric definitions, each implemented once (section 8.7) |
| Client-facing reports a CFO can act on | Provider cost breakdown, effective rates, savings summary, provider comparison, dispute packs (section 13) |
| Revenue side: billing that lags the product | Client billing checks and margin per client (section 9) |
| Statistics where it earns its keep; evaluate outputs | Precision and recall of the engine against planted ground truth; a simple, explainable anomaly model (sections 10, 12) |
| AI-native working style, guardrails, human approval | Optional phase: LLM extraction of contract terms, scored against ground truth, with a human approval gate (section 17.3) |

### 1.3 Why synthetic data, and why this shape

- Real provider statements and contracts are confidential, so the data must be synthetic. Synthetic data also gives a **ground truth**: the generator plants every overcharge, so the README can report precision and recall instead of "it found some things".
- The world is shaped like a real European payments business: interchange++ and blended card pricing, volume tiers, minimums, refunds, chargebacks, payouts over different bank rails, and FX with a contractual spread cap. These are exactly the places where provider bills go wrong in practice.
- Both directions are covered. **What you pay:** provider fees and FX. **What you bill:** your own client invoices and margin. That mirrors the product category, without copying any company's wording or visuals.

### 1.4 How it will be used

1. Once the first shareable state is reached (section 18, end of phase 6), the owner sends the repository link as a follow-up to his application and uses it in the interviews.
2. It stays useful for any fintech, payments or billing data role, so it must be presented as a general project, not an application to one company.
3. The repository link is added to the matching side project in the owner's job-hunt dashboard.

## 2. Goal

Publish a public, reproducible project that, for a fictional payments company and six months of synthetic data:

1. turns provider contracts and the company's own pricing agreements into versioned, machine-readable pricing rules,
2. reconciles the internal ledger with each provider's settlement exports,
3. recomputes every provider fee and FX conversion from the contract terms in force at the time, and flags every discrepancy with its evidence: transaction, statement line, contract clause, expected amount, billed amount and the formula,
4. checks the company's own client invoices against its pricing agreements (revenue leakage) and reports margin per client,
5. measures itself against the planted ground truth and reports precision, recall and money found against money planted,
6. produces the outputs a finance team acts on: cost breakdowns, effective rates, a provider comparison, and dispute packs sorted by deadline.

## 3. Decisions already made

| Topic | Decision |
|---|---|
| Business | Fictional "Fernway", a B2B payments platform for European online marketplaces: it collects card payments from buyers, pays out to sellers, and bills its marketplace clients |
| Providers | Three fictional providers: a card acquirer on interchange++ pricing, a card PSP on blended pricing, and a payouts and FX provider (section 7.2) |
| Stack | Python (generator, contract loader, reference engine, evaluation) + dbt with DuckDB (all SQL modelling), the same stack as the owner's attribution project |
| Money | Integer minor units everywhere (cents); rounding is a contract term and is applied exactly as the contract states; no floats in money columns |
| Contracts | YAML files, one per contract version, with clause ids, effective dates and amendments; loaded into tables by a Python step |
| Data | Fully synthetic and seeded, generated in CI; no real or scraped data, no API calls in the default build |
| Period | 2026-01-01 to 2026-06-30 by default (configurable) |
| Location | A new public GitHub repository under the owner's account (proposed name `payments-fee-audit`; confirm with the owner before creating it) |

## 4. Non-goals

- A live app, a server, a database other than the DuckDB file, or any real provider integration.
- Moving money, filing real disputes or sending email; dispute packs are files.
- Real interchange or scheme fee tables. The rate tables are illustrative and labelled as such; they only need to be realistic in shape and order of magnitude.
- Card-scheme rules beyond what the planted cases need (no full interchange qualification logic).
- Tax (VAT on fees) and accounting entries.
- Anything about the owner's job search, the company he applied to, his CV or his employers in the public repository.

## 5. Domain primer (for the implementer)

| Term | Meaning in this project |
|---|---|
| Interchange | Fee the card issuer's bank receives on each card payment; depends on card scheme, card type (consumer debit, consumer credit, commercial) and region (domestic, intra-EEA, inter-regional). The acquirer passes it through at cost. |
| Scheme fee | Fee the card scheme (Visa, Mastercard) charges; also passed through at cost. |
| Acquirer markup | The acquirer's own margin, usually basis points (bps; 1 bp = 0.01%) of the amount plus a fixed amount per transaction. |
| Interchange++ (IC++) | Pricing = interchange + scheme fee + markup, each shown separately. Overcharges hide in the markup, and in "pass-through" lines that are higher than the real interchange. |
| Blended pricing | One all-in rate per card segment, for example 1.4% + €0.25. |
| Volume tiers | The rate depends on monthly processed volume. **Retroactive (whole-volume):** once volume crosses a threshold, the lower rate applies to all of that month's volume. **Marginal (incremental):** each band of volume gets its own rate, like income tax. Contracts state which; mixing them up is a classic billing error. |
| Monthly minimum | If fees for the month are below the minimum, the provider charges the difference as a separate line. |
| Refund | A payment returned to the buyer. Contracts say whether the original markup is credited back and what the refund itself costs. |
| Chargeback | A disputed payment reversed by the issuer; costs a fixed fee per case. |
| Settlement file / statement | The provider's line-level export of what it charged per transaction and per fee type. |
| Invoice | The provider's monthly total; it must equal the sum of its statement lines. |
| FX mid rate | The market rate between two currencies, with no margin. Here a synthetic hourly reference series stands in for a published benchmark. |
| FX spread | (provider rate vs mid) ÷ mid, in bps, measured in the direction that costs the customer. Contracts cap it per currency group and say which timestamp the rate is taken at. |
| Payout rails | SEPA (EUR in Europe), UK Faster Payments (GBP), SWIFT (international, often a percentage fee with a cap). |
| Dispute window | Days after the invoice date during which a billing error can be disputed; after that the money is usually lost. |

## 6. Repository layout

```
payments-fee-audit/
  README.md                 # write-up (section 15)
  ASSUMPTIONS.md            # numbered assumptions (section 14)
  METRICS.md                # numbered metric definitions (section 8.7)
  LICENSE                   # MIT
  Makefile
  pyproject.toml / requirements*.txt   # pinned versions
  config/generator.yml      # every generator parameter
  contracts/
    providers/              # one YAML per provider contract version (truth: what was agreed)
    clients/                # one YAML per client pricing agreement version
  generator/                # Python package: world, transactions, FX series, provider billing, planted issues, truth
  feeaudit/                 # Python package: contract loader, reference fee engine, evaluation, report and dispute-pack builders
  warehouse/                # DuckDB file is written here (git-ignored)
  dbt/                      # dbt-duckdb project
  reports/                  # generated outputs (git-ignored, except examples linked from the README)
  tests/                    # pytest
  .github/workflows/ci.yml
```

| Make target | Does |
|---|---|
| `make data` | Generates raw files and truth from `config/generator.yml`, loads them into `warehouse/fees.duckdb` (schemas `raw`, `truth`), prints the calibration summary (section 7.8) |
| `make contracts` | Validates every contract YAML against its schema and loads it into schema `contracts` |
| `make build` | `dbt build` (models and tests) |
| `make verify` | Runs the Python reference engine and the differential check against dbt (section 11) |
| `make eval` | Evaluation against truth (section 12); writes schema `evaluation` |
| `make report` | Writes the CFO outputs and dispute packs to `reports/` (section 13) |
| `make all` | Everything above from a clean state |

Everything runs offline after the dependency install. `make all` reproduces every number in the README.

## 7. Synthetic world

### 7.1 Company and flows (defaults, all configurable)

- **Fernway** has about 40 marketplace clients in the EU, the UK and Poland, with a base currency of EUR.
- **Card payments in:** about 400k over six months, with amounts drawn per client from a lognormal (median about €60). The card mix covers scheme (Visa or Mastercard), type (consumer debit, consumer credit, commercial) and region (domestic, intra-EEA, inter-regional), with configured shares.
- Each client is routed to one of the two card providers (configured split, about 60/40). A few clients switch provider mid-period.
- **Refunds:** about 3% of payments, 1–30 days after the payment. **Chargebacks:** about 0.3%, 20–90 days after.
- **Payouts out:** about 60k, to sellers in EUR (SEPA), GBP (Faster Payments) and USD/PLN/CHF (SWIFT or local rails), with FX from EUR where the payout currency differs.
- Fernway bills each client monthly under that client's pricing agreement (section 9).

### 7.2 Providers and contract terms (illustrative values)

| Provider | Type | Key terms |
|---|---|---|
| **Northgate Acquiring** | Card acquirer, IC++ | Interchange and scheme fees at cost. Markup tiers on monthly volume, **retroactive**: < €2.5M 45 bps; €2.5–8M 35 bps; > €8M 28 bps; plus €0.04 per transaction. **Amendment** effective 2026-04-01: the €2.5–8M tier drops to 32 bps. Refund: €0.10 fee and the original markup credited back. Chargeback €15. Monthly minimum markup €2,500. Rounding half-up per transaction. Dispute window 60 days. |
| **Brightpay** | Card PSP, blended | EEA consumer cards 1.40% + €0.25; commercial and non-EEA cards 2.90% + €0.25. Monthly volume discount, **marginal**: volume above €1.5M at 1.25% for EEA consumer cards. No fee on declined or failed attempts. Refund €0.20, no credit of the original fee. Chargeback €20. Rounding half-even (banker's rounding) per transaction. Dispute window 30 days. |
| **Kestrel Payouts** | Payouts and FX | SEPA €0.20 per payout; Faster Payments £0.25; SWIFT 0.10% with a minimum of $8 and a cap of $40. FX spread cap vs the hourly mid rate, taken at the execution hour: 40 bps for majors (GBP, USD, CHF), 90 bps for PLN. Dispute window 45 days. |

Values only need to be plausible. The README says they are illustrative.

### 7.3 Contract file format

One YAML file per contract version. Every term has a stable `clause` id, so findings can cite it.

```yaml
contract_id: NG-2025-01
provider: northgate
version: 2
effective_from: 2026-04-01        # version 1 covers up to 2026-03-31
currency: EUR
rounding: {mode: half_up, level: transaction}
dispute_window_days: 60
terms:
  - clause: "4.2"
    kind: markup_bps_tiered
    basis: monthly_processed_volume
    tiering: retroactive
    tiers:
      - {up_to: 2500000, bps: 45}
      - {up_to: 8000000, bps: 32}    # was 35 in version 1
      - {up_to: null, bps: 28}
  - clause: "4.3"
    kind: per_transaction_fixed
    amount_minor: 4
  - clause: "5.1"
    kind: refund
    fee_minor: 10
    credit_original_markup: true
  - clause: "6.1"
    kind: chargeback_fee
    amount_minor: 1500
  - clause: "7.4"
    kind: monthly_minimum
    applies_to: markup
    amount_minor: 250000
  - clause: "3.1"
    kind: pass_through
    components: [interchange, scheme_fee]
    reference: interchange_table_v1
```

- A JSON Schema in `feeaudit/` validates every file (`make contracts` fails on any error). The term kinds are a closed list, each with a reference implementation (section 11) and a SQL implementation.
- Client pricing agreements (section 9) use the same format with `party: client`.
- Illustrative interchange and scheme fee tables are versioned seeds, keyed by scheme × card type × region.

### 7.4 How provider billing is simulated (and how truth is known)

1. Generate the ledger: payments, refunds, chargebacks and payouts with ids, timestamps (UTC), amounts, currency, card attributes and client.
2. Generate the hourly FX mid series per currency pair: a random walk with realistic daily volatility, seeded.
3. For each provider, compute the **correct** charges for every ledger event from the contract version in force at the event time. This is the truth.
4. Simulate what the provider **actually** bills: the correct charges, then the planted discrepancies (section 7.5), then the export quirks and noise (section 7.6).
5. Write each provider's export in its own format, plus a monthly invoice total per provider.

Truth records every planted discrepancy, with the affected line and transaction ids and the correct and billed amounts, and every planted noise event, so false positives caused by noise can be counted.

### 7.5 Planted discrepancies (each rate configurable)

These are what the engine must find. Defaults are set so that planted overcharges are about 2–6% of total provider fees.

| Id | Provider | Discrepancy | Detected by |
|---|---|---|---|
| D01 | Northgate | Stale rate: the old €2.5–8M tier markup (35 bps) is still charged for the first 12 days after the amendment | Contract version by date |
| D02 | Northgate | Retroactive tier not applied: in months that cross €2.5M, the lower rate is applied only to the volume above the threshold | Tier recalculation |
| D03 | Northgate | "Pass-through" interchange above the reference table for some commercial-card transactions | Interchange reference |
| D04 | Northgate | Original markup not credited back on refunds | Refund clause |
| D05 | Northgate | Monthly minimum charged in a month where markup exceeded the minimum | Minimum clause |
| D06 | Brightpay | The same transaction charged twice under two different line ids | Reconciliation |
| D07 | Brightpay | EEA consumer card billed at the non-EEA rate (card region misclassified) | Card segment rule |
| D08 | Brightpay | Fee on a declined or failed attempt | Clause: no fee on failure |
| D09 | Brightpay | Chargeback fee €25 instead of €20 | Chargeback clause |
| D10 | Brightpay | Marginal volume discount not applied above €1.5M | Tier recalculation |
| D11 | Kestrel | FX spread above the cap | Spread vs mid |
| D12 | Kestrel | FX rate taken from a worse hour than the execution hour (spread may still be under the cap) | Timestamp clause |
| D13 | Kestrel | SWIFT fee cap not applied on large payouts | Cap clause |
| D14 | Any | Charge for a ledger event that does not exist (unknown charge) | Reconciliation |
| D15 | Brightpay | Refund fee not charged (an undercharge: reported, never disputed) | Refund clause |

The engine reports differences in both directions. Undercharges (D15) show the checks are not built only to find money in the company's favour.

Rounding differences are not discrepancies. The engine reports them in their own category (`rounding`), with a threshold, and they never count as overcharges.

### 7.6 Planted data-quality noise (must not become findings)

| Id | Noise | Correct handling |
|---|---|---|
| N01 | Exact duplicate rows in a re-exported file (same line id) | Deduplicate in staging; not D06 |
| N02 | Brightpay amounts in minor units, others in major units with comma decimal separators in one file | Parse per provider |
| N03 | Northgate timestamps in Europe/Berlin local time, others in UTC | Normalise to UTC; month boundaries for tiers use the contract's stated time zone |
| N04 | Lines for the last days of a month arriving in the next month's file | Assign by event date, not file date |
| N05 | Lower-case or padded currency codes; inconsistent card type labels | Normalise in staging |
| N06 | Missing provider reference on about 2% of lines | Fallback match on amount, currency, time window and client; report the match method |
| N07 | Ledger export lagging a day behind at the period end | Wait for the next file in the period; at the very end, report the lines as `unmatched_pending`, not as D14 |

### 7.7 Output tables

Schema `raw` (shaped like the sources, with each source's own column names):

| Table | Grain | Notes |
|---|---|---|
| `ledger__payments`, `ledger__refunds`, `ledger__chargebacks`, `ledger__payouts` | event | Internal source of truth for what happened |
| `northgate__settlement_lines` | fee line | Separate lines for interchange, scheme fee, markup, fixed fee, refund, chargeback and minimum |
| `brightpay__transactions_export` | transaction | One all-in fee per transaction, minor units, plus a monthly adjustments file |
| `kestrel__payouts_statement` | payout | Payout fee, applied FX rate, execution timestamp |
| `<provider>__invoices` | provider × month | Invoice total and date |
| `fx__mid_hourly` | pair × hour | Reference rates |
| `fernway__client_invoices`, `fernway__client_invoice_lines` | invoice, line | What Fernway billed its clients |

Every raw row carries `source_file` and `source_row`, which flow through to findings.

Schema `truth` (read only by `feeaudit/evaluation`, never by dbt; dbt must not declare it as a source): `discrepancies`, `client_leakage`, `noise_events`, `parameters`.

### 7.8 Calibration and generator checks

`make data` prints each target with its value and whether it is in range:

| Metric over the period | Target range |
|---|---|
| Card payments | 350k–450k |
| Card volume | €20M–€35M |
| Provider fees ÷ card volume, Northgate | 0.6%–1.1% |
| Provider fees ÷ card volume, Brightpay | 1.4%–2.0% |
| Planted overcharges ÷ provider fees | 2%–6% |
| Northgate months above €2.5M / below €2.5M (seasonality) | at least 2 / at least 1 |
| Every D, N and L type planted | at least once; line-level types on at least 10 lines each (month-level types such as D02 and D05 at least twice) |

pytest checks:
- The same seed gives identical output (row counts and a checksum per table).
- Every target is in range for the default seed.
- With every discrepancy rate set to 0, provider charges equal the truth exactly.
- Each invoice total equals the sum of its lines plus the planted invoice-level issues.

## 8. Fee engine (dbt-duckdb)

Naming: `stg_<source>__<entity>`, `int_<entity>`, `fct_` and `dim_` marts. Every model has a description and column docs; every primary key has `unique` and `not_null` tests.

### 8.1 Contracts

`make contracts` loads the YAML files into schema `contracts`: `contract_versions` (contract, version, effective_from, effective_to, currency, rounding, dispute window) and `contract_terms` (one row per clause, with its parameters as typed columns or a JSON column). The dbt project declares them as sources. A dbt test requires contract versions of the same contract to be contiguous and non-overlapping.

### 8.2 Staging

One model per raw table: rename, cast, parse amounts into minor units, normalise currency codes and card labels, convert timestamps to UTC, deduplicate exact re-exports (N01). It keeps `source_file` and `source_row` and does nothing else.

### 8.3 Reconciliation

| Model | Content |
|---|---|
| `int_ledger_events` | All ledger events in one table: event_id, type, client, amount_minor, currency, timestamp_utc, card attributes, provider |
| `int_provider_lines` | All providers' charges in one shape: line_id, provider, event reference, fee component, amount_minor, currency, event timestamp, statement month, source_file, source_row |
| `int_reconciliation` | Ledger event ↔ provider line matches with match method (`reference`, `fallback`) and status (`matched`, `ledger_only`, `provider_only`, `unmatched_pending`) |

Tests:
- Every provider line ends up in exactly one status.
- The fallback match is one-to-one.
- The sum of lines per provider and month equals the invoice total, or the difference is reported in `fct_invoice_reconciliation`.

### 8.4 Expected charges

`int_expected_charges` (grain: ledger event × fee component) computes what each event should cost under the contract version in force at the event time:

- tiers: monthly volume in the contract's time zone; retroactive or marginal as the clause says,
- fixed per-transaction fees, refund fees and credits, chargeback fees, pass-through interchange and scheme fees from the reference tables,
- monthly minimums as a provider × month line,
- payout fees with minimums and caps,
- FX: expected rate = mid at the execution hour, adjusted by the capped spread. The cost of the realised spread is computed per payout.
- rounding exactly as the contract states (per transaction or per monthly total, half-up or half-even).

Each row keeps the `clause` ids used and a `formula` string with the actual numbers, for example `8,000.00 × 0.32% + 0.04 = 25.64 EUR (NG-2025-01 v2 §4.2, §4.3)`.

### 8.5 Findings

`fct_findings` (grain: finding):

| Column | Content |
|---|---|
| `finding_id` | Stable hash of its inputs |
| `party` | `provider` or `client` |
| `counterparty` | Provider or client |
| `finding_type` | One of the D or L ids' categories (stale rate, wrong tier, duplicate, unknown charge, spread above cap, ...) or `rounding` |
| `event_id`, `line_ids` | Ledger event and provider or invoice lines involved |
| `contract_id`, `contract_version`, `clause` | The term breached |
| `expected_minor`, `billed_minor`, `difference_minor`, `currency` | Money |
| `formula` | The expected calculation with real numbers |
| `evidence` | Source files and rows of every input |
| `invoice_date`, `dispute_deadline`, `days_left` | From the contract's dispute window |
| `method` | `rule` or `anomaly` (section 10) |

Rules:
- A finding is raised only when the difference is at least one minor unit after the contract's rounding.
- Noise handled in staging and reconciliation never produces a finding (evaluated in section 12).

### 8.6 Marts

| Model | Grain | Content |
|---|---|---|
| `fct_provider_costs` | provider × month × fee component × card segment | Billed and expected fees, volume, effective rate |
| `fct_invoice_reconciliation` | provider × month | Invoice total, sum of lines, expected total, difference, findings total |
| `fct_fx_conversions` | payout | Mid rate, applied rate, spread bps, cap, cost of spread, over-cap cost |
| `fct_provider_comparison` | card segment × month | What the same volume cost at its actual provider and would have cost under the other card provider's contract |
| `fct_findings` | finding | As in 8.5 |
| `fct_dispute_queue` | provider × invoice | Open findings, total, earliest deadline, days left |
| `dim_contract_terms` | clause | Readable clause text for the reports |

### 8.7 Metric definitions (`METRICS.md`)

Each metric has a numbered id, one definition, a unit, a grain and the one model that implements it. At least:

| Id | Metric | Definition |
|---|---|---|
| M01 | Processed volume | Sum of successful card payment amounts in EUR (ledger), by event date in UTC |
| M02 | Provider fees | Sum of billed provider charges, net of credits |
| M03 | Effective rate | M02 ÷ M01, in bps |
| M04 | Markup rate | Acquirer markup ÷ M01, in bps (IC++ provider only) |
| M05 | Overcharge | Sum of positive `difference_minor` of rule findings, excluding `rounding` |
| M06 | Undercharge | Sum of negative differences (the provider billed too little) |
| M07 | Recoverable | M05 for findings whose dispute deadline has not passed |
| M08 | FX spread cost | Applied vs mid, in EUR, per payout |
| M09 | Revenue leakage | Expected client billing − actual client billing |
| M10 | Client margin | Client billing − provider costs allocated to that client's events |

## 9. Client billing and margin

- **Pricing agreements** in `contracts/clients/`: per-payment fees by card segment, payout fees by rail, an FX markup on payouts, a monthly platform fee, volume discounts, and promotional discounts with end dates.
- **Planted leakage:**

| Id | Leakage |
|---|---|
| L01 | A payout rail launched mid-period is never billed to clients ("the product launched before billing caught up") |
| L02 | A promotional discount still applied after its end date |
| L03 | The FX markup not billed for one client |
| L04 | A client billed at a higher tier's discount than its volume earns |

- **Models:**
  - `int_expected_client_billing` (event × billing component).
  - `fct_client_billing_findings`, which uses the same columns as `fct_findings` with `party = client`.
  - `fct_client_margin` (client × month): billed, expected, provider costs from `int_provider_lines` allocated by event, and margin (M10). Costs that cannot be traced to an event, such as minimums, are allocated by volume share; this is stated in `ASSUMPTIONS.md`.

## 10. Anomaly detection

It catches what no rule describes: for example a new fee type appearing, or a gradual drift in a pass-through component.

- Per provider, fee component and card segment: the daily effective rate in bps, compared with a 28-day rolling median using a robust z-score based on the median absolute deviation (MAD). Default threshold 4.
- Also flag any fee component or line type never seen before for that provider.
- Output: `fct_anomalies`, plus findings with `method = anomaly` when no rule finding already explains the same lines.
- Each anomaly states the baseline, the observed value and the threshold in plain words.
- Evaluation reports it separately and sets no target.

## 11. Reference engine and calculation validation

- `feeaudit/reference/` holds pure Python functions, one per term kind, that compute the expected charges from the contract YAML and the raw ledger. They share no code with the dbt SQL. Money is `int` minor units; rounding uses `decimal` with the contract's mode.
- Unit tests on hand-computed examples for every term kind:
  - retroactive and marginal tiers at, just below and just above each threshold,
  - minimum and cap,
  - refund credit,
  - half-up and half-even rounding,
  - FX spread at the cap, and one minor unit over it.
- `make verify` is a differential check: the Python expected charges must equal `int_expected_charges` exactly for every event. CI fails on any difference and prints the first ten.

## 12. Evaluation against truth (`make eval`, schema `evaluation`)

- `detection_by_type`: for each D and L id, the planted count, found count, true positives, precision, recall, planted money, found money and money recall.
- `noise_false_positives`: findings caused by any N event (target: zero).
- `invoice_level`: per provider × month, findings total against planted total.
- `anomaly_report`: anomalies against planted events that have no rule (if any are configured), reported only.
- **Targets for the default seed:** precision and recall of at least 0.98 for every D and L type, money recall of at least 0.99, and zero noise false positives. Any target missed fails `make eval`.

## 13. Outputs for a finance team (`make report`)

The outputs are generated, never typed by hand, and written to `reports/`:

1. **Monthly provider cost report:** volume, fees by component, effective and markup rates by provider and card segment, and change against the previous month.
2. **Findings summary:** overcharge and undercharge by provider, type and month; money recoverable now (M07) against money already past its deadline.
3. **Dispute packs:** one folder per provider and invoice, containing:
   - a CSV of the findings, with evidence columns,
   - a short markdown letter: invoice, clauses breached, count, total, and the request for credit,
   - the deadline.
4. **Provider comparison:** cost per card segment under each card contract, and the switch that would have saved the most.
5. **Client report:** leakage findings and margin per client.
6. **"Follow one finding":** one finding traced step by step from raw file row → staging → reconciliation → expected charge → finding. It is generated from real output and linked from the README.

A richer report site is optional (section 17.2).

## 14. `ASSUMPTIONS.md`

The same convention as the owner's attribution project: numbered, stable ids (`A01`, ...). Each entry gives the assumption, the default, why, the alternatives, and where it is set. Start with at least:

| Id | Assumption | Default |
|---|---|---|
| A01 | Base currency and FX for reporting | EUR; ledger amounts in other currencies converted at the hourly mid rate of the event hour |
| A02 | Month boundaries for tiers and minimums | The contract's time zone (Northgate Europe/Berlin, others UTC) |
| A03 | Contract version for an event | The version whose effective dates contain the event timestamp |
| A04 | Rounding | Exactly as each contract states; rounding differences below the threshold are `rounding`, not findings |
| A05 | Finding threshold | One minor unit after rounding |
| A06 | Matching | Provider reference first; fallback on amount, currency, ±48 h and client; one-to-one |
| A07 | Interchange and scheme reference | Illustrative versioned tables, not real scheme rates |
| A08 | FX benchmark | Synthetic hourly mid series standing in for a published reference rate |
| A09 | Cost allocation to clients | By event; fees with no event (minimums) by volume share |
| A10 | Anomaly model | Robust z-score of the daily effective rate vs a 28-day median, threshold 4 |

## 15. README outline (public repository)

1. **The problem** in one paragraph: provider bills and client invoices are rarely checked line by line against contracts. Headline result: one chart of money found, by type.
2. **The world:** the fictional company, providers and contracts, what is planted and why the data is synthetic.
3. **From contract to rule:** one YAML clause, its SQL, and its Python reference function.
4. **The pipeline:** a lineage diagram from dbt docs; reconciliation statuses; how noise is handled.
5. **Findings:** the summary table and "follow one finding" (section 13.6).
6. **Evaluation:** precision and recall by type; no false positives from noise; the differential check.
7. **Finance outputs:** example cost report, provider comparison, a dispute pack.
8. **Revenue side:** leakage and margin per client.
9. **Limits and next steps:** illustrative rates; LLM contract extraction (if not built); real benchmark rates; streaming checks as transactions happen.
10. **Reproduce:** `make all`; repository map; pinned versions.

Tone: plain and precise. Every number comes from the pipeline.

## 16. CI

GitHub Actions on push and pull request: install pinned dependencies, then `make all`, pytest, dbt tests, the differential check and the evaluation targets. Any failure fails the build. On `main`, publish dbt docs (and the report site, if chosen) to GitHub Pages, after the owner approves publishing. No secrets are needed.

## 17. Open decisions for the owner

### 17.1 Repository name and publishing

Proposed: `payments-fee-audit`. Ask before creating the repository and before the first publish to GitHub Pages.

### 17.2 Report layer

The default is generated markdown and CSV in `reports/` plus dbt docs, which is enough for the first shareable state. If a richer site is wanted, use the same tool the owner picks for his attribution project, so his portfolio is consistent. The candidates are:

- **Evidence.dev:** SQL-first pages on the DuckDB file; looks like a BI tool; adds Node.
- **Quarto:** narrative report with Python charts; best for methods text.
- **Observable Framework:** the most polished interactive visuals, with the most JavaScript to maintain.

**The implementer must not choose.** The report layer may read only marts, the `evaluation` schema and `reports/` exports.

### 17.3 Optional phase: LLM contract extraction

- **What it would add:**
  - The generator also writes each contract as prose (`contracts/text/*.md`), with realistic legal wording, cross-references and one amendment letter.
  - An extraction step uses an LLM to turn the text into contract YAML.
  - An evaluation scores the extracted terms field by field against the ground-truth YAML.
  - A **human approval gate** applies: extracted terms carry `approved: false`, and the engine refuses unapproved terms.
- **Why it matters:** it shows AI-native work with guardrails and an evaluation harness, which the role lists explicitly.
- **Costs:**
  - an API key, and a small model bill (a handful of contracts, re-run on change),
  - a non-deterministic step, so it stays out of `make all` and CI uses recorded outputs.
- Ask the owner whether to build it, which model provider to use, and the budget.

## 18. Phases and acceptance criteria

| Phase | Done when |
|---|---|
| 1. Repository and scaffolding | Owner confirmed the name; repository with layout, MIT licence, pinned dependencies, Makefile, an empty dbt project that builds, CI running |
| 2. Contracts and generator | Contract schema and all provider contract YAML files validate; `make data` writes every section 7.7 table and truth; calibration in range; generator tests pass |
| 3. Staging and reconciliation | Staging handles every N type; `int_reconciliation` statuses complete; line sums against invoice totals tested |
| 4. Fee engine and findings | `int_expected_charges`, `fct_findings` and the section 8.6 marts with docs and tests; the reference engine and `make verify` pass with zero differences |
| 5. Evaluation | `make eval` meets every section 12 target for the default seed |
| 6. Finance outputs and README v1 | `make report` writes everything in section 13, items 1–4 and 6; README sections 1–7 and 10 with real numbers. **First shareable state:** the owner can send the link. |
| 7. Client billing and margin | Section 9 models and L01–L04 detection meet the section 12 targets; README section 8 |
| 8. Anomaly detection | Section 10 output and its README paragraph |
| 9. Optional: LLM extraction | Only if the owner approved it (section 17.3) |
| 10. Publish | dbt docs (and report site, if chosen) live; README complete; link added to the side project in the owner's job-hunt dashboard |

Work in small pull requests, one phase or less each, each showing its acceptance criteria passing. Phases 1–6 come first: they are what makes the project useful while the owner's application is open.

## 19. Rules for the implementer

- Ask the owner before creating the public repository, before publishing to GitHub Pages, before phase 9, and before choosing a report tool.
- Money is integer minor units end to end; never use floats for money. Rates may be decimals.
- Keep `truth` out of dbt: only `feeaudit/evaluation` reads it, so the engine cannot cheat.
- The dbt engine and the Python reference engine must not share calculation code.
- Never hard-code a number that appears in the README or reports; generate it.
- Pin every dependency and record the versions in the README.
- The public repository must not mention the owner's job search, any company he applied to, his CV or his employers, and must not copy any company's product wording or visuals.
