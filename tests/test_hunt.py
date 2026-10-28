import argparse
import csv
import json
from pathlib import Path

import pytest

import hunt
import jev
import runs
import scoring
from dashboard_data import INTERVIEW_TURNS, PREP, review_fields, wispr, write_interview
from exa_mock import contents_response, error, result, structured_response

PROFILE_TOML = """
[search]
roles = ["analytics engineer", "growth engineer"]
seniority = ["senior", "lead"]
work_mode = "Prefer fully remote roles."
exclude_domains = ["https://www.mine.com/"]

[candidate]
location = "Belgrade, Serbia"
experience = "8 years of analytics engineering (dbt, Snowflake)."
"""


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Runs the CLI in an empty directory with profile.toml and companies.csv; out/ is created there."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.toml").write_text(PROFILE_TOML)
    (tmp_path / "companies.csv").write_text("# name,domain\nAcme,acme.com\n\nBeta,beta.io\n")
    return tmp_path


def run_args(**overrides):
    return argparse.Namespace(
        **{"profile": "profile.toml", "companies": "companies.csv", "dry_run": False, "research_only": False, **overrides}
    )


def saved_results(workdir):
    """{file name: parsed JSON} of the only saved run, the manifest included."""
    (run_dir,) = (workdir / "out").iterdir()
    return {path.name: json.loads(path.read_text()) for path in run_dir.iterdir()}


def company(name, domain, roles):
    return {
        "company": name,
        "domain": domain,
        "identity_verified": True,
        "business_summary": f"{name} sells software.",
        "signals": "unknown",
        "open_roles": roles,
    }


# discover ---------------------------------------------------------------------------------------


def discovered(*companies):
    return structured_response({"companies": [{"name": n, "domain": d, "why": f"{n} is hiring."} for n, d in companies]})


def discovery_exa(exa, *, deep):
    """Analytics query: Ramp, job board, anonymous employer, Notion (URL-form domain).
    Growth query: Ramp again (www.), Linear, the profile's excluded domain."""

    def handler(body):
        if body["type"] == "deep":
            return deep
        if "analytics engineer" in body["query"]:
            return discovered(
                ("Ramp", "ramp.com"),
                ("LinkedIn", "linkedin.com"),
                ("Confidential fintech", "stealth.io"),
                ("Notion", "https://www.Notion.so/careers"),
            )
        return discovered(("Ramp", "www.ramp.com"), ("Linear", "linear.app"), ("Mine", "mine.com"))

    exa.on_search(handler)


def discover(limit, capsys):
    args = argparse.Namespace(profile="profile.toml", focus="fintech", limit=limit, history="discovered.csv")
    assert hunt.cmd_discover(args) == 0
    return list(csv.reader(capsys.readouterr().out.splitlines()))


def test_discover_filters_merges_and_remembers_history(exa, workdir, capsys):
    discovery_exa(exa, deep=error(400))  # a failing query does not lose the others

    first = discover(2, capsys)
    second = discover(10, capsys)

    assert first == [["Ramp", "ramp.com", "Ramp is hiring."], ["Notion", "notion.so", "Notion is hiring."]]
    assert second == [["Linear", "linear.app", "Linear is hiring."]]
    with open(workdir / "discovered.csv", newline="") as f:
        history = list(csv.DictReader(f))
    assert [(r["domain"], r["focus"]) for r in history] == [
        ("ramp.com", "fintech"),
        ("notion.so", "fintech"),
        ("linear.app", "fintech"),
    ]
    sent = exa.requests()
    assert sorted(b["type"] for b in sent[:3]) == ["auto", "auto", "deep"]
    assert {b["numResults"] for b in sent} == {12, 20}  # min(100, limit + 10)


def test_discover_exits_when_every_query_fails(exa, workdir, capsys):
    exa.on_search(error(400))

    with pytest.raises(SystemExit, match="All discovery searches failed"):
        discover(5, capsys)
    assert not (workdir / "discovered.csv").exists()


# run --------------------------------------------------------------------------------------------


def research_exa(exa, http):
    """Acme: one live Ashby role. Beta: Exa rejects the request."""
    acme_role = {
        "title": "Senior Analytics Engineer",
        "url": "https://jobs.ashbyhq.com/acme/1111",
        "location": "Remote",
        "remote_or_relocation": "Remote (EU)",
    }
    exa.on_search(
        lambda body: structured_response(company("Acme", "acme.com", [acme_role])) if "Acme" in body["query"] else error(400)
    )
    exa.on_contents(contents_response([result(acme_role["url"], text="dbt, Snowflake, Looker.")]))
    http.get("https://api.ashbyhq.com/posting-api/job-board/acme", json={"jobs": [{"id": "1111", "title": "x"}]})
    return acme_role


