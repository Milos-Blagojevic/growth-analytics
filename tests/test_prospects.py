import json
import shutil
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from exa_mock import AGENT_URL, agent_run, error, structured_response
from pipeline import scoring
from prospect_data import CONFIG_TOML, SITE, assessment, exa_replies, figure, freeze_today, money_reply, research
from prospects import __main__ as cli
from prospects import discover, warehouse


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Runs the CLI in an empty directory with prospects.toml and prospects.csv; its files are created there."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prospects.toml").write_text(CONFIG_TOML)
    (tmp_path / "prospects.csv").write_text("# name,domain\nAcme,https://www.acme.com/\nBeta,beta.io,from the webinar\n")
    freeze_today(monkeypatch)
    return tmp_path


def run_cli(monkeypatch, *argv):
    """`python -m prospects <argv>`: its exit code, or its exit message."""
    monkeypatch.setattr("sys.argv", ["prospects", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def acme_answers_beta_fails(exa):
    """Acme: both searches answer. Beta: Exa rejects the site search."""
    answer = exa_replies()
    exa.on_search(lambda body: answer(body) if "Acme" in body["query"] else error(400))


def with_claude(monkeypatch, fake):
    monkeypatch.setattr(scoring, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    return fake


def saved_run(workdir):
    """{file name: parsed JSON} of the only saved run, the manifest included."""
    (run_dir,) = (workdir / "prospect_runs").iterdir()
    return {path.name: json.loads(path.read_text()) for path in run_dir.iterdir()}


def test_run_assesses_rates_and_saves_and_a_failing_prospect_does_not_stop_it(exa, workdir, monkeypatch, fake_claude, capsys):
    acme_answers_beta_fails(exa)
    fake = with_claude(monkeypatch, fake_claude(assessment()))

    assert run_cli(monkeypatch, "run") == 1

    saved = saved_run(workdir)
    assert saved["acme.com.json"] == {
        "name": "Acme",
        "domain": "acme.com",
        "research": research(),
        "assessment": assessment(),
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    }
    assert "Request failed with status code 400" in saved["beta.io.json"]["error"]
    manifest = saved["_run.json"]
    assert (manifest["mode"], manifest["companies"], manifest["errors"]) == ("assessed", 2, 1)
    assert manifest["exa"]["requests"] == 2  # Acme's two searches; failed requests are not tallied
    assert manifest["claude"] == {"model": scoring.DEFAULT_MODEL, "input_tokens": 1200, "output_tokens": 300}
    (run_dir,) = (workdir / "prospect_runs").iterdir()  # named after the start of the run
    assert datetime.fromisoformat(manifest["started_at"]).strftime("%Y%m%d-%H%M%S") == run_dir.name
    assert manifest["started_at"] <= manifest["saved_at"]
    assert len(fake.calls) == 1  # Beta never reached Claude
    out = capsys.readouterr().out
    assert "[82] A -> founder outreach | Acme (acme.com) | $10,000-$40,000 a year (headcount)" in out
    assert "Acme sells a meditation app on subscription.\nroute: about $20,000 a year\n+ Sells in-app subscriptions." in out
    assert "Beta: ERROR ValueError: Request failed with status code 400" in out
    assert "Exa: 2 requests, $0.0240 | Claude: 1200 input / 300 output tokens" in out
    assert "prospects.duckdb: 2 prospect result(s) from 1 run(s)" in out
    assert [r["name"] for r in warehouse.ranking("prospects.duckdb")] == ["Acme"]


def test_run_prints_what_backs_a_rating_and_keeps_research_warnings(exa, workdir, monkeypatch, fake_claude, capsys):
    exa.on_search(exa_replies(money=money_reply(identity_verified=False)))
    fake = with_claude(monkeypatch, fake_claude(assessment(
        revenue_figures=[
            figure(9e6, "2025-09", "company", "$9M ARR in September 2025 (company blog)"),
            figure(12e6, "2026-07", "third party", "$12M ARR as of July 2026 (Sacra estimate)"),
            figure(10e6, "", "company", "about $10M\n a year"),
            figure(40e6, "2028", "company", "targets $40M ARR by 2028"),  # not revenue it has: never valued on it
        ],
        fit_concerns=["Pricing is not public."],
    )))  # fmt: skip
    monkeypatch.setenv("SCORING_MODEL", "claude-test")

    assert run_cli(monkeypatch, "run", "--limit", "1") == 0

    out = capsys.readouterr().out
    assert "[82] A -> founder outreach | Acme (acme.com) | $60,000 a year (revenue)" in out
    assert (
        "revenue: $12M ARR as of July 2026 (Sacra estimate) (third party, 2026-07)\n"
        "  not used: $9M ARR in September 2025 (company blog) (company, 2025-09)\n"
        "  not used: about $10M a year (company, no date)\n"
        "+ Sells in-app subscriptions.\n- Pricing is not public.\n* Raised"
    ) in out
    warning = "Funding and news search described a different company; its findings were dropped"
    assert f"warnings: {warning}" in out
    with duckdb.connect("prospects.duckdb", read_only=True) as db:
        assert db.execute("SELECT warnings FROM prospects").fetchall() == [([warning],)]
    assert fake.calls[0]["model"] == saved_run(workdir)["_run.json"]["claude"]["model"] == "claude-test"


def test_an_interrupted_run_keeps_the_prospects_that_finished(exa, workdir, monkeypatch, fake_claude, capsys):
    answer = exa_replies()

    def interrupted_at_beta(body):
        if "Acme" in body["query"]:
            return answer(body)
        raise KeyboardInterrupt

    exa.on_search(interrupted_at_beta)
    with_claude(monkeypatch, fake_claude(assessment()))
    monkeypatch.setattr("sys.argv", ["prospects", "run"])

    with pytest.raises(KeyboardInterrupt):
        cli.main()

    assert set(saved_run(workdir)) == {"acme.com.json"}  # saved when it finished; the manifest was never written
    assert run_cli(monkeypatch, "load") == 0
    assert "[82] A -> founder outreach | Acme (acme.com)" in capsys.readouterr().out


def test_a_claude_failure_fails_that_prospect_and_keeps_its_research(exa, workdir, monkeypatch, fake_claude, capsys):
    exa.on_search(exa_replies())
    with_claude(monkeypatch, fake_claude(None))  # nothing parsable comes back

    assert run_cli(monkeypatch, "run", "--limit", "1") == 1

    saved = saved_run(workdir)["acme.com.json"]
    assert saved["research"] == research() and "assessment" not in saved
    assert "Acme: ERROR RuntimeError: Claude returned no parsable output for Acme" in capsys.readouterr().out
    assert warehouse.ranking("prospects.duckdb") == []  # a failed result is never a prospect's latest


def test_research_only_prints_odd_research_and_warnings_without_stopping(exa, workdir, monkeypatch, capsys):
    acme = exa_replies(
        site=structured_response({**SITE, "pricing": "Free \ud83d", "customers": None}),  # cut inside an emoji; a null field
        money=money_reply(news=[{"title": "Acme expands", "url": None, "date": None}]),
    )
    beta = exa_replies(money=error(400))
    exa.on_search(lambda body: (acme if "Acme" in body["query"] else beta)(body))

    assert run_cli(monkeypatch, "run", "--research-only") == 0

    out = capsys.readouterr().out
    assert "  pricing: Free ?\n" in out and "  customers: unknown\n" in out
    assert "  news : Acme expands \n" in out
    assert "Beta (beta.io)\n" in out
    assert "  warnings: Funding and news search failed: ValueError: Request failed with status code 400" in out
    assert "prospects.duckdb: 2 prospect result(s) from 1 run(s)" in out


def test_a_result_that_cannot_be_saved_fails_that_prospect_only(exa, workdir, monkeypatch, capsys):
    long_domain = "a" * 260 + ".com"  # passes as a domain; too long for a file name
    (workdir / "prospects.csv").write_text(f"Long,{long_domain}\nNul,a\x00b.com\nAcme,acme.com\n")
    exa.on_search(exa_replies())

    assert run_cli(monkeypatch, "run", "--research-only") == 1

    saved = saved_run(workdir)
    assert set(saved) == {"acme.com.json", "_run.json"}
    assert (saved["_run.json"]["companies"], saved["_run.json"]["errors"]) == (3, 2)
    out = capsys.readouterr().out
    assert "Long: ERROR not saved: OSError" in out and "Nul: ERROR not saved: ValueError" in out
    assert "Acme (acme.com)\n" in out


def test_run_keeps_its_results_and_says_so_when_the_warehouse_cannot_be_rebuilt(exa, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "leads?"  # DuckDB cannot open a database under a path with "?" in it
    folder.mkdir()
    monkeypatch.chdir(folder)
    (folder / "prospects.toml").write_text(CONFIG_TOML)
    (folder / "prospects.csv").write_text("Acme,acme.com\n")
    exa.on_search(exa_replies())

    assert run_cli(monkeypatch, "run", "--research-only").startswith("prospects.duckdb was not rebuilt: ")

    assert set(saved_run(folder)) == {"acme.com.json", "_run.json"}
    assert "Acme (acme.com)\n  business_summary: Acme sells a meditation app to consumers." in capsys.readouterr().out


def test_a_prospects_file_that_is_not_utf8_stops_the_run_with_a_message(http, workdir, monkeypatch):
    (workdir / "prospects.csv").write_bytes("Čačak Apps,cacak.rs\n".encode("cp1250"))  # Excel's plain "CSV"

    assert "prospects.csv: 'utf-8' codec can't decode" in run_cli(monkeypatch, "run", "--research-only")
    assert len(http.calls) == 0


def test_run_stops_before_spending_when_its_folder_cannot_be_created(http, workdir, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 11, 1, 9, 0, 0)

    monkeypatch.setattr(cli, "datetime", Frozen)
    (workdir / "prospect_runs" / "20261101-090000").mkdir(parents=True)

    assert "prospect_runs/20261101-090000/ already exists" in run_cli(monkeypatch, "run", "--research-only")

    shutil.rmtree(workdir / "prospect_runs")
    (workdir / "prospect_runs").write_text("not a folder")

    assert "prospect_runs/20261101-090000/ cannot be created" in run_cli(monkeypatch, "run", "--research-only")
    assert len(http.calls) == 0


def test_research_only_needs_no_anthropic_key_and_prints_what_exa_found(exa, workdir, monkeypatch, capsys):
    exa.on_search(exa_replies())

    assert run_cli(monkeypatch, "run", "--research-only", "--limit", "1") == 0

    saved = saved_run(workdir)
    assert set(saved) == {"acme.com.json", "_run.json"}  # --limit 1: Beta was not researched
    assert "assessment" not in saved["acme.com.json"]
    assert (saved["_run.json"]["mode"], saved["_run.json"]["claude"]) == ("research_only", None)
    out = capsys.readouterr().out
    assert "Acme (acme.com)\n  business_summary: Acme sells a meditation app to consumers." in out
    assert "  funding: Series A, $12M in March 2026 led by Index; $15M raised in total." in out
    assert "  news 2026-03-04: Acme raises $12M https://news.example/acme-series-a" in out

    assert run_cli(monkeypatch, "load") == 0
    assert "[--] not assessed | Acme (acme.com)" in capsys.readouterr().out


def test_load_rates_saved_prospects_again_after_a_config_change_without_any_request(exa, http, workdir, monkeypatch,
                                                                                    fake_claude, capsys):
    exa.on_search(exa_replies())
    with_claude(monkeypatch, fake_claude(assessment()))
    assert run_cli(monkeypatch, "run", "--limit", "1") == 0
    requests_made = len(http.calls)
    (workdir / "prospects.toml").write_text(CONFIG_TOML.replace("take_rate = 0.01", "take_rate = 0.005"))
    capsys.readouterr()

    assert run_cli(monkeypatch, "load") == 0

    assert "[82] B -> sales sequence | Acme (acme.com) | $5,000-$20,000 a year (headcount)" in capsys.readouterr().out
    assert len(http.calls) == requests_made


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        (["run"], {}, "ANTHROPIC_API_KEY is not set"),
        (["run", "--research-only"], {"EXA_API_KEY": None}, "EXA_API_KEY is not set"),
        (["run", "--limit", "0"], {"ANTHROPIC_API_KEY": "k"}, "--limit must be 1 or more"),
        (["--config", "missing.toml", "run"], {"ANTHROPIC_API_KEY": "k"}, "missing.toml not found: cp prospects.example.toml"),
        (["run", "--prospects", "missing.csv"], {"ANTHROPIC_API_KEY": "k"}, "missing.csv not found: cp prospects.example.csv"),
        (["--config", ".", "run"], {"ANTHROPIC_API_KEY": "k"}, ".: [Errno 21] Is a directory"),
        (["run", "--prospects", "."], {"ANTHROPIC_API_KEY": "k"}, ".: [Errno 21] Is a directory"),
    ],
    ids=["no-anthropic-key", "no-exa-key", "limit-zero", "no-config", "no-prospects-file", "config-is-a-folder",
         "prospects-is-a-folder"],
)
def test_run_stops_before_any_paid_call(http, workdir, monkeypatch, argv, env, message):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key)
        else:
            monkeypatch.setenv(key, value)

    assert message in run_cli(monkeypatch, *argv)
    assert len(http.calls) == 0
    assert not Path("prospect_runs").exists()


def test_an_invalid_config_stops_run_and_load_with_the_key_named(http, workdir, monkeypatch):
    (workdir / "prospects.toml").write_text(CONFIG_TOML.replace("take_rate = 0.01", "take_rate = 5"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    for command in ("run", "load"):
        assert "prospects.toml: [pricing].take_rate must be a number above 0" in run_cli(monkeypatch, command)
    assert len(http.calls) == 0
    assert not Path("prospects.duckdb").exists()


# discover ---------------------------------------------------------------------------------------


def found(name, domain):
    return {"name": name, "domain": domain, "why": f"{name} sells subscriptions.", "employees": 40, "funding_stage": "seed"}


def test_discover_prints_and_saves_new_companies_and_leaves_them_out_next_time(exa, workdir, monkeypatch, capsys, caplog):
    caplog.set_level("INFO")
    exa.on_agent(agent_run({"companies": [found("Acme", "acme.com"), found("Gamma", "gamma.dev")]}, cost=0.745))

    assert run_cli(monkeypatch, "discover", "10 to 100 employees", "--limit", "5") == 0

    # Acme is in prospects.csv already; the printed row pastes into prospects.csv
    assert capsys.readouterr().out == "Gamma,gamma.dev,Gamma sells subscriptions.,40,seed\n"
    assert "1 new company saved to prospects_discovered.csv | 2 known domains not listed again | Exa: $0.7450" in caplog.text
    saved = (workdir / "prospects_discovered.csv").read_text().splitlines()
    assert saved[1].startswith("Gamma,gamma.dev,Gamma sells subscriptions.,40,seed,10 to 100 employees,20") and len(saved) == 2

    assert run_cli(monkeypatch, "discover") == 0  # the database returns the same two again

    first, second = exa.requests(AGENT_URL)
    assert "Narrow it to: 10 to 100 employees." in first["query"] and "Narrow it to" not in second["query"]
    assert first["input"] == {"exclusion": [{"domain": "acme.com"}, {"domain": "beta.io"}]}
    assert {"domain": "gamma.dev"} in second["input"]["exclusion"]
    assert capsys.readouterr().out == ""
    assert (workdir / "prospects_discovered.csv").read_text().splitlines() == saved
    assert "0 new companies | 3 known domains not listed again" in caplog.text


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        (["discover"], {"EXA_API_KEY": None}, "EXA_API_KEY is not set"),
        (["discover", "--limit", "0"], {}, "--limit must be from 1 to 50"),
        (["discover", "--limit", "51"], {}, "--limit must be from 1 to 50"),
        (["--config", "missing.toml", "discover"], {}, "missing.toml not found"),
        (["discover", "--history", "no_such_folder/found.csv"], {}, "no_such_folder/found.csv cannot be written"),
    ],
    ids=["no-exa-key", "limit-zero", "limit-too-high", "no-config", "history-not-writable"],
)
def test_discover_stops_before_any_paid_call(http, workdir, monkeypatch, argv, env, message):
    for key in env:
        monkeypatch.delenv(key)

    assert message in run_cli(monkeypatch, *argv)
    assert len(http.calls) == 0
    assert not Path("prospects_discovered.csv").exists()


def test_discover_says_what_a_failed_run_cost(exa, workdir, monkeypatch):
    exa.on_agent(agent_run(status="failed", error="Run timed out", cost=0.31))

    message = run_cli(monkeypatch, "discover")

    assert message.startswith("Discovery failed: RuntimeError: Exa agent run agent_run_1 failed: Run timed out")
    assert message.endswith("| Exa: $0.3100")
    assert not Path("prospects_discovered.csv").exists()


def test_discover_prints_what_it_paid_for_even_when_the_history_cannot_be_written(exa, workdir, monkeypatch, capsys):
    exa.on_agent(agent_run({"companies": [found("Gamma", "gamma.dev")]}, cost=0.745))

    def full_disk(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(discover, "save", full_disk)

    message = run_cli(monkeypatch, "discover")

    assert "No space left on device): the rows printed above were not saved | Exa: $0.7450" in message
    assert capsys.readouterr().out == "Gamma,gamma.dev,Gamma sells subscriptions.,40,seed\n"


def test_discover_saves_what_it_paid_for_even_when_printing_fails(exa, workdir, monkeypatch):
    exa.on_agent(agent_run({"companies": [found("Gamma", "gamma.dev")]}))

    class ClosedPipe:
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

    monkeypatch.setattr("sys.argv", ["prospects", "discover"])
    monkeypatch.setattr("sys.stdout", ClosedPipe())

    with pytest.raises(BrokenPipeError):
        cli.main()
    assert "gamma.dev" in (workdir / "prospects_discovered.csv").read_text()
