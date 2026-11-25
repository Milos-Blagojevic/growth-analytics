---
name: posthog
description: Keep PostHog analytics and observability in step with a code change. Use whenever a change adds, renames or removes a dashboard action or route, a model call (Claude, Jev, Exa or a new provider), a chat agent tool, a hunt.py command, a dashboard job kind or a scheduled command. Says what each has to report to PostHog and through which function, what may never be sent, how to test it, and what the owner has to change in PostHog itself.
when_to_use: While implementing any feature or behavior change in hunt.py, clients/, pipeline/, inbox/, interview/ or dashboard/, before running the verify skill and before reporting the work done. Also when asked whether something is tracked, why an event, trace or log record is missing in PostHog, or to add, rename or remove one. Skip for docs-only and test-only changes.
argument-hint: "[what the change adds or alters]"
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git merge-base *) Bash(git ls-files *) Bash(.venv/bin/python -m pytest *)
---

# PostHog with every change

A feature is done when PostHog can tell that it is used and whether it works, and still learns nothing the owner keeps private. PostHog is a third party: it gets names, counts, timings and token counts, never text. This skill is the list to go through while you build the change, not after it.

The change: $ARGUMENTS

If that is empty, take the change from the diff (`git diff` against `git merge-base HEAD origin/main`, plus untracked files).

Everything for PostHog goes through `clients/posthog_client.py`; read its docstring and the PostHog convention in AGENTS.md first. What exists today is listed in `clients/README.md` (PostHog) and `dashboard/README.md` (PostHog events).

## 1. What the change has to report

Go through every row. Most changes hit one or two.

| The change adds or alters | PostHog has to hear | How |
|---|---|---|
| Something the owner does on the dashboard: a non-GET route in `dashboard/app.py`, or a new outcome of one | One event each time it succeeds | `posthog_client.capture("<object>_<verb>", {...})` once the work is done, right before the response. Nothing on a refusal (400, 404, 409). Three routes change nothing of the owner's and are not events: `/login`, `/logout` and `/runs/preview` |
| A job kind (`jobs.build`) or a schedulable command (`schedule._form`), or an option the owner chooses when starting one | Nothing new for the kind: `hunt_run_started` carries `kind` and `trigger` for both the button and the schedule. An option is one more property of that event | Add the kind to the `kind` list in `dashboard/README.md`. Give an option to the `Spec` in `jobs.build` (`options`) as a value from a fixed set, a boolean or a count (`mode`, `fiber`, `has_focus`, `limit`, `dry_run`), never as the text of a field, and add it to the same README row |
| A call to Claude | A generation | `posthog_client.parse_anthropic(client, ...)` in place of `client.messages.parse(...)`. Calls that belong together (the batches of one check) share `trace_id=posthog_client.new_trace_id()` |
| A call to another model or a service that answers with a model (a Jev question set, an Exa structured search or agent run, a new provider) | A generation when it was answered | Through `jev.ask`, `exa_client.search_structured` or `exa_client.agent_answer` it is already reported. A new provider reports from its own client module: `posthog_client.generation("<provider>", "<model>", input_tokens=..., output_tokens=..., latency=...)` |
| A tool of the chat agent (`inbox/chat.py`) | A span per call, in the answer's trace | Add the function to the tuple in `build_agent` that `posthog_client.instrument_tool` wraps. Model calls the tool makes join the trace by themselves |
| Another agent, or a flow of several model calls that is one answer to the owner | One trace per answer, one session per conversation | Run it inside `with posthog_client.agent_turn(session_id):` and report its model usage with `generation` before the block ends |
| A `hunt.py` command | A record when it starts and one when it ends, with its exit code | Nothing to add: `main()` runs every command inside `posthog_client.command(args.command)` |
| A `python -m evals` exam | The same for its `--live` run, and a generation per model call | The live part of `main()` already runs inside `command`; a new exam reuses it. The free check reports nothing and makes no request: keep it that way |
| A new entry point (another `__main__`, another long-lived process) | The same as the existing ones | `posthog_client.configure(os.environ.get("POSTHOG_PROJECT_TOKEN"), os.environ.get("POSTHOG_HOST"), service="<name>")` in its `main()`, then `with posthog_client.command(...)` around each command (a `main` that returns its exit code instead of exiting sets it on what `command` yields, as `evals/__main__.py` does), or `log("<name>_started")` and `flush()` on shutdown for a server |
| A page or another GET route, or a link or tab that leads to one | A `$pageview` when the browser loads it as a page, which says where the link was clicked; nothing for what htmx asks for by itself | Nothing to add: `_ReportPageviews` in `dashboard/app.py` reports every GET answered with a full HTML page and 200, by its route as it is declared (`/c/{domain}`, never the address or its query), with the declared route of the dashboard page it was opened from (`previous_route`, which `_previous_route` reads off the `Referer` header, never that page's address), and skips requests with `HX-Request` (a slide-over panel, a form, a live filter, a poll). An exception no route handles is reported by `_ReportErrors` the same way |
| The removal or renaming of any of the above | The event, property value or record goes or changes with it | In the code, its test and the README tables, and tell the owner (step 5) |

A change that touches none of these rows needs nothing for PostHog. Say so in your final message in one line, so the owner knows it was checked.

## 2. Names and properties

- **Events** are `object_verb` in snake case, past tense, named for what the owner did and not for the route or the button: `project_created`, `leads_queued`, `hunt_run_started`. To tell two outcomes of one action apart, add a property with fixed values (`decision`: `accepted` or `dismissed`) rather than a second event.
- **Properties** hold a value from a set fixed in the code (a status from `tracking.STATUSES`, a job kind, `button` or `schedule`), a boolean or a count. One or two per event; `hunt_run_started` also carries the options of its kind.
- **Generations** name the provider in lower case (`anthropic`, `typesafe`, `exa`) and the model as the provider names it, or `<operation>-<variant>` where there is no model name (`search-auto`, `agent-low`).
- **Log records** are `<service>_<what happened>`, with attributes under the same rule as properties.

## 3. What may never be sent

Before you pass a value to `posthog_client`, ask where it comes from. A value from one of these is not a property, an attribute or a name:

- a file under Private files in AGENTS.md (the profile, the CV, `companies.csv`, saved runs, interviews, mail);
- an email, a web page, an Exa result, a posting, an article;
- a prompt, a model's answer, a tool's arguments or what it returned;
- a form field the owner typed (a note, a title, a focus, a topic);
- a company, a domain, a URL, a file path, or an id that names any of them (an interview id, a run id, a suggestion key);
- an exception's message, anywhere (its type is fine; `posthog_client` takes the message off the dashboard's exception reports by itself).