def test_research_only_saves_results_and_reports_failures(exa, http, workdir, monkeypatch, capsys):
    research_exa(exa, http)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")  # set, but research-only never screens (no mock needed)

    assert hunt.cmd_run(run_args(research_only=True)) == 1

    results = saved_results(workdir)
    assert results["acme.com.json"]["research"]["content"]["open_roles"][0]["description"] == "dbt, Snowflake, Looker."
    assert "Request failed with status code 400" in results["beta.io.json"]["error"]
    manifest = results["_run.json"]
    assert (manifest["mode"], manifest["companies"], manifest["errors"]) == ("research_only", 2, 1)
    assert manifest["exa"]["requests"] == 2
    assert manifest["claude"] is None and manifest["jev"] is None
    assert "Exa: 2 requests" in capsys.readouterr().out  # Acme search + posting text; failures are not tallied


def test_run_scores_and_sends_digest_to_telegram(exa, http, workdir, monkeypatch, claude, capsys):
    acme_role = research_exa(exa, http)
    fake = claude(score=82, verdict="apply", best_role_title=acme_role["title"], best_role_url=acme_role["url"])
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    telegram_api = http.post("https://api.telegram.org/bottest-token/sendMessage", json={"ok": True})

    assert hunt.cmd_run(run_args()) == 1

    (sent,) = [json.loads(call.request.body) for call in telegram_api.calls]
    assert sent["chat_id"] == "42"
    assert "[82] APPLY Acme: Senior Analytics Engineer\nhttps://jobs.ashbyhq.com/acme/1111" in sent["text"]
    assert "Failed: Beta" in sent["text"]
    assert "dbt, Snowflake, Looker." in fake.calls[0]["messages"][0]["content"]  # posting text reaches the scorer
    results = saved_results(workdir)
    assert results["acme.com.json"]["usage"] == {"input_tokens": 1200, "output_tokens": 300}
    manifest = results["_run.json"]
    assert manifest["mode"] == "deliver"
    assert manifest["claude"] == {"model": scoring.DEFAULT_MODEL, "input_tokens": 1200, "output_tokens": 300}
    assert manifest["jev"] is None  # no Jev key: no screen
    out = capsys.readouterr().out
    assert "No Jev key (JEV_API_KEY or TYPESAFE_API_KEY) is set: Claude scores every open role" in out
    assert "Sent 1 Telegram message(s)" in out


def test_run_skips_claude_for_a_company_jev_screens_out_and_records_jev_cost(exa, http, workdir, monkeypatch, claude,
                                                                            jev_api, capsys):
    research_exa(exa, http)
    jev_api.on(lambda state, key, question: [0, 0.05, 0.9, 0.05] if key == "seniority"
               else 0.03 if key == "target_role" else 0.5)
    fake = claude()
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    hunt.cmd_run(run_args(dry_run=True))

    assert fake.calls == []
    acme = saved_results(workdir)["acme.com.json"]
    assert (acme["fit"]["score"], acme["fit"]["verdict"]) == (0, "skip")
    assert acme["screen"]["roles"][0]["failed"] == ["not one of your target roles"]
    manifest = saved_results(workdir)["_run.json"]
    assert manifest["jev"] == {"model": jev.DEFAULT_MODEL, "requests": 1, "input_tokens": 500, "dollars": 0.000021}
    assert manifest["claude"]["input_tokens"] == 0
    assert "Acme [0]: Senior Analytics Engineer: not one of your target roles" in capsys.readouterr().out


def test_run_scores_every_role_with_claude_when_jev_fails(exa, http, workdir, monkeypatch, claude):
    research_exa(exa, http)
    monkeypatch.setenv("TYPESAFE_API_KEY", "revoked")
    http.post(jev.API_URL, status=401, json={"detail": "invalid key"})
    fake = claude()
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    hunt.cmd_run(run_args(dry_run=True))

    acme = saved_results(workdir)["acme.com.json"]
    assert "HTTP 401" in acme["screen"]["error"] and acme["fit"]["score"] == 80
    assert "Senior Analytics Engineer" in fake.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        (run_args(dry_run=True), {}, "ANTHROPIC_API_KEY is not set"),
        (run_args(), {"ANTHROPIC_API_KEY": "k"}, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set"),
    ],
    ids=["no-anthropic-key", "no-telegram"],
)
def test_run_checks_keys_before_any_paid_call(http, workdir, monkeypatch, args, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit, match=message):
        hunt.cmd_run(args)
    assert len(http.calls) == 0
    assert not Path("out").exists()


# notification and send --------------------------------------------------------------------------


def scored(name, score, verdict, role="", url=""):
    fit = {
        "score": score,
        "verdict": verdict,
        "best_role_title": role,
        "best_role_url": url,
        "reasons": [],
        "concerns": ["Too junior."],
        "location_assessment": "Remote EU.",
        "next_step": "Apply.",
        "gap_note": "",
        "proof_ideas": [],
        "scope_note": "",
    }
    return {"name": name, "domain": f"{name.lower()}.com", "fit": fit}


