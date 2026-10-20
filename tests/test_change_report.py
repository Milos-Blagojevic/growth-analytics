"""The visualize-changes skill's scripts: diff parsing, the diagram allowlist and dashboard page snapshots."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".claude" / "skills" / "visualize-changes" / "scripts"
spec = importlib.util.spec_from_file_location("change_report", SCRIPTS / "report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository with one commit; the test changes its working tree."""
    git("init", "-q", cwd=tmp_path)
    (tmp_path / "keep.py").write_text("a = 1\nb = 2\n")
    (tmp_path / "old_name.py").write_text("".join(f"line {n}\n" for n in range(20)))
    (tmp_path / "gone.txt").write_text("bye\n")
    (tmp_path / "run.sh").write_text("echo hi\n")
    git("add", ".", cwd=tmp_path)
    git("commit", "-q", "-m", "base", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_parses_every_kind_of_change(repo):
    # \f is not a line break for git, and an added "++ ..." line shows up as "+++ ..." inside a hunk, not a header
    (repo / "keep.py").write_bytes(b"a = 1\nb = 3\nc = '\f'\n++ not a header\n")
    git("mv", "old_name.py", 'new"name.py', cwd=repo)  # git C-quotes this name in the rename lines
    (repo / "gone.txt").unlink()
    (repo / "with space.txt").write_text("x\n")
    (repo / 'q"t.txt').write_text("quoted\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x01")
    (repo / "latin.csv").write_bytes("ime;čaša\n".encode("cp1250"))  # not UTF-8
    git("add", "with space.txt", 'q"t.txt', "logo.png", "latin.csv", "gone.txt", cwd=repo)
    (repo / "run.sh").chmod(0o755)
    git("config", "diff.noprefix", "true", cwd=repo)  # the report must not depend on the owner's diff config

    files = {f["path"]: f for f in report.changed_files("HEAD")}

    assert {p: f["status"] for p, f in files.items()} == {
        "keep.py": "modified", 'new"name.py': "renamed", "gone.txt": "deleted", "with space.txt": "added",
        'q"t.txt': "added", "logo.png": "added", "latin.csv": "added", "run.sh": "modified",
    }
    assert files['new"name.py']["old_path"] == "old_name.py"
    assert files["logo.png"]["binary"]
    assert [report.count(files[p]) for p in ("keep.py", "gone.txt", "with space.txt", "run.sh")] == [
        (3, 1), (0, 1), (1, 0), (0, 0),
    ]


def test_report_explains_without_showing_file_contents(repo):
    (repo / "keep.py").write_text("a = 1\nsecret_marker = 42\n")
    notes = {"title": "T", "summary": "S", "sections": [{"title": "One", "files": ["keep.py", "not/changed.py"]}]}

    page = report.build("HEAD", notes, None, None)

    assert "secret_marker" not in page and "b = 2" not in page
    assert '<span class="chip">keep.py</span>' in page
    assert '<span class="chip missing" title="not among the changed files">not/changed.py</span>' in page
    assert ">+1 −1<" in page  # keep.py's line counts in the map of where the change lands


def test_commit_subjects_with_line_break_characters(repo):
    (repo / "keep.py").write_text("a = 2\n")
    git("commit", "-q", "-am", "Fix <b>scores</b>\rand\fmore", cwd=repo)

    page = report.build("HEAD~1", {"title": "T", "summary": "S"}, None, None)

    assert re.search(r"<li><code>[0-9a-f]+</code>Fix &lt;b&gt;scores&lt;/b&gt;\rand\fmore</li>", page)


def test_untracked_files_are_counted_but_symlinks_are_not_followed(repo, tmp_path_factory):
    secret = tmp_path_factory.mktemp("outside") / ".env"
    secret.write_text("KEY=secret\nTOKEN=x\n")
    (repo / "notes.md").write_text("new\fpage\nlast line without break")
    (repo / "link").symlink_to(secret)

    files = {f["path"]: f for f in report.untracked_files()}

    assert report.count(files["notes.md"]) == (2, 0)
    assert files["link"]["symlink"] and report.count(files["link"]) == (0, 0)


def test_diagram_keeps_drawings_at_natural_width():
    out = report.diagram('<svg viewBox="0 0 420 80"><g transform="translate(10, 20) scale(2)">'
                         '<path d="M0 0" fill="var(--accent)"/><text>a &amp; b</text></g></svg>', "d0-")

    assert out.startswith('<figure class="diagram"><svg viewBox="0 0 420 80" width="420">')
    assert "a &amp; b" in out and 'fill="var(--accent)"' in out


def test_diagrams_that_reuse_an_id_stay_apart(repo):
    (repo / "keep.py").write_text("a = 2\n")
    arrow = ('<svg xmlns:xlink="http://www.w3.org/1999/xlink"><defs><marker id="arrow"><path d="{d}"/></marker></defs>'
             '<line marker-end="url(#arrow)"/><use xlink:href="#arrow"/></svg>')
    notes = {"title": "T", "summary": "S", "sections": [{"title": "One", "diagram": arrow.format(d="M0 0")},
                                                        {"title": "Two", "diagram": arrow.format(d="M1 1")}]}

    page = report.build("HEAD", notes, None, None)

    for n in (0, 1):  # each diagram's references point at its own marker
        assert f'id="d{n}-arrow"' in page and f'marker-end="url(#d{n}-arrow)"' in page
        assert f'xlink:href="#d{n}-arrow"' in page


@pytest.mark.parametrize(("notes", "message"), [
    ([], "must be a JSON object"),
    ({"title": 5, "summary": "S"}, "title must be non-empty text"),
    ({"title": "T", "summary": " "}, "summary must be non-empty text"),
    ({"title": "T", "summary": "S", "sections": "abc"}, "sections must be a list of objects"),
    ({"title": "T", "summary": "S", "sections": [{"title": "A", "body": ["a"]}]}, r"sections\[0\].body must be text"),
    ({"title": "T", "summary": "S", "sections": [{"title": "A", "files": "AGENTS.md"}]}, "must be a list of paths"),
    ({"title": "T", "summary": "S", "page_notes": {"/": 1}}, "page_notes must map"),
])
def test_notes_with_the_wrong_shape_are_rejected(tmp_path, notes, message):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps(notes))

    with pytest.raises(SystemExit, match=message):
        report.load_notes(path)