Send the count, the fixed-set value, the boolean or the duration instead. And never:

- construct `Posthog` or an OpenTelemetry exporter outside `clients/posthog_client.py`, use the SDK's model wrappers (`posthog.ai.*`) or its `PosthogASGIMiddleware`;
- pass a `distinct_id`, call `identify`, set person or group properties, or turn person processing on: every event is the owner's already (`_before_send` sets `posthog_client.OWNER` and `process_person_profile: false` on each), since the dashboard has one shared password and no accounts and there is no person to name;
- export one of the programs' own loggers, or attach a handler to them;
- read `POSTHOG_PROJECT_TOKEN` or `POSTHOG_HOST` anywhere but an entry point's `main()`;
- let PostHog fail or slow the work: no call to it that can raise into the feature, no flush in the middle of a command.

If a change seems to need a function in `posthog_client` that takes free text, stop and ask the owner: that is a change to what leaves the machine, not an implementation detail.

The one exception is the owner's choice: the dashboard's opt-in Support widget (`POSTHOG_SUPPORT_WIDGET`, settings in `posthog_client.support_widget`, the loader in `dashboard/templates/base.html`) sends what the owner types in it as a PostHog Support ticket. It makes no other text sendable; a change that widens what the widget's page sends (another event, another page, a capture setting turned on) needs the owner's say, as in AGENTS.md.

## 4. Test it

PostHog is off in tests unless the test asks for the `posthog_api` fixture (`tests/posthog_mock.py`). With it, `posthog_api.events()` and `posthog_api.records()` return what was sent, in the wire format.

- Put recognisable text through the feature (`PRIVATE-NOTE`, a company name, a subject), then assert exactly what was sent (event names with their own properties, in order) and that none of that text is in `json.dumps` of it.
- Check the refusal too: an action that answers 400 sent nothing.
- Put the test where the feature's tests are. A new dashboard action joins the sequence in `tests/test_dashboard_app.py::test_every_action_of_the_owner_reaches_posthog_as_an_event_that_names_nothing_of_theirs`, with each value its properties can take, instead of getting a test of its own; a new page, or a new kind of request a page makes by itself, joins `test_each_page_the_owner_opens_reaches_posthog_as_a_pageview_of_its_route_and_nothing_they_asked_for`. Model calls and commands follow `tests/test_hunt.py::test_posthog_gets_a_command_and_its_model_calls_and_nothing_they_read_or_wrote`, agent tools `tests/test_chat.py::test_posthog_gets_an_answer_as_one_trace_and_a_conversation_as_one_session_without_any_text`.
- Do not patch `posthog_client` functions, and do not add a test that only repeats a `capture` call.

```bash
.venv/bin/python -m pytest tests/test_posthog_client.py tests/test_dashboard_app.py tests/test_hunt.py tests/test_chat.py -q
```

## 5. Docs, and PostHog itself

- `dashboard/README.md`, PostHog events: a row for each event added, changed or removed.
- `clients/README.md`, PostHog: when a new kind of thing is sent (a provider, a log record, a service).
- AGENTS.md: only when the rule of what may be sent changes, which needs the owner's say first.

The code cannot change the PostHog project. Its insights and dashboards (the starter one is "Analytics basics (wizard)", https://eu.posthog.com/project/185214/dashboard/1128817) are built on event names and property values, so in your final message tell the owner:

- each new event, with its properties, so they can add it to an insight;
- each event or property value that was renamed or removed, since an insight built on the old name goes empty without an error;
- a new provider or model name, when cost or latency views filter on it.

## 6. Before you report the change done

- Every row of step 1 that applies is covered, or the final message says that none applies.
- Nothing in step 3 is sent, and a test with recognisable text proves it.
- The README rows are in step with the code.
- The owner has the list from step 5.
- Then run the `verify` skill: it checks this list again and fails a change that leaves an action, a model call or a command unreported, or that sends text.
