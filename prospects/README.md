# Prospects: lead enrichment, scoring and routing

For each company in `prospects.csv`: Exa reads the company's own site and searches the open web for its funding, size and news; Claude scores how well it fits your ideal customer, copies the figures the research states and writes a short brief; fixed rules turn those figures into a yearly value range, a tier and a route. Every run is saved as JSON and loaded into `prospects.duckdb`.

This is a prototype for a product priced as a share of the customer's revenue or volume. Not built yet: a sync to a CRM such as Attio, a dashboard page, a Jev screen in front of Claude and discovery of new prospects. Nothing here is fitted to closed deals, so the value is a range to rank by, not a forecast.

## Files

| File | Purpose |
|---|---|
| `__main__.py` | `python -m prospects`: the `run` and `load` commands |
| `config.py` | Reads and validates `prospects.toml` |
| `enrich.py` | Per-prospect Exa research (the prospect's own site, then the open web) |
| `assess.py` | Claude assessment: fit score, published figures, value signals, brief (structured output) |
| `value.py` | The value range, tier and route: arithmetic and rules, no model call |
| `warehouse.py` | Builds `prospects.duckdb` from the saved runs |

## Configure

1. `EXA_API_KEY` and `ANTHROPIC_API_KEY` in `.env`, as for the job hunt. `SCORING_MODEL` overrides the Claude model here too.
2. `cp prospects.example.toml prospects.toml` and fill it in. A missing, invalid or unknown key (a misspelt one, say) stops a command before any paid call, with the key named.

   | Key | What it is | Goes to |
   |---|---|---|
   | `[product].description` | What you sell | Claude |
   | `[icp].description` | The kind of company the product is for; the fit score is judged against it | Claude |
   | `[icp].look_for` | What to look for on a prospect's site as evidence of need | Exa |
   | `[icp].disqualifiers` | Hard no-gos; a prospect that clearly matches one is dropped (optional) | Claude |
   | `[pricing].basis` | What your fee is charged on, as a yearly dollar amount at the customer | Exa, Claude |
   | `[pricing].take_rate` | The share of the basis you keep (`0.01` is 1%) | nobody |
   | `[pricing].basis_share_of_revenue` | The share of a prospect's total revenue that would be your basis (default `1.0`) | nobody |
   | `[pricing].revenue_per_employee` | `[low, high]` dollars a year, used when no revenue figure is published; leave it out to never estimate from headcount | nobody |
   | `[routing].min_fit` | Fit score below which a prospect is dropped (default 50) | nobody |
   | `[routing].unknown_value` | Route for a prospect whose value cannot be estimated (default `research manually`) | nobody |
   | `[[routing.tiers]]` | `name`, `min_value` (dollars a year to you) and `route`, listed from the highest `min_value` to the lowest; the names `out` and `unknown` are taken (see [Routing](#routing)) | nobody |

3. `cp prospects.example.csv prospects.csv` and list `name,domain` rows. The file is read like `companies.csv` (see [Configure](../README.md#configure)): domains are normalised, a repeated domain is researched once, columns after the domain are ignored.

## Run

```bash
python -m prospects run                  # research + assess every row of prospects.csv (paid: Exa, Claude)
python -m prospects run --limit 3        # only the first 3 rows
python -m prospects run --research-only  # Exa only: prints what the research found, no Claude
python -m prospects load                 # rebuild prospects.duckdb with the current prospects.toml (free)
```

Options: `--config FILE` before the command (default `prospects.toml`); `run --prospects FILE` (default `prospects.csv`). Run it from the folder that holds those files; `prospect_runs/` and `prospects.duckdb` are created there.

What `run` does for each prospect:

1. **Site search.** One Exa structured search restricted to the prospect's own domain and its subdomains (`www.`, `info.` and the like): what it sells and to whom, how it charges, its published pricing, its customers, figures it states about its size, and what the site shows about `[icp].look_for`. If this search fails the prospect is reported as failed and the second search is not made.
2. **Funding search.** One Exa structured search over the open web: funding stage and rounds, published figures for revenue or your pricing basis, headcount, growth signals and up to five news items. When Exa says the sources describe a different company with a similar name, or the search fails, its findings are dropped and the prospect carries a warning; the site research is still assessed.
3. **Assessment.** One Claude call with your product, ideal customer, disqualifiers and pricing basis, plus the research and the pages Exa cites for each field. It returns a fit score (0-100) with up to three reasons and three concerns, a disqualifier if one of yours clearly applies, the figures the research states (pricing basis, revenue, headcount, funding stage, last round, total raised), up to five value signals and a brief of at most 120 words. Claude is told to copy figures and use null when the research gives none, never to estimate. For revenue and the pricing basis it lists every figure the research states, each with the period it refers to and whether the company or a third party published it, and does not choose between them (see [When figures disagree](#when-figures-disagree)). The disqualifier can only be one you listed, word for word (the output format allows nothing else), and a figure that is zero, negative or beyond any company (above $10 trillion, or 10 million employees) is treated as not published. Claude gets no tools, and the prompt treats the research as data: a page cannot make anything happen beyond a wrong score, figure or brief.
4. **Value and route.** Fixed code, see below.

A failing prospect does not stop the run: it is listed as `ERROR`, saved with its error, and the command exits with status 1. Each prospect is saved to `prospect_runs/<start time>/<domain>.json` as soon as it is done, so a run you interrupt keeps the research it has paid for (it has no `_run.json`, and the next `run` or `load` brings its prospects into the warehouse). A result that cannot be written (a domain too long for a file name, a full disk) fails that prospect only. The run prints each prospect's fit, tier, route, value range, brief, the published figure the value ran on, reasons (`+`), concerns (`-`), value signals (`*`) and research warnings, then what Exa charged and the Claude tokens used.

### Value estimate

The yearly value to you, from the best evidence the assessment holds:

| Method | When | Low and high |
|---|---|---|
| `basis` | The research states a figure for your pricing basis | figure x `take_rate` |
| `revenue` | It states the company's revenue or ARR | revenue x `basis_share_of_revenue` x `take_rate` |
| `headcount` | It states a headcount and `revenue_per_employee` is set | employees x `revenue_per_employee` (low, high) x `basis_share_of_revenue` x `take_rate` |
| `unknown` | None of the above | no estimate |

A published figure gives one number; headcount gives a wide range, because revenue per employee is an assumption you set, not something measured for the prospect. The method is saved with the estimate so you can tell the two apart.

### When figures disagree

Research often states several figures for the same thing: a company's own number from last year and an analyst's newer estimate, say. Fixed code picks the one the estimate runs on, in this order:

1. The most recent year.
2. Within that year, a figure the company stated over a third party's estimate.
3. Then the latest month (a figure dated only by year counts as earlier than one with a month).
4. Then the lower amount.

A figure with no period ranks below every dated one. So a company's $500M from September 2025 loses to an analyst's $865M for July 2026, but would win over an analyst's estimate for any month of 2025. The run prints the figure used and, under it, each other figure of the same kind that was set aside (`not used:`); all of them are in the `figures` table. The pick is made again at every rebuild, like the value and the route.

Two things keep the list honest before the pick:

- **Targets and forecasts are not figures.** Claude is told to leave them out, and a figure for a period later than the month of the run is dropped with a warning in the log: as the most recent one it would win, and a prospect at $1.5M with a "$10M by 2028" plan would be valued at $10M. A forecast with no date, one dated only by the current year, or one whose month the code does not read (a quarter, "December 2026", `12/2026`) relies on Claude leaving it out. The cut is made when a prospect is assessed; a result already saved is not checked again.
- **Periods are read as a month or a year.** A `YYYY-MM` anywhere in the text counts as that month (`2026-07`, `2026-07-15`, `LTM 2026-07`). Any other mention of a year from 1900 to 2099 counts as that year (`FY2026`, `July 2026`, `Q3 2026`). When the text names several periods the latest one counts, even when it is the year a figure was reported in (`2025 (reported 2026)` reads as 2026). Text with no such year (`FY27`, "last year") counts as no period. A figure listed twice is kept once, and an amount that is zero, negative or beyond any company is dropped.

### Routing

Checked in this order:

1. A disqualifier applies, or the fit score is below `min_fit`: tier `out`, route `drop`.
2. The value is unknown: tier `unknown`, route `[routing].unknown_value`.
3. Otherwise the first tier whose `min_value` the estimate reaches. For a range the geometric middle of low and high is compared. Below the last tier: tier `out`, route `drop`.

Value and route are not saved in the run files. They are computed from the saved assessment and the current `prospects.toml` whenever the warehouse is rebuilt, so after changing the take rate, the tiers or `min_fit`, `python -m prospects load` rates every saved prospect again without new research. The disqualifier is part of the saved assessment: removing one from `[icp].disqualifiers` does not bring back prospects already dropped for it, and adding one needs a new `run`.

## The warehouse

`prospects.duckdb` is rebuilt from `prospect_runs/` at the end of every `run` and by `load`. It is derived: deleting it loses nothing, deleting `prospect_runs/` loses research you paid for. Only folders named like a run (`YYYYMMDD-HHMMSS`) and result files named after their domain are read; anything else there (a backup copy, a file that cannot be read, a result in a shape the code does not know or with a value DuckDB will not load) is skipped with a warning that names it, and never stops the rebuild.

| Table | One row per | Holds |
|---|---|---|
| `runs` | run | mode, start and save times, prospect and error counts, Exa dollars, Claude model and tokens |
| `prospects` | prospect per run | the assessment (fit, reasons, concerns, headcount, funding, value signals, brief), the picked pricing-basis and revenue figure (`basis_usd`, `revenue_usd`), the value range and method, tier, route and its reason, research warnings, or the error |
| `figures` | published figure per prospect per run | `kind` (`basis` or `revenue`), amount, the period it refers to, who stated it, the wording in the research, and `used` for the one the value ran on |
| `facts` | research field per prospect per run | what Exa found (`source` is `site` or `money`), its confidence and the URLs it cites |
| `news` | news item per prospect per run | title, URL, date, summary |
| `latest` (view) | prospect | its newest assessed result, or its newest research when it was never assessed; failed results aside. A later `--research-only` run or a failure does not hide a rating |

```sql
-- who to contact first
SELECT name, tier, route, value_low, value_high, value_method, fit_score FROM latest
WHERE tier NOT IN ('out', 'unknown') ORDER BY sqrt(value_low * value_high) DESC;

-- where a figure came from
SELECT field, value, confidence, urls FROM facts WHERE domain = 'acme.com' AND field IN ('revenue', 'headcount', 'funding');

-- prospects whose value rests on an outside estimate
SELECT domain, amount_usd, as_of, evidence FROM figures JOIN latest USING (run_id, domain)
WHERE used AND stated_by = 'third party';
```

DuckDB lets one process write a database file or several read it, never both. A rebuild therefore writes a new file and swaps it in, so an app or notebook that holds `prospects.duckdb` open read-only (`duckdb.connect(path, read_only=True)`) keeps working and sees the new data when it reconnects.

## Costs

Two Exa structured searches and one Claude call per prospect. `run` prints what Exa charged and the Claude tokens, and saves both in `prospect_runs/<start time>/_run.json`. On the first real runs (two large, well-indexed companies) Exa charged $0.007 per search, so $0.014 per prospect, and the Claude call took about 5,700 input and 900 to 1,300 output tokens per prospect on `claude-sonnet-5-5`. Small prospects have not been measured; use `--limit` for the first runs.

## Known limits

- Exa is a search engine, not a company database. A small company's site may be barely indexed, so its fields come back "unknown"; the prospect then scores low or lands in `research manually`. Private companies rarely publish revenue, so most estimates will use the headcount method.
- Claude converts other currencies and monthly figures to yearly dollars at a rough rate, and the period of a figure and who stated it are its reading of the research. The original wording is kept in `figures.evidence` to check against.
- When your pricing basis is close to revenue itself (subscription revenue at a software company, say), Claude may list the same figures under both or under either one; under the basis the estimate runs without `basis_share_of_revenue`.
- Two runs on the same prospect differ. Exa returns other sources and other figures, and the scoring model accepts no temperature (see Score noise in [Run](../pipeline/README.md#run)). On two runs half an hour apart, with a loose placeholder profile, one company's fit moved from 30 to 52 (from dropped to the top tier) and another's value from $8.65M to $6M, because the second run found a newer, lower third-party estimate. The rule for figures is fixed, but it weighs every third party the same. Read fit and value as bands, and look at `figures` before acting on a prospect near a threshold.
- Saved research cannot be assessed again without researching again: a prospect whose Claude call failed, or one from a `--research-only` run, needs a new `run`.
- `python -m prospects` does not take the `hunt.py` lock: two `run`s at once in one folder both spend, and one started in the same second as another exits before spending.
- DuckDB cannot open a database under a folder whose path contains `?` or a backslash. `run` still saves its results there, but the warehouse is not rebuilt and the command says so.
