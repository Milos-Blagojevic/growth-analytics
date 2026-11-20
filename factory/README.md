# Factory

Code tasks from Telegram: you ask the chat bot for a change in this repository, confirm the task it proposes, and a coding agent works on it unattended and opens a pull request. The pull request's overview arrives in Telegram. This is stage one: this repository only, one pull request per task, and nothing is ever merged for you.

## Files

| File | Purpose |
|---|---|
| `intake.py` | The chat bot's side: the two tools the agent gets (propose a task, list tasks), the proposal shown under its reply, and the `/go` command that starts the workflow |
| `worker.py` | The job's side, run by the workflow with `uv run`: clone, container, Pydantic AI `Coder` agent, patch, branch, pull request, Telegram overview |
| `../.github/workflows/factory.yml` | The Factory workflow: started only by `workflow_dispatch`, only on the default branch |
| `../clients/github.py` | The GitHub REST calls both sides make |

## How a task runs

1. You write to the bot, for example "on the Runs page, show the newest run first". The agent words a self-contained task and proposes it.
2. The bot puts the task under the agent's reply, word for word, with a command such as `/go_482915`. Nothing has started. A newer proposal, `/new` or a failed answer discards the waiting one.
3. You tap the command. The bot starts the Factory workflow on `main` with the task and a task id (the UTC time, `YYYYMMDD-HHMMSS`) and replies with the link to the run. Each command works once.
4. The job clones the default branch and builds the `factory-sandbox` image (Python 3.13, git, ripgrep, `requirements-dev.txt`). The agent ([`Coder`](https://pydantic.dev/docs/ai/harness/coder) from Pydantic AI Harness, on `claude-opus-5-5`) works on a copy of the clone in a container without network access. It reads `AGENTS.md`, edits files and runs the tests.
5. What the agent changed leaves the container as one patch (git-ignored files stay behind). The job applies it to its own clone, makes one commit, pushes the branch `factory/<task id>` and opens the pull request. The pull request holds the agent's summary, your task, the checks it ran with their outcomes and what it could not verify. It is a draft when the agent says the task is not finished.
6. Telegram gets the overview: the pull request's link, the agent's report (its title and summary, quoted), the changed files, the checks and what the run used. When nothing needed changing there is no pull request and the message says why. A failure (a limit, a refused change, a GitHub error) is reported the same way, with what the run used.

"How are my code tasks doing?" lists the latest tasks with the state of their job and their pull request.

You can also start a task without the bot: **Actions → Factory → Run workflow**, with a task id in the same format.

## What can and cannot happen

- **An email cannot start a task.** The chat agent reads untrusted email, so it can only propose. Starting is fixed code: it takes the command from your chat (`TELEGRAM_CHAT_ID`), for the proposal you were shown, once.
- **The bot cannot push code.** Its token (`FACTORY_DISPATCH_TOKEN`) starts workflows and reads their runs. The token that pushes (`FACTORY_PUSH_TOKEN`) exists only as an Actions secret. Someone holding the bot's token could start the Factory with a task of their own (a paid run, a pull request and a Telegram message you would see), start the Verify workflow, and cancel or re-run runs. They could not push code or read a secret.
- **The agent holds no credentials and has no network.** The container gets a copy of the clone and nothing from the job's environment. The push token is passed to git through the environment and never written into the clone.
- **Nothing the agent wrote runs on the job.** Only the patch leaves the container, as data. Git on the job runs in the job's own clone with hooks off, and refuses a patch that reaches into `.git` or outside the clone.
- **Workflows are off limits.** The worker refuses a change under `.github/workflows`, and the push token has no Workflows permission, so GitHub would refuse the push as well.
- **The job runs only on the default branch.** A run started on another branch is skipped: it would take `worker.py` from that branch and hand it the secrets.
- **Branches are never overwritten and nothing is merged.** The push is not forced, and merging stays yours.
- **What the agent writes stays text.** In the commit and the pull request it cannot mention people or refer to an issue: a zero-width space follows each `@`, each `#` and each `GH-`, `/issues/` or `/pull/` before a number. In Telegram its report, and the reason of a failure (which can quote git or the container), are quoted line by line, and each file, check and unverified item is one list entry; the message shows at most 3,000 characters of the report and 30 entries per list, and the pull request has the rest. A line that is neither quoted nor a list entry is therefore the job's own, the link at the top included. In the job's log the runner takes no commands while the agent runs or a message is printed.
- **A container is not a virtual machine.** It shares the runner's kernel. The runner is a fresh GitHub machine per job, which is the outer boundary.

A factory pull request is code like any other: CI runs its tests without secrets, and you read it before merging. Read what it changes under `.claude/skills/verify/` before adding the `verify` label: the Verify workflow runs the pull request's own `ci.py` with the Anthropic key.

This repository is public, so the task text, the pull request and the job's log (each command the agent ran) are public too. Do not put anything private in a task.

## Set up, once

1. Create two fine-grained personal access tokens (**GitHub → Settings → Developer settings → Fine-grained tokens**), each limited to this repository:
   - The dispatch token: **Actions** read and write. On a private repository also **Pull requests** read-only.
   - The push token: **Contents** read and write, **Pull requests** read and write. Leave **Workflows** off.
2. Add the Actions secrets (**Repository → Settings → Secrets and variables → Actions**): `FACTORY_PUSH_TOKEN`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. `ANTHROPIC_API_KEY` is already there for the Verify workflow. Optional, both or neither: `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` (see [PostHog](#posthog)).
3. Give the bot `FACTORY_REPO` (`owner/name`) and `FACTORY_DISPATCH_TOKEN`: in `.env` on the laptop, as service variables on Railway (changing them redeploys). Without both, the bot answers that code tasks are not set up, to a request and to `/go`.
4. Merge this workflow to `main`: GitHub starts `workflow_dispatch` runs only for a workflow file on the default branch.

## Cost and limits

- Each task is one paid Claude session. It stops at 150 model requests or $10, whichever comes first (`worker.MAX_REQUESTS`, `worker.MAX_DOLLARS`; `--max-requests` and `--max-dollars` in the workflow change them). The dollar limit only works while Pydantic AI knows the model's price, which it does for `claude-opus-5-5`; the request limit always works. The verify runs on this repository take 50 to 120 requests. The overview states what each run used.
- A run that stops at a limit leaves no pull request: the unfinished work is gone with the container.
- The job stops after 60 minutes. GitHub Actions minutes are free for a public repository.
- A task is at most 2,000 characters and a change at most 2 MB.
- Proposing a task and listing tasks cost what any chat answer costs.

## PostHog

Optional, like everywhere else in this repo (see [PostHog](../clients/README.md#posthog)). In the chat bot each call of the two code tools is a span in the answer's trace: its name (`propose_code_task`, `code_tasks`) and its seconds. With the two PostHog secrets set, the job reports as the service `factory`: `factory_command_started` and `factory_command_finished` (exit code, seconds) per task, and one generation per run with the provider, the model, the tokens of the whole run and its seconds, also for a run that failed. Never the task, the repository, the task id or anything the agent read or wrote.

## Not in stage one

Other repositories, follow-ups on an open pull request, the verify skill as a gate before you are told, and tasks from a schedule or from GitHub issues.
