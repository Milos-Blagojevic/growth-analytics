import json
import sys

import pytest

from dashboard import db
from dashboard.app import create_app
from dashboard_data import INTERVIEW_TURNS, PREP, client, fit, result, review_fields, wispr, write_interview, write_run
from test_dashboard_jobs import STUB, finished

SER = "20261020-se-ranking-senior-data-analyst"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("STUB_SLEEP", raising=False)
    monkeypatch.delenv("STUB_EXIT", raising=False)
    (tmp_path / "stub_hunt.py").write_text(STUB)
    return tmp_path


@pytest.fixture
def c(root):
    return client(create_app(root, hunt_command=[sys.executable, str(root / "stub_hunt.py")]))


def redirected_to(response):
    assert response.status_code == 204, response.text
    return response.headers["HX-Redirect"]


def reloads(response):
    assert response.status_code == 204, response.text
    return response.headers.get("HX-Refresh") == "true"


def test_an_interview_goes_from_form_to_transcript_with_measured_answers(root, c):
    url = redirected_to(c.post("/interviews", data={
        "company": "SE Ranking", "role": "Senior Data Analyst", "stage": "hr", "scheduled_at": "2099-10-20T14:00",
        "language": "Serbian", "posting_url": "https://seranking.com/vacancies/senior-data-analyst.html",
    }))
    assert url == "/interviews/20991020-se-ranking-senior-data-analyst"
    assert "SE Ranking" in c.get("/interviews").text.split("Past")[0]  # dated in the future: upcoming
    page = c.get(url).text
    assert "No prep yet" in page and "Write prep…" in page

    upload = c.post(url + "/transcript", files={"file": ("refined.ndjson", wispr(*INTERVIEW_TURNS).encode())})
    assert reloads(upload)  # the page must reload; a redirect to its own URL would not
    page = c.get(url).text
    assert "Which speaker is you?" in page and "35 words" in page and "Filler words" not in page

    assert c.post(url + "/me", data={"me": "9"}).status_code == 400
    assert reloads(c.post(url + "/me", data={"me": "2"}))
    page = c.get(url).text
    assert "Longest answer" in page and "2:50" in page and "mislim 1" in page  # 170 s from 0:10
    assert "Review the interview…" in page

    bad = c.post(url + "/transcript", files={"file": ("notes.txt", b"just some notes without speakers")})
    assert bad.status_code == 400 and "two speakers" in bad.text


def test_prep_and_review_start_hunt_with_the_interview_folder(root, c):
    write_interview(root, SER, posting_text="", posting_url="https://seranking.com/jobs/1",
                    transcript=wispr(*INTERVIEW_TURNS))
    preview = c.post("/runs/preview", data={"kind": "interview_prep", "interview": SER}).text
    assert f"python hunt.py interview-prep interviews/{SER}" in preview
    assert "fetch the posting text" in preview and "findings of your past interview reviews" in preview

    blocked = c.post("/runs/preview", data={"kind": "interview_review", "interview": SER}).text
    assert "choose which speaker is you first" in blocked
    assert "No such interview" in c.post("/runs/preview", data={"kind": "interview_prep", "interview": "../out"}).text

    write_interview(root, SER, transcript=wispr(*INTERVIEW_TURNS), me="2")
    job_id = int(redirected_to(c.post("/runs", data={"kind": "interview_review", "interview": SER})).rsplit("/", 1)[1])
    job = finished(root, job_id)
    assert (job["args"], job["status"]) == (["interview-review", f"interviews/{SER}"], "succeeded")
    assert f'href="/interviews/{SER}"' in c.get(f"/runs/jobs/{job_id}").text


def test_prep_and_review_pages_and_the_watch_outs_on_the_interviews_page(root, c):
    rho = "20261013-rho-senior-data-engineer"
    write_interview(root, rho, company="Rho", scheduled_at="2026-10-13T10:00",
                    transcript=wispr(*INTERVIEW_TURNS), me="2", review=review_fields(score=2))
    write_interview(root, SER, scheduled_at="2099-10-20T14:00", prep=PREP)

    page = c.get(f"/interviews/{SER}").text
    for text in (PREP["positioning"], PREP["intro"], "AI analyst in Slack", "Why are you leaving?", "Looker"):
        assert text in page
    assert PREP["watch_outs"][0] not in page  # watch-outs live on the Interviews page
    review_page = c.get(f"/interviews/{rho}").text
    assert "Let me walk you through it." in review_page and "2/5" in review_page
    assert "The transcript changed" not in review_page

    listing = c.get("/interviews").text
    assert "Watch out · for" in listing and PREP["watch_outs"][0] in listing  # from the next interview's prep
    ready, reviewed = listing.split("Ready for the call")[1], listing.split("Reviewed")[1]
    assert ready.index("SE Ranking") < ready.index("To review") and "Rho" in reviewed
    progress = listing.split("Progress")[1]
    assert 'title="Rho: 2/5"' in progress and "5:59" in progress

    (root / "interviews" / SER / "prep.json").unlink()  # no upcoming prep: the latest review's actions instead
    listing = c.get("/interviews").text
    assert "from your review of" in listing and "Keep the intro to 90 seconds." in listing

    transcript = json.loads((root / "interviews" / rho / "transcript.json").read_text())
    transcript["uploaded_at"] = "2026-10-21T09:00:00"  # uploaded again after the review
    (root / "interviews" / rho / "transcript.json").write_text(json.dumps(transcript))
    assert "The transcript changed after this review" in c.get(f"/interviews/{rho}").text
    assert "review outdated" in c.get("/interviews").text


def test_a_new_interview_can_start_from_a_researched_company(root, c):
    write_run(root, "20261001-090000", [
        result("SE Ranking", "seranking.com", fit=fit(81, "apply", "Senior Data Analyst", "https://seranking.com/jobs/1")),
    ])
    form = c.get("/interviews/new", params={"domain": "seranking.com"}).text
    assert 'value="SE Ranking"' in form and 'value="Senior Data Analyst"' in form
    assert 'value="https://seranking.com/jobs/1"' in form
    assert "Prepare an interview…" in c.get("/c/seranking.com").text


def test_unreadable_interview_files_are_reported_not_fatal(root, c):
    path = write_interview(root, SER)
    (path / "review.json").write_text("{not json")
    page = c.get("/interviews").text
    assert f"interviews/{SER}" in page and "could not be read" in page
    conn = db.connect(root)
    assert conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 0
    conn.close()
