"""Builders for pipeline files (out/<run>/, watch_state.json, discovered.csv, interviews/) and a dashboard test client."""

import json

from fastapi.testclient import TestClient

import interviews

BASE_URL = "http://127.0.0.1:8000"  # the dashboard only accepts loopback Host headers


def client(app):
    """TestClient that sends the app's CSRF token on every request, as the dashboard's pages do."""
    test_client = TestClient(app, base_url=BASE_URL)
    test_client.headers["X-CSRF-Token"] = app.state.csrf_token
    return test_client


def fit(score, verdict, role="", url="", **extra):
    return {
        "score": score,
        "verdict": verdict,
        "best_role_title": role,
        "best_role_url": url,
        "reasons": ["Strong dbt match."],
        "concerns": ["Series A risk."],
        "location_assessment": "Remote within the EU.",
        "next_step": "Apply via Ashby.",
        "gap_note": "",
        "proof_ideas": [],
        "scope_note": "",
        **extra,
    }


def result(name, domain, *, fit=None, roles=(), error=None):
    r = {"name": name, "domain": domain}
    if error:
        r["error"] = error
        return r
    r["research"] = {
        "name": name,
        "domain": domain,
        "content": {
            "company": name,
            "domain": domain,
            "identity_verified": True,
            "business_summary": f"{name} sells software.",
            "signals": "Series B.",
            "open_roles": [
                {"title": t, "url": u, "location": "Remote", "remote_or_relocation": "Remote (EU)", "link_status": s}
                for t, u, s in roles
            ],
        },
        "grounding": {"open_roles": {"confidence": "high", "urls": [u for _, u, _ in roles]}},
        "role_source": "main" if roles else "none",
        "dead_roles": [],
    }
    if fit:
        r["fit"] = fit
    return r


def write_run(root, run_id, results, manifest=None):
    run_dir = root / "out" / run_id
    run_dir.mkdir(parents=True)
    for r in results:
        (run_dir / f"{r['domain']}.json").write_text(json.dumps(r))
    if manifest is not None:
        (run_dir / "_run.json").write_text(json.dumps(manifest))
    return run_dir


PREP = {
    "positioning": "An analyst who builds the data layer others rely on.",
    "intro": "I started as a data analyst at PwC.",
    "role_match": [{"requirement": "Expert SQL and dbt", "evidence": "dbt platform at Tenderly"}],
    "stories": [{"title": "AI analyst in Slack", "use_when": "AI questions", "points": ["About 30 users"]}],
    "questions": [{"question": "Why are you leaving?", "points": ["Pace as the only data person"]}],
    "gaps": [{"gap": "Looker", "handle": "Hex is a close equivalent"}],
    "ask_them": ["How big is the data team?"],
    "watch_outs": ["Keep the intro under two minutes (Rho, HR screen)"],
    "compensation": "Give a range for this role only.",
}


def review_fields(score=3, **overrides):
    return {
        "summary": "Warm and honest, but the intro ran long.",
        "scores": {key: score for key in interviews.SCORES},
        "strengths": [{"at": "1:24", "quote": "E, ćao", "note": "Warm start.", "better": ""}],
        "improvements": [{"at": "6:02", "quote": "nisam uopšte dobro isplanirao", "note": "Undermines you.",
                          "better": "Let me walk you through it."}],
        "next_time": ["Keep the intro to 90 seconds."],
        **overrides,
    }


def wispr(*turns):
    """A Wispr Flow refined.ndjson body from (mm:ss, speaker id, text) tuples."""
    return "\n".join(
        json.dumps({"id": n, "speaker": {"id": s, "name": None, "source": "refined"}, "text": text, "timestamp": t},
                   ensure_ascii=False)
        for n, (t, s, text) in enumerate(turns)
    )


INTERVIEW_TURNS = (
    ("00:05", 1, "Tell me about yourself and your career so far, please."),
    ("00:10", 2, "Sure, mislim, I started at PwC as an analyst, ovaj, and then moved to engineering."),
    ("01:10", 1, "Okay."),
    ("01:12", 2, "Then I built a platform at project44, kako da kažem, from scratch with ten people."),
    ("03:00", 1, "Great, and why are you looking now, what changed recently for you?"),
    ("03:20", 2, "The pace was not sustainable."),
)


def write_interview(root, interview_id="20261020-se-ranking-senior-data-analyst", *, transcript=None, me=None,
                    prep=None, review=None, **fields):
    """interviews/<id>/ with interview.json and, optionally, transcript, prep and review files."""
    path = root / interviews.DIR / interview_id
    interview = {"id": interview_id, "company": "SE Ranking", "domain": "seranking.com", "role": "Senior Data Analyst",
                 "posting_url": "", "posting_text": "Senior Data Analyst: experiments, dbt, Looker.", "stage": "hr",
                 "scheduled_at": "2026-10-20T14:00", "language": "Serbian", "interviewer": "", "focus": "",
                 "created_at": "2026-10-16T10:00:00", **fields}
    interviews.save_json(path, interviews.INTERVIEW, interview)
    if transcript is not None:
        data = interviews.parse_transcript(transcript, "refined.ndjson")
        interviews.save_json(path, interviews.TRANSCRIPT, data | {"me": me, "uploaded_at": "2026-10-20T15:00:00"})
    if prep is not None:
        interviews.save_json(path, interviews.PREP, {"created_at": "2026-10-19T09:00:00", "model": "m",
                                                     "usage": {"input_tokens": 3000, "output_tokens": 900},
                                                     "posting_source": "pasted", "posting_text": "", "lessons_from": [],
                                                     "prep": prep})
    if review is not None:
        interviews.save_json(path, interviews.REVIEW, {"created_at": "2026-10-20T16:00:00", "model": "m",
                                                       "usage": {"input_tokens": 9000, "output_tokens": 1500},
                                                       "transcript_uploaded_at": "2026-10-20T15:00:00",
                                                       "metrics": {"word_share": 0.6, "time_share": 0.62,
                                                                   "longest_answer_s": 359, "fillers_per_100_words": 4.3},
                                                       "review": review})
    return path
