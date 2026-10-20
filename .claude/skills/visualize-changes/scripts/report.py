"""Build a self-contained HTML report of the changes between a base commit and the working tree.

    .venv/bin/python .claude/skills/visualize-changes/scripts/report.py --base REF --notes NOTES.json
        [--before DIR --after DIR] [--out FILE] [--open]

The facts come from git: commits, the changed files with their line counts and statuses (committed, uncommitted and
untracked), and where the changes land. The report shows no file contents and no diff: the change is explained by
NOTES.json, written by whoever describes it:

    {
      "title": "One-line name of the change",
      "summary": "What changed and why, for someone who has not seen the work.",
      "sections": [                                  # one per area or behavior, in reading order
        {
          "title": "Short heading",
          "body": "Explanation.",
          "before": "Behavior before (optional)",    # before/after are shown side by side
          "after": "Behavior after (optional)",
          "files": ["path/of/a/changed/file.py"],   # optional; shown as file chips
          "diagram": "<svg viewBox=...>...</svg>"   # optional; drawing elements only (see diagram())
        }
      ],
      "page_notes": {"/runs": "What to look at on this page"},   # optional, for --before/--after pages
      "review_notes": "Risks, open questions, how to try it."     # optional
    }

Text fields are plain text: blank lines separate paragraphs, lines starting with "- " form a list, `code` and
**bold** are formatted. --before and --after are snapshot.py output folders (old and new code); their pages are
shown side by side. The report goes to --out (default .change-reports/<branch>-<commit>.html, git-ignored).
"""

import argparse
import codecs
import html
import json
import os
import re
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

MAX_UNTRACKED_BYTES = 300_000  # larger untracked files are listed without a line count
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def git(*args):
    # Bytes, decoded here with errors="replace", so a file in another encoding (a cp1250 CSV) or a lone \r in a
    # diffed line (which text mode would turn into a line break) cannot stop or skew the report.
    result = subprocess.run(["git", "-c", "core.quotePath=false", *args], capture_output=True)
    if result.returncode:
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", errors="replace")


def unquote(path):
    """A path as git prints it -> the path. Paths with a space end with a tab; paths with a quote, backslash or
    control character are C-quoted ("q\\"t.txt")."""
    path = path.rstrip("\t")
    if len(path) > 1 and path.startswith('"') and path.endswith('"'):
        return codecs.escape_decode(path[1:-1].encode())[0].decode(errors="replace")
    return path


def git_line_path(rest):
    """The path in `diff --git a/X b/X` when both sides name the same file (binary and mode-only changes have no
    ---/+++ lines to take it from); None when they differ."""
    quoted = re.fullmatch(r'"a/(.*)" "b/(.*)"', rest)
    if quoted:
        return unquote(f'"{quoted.group(1)}"') if quoted.group(1) == quoted.group(2) else None
    size = (len(rest) - 5) // 2
    path = rest[2:2 + size]
    return path if size > 0 and rest == f"a/{path} b/{path}" else None


def parse_patch(text):
    """Unified diff text -> [{path, old_path, status, added, removed, binary?}], one per file. Only the counts are
    kept: the report never shows file contents."""
    files, current, in_hunk = [], None, False
    for line in text.split("\n"):  # not splitlines(): diffed text may hold form feeds and other line breaks
        if line.startswith("diff --git "):
            current = {"path": None, "old_path": None, "status": "modified", "added": 0, "removed": 0,
                       "git_path": git_line_path(line[len("diff --git "):])}
            files.append(current)
            in_hunk = False
        elif current is None:
            continue
        elif HUNK.match(line):
            in_hunk = True
        elif in_hunk:  # content lines; a removed line may itself start with "-- "
            if line.startswith("+"):
                current["added"] += 1
            elif line.startswith("-"):
                current["removed"] += 1
        elif line.startswith("new file mode"):
            current["status"] = "added"
        elif line.startswith("deleted file mode"):
            current["status"] = "deleted"
        elif line.startswith("rename from "):
            current["old_path"], current["status"] = unquote(line[len("rename from "):]), "renamed"
        elif line.startswith("rename to "):
            current["path"] = unquote(line[len("rename to "):])
        elif line.startswith("Binary files"):
            current["binary"] = True
        elif line.startswith("--- ") and line != "--- /dev/null":
            current["old_path"] = current["old_path"] or unquote(line[4:])[2:]
        elif line.startswith("+++ ") and line != "+++ /dev/null":
            current["path"] = unquote(line[4:])[2:]
    for f in files:
        if f["path"] is None:  # deleted files, binary and mode-only changes
            f["path"] = f["old_path"] or f["git_path"] or "?"
        if f["status"] != "renamed":
            f["old_path"] = None
        del f["git_path"]
    return files


