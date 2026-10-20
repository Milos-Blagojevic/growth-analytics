"""Render dashboard pages of one version of the code to standalone HTML files, for a change report's before/after view.

    .venv/bin/python .claude/skills/visualize-changes/scripts/snapshot.py [--ref REF] --out DIR PATH [PATH ...]

Without --ref it renders the working tree; with --ref it renders that commit (extracted with `git archive` into a
temporary folder). Every version gets the same sample data folder, built in a fresh temporary folder by the verify
skill's sandbox.py from this checkout's tests/dashboard_data.py, so differences come from the code alone. Pages are
requested in-process through FastAPI's TestClient: no server, no port, no job is ever started, and job buttons would
only reach a fake hunt.py.

Each PATH (for example / or /runs) is saved as DIR/<n>.html with its stylesheets inlined and its scripts removed, so it
renders from a file. DIR/pages.json maps each path to its file and HTTP status. Temporary folders are deleted.
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / ".claude" / "skills" / "verify" / "scripts"))
import sandbox  # noqa: E402  (the verify skill's sandbox: folder checks, sample data, fake hunt.py)

STYLESHEET = re.compile(r'<link\b[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"[^>]*>')
SCRIPT = re.compile(r"<script\b.*?</script>", re.S)


def extract(ref, into):
    result = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", ref], capture_output=True)
    if result.returncode:
        sys.exit(f"git archive {ref} failed: {result.stderr.decode(errors='replace').strip()}")
    archive = result.stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        # Extraction filters arrived in Python 3.11.4; the archive comes from this repository either way.
        tar.extractall(into, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def forget(tree):
    """Drops `tree` and `tree/tests` from sys.path and the modules imported from them (not from a .venv inside
    `tree`), so the code tree served next loads its own modules."""
    own = {str(tree), str(tree / "tests")}
    sys.path[:] = [p for p in sys.path if p not in own]
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file and Path(file).resolve().is_relative_to(tree) and "site-packages" not in Path(file).parts:
            del sys.modules[name]


def standalone(client, html, token):
    """Inlines the page's stylesheets, removes its scripts and replaces the per-process CSRF token, so a page that
    the change does not affect comes out identical in both versions."""
    def inline(match):
        css = client.get(match.group(1))
        return f"<style>{css.text}</style>" if css.status_code == 200 else ""

    return SCRIPT.sub("", STYLESHEET.sub(inline, html)).replace(token, "csrf")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", help="commit to render (default: the working tree)")
    parser.add_argument("--out", type=Path, required=True, help="folder for the HTML files and pages.json")
    parser.add_argument("paths", nargs="+", help="dashboard paths, for example / /runs")
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="change-report-"))
    try:
        root = scratch / "data"
        sandbox.claim(root)
        sys.path.insert(0, str(REPO))
        sandbox.seed(root, REPO)
        forget(REPO)
        code = REPO
        if args.ref:
            code = scratch / "code"
            extract(args.ref, code)
        sys.path.insert(0, str(code))
        from fastapi.testclient import TestClient

        from dashboard.app import create_app

        fake = root / "fake_hunt.py"
        fake.write_text(sandbox.FAKE_HUNT)
        app = create_app(root, hunt_command=[sys.executable, str(fake)])
        client = TestClient(app, base_url="http://127.0.0.1:8000")

        args.out.mkdir(parents=True, exist_ok=True)
        pages = {}
        for n, path in enumerate(args.paths):
            response = client.get(path, follow_redirects=False)
            name = f"{n}.html"
            html = response.text if "html" in response.headers.get("content-type", "") else ""
            body = standalone(client, html, app.state.csrf_token) if html else ""
            (args.out / name).write_text(body)
            pages[path] = {"file": name, "status": response.status_code,
                           "location": response.headers.get("location")}
            print(f"{response.status_code} {path}")
        (args.out / "pages.json").write_text(json.dumps(pages, indent=2))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