@pytest.mark.parametrize("svg", [
    "<svg><script>alert(1)</script></svg>",
    "<svg><a href='javascript:alert(1)'><text>x</text></a></svg>",
    '<svg xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="https://example.com/x.svg#a"/></svg>',
    '<svg><animate attributeName="href" values="javascript:alert(1)"/></svg>',
    "<svg><style>body{display:none}</style></svg>",
    '<svg onload="alert(1)"/>',
    '<svg><rect style="fill:url(https://example.com/x)"/></svg>',
    '<svg><rect style="position:fixed;inset:0;width:100vw;height:100vh"/></svg>',
    '<svg><rect filter="URL(https://example.com/x)"/></svg>',
    r'<svg><rect fill="u\72l(https://example.com/x)"/></svg>',
    "<svg><foreignObject><div/></foreignObject></svg>",
    '<svg xmlns:h="http://www.w3.org/1999/xhtml"><h:rect/></svg>',
    '<!DOCTYPE svg [<!ENTITY x "y">]><svg><text>&x;</text></svg>',
    '<svg><rect xml:base="https://example.com/"/></svg>',
    "<div/>",
], ids=["script", "js-link", "external-use", "animate", "style", "onload", "external-url", "overlay", "upper-url",
        "escaped-url", "foreign", "html-namespace", "doctype", "xml-base", "not-svg"])
def test_diagram_rejects_anything_but_drawing(svg):
    with pytest.raises(SystemExit):
        report.diagram(svg, "d0-")


def test_snapshot_renders_standalone_pages(tmp_path):
    subprocess.run([sys.executable, SCRIPTS / "snapshot.py", "--out", tmp_path, "/", "/nope"], cwd=REPO, check=True,
                   capture_output=True)

    pages = json.loads((tmp_path / "pages.json").read_text())
    assert {path: page["status"] for path, page in pages.items()} == {"/": 200, "/nope": 404}
    board = (tmp_path / pages["/"]["file"]).read_text()
    assert "<script" not in board and "<link" not in board and "<style>" in board
    assert '"X-CSRF-Token": "csrf"' in board  # the per-process token is replaced, so versions compare equal
    assert "Acme" in board  # the sandbox's sample data