def untracked_files():
    files = []
    for path in filter(None, git("ls-files", "--others", "--exclude-standard", "-z").split("\0")):
        f = {"path": path, "old_path": None, "status": "added", "added": 0, "removed": 0, "untracked": True}
        if Path(path).is_symlink():  # never follow a link out of the repo
            f["symlink"] = True
            files.append(f)
            continue
        size = Path(path).stat().st_size if Path(path).is_file() else 0
        if size > MAX_UNTRACKED_BYTES:  # never load a huge file just to count its lines
            f["large"] = True
            files.append(f)
            continue
        data = Path(path).read_bytes() if size else b""
        if b"\0" in data:
            f["binary"] = True
        else:
            f["added"] = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)  # git's line breaks
        files.append(f)
    return files


def count(f):
    return f["added"], f["removed"]


def area(path):
    parts = Path(path).parts
    if len(parts) == 1:
        return "(repo root)"
    if parts[0] == ".claude" and len(parts) > 3:
        return "/".join(parts[:3])
    return "/".join(parts[:2]) if parts[0] == "dashboard" and len(parts) > 2 else parts[0]


def inline(text):
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)


def fmt(text):
    """Plain text -> paragraphs and lists (see the module docstring)."""
    blocks = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(line.lstrip().startswith("- ") for line in lines):
            items = "".join(f"<li>{inline(line.lstrip()[2:])}</li>" for line in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append(f"<p>{inline(' '.join(line.strip() for line in lines))}</p>")
    return "\n".join(blocks)


SVG_NS, XLINK_NS = "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)
SVG_ELEMENTS = {"svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
                "text", "tspan", "marker", "use", "symbol", "clipPath", "linearGradient", "radialGradient", "stop"}
# The only functions an attribute value may call: references inside the SVG, theme variables and transforms.
SVG_FUNCTIONS = re.compile(r"(?:\s*(?:url\(#[\w.-]+\)|var\(--[\w-]+\)"
                           r"|(?:matrix|translate|scale|rotate|skewX|skewY)\([-\d\s.,eE+]*\))\s*)+")


def local(name):
    return name.rsplit("}", 1)[-1]


def attribute_allowed(name, value):
    """`name` is the attribute's local name; namespaced attributes other than xlink:href are refused before this."""
    if name == "style" or name.startswith("on") or "\\" in value:  # no CSS: it can load URLs and cover the page
        return False
    if name == "href":
        return re.fullmatch(r"#[\w.-]+", value) is not None
    return "(" not in value or SVG_FUNCTIONS.fullmatch(value) is not None


def diagram(svg, prefix):
    """Checks a notes diagram against an allowlist of drawing elements and attributes, and returns it re-serialized
    from the checked tree at its natural width. No scripts, CSS, event handlers or references outside the SVG.
    Every id and reference gets `prefix`, so diagrams that reuse an id (a marker called "arrow") stay apart."""
    svg = (svg or "").strip()
    if not svg:
        return ""
    if "<!" in svg:
        sys.exit("a diagram may not contain a DOCTYPE, entities, comments or CDATA")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        sys.exit(f"a diagram is not well-formed SVG: {exc}")
    if local(root.tag) != "svg":
        sys.exit("a diagram must be a single <svg> element")
    for element in root.iter():
        if element.tag not in SVG_ELEMENTS and element.tag.removeprefix(f"{{{SVG_NS}}}") not in SVG_ELEMENTS:
            sys.exit(f"diagram element <{local(element.tag)}> is not allowed; use: {', '.join(sorted(SVG_ELEMENTS))}")
        for name, value in element.attrib.items():
            allowed = name == f"{{{XLINK_NS}}}href" or "}" not in name
            if not allowed or not attribute_allowed(local(name).lower(), value):
                sys.exit(f"diagram attribute {local(name)}={value!r} on <{local(element.tag)}> is not allowed")
        for name, value in element.attrib.items():
            if local(name) == "id":
                element.set(name, prefix + value)
            elif local(name).lower() == "href":
                element.set(name, f"#{prefix}{value[1:]}")
            else:
                element.set(name, value.replace("url(#", f"url(#{prefix}"))
    view_box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(view_box) == 4 and "width" not in root.attrib:  # natural size, not stretched to the column
        root.set("width", view_box[2])
    return f'<figure class="diagram">{ET.tostring(root, encoding="unicode")}</figure>'


def load_notes(path):
    """The notes, with their shape checked so a mistake ends in a message rather than a traceback or a wrong report."""
    notes = json.loads(Path(path).read_text())

    def text(value, where, required=False):
        if (value is None and not required) or (isinstance(value, str) and (value.strip() or not required)):
            return
        sys.exit(f"{path}: {where} must be {'non-empty ' if required else ''}text")

    if not isinstance(notes, dict):
        sys.exit(f"{path}: the notes must be a JSON object")
    text(notes.get("title"), "title", required=True)
    text(notes.get("summary"), "summary", required=True)
    text(notes.get("review_notes"), "review_notes")
    sections = notes.get("sections") or []
    if not isinstance(sections, list) or not all(isinstance(s, dict) for s in sections):
        sys.exit(f"{path}: sections must be a list of objects")
    for n, section in enumerate(sections):
        text(section.get("title"), f"sections[{n}].title", required=True)
        for key in ("body", "before", "after", "diagram"):
            text(section.get(key), f"sections[{n}].{key}")
        files = section.get("files", [])
        if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
            sys.exit(f"{path}: sections[{n}].files must be a list of paths")
    page_notes = notes.get("page_notes") or {}
    if not isinstance(page_notes, dict) or not all(isinstance(v, str) for v in page_notes.values()):
        sys.exit(f"{path}: page_notes must map page paths to text")
    return notes


def file_chips(paths, changed):
    chips = []
    for p in paths or []:
        chips.append(f'<span class="chip">{html.escape(p)}</span>' if p in changed
                     else f'<span class="chip missing" title="not among the changed files">{html.escape(p)}</span>')
    return f'<div class="chips">{"".join(chips)}</div>' if chips else ""


def render_section(n, section, changed):
    compare = ""
    if section.get("before") or section.get("after"):
        compare = (f'<div class="compare"><div><h4>Before</h4>{fmt(section.get("before", "")) or "<p>—</p>"}</div>'
                   f'<div><h4>After</h4>{fmt(section.get("after", "")) or "<p>—</p>"}</div></div>')
    return (f'<section class="card"><h3>{inline(section["title"])}</h3>{fmt(section.get("body", ""))}{compare}'
            f'{diagram(section.get("diagram"), f"d{n}-")}{file_chips(section.get("files"), changed)}</section>')


def render_areas(files):
    stats = [(f, *count(f)) for f in files]
    biggest = max([a + d for _, a, d in stats] + [1])
    groups = {}
    for f, a, d in stats:
        groups.setdefault(area(f["path"]), []).append((f, a, d))
    out = []
    for name, rows in sorted(groups.items(), key=lambda g: -sum(a + d for _, a, d in g[1])):
        total_a, total_d = sum(a for _, a, _ in rows), sum(d for _, _, d in rows)
        items = []
        for f, a, d in sorted(rows, key=lambda r: -(r[1] + r[2])):
            label = next((name for name in ("symlink", "binary", "large") if f.get(name)), f"+{a} −{d}")
            title = f'{f["old_path"]} → {f["path"]}' if f.get("old_path") else f["path"]
            items.append(
                f'<li><span class="name" title="{html.escape(title)}">{html.escape(Path(f["path"]).name)}</span>'
                f'<span class="status {f["status"]}" title="{f["status"]}{" (untracked)" if f.get("untracked") else ""}">'
                f'{"U" if f.get("untracked") else f["status"][0].upper()}</span>'
                f'<span class="bar"><span class="a" style="width:{100 * a / biggest:.1f}%"></span>'
                f'<span class="d" style="width:{100 * d / biggest:.1f}%"></span></span>'
                f'<span class="num">{label}</span></li>')
        out.append(f'<div class="area"><h4>{html.escape(name)} <span class="num">+{total_a} −{total_d}</span></h4>'
                   f'<ul class="files">{"".join(items)}</ul></div>')
    return "".join(out)


def render_pages(before_dir, after_dir, page_notes):
    before = json.loads((before_dir / "pages.json").read_text())
    after = json.loads((after_dir / "pages.json").read_text())
    out = []
    for path in dict.fromkeys([*after, *before]):
        frames, contents = [], []
        for label, folder, pages in (("Before", before_dir, before), ("After", after_dir, after)):
            page = pages.get(path)
            body = (folder / page["file"]).read_text() if page else ""
            contents.append(body)
            if not page:
                frame = '<div class="empty">Not captured</div>'
            elif page["status"] != 200 or not body:
                where = f" → {html.escape(page['location'])}" if page.get("location") else ""
                frame = f'<div class="empty">HTTP {page["status"]}{where}</div>'
            else:
                frame = f'<iframe sandbox="" loading="lazy" srcdoc="{html.escape(body, quote=True)}"></iframe>'
            frames.append(f'<div class="frame"><h4>{label}</h4>{frame}</div>')
        same = ' <span class="status">unchanged</span>' if contents[0] == contents[1] and contents[0] else ""
        note = fmt(page_notes.get(path, ""))
        out.append(f'<section class="page"><h3><code>{html.escape(path)}</code>{same}</h3>{note}'
                   f'<div class="frames">{"".join(frames)}</div></section>')
    return "".join(out)


CSS = """
:root { --bg:#fbfbfa; --fg:#1d1d1f; --muted:#6b6b70; --border:#e3e3e0; --surface:#fff; --accent:#3b5bdb;
  --add:#1a7f37; --add-bg:#e6f4ea; --del:#c62828; --del-bg:#fdecea; --hunk:#eef1fb; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --fg:#e8e8ea; --muted:#9a9aa2; --border:#2c2c31;
  --surface:#1c1c20; --accent:#8ea4ff; --add:#56d364; --add-bg:#12261a; --del:#ff7b72; --del-bg:#2d1517; --hunk:#1d2236; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:1180px; margin:0 auto; padding:40px 24px 80px; }
h1 { font-size:28px; margin:0 0 6px; } h2 { font-size:19px; margin:44px 0 14px; border-bottom:1px solid var(--border); padding-bottom:6px; }
h3 { font-size:16px; margin:0 0 8px; } h4 { font-size:13px; margin:0 0 6px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
code { font:13px ui-monospace, SFMono-Regular, Menlo, monospace; background:var(--hunk); padding:1px 4px; border-radius:4px; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
.meta { color:var(--muted); font-size:14px; }
.stats { display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin:24px 0; }
.stat { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
.stat b { display:block; font-size:24px; } .stat span { color:var(--muted); font-size:13px; }
.stat.add b { color:var(--add); } .stat.del b { color:var(--del); }
.card, .area, .page { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:18px 20px; margin:0 0 14px; }
.compare { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:12px 0; }
.compare > div { border:1px solid var(--border); border-radius:8px; padding:10px 14px; }
.compare > div:first-child { border-left:3px solid var(--del); } .compare > div:last-child { border-left:3px solid var(--add); }
.diagram { margin:14px 0; padding:12px; border:1px dashed var(--border); border-radius:8px; overflow-x:auto; color:var(--fg); }
.diagram svg { max-width:100%; height:auto; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
.chip { font:12px ui-monospace, Menlo, monospace; border:1px solid var(--border); border-radius:999px; padding:2px 9px; }
.chip.missing { color:var(--muted); text-decoration:line-through; }
.areas { display:grid; grid-template-columns:repeat(auto-fill, minmax(420px, 1fr)); gap:14px; }
.area h4 { display:flex; justify-content:space-between; }
.files { list-style:none; margin:0; padding:0; }
.files li { display:grid; grid-template-columns:minmax(0, 1fr) 22px 90px 76px; gap:8px; align-items:center; padding:3px 0; font-size:13px; }
.files .status { margin:0; text-align:center; padding:1px 0; }
.files li .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bar { display:flex; height:8px; background:var(--hunk); border-radius:4px; overflow:hidden; }
.bar .a { background:var(--add); } .bar .d { background:var(--del); }
.num { font:12px ui-monospace, Menlo, monospace; color:var(--muted); text-align:right; }
.status { font-size:11px; border-radius:4px; padding:1px 6px; background:var(--hunk); color:var(--muted); margin-left:8px; }
.status.added { color:var(--add); background:var(--add-bg); } .status.deleted { color:var(--del); background:var(--del-bg); }
.frames { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
@media (max-width:1100px) { .frames, .compare { grid-template-columns:1fr; } .stats { grid-template-columns:repeat(2, 1fr); } }
.frame iframe { width:100%; height:640px; border:1px solid var(--border); border-radius:8px; background:#fff; }
.empty { color:var(--muted); padding:20px; border:1px dashed var(--border); border-radius:8px; text-align:center; }
.commits { list-style:none; padding:0; } .commits li { padding:3px 0; } .commits code { margin-right:8px; }
nav.toc { font-size:14px; margin-top:10px; } nav.toc a { margin-right:14px; }
"""


def changed_files(base):
    """Every changed file between `base` and the working tree, with line counts: tracked changes (explicit a/ and b/
    prefixes, which parse_patch expects whatever diff.noprefix or diff.mnemonicPrefix say) plus untracked files."""
    diff = git("diff", "-M", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", base)
    return parse_patch(diff) + untracked_files()


def build(base, notes, before_dir, after_dir):
    files = changed_files(base)
    if not files:
        sys.exit(f"no changes between {base} and the working tree")
    changed = {f["path"] for f in files}
    log = git("log", "--format=%h\t%s", f"{base}..HEAD")
    commits = [line.split("\t", 1) for line in log.split("\n") if line]  # a subject may hold \r or \f
    added = sum(count(f)[0] for f in files)
    removed = sum(count(f)[1] for f in files)
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    head = git("rev-parse", "--short", "HEAD").strip()
    base_short = git("rev-parse", "--short", base).strip()
    dirty = bool(git("status", "--porcelain").strip())
    sections = "".join(render_section(n, s, changed) for n, s in enumerate(notes.get("sections") or []))
    pages = render_pages(before_dir, after_dir, notes.get("page_notes") or {}) if before_dir else ""
    commit_items = "".join(f"<li><code>{html.escape(h)}</code>{inline(s)}</li>" for h, s in commits)
    toc = ["<a href='#summary'>Summary</a>", "<a href='#where'>Where</a>"]
    toc += ["<a href='#pages'>Pages</a>"] if pages else []
    toc += ["<a href='#review'>Review notes</a>"] if notes.get("review_notes") else []
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(notes["title"])}</title><style>{CSS}</style></head>
<body><main>
<h1>{inline(notes["title"])}</h1>
<div class="meta"><code>{html.escape(branch)}</code> · <code>{base_short}</code> → <code>{head}</code>{" + uncommitted changes" if dirty else ""}
 · generated {datetime.now():%Y-%m-%d %H:%M}</div>
<nav class="toc">{"".join(toc)}</nav>
<div class="stats">
  <div class="stat"><b>{len(files)}</b><span>files changed</span></div>
  <div class="stat add"><b>+{added}</b><span>lines added</span></div>
  <div class="stat del"><b>−{removed}</b><span>lines removed</span></div>
  <div class="stat"><b>{len(commits)}</b><span>commits{" + working tree" if dirty else ""}</span></div>
</div>
<h2 id="summary">Summary</h2>
<section class="card">{fmt(notes["summary"])}</section>
{sections}
<h2 id="where">Where the change lands</h2>
<div class="areas">{render_areas(files)}</div>
{f'<h2 id="pages">Dashboard pages, before and after</h2>{pages}' if pages else ""}
{f'<h2 id="review">Review notes</h2><section class="card">{fmt(notes["review_notes"])}</section>' if notes.get("review_notes") else ""}
{f'<h2>Commits</h2><ul class="commits">{commit_items}</ul>' if commits else ""}
</main></body></html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="base commit, usually `git merge-base HEAD origin/main`")
    parser.add_argument("--notes", type=Path, required=True, help="the description, as JSON (see above)")
    parser.add_argument("--before", type=Path, help="snapshot.py output for the base code")
    parser.add_argument("--after", type=Path, help="snapshot.py output for the working tree")
    parser.add_argument("--out", type=Path, help="report file (default: .change-reports/<branch>-<commit>.html)")
    parser.add_argument("--open", action="store_true", help="open the report in the default browser")
    args = parser.parse_args()
    if bool(args.before) != bool(args.after):
        parser.error("--before and --after go together")
    # Paths given relative to where the command ran; git paths are relative to the repository root.
    notes, before, after = (p.resolve() if p else None for p in (args.notes, args.before, args.after))
    out = args.out.resolve() if args.out else None
    os.chdir(git("rev-parse", "--show-toplevel").strip())
    notes = load_notes(notes)
    report = build(args.base, notes, before, after)
    if out is None:
        branch = re.sub(r"[^A-Za-z0-9._-]+", "-", git("rev-parse", "--abbrev-ref", "HEAD").strip())
        out = Path(".change-reports").resolve() / f"{branch}-{git('rev-parse', '--short', 'HEAD').strip()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(out.resolve())
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
