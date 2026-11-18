---
name: visualize-changes
description: "Build and open a self-contained HTML change briefing: takeaway first, then a selectable system map with before/after behavior, relationships and related files. Dashboard captures, review notes and git facts are expandable supporting detail. No diff."
disable-model-invocation: true
argument-hint: "[--base <git ref>] [what to emphasise, or who the report is for]"
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(git merge-base *) Bash(git ls-files *) Bash(git rev-parse *) Bash(.venv/bin/python .claude/skills/visualize-changes/scripts/snapshot.py *) Bash(.venv/bin/python .claude/skills/visualize-changes/scripts/report.py *)
---

# Visualize the changes

Build an HTML report that lets the owner understand this change without reading the code first, then open it. Run this only because the owner asked for it.

Arguments: $ARGUMENTS

`report.py` takes the facts it shows (commits, changed files, line counts) from git; the report contains no diff or file contents. You write the explanation, and it carries the whole change: describe it in short plain language and pictures rather than code. Every claim must match the diff: describe what the code now does, not what was intended, and say plainly where they differ.

Use the combined editorial briefing and system-map presentation. Lead with the owner-visible takeaway, not line counts. The map selects one behavior area at a time; its before/after, explanation and related files appear together. Relationships are explicit, never guessed from file order. Review notes, dashboard captures and git facts sit below in disclosures. With JavaScript disabled, every area stays readable.

## 1. Scope

```bash
git merge-base HEAD origin/main              # <base>, unless the arguments give --base
git log --oneline <base>..HEAD
git diff <base> --stat                       # committed and uncommitted changes
git ls-files --others --exclude-standard     # untracked files count too
```

If there are no changes, say so and stop. Read the full `git diff <base>` and every untracked file, plus the README of each touched folder. Use this conversation for the reasons behind the change; when the reason is not known, leave it out rather than guess. Use `.venv/bin/python`; if it is missing, create it with `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.

## 2. Dashboard pages (when the change touches `dashboard/`)

Render the affected pages with the old code and the new, on identical sample data (read `scripts/snapshot.py`'s docstring):

```bash
.venv/bin/python .claude/skills/visualize-changes/scripts/snapshot.py --ref <base> --out .change-reports/pages/before <path> ...
.venv/bin/python .claude/skills/visualize-changes/scripts/snapshot.py --out .change-reports/pages/after <path> ...
```

Pass the same paths to both. Choose the pages the change affects, from: `/`, `/leads`, `/pipeline`, `/projects`, `/runs`, `/interviews`, `/interviews/20261020-se-ranking-senior-data-analyst`, `/c/acme.com`, and any new route. For a CSS or `base.html` change, take `/` and two other pages. Nothing is started: requests run in-process, and the folders are temporary.

## 3. Write the description

Write `.change-reports/notes.json` (git-ignored) in the format at the top of `scripts/report.py`:

- `title`: the takeaway in a few words, not a file name or a generic "Changes" heading.
- `summary`: one or two short paragraphs: what changed for the owner, why, and anything they must do (new setting, migration, redeploy, cost change). This is the first explanation the reader sees.
- `sections`: one per behavior or area, in reading order, not file order. The `title` is also the map's selectable label: keep it short and concrete. Give `before` and `after` whenever behavior changed, as the owner would observe it (a page, command output, a Telegram message, a cost), not as code. Keep each to a sentence or two. `body` adds only the context the comparison does not show. List the section's related `files`.
- `id`: optional on a section; use a unique lowercase name such as `research` or `saved-results` when a connection needs to reference it. IDs start with a letter and otherwise use lowercase letters, digits and hyphens. Existing notes without IDs still work.
- `connections`: optional top-level list, for example `{"from": "research", "to": "scoring", "label": "Eligible roles"}`. Both ends must name section IDs and must differ. Each connection draws an arrow and adds selectable relationship links to the two areas. Include only real dependencies or data moving between areas, supported by the diff. Omit the list when the change has independent areas; the report does not invent arrows. The map's layout follows reading order, not a claim about system topology.
- `diagram`: optional inline SVG inside a section, only when a detailed sequence, branch or layout explains something the map and comparison do not. Do not illustrate the same idea twice. Keep the `viewBox` at most 1000 wide with 13–14px text, and paint no background. Use only `currentColor` and `var(--fg)`, `var(--muted)`, `var(--border)`, `var(--surface)`, `var(--accent)`, `var(--add)`, `var(--del)` for colours, so it follows the report's light and dark themes. It must be well-formed XML made of drawing elements only (shapes, paths, text, groups, markers, gradients). `report.py` rejects anything else, including scripts, styles, links, animation and `foreignObject`, as well as event handlers and references outside the SVG.
- `page_notes`: for each snapshot path, what to look at on that page.
- `review_notes`: risks and edge cases, what was and was not verified (only checks that actually ran in this conversation, with their result), how to try the change locally without spending money, and open questions.

Follow the argument's emphasis or audience if one was given. Keep each text short, a few sentences or bullets; prefer a diagram or the page snapshots wherever they show the change faster. Quote code only when a name is the point (a setting, a command, a file).

## 4. Build and open

```bash
.venv/bin/python .claude/skills/visualize-changes/scripts/report.py --base <base> --notes .change-reports/notes.json \
    [--before .change-reports/pages/before --after .change-reports/pages/after] --open
```

It prints the report's path. Reply with that path and a three- to five-line description of the change. If `report.py` rejects the notes or a diagram, fix them and run it again.

The report inlines `scripts/report.css` and `scripts/report.js`; it needs no server, network, external fonts or libraries. Map areas support click, Enter/Space, arrow keys and Home/End. Relationship links select their destination area. Ordinary section links remain usable without JavaScript. Dashboard snapshots retain a script-disabled iframe sandbox.
