"""The visualize-changes skill's scripts: diff parsing, the diagram allowlist and dashboard page snapshots."""

import importlib.util
import json
from html.parser import HTMLParser
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".claude" / "skills" / "visualize-changes" / "scripts"
spec = importlib.util.spec_from_file_location("change_report", SCRIPTS / "report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class ReportPage(HTMLParser):
    """Inspect consumer-facing text and links, without pinning the report's layout or wording."""

    def __init__(self, source):
        super().__init__()
        self.ids, self.links, self.text, self.scripts = [], [], [], []
        self.raw = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag in ("script", "style"):
            self.raw = tag

    def handle_endtag(self, tag):
        if tag == self.raw:
            self.raw = None

    def handle_data(self, data):
        if self.raw == "script":
            self.scripts.append(data)
        elif not self.raw:
            self.text.append(data)


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
    text = "".join(ReportPage(page).text)
    assert "keep.py" in text and "not/changed.py" in text


def test_commit_subjects_with_line_break_characters(repo):
    (repo / "keep.py").write_text("a = 2\n")
    git("commit", "-q", "-am", "Fix <b>scores</b>\rand\fmore", cwd=repo)

    page = report.build("HEAD~1", {"title": "T", "summary": "S"}, None, None)

    assert "Fix <b>scores</b>\rand\fmore" in "".join(ReportPage(page).text)


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


@pytest.mark.parametrize(("sections", "connections", "message"), [
    ([{"id": "a", "title": "A"}, {"id": "a", "title": "B"}], [], "duplicate section id"),
    ([{"id": "section-2", "title": "A"}, {"title": "B"}], [], "duplicate section id"),
    ([{"id": 'a" onclick="bad', "title": "A"}], [], "id must use lowercase"),
    ([{"id": "a", "title": "A"}], "a", "connections must be a list"),
    ([{"id": "a", "title": "A"}], [None], "connections must be a list"),
    ([{"id": "a", "title": "A"}], [{"from": "a", "to": "missing"}], "to must name a section id"),
    ([{"id": "a", "title": "A"}], [{"from": 0, "to": "a"}], "from must name a section id"),
    ([{"id": "a", "title": "A"}], [{"from": "a", "to": "a"}], "must join two different sections"),
    ([{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
     [{"from": "a", "to": "b", "label": ["wrong"]}], "label must be text"),
])
def test_invalid_map_relationships_are_rejected(tmp_path, sections, connections, message):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"title": "T", "summary": "S", "sections": sections, "connections": connections}))

    with pytest.raises(SystemExit, match=message):
        report.load_notes(path)


def test_map_relationships_resolve_with_repeated_titles_and_html_text(repo):
    (repo / "keep.py").write_text("private_source_marker = 123\n")
    hostile = '</script><script>window.stolen = true</script>'
    notes = {
        "title": "A useful takeaway",
        "summary": "Owner-facing explanation.",
        "sections": [
            {"id": "research", "title": "Repeated <heading>", "body": "Research explanation.",
             "before": "Aggregator role.", "after": "Lead only.", "files": ["keep.py"]},
            {"id": "scoring", "title": "Repeated <heading>", "body": "Scoring explanation."},
        ],
        "connections": [{"from": "research", "to": "scoring", "label": hostile}],
    }
    path = repo / "notes.json"
    path.write_text(json.dumps(notes))

    page = ReportPage(report.build("HEAD", report.load_notes(path), None, None))

    # Labels must be readable, never executable; each area remains reachable with JavaScript disabled.
    text = "".join(page.text)
    assert hostile in text
    assert "Research explanation." in text and "Scoring explanation." in text
    assert "Aggregator role." in text and "Lead only." in text
    assert "private_source_marker" not in text
    assert all("window.stolen" not in script for script in page.scripts)
    assert len(page.ids) == len(set(page.ids))
    assert all(link[1:] in page.ids for link in page.links if link.startswith("#"))


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