def test_notification_lists_only_actionable_roles(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "http://127.0.0.1:9000")
    results = [
        scored("Low", 60, "consider", "Data Analyst", "https://low.com/jobs/1"),
        scored("Top", 90, "apply", "Lead AE", "https://top.com/jobs/1"),
        scored("Grow", 70, "stretch", "Growth Engineer"),
        scored("Careers", 55, "check_careers"),
        scored("Nope", 20, "skip"),
        {"name": "Broken", "domain": "broken.com", "error": "ValueError: boom"},
    ]

    blocks = hunt.build_notification(results)

    assert blocks[0].endswith("| 6 companies | 1 apply, 1 consider, 1 stretch, 1 check careers, 1 skip, 1 errors")
    assert blocks[1:] == [
        "[90] APPLY Top: Lead AE\nhttps://top.com/jobs/1",
        "[60] CONSIDER Low: Data Analyst\nhttps://low.com/jobs/1",
        "[70] STRETCH Grow: Growth Engineer",
        "Failed: Broken",
        "Dashboard: http://127.0.0.1:9000",
    ]


def test_notification_links_the_railway_dashboard_unless_dashboard_url_is_set(monkeypatch):
    results = [scored("Top", 90, "apply", "Lead AE", "https://top.com/jobs/1")]
    assert hunt.build_notification(results)[-1] == "Dashboard: http://127.0.0.1:8000"
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "hunt-production.up.railway.app")
    assert hunt.build_notification(results)[-1] == "Dashboard: https://hunt-production.up.railway.app"
    monkeypatch.setenv("DASHBOARD_URL", "https://jobs.example.com")
    assert hunt.build_notification(results)[-1] == "Dashboard: https://jobs.example.com"


def test_send_uses_latest_run_and_ignores_its_manifest(http, workdir, monkeypatch, capsys):
    old, new = workdir / "out" / "20260101-090000", workdir / "out" / "20260102-090000"
    for run_dir, result in ((old, scored("Old", 80, "apply", "Old role")), (new, scored("Top", 90, "apply", "Lead AE"))):
        run_dir.mkdir(parents=True)
        (run_dir / f"{result['domain']}.json").write_text(json.dumps(result))
        (run_dir / "_run.json").write_text(json.dumps({"mode": "dry_run", "companies": 1}))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    telegram_api = http.post("https://api.telegram.org/bottok/sendMessage", json={"ok": True})

    assert hunt.cmd_send(argparse.Namespace(run_dir=None)) == 0

    (sent,) = [json.loads(call.request.body)["text"] for call in telegram_api.calls]
    assert "| 1 companies | 1 apply" in sent
    assert "[90] APPLY Top: Lead AE" in sent
    assert "Old" not in sent


def test_a_second_command_exits_while_one_holds_the_lock(workdir, monkeypatch):
    monkeypatch.setattr("sys.argv", ["hunt.py", "send"])

    with runs.lock(workdir), pytest.raises(SystemExit, match="Another hunt.py command is running"):
        hunt.main()
    with pytest.raises(SystemExit, match="No saved runs"):  # lock released: the command itself runs
        hunt.main()


RHO = "20261013-rho-senior-data-engineer"
SER = "20261020-se-ranking-senior-data-analyst"


def interview_args(interview_id):
    return argparse.Namespace(profile="profile.toml", interview=f"interviews/{interview_id}")


def test_interview_prep_fetches_the_posting_and_learns_from_past_reviews(workdir, exa, fake_claude, monkeypatch):
    write_interview(workdir, RHO, company="Rho", transcript=wispr(*INTERVIEW_TURNS), me="2", review=review_fields())
    posting_url = "https://seranking.com/vacancies/senior-data-analyst.html"
    write_interview(workdir, SER, posting_text="", posting_url=posting_url, focus="the AI analyst at Tenderly")
    exa.on_contents(contents_response([result(posting_url, text="Senior Data Analyst: experiments and AI Analyst.")]))
    fake = fake_claude(PREP)
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    assert hunt.cmd_interview_prep(interview_args(SER)) == 0

    saved = json.loads((workdir / "interviews" / SER / "prep.json").read_text())
    assert (saved["posting_source"], saved["lessons_from"], saved["prep"]) == ("fetched", [RHO], PREP)
    prompt = fake.calls[0]["messages"][0]["content"]
    for expected in ("experiments and AI Analyst", "Keep the intro to 90 seconds.", "the AI analyst at Tenderly",
                     "8 years of analytics engineering"):
        assert expected in prompt


def test_interview_review_needs_your_speaker_and_saves_metrics_and_bounded_scores(workdir, fake_claude, monkeypatch):
    write_interview(workdir, RHO, company="Rho", transcript=wispr(*INTERVIEW_TURNS))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    with pytest.raises(SystemExit, match="which speaker is you"):
        hunt.cmd_interview_review(interview_args(RHO))

    write_interview(workdir, RHO, company="Rho", transcript=wispr(*INTERVIEW_TURNS), me="2")
    fake = fake_claude(review_fields(score=9))
    monkeypatch.setattr(scoring, "_client", lambda: fake)

    assert hunt.cmd_interview_review(interview_args(RHO)) == 0

    saved = json.loads((workdir / "interviews" / RHO / "review.json").read_text())
    assert set(saved["review"]["scores"].values()) == {5}  # Claude said 9; scores are 1-5
    assert (saved["metrics"]["answers"], saved["transcript_uploaded_at"]) == (2, "2026-10-20T15:00:00")
    assert "] YOU: Sure, mislim" in fake.calls[0]["messages"][0]["content"]
