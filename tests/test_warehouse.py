import json
import shutil
import subprocess
import sys

import duckdb

from prospect_data import NEWS, SETTINGS, SITE, assessment, figure, research, write_run
from prospects import warehouse

# A process that writes to the warehouse with another tool and dies before DuckDB folds its write-ahead log in.
CRASHED_WRITER = """
import os, sys, duckdb
db = duckdb.connect(sys.argv[1])
db.execute("PRAGMA disable_checkpoint_on_shutdown")
db.execute("CREATE TABLE scratchpad AS SELECT 1 AS x")
os._exit(0)
"""

MANIFEST = {
    "mode": "assessed",
    "started_at": "2026-11-02T09:00:00",
    "saved_at": "2026-11-02T09:01:30",
    "companies": 2,
    "errors": 1,
    "exa": {"requests": 3, "dollars": 0.036, "by_label": {}},
    "claude": {"model": "claude-sonnet-5-5", "input_tokens": 1200, "output_tokens": 300},
}
RATING = "SELECT value_low, value_high, value_method, tier, route, route_reason FROM prospects"
CHEAPER = {**SETTINGS, "pricing": {**SETTINGS["pricing"], "take_rate": 0.005}}


def assessed(name, domain, **overrides):
    return {"name": name, "domain": domain, "research": research(name, domain), "assessment": assessment(**overrides)}


def researched(name, domain):
    return {"name": name, "domain": domain, "research": research(name, domain)}


def build(root, settings=SETTINGS):
    counts = warehouse.build(root / "prospect_runs", root / "prospects.duckdb", settings)
    return counts, duckdb.connect(str(root / "prospects.duckdb"), read_only=True)


def test_build_keeps_every_run_and_latest_shows_each_prospects_newest_assessment(tmp_path):
    write_run(tmp_path, "20261101-090000", [assessed("Acme", "acme.com", fit_score=60), assessed("Beta", "beta.io"),
                                           assessed("Gamma", "gamma.dev", fit_score=70), researched("Delta", "delta.ai")])
    write_run(
        tmp_path, "20261102-090000",
        [assessed("Acme", "acme.com"), {"name": "Beta", "domain": "beta.io", "error": "ValueError: boom"},
         researched("Gamma", "gamma.dev"), researched("Delta", "delta.ai")],
        manifest=MANIFEST,
    )  # fmt: skip

    counts, db = build(tmp_path)

    assert counts == (2, 8)
    assert db.execute("SELECT domain, fit_score, error FROM prospects WHERE run_id = '20261102-090000' ORDER BY domain").fetchall() == [
        ("acme.com", 82, None),
        ("beta.io", None, "ValueError: boom"),
        ("delta.ai", None, None),
        ("gamma.dev", None, None),
    ]
    assert db.execute("SELECT domain, run_id, fit_score FROM latest ORDER BY domain").fetchall() == [
        ("acme.com", "20261102-090000", 82),
        ("beta.io", "20261101-090000", 82),  # its failed second run does not hide the first
        ("delta.ai", "20261102-090000", None),  # never assessed: its newest research
        ("gamma.dev", "20261101-090000", 70),  # a later research-only run does not hide the rating
    ]
    assert db.execute(
        "SELECT mode, companies, errors, exa_dollars, model, input_tokens, saved_at::TEXT FROM runs ORDER BY run_id"
    ).fetchall() == [
        (None, None, None, None, None, None, None),  # a run without a manifest (interrupted) still counts
        ("assessed", 2, 1, 0.036, "claude-sonnet-5-5", 1200, "2026-11-02 09:01:30"),
    ]


def test_build_keeps_every_published_figure_and_marks_the_one_the_value_ran_on(tmp_path):
    stated = figure(500e6, "2025-09", "company", "$500M ARR in September 2025")
    estimated = figure(865e6, "2026-07", "third party", "$865M ARR as of July 2026 (Sacra estimate)")
    basis = figure(40e6, "2026", "third party", "$40M of in-app revenue in 2026 (estimate)")
    write_run(tmp_path, "20261101-090000", [
        assessed("Acme", "acme.com", revenue_figures=[stated, estimated]),
        assessed("Beta", "beta.io", revenue_figures=[stated], basis_figures=[basis]),
    ])  # fmt: skip

    _, db = build(tmp_path)

    assert db.execute(
        "SELECT domain, kind, amount_usd, as_of, stated_by, evidence, used FROM figures ORDER BY domain, amount_usd"
    ).fetchall() == [
        ("acme.com", "revenue", 500e6, "2025-09", "company", "$500M ARR in September 2025", False),
        ("acme.com", "revenue", 865e6, "2026-07", "third party", "$865M ARR as of July 2026 (Sacra estimate)", True),
        ("beta.io", "basis", 40e6, "2026", "third party", "$40M of in-app revenue in 2026 (estimate)", True),
        ("beta.io", "revenue", 500e6, "2025-09", "company", "$500M ARR in September 2025", False),  # basis comes first
    ]
    assert db.execute("SELECT domain, basis_usd, revenue_usd, value_low, value_method FROM prospects ORDER BY domain").fetchall() == [
        ("acme.com", None, 865e6, 4_325_000, "revenue"),  # the newer figure x half of revenue x 1%
        ("beta.io", 40e6, 500e6, 400_000, "basis"),
    ]


def test_build_keeps_what_exa_found_with_the_pages_it_cites(tmp_path):
    write_run(tmp_path, "20261101-090000", [assessed("Acme", "acme.com")])

    _, db = build(tmp_path)

    facts = "SELECT source, value, confidence, urls FROM facts WHERE field = ?"
    assert db.execute(facts, ["pricing"]).fetchall() == [
        ("site", "$12.99 a month or $69.99 a year.", "medium", ["https://acme.com/pricing"])
    ]
    assert db.execute(facts, ["headcount"]).fetchall() == [("money", "About 40 employees (LinkedIn).", None, [])]
    assert db.execute("SELECT count(*) FROM facts WHERE field = 'news'").fetchone() == (0,)
    assert db.execute("SELECT domain, title, url, date, summary FROM news").fetchall() == [("acme.com", *NEWS.values())]
    assert db.execute(
        "SELECT business_summary, value_signals, headcount_low, last_round_usd, funding_stage FROM prospects"
    ).fetchall() == [("Acme sells a meditation app to consumers.", ["Raised a Series A in March 2026."], 40, 12e6, "series A")]


def test_a_rebuild_rates_saved_prospects_again_with_the_current_config(tmp_path):
    run_dir = write_run(tmp_path, "20261101-090000", [assessed("Acme", "acme.com")])
    saved = (run_dir / "acme.com.json").read_text()

    _, db = build(tmp_path)
    assert db.execute(RATING).fetchall() == [(10000, 40000, "headcount", "A", "founder outreach", "about $20,000 a year")]
    db.close()

    counts, db = build(tmp_path, CHEAPER)

    assert counts == (1, 1)  # replaced, not appended
    assert db.execute(RATING).fetchall() == [(5000, 20000, "headcount", "B", "sales sequence", "about $10,000 a year")]
    assert (run_dir / "acme.com.json").read_text() == saved
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prospect_runs", "prospects.duckdb"]  # no scratch files left


def test_a_reader_keeps_its_data_while_a_rebuild_swaps_the_file_in(tmp_path):
    root = tmp_path / "it's here"  # the path is quoted into SQL
    root.mkdir()
    write_run(root, "20261101-090000", [assessed("Acme", "acme.com")])
    _, reader = build(root)

    build(root, CHEAPER)[1].close()  # DuckDB would refuse to write a file that `reader` holds open

    assert reader.execute("SELECT tier FROM prospects").fetchall() == [("A",)]
    reader.close()
    assert [row["tier"] for row in warehouse.ranking(root / "prospects.duckdb")] == ["B"]


def test_a_rebuild_clears_the_log_a_crashed_writer_left_beside_the_file(tmp_path):
    write_run(tmp_path, "20261101-090000", [assessed("Acme", "acme.com")])
    build(tmp_path)[1].close()
    subprocess.run([sys.executable, "-c", CRASHED_WRITER, str(tmp_path / "prospects.duckdb")], check=True)
    assert (tmp_path / "prospects.duckdb.wal").exists()  # beside a new file it would make every open fail

    build(tmp_path, CHEAPER)[1].close()

    assert [row["tier"] for row in warehouse.ranking(tmp_path / "prospects.duckdb")] == ["B"]


def test_ranking_lists_by_value_then_unknown_then_dropped_then_unassessed(tmp_path):
    write_run(tmp_path, "20261101-090000", [
        assessed("Small", "small.com", headcount_low=20, headcount_high=20),
        assessed("Big", "big.com", revenue_figures=[figure(9_000_000.0)]),
        assessed("Opaque", "opaque.com", headcount_low=None, headcount_high=None),
        assessed("Agency", "agency.com", basis_figures=[figure(90_000_000.0)], disqualifier="Agency that builds apps for clients"),
        researched("Fresh", "fresh.com"),
        {"name": "Broken", "domain": "broken.com", "error": "ValueError: boom"},
    ])  # fmt: skip

    build(tmp_path)[1].close()
    rows = warehouse.ranking(tmp_path / "prospects.duckdb")

    assert [(r["name"], r["tier"], r["route"]) for r in rows] == [
        ("Big", "A", "founder outreach"),
        ("Small", "B", "sales sequence"),
        ("Opaque", "unknown", "research manually"),
        ("Agency", "out", "drop"),
        ("Fresh", None, None),
    ]
    assert rows[0] | {"name": "", "route": ""} == {
        "name": "", "domain": "big.com", "fit_score": 82, "value_low": 45000, "value_high": 45000,
        "value_method": "revenue", "tier": "A", "route": "",
    }  # fmt: skip


def test_build_skips_what_is_not_a_run_or_a_result_and_says_so(tmp_path, caplog):
    run_dir = write_run(tmp_path, "20261101-090000", [assessed("Acme", "acme.com")])
    (run_dir / "beta.io.json").write_text('{"name": "Beta", "domain"')  # a run interrupted while saving
    (run_dir / "_run.json").write_text("[]")
    shutil.copy(run_dir / "acme.com.json", run_dir / "acme.com copy.json")  # the same prospect twice in one run
    (run_dir / "old.dev.json").write_text(json.dumps({"name": "Old", "domain": "old.dev", "assessment": {"fit_score": 80}}))
    (run_dir / "notes.json").write_text('{"todo": "call them"}')
    (run_dir / "True.json").write_text('{"name": "T", "domain": true}')
    (run_dir / "folder.dev.json").mkdir()  # cannot be opened as a file
    (run_dir / "deep.dev.json").write_text("[" * 100_000 + "]" * 100_000)  # nested beyond what json reads
    # A backup whose name sorts after every run must not pass for the newest result.
    for not_a_run in ("zz-backup", "20261101-090000 copy", "99999999-999999", "20261101-09000"):
        write_run(tmp_path, not_a_run, [assessed("Acme", "acme.com", fit_score=10)])

    counts, db = build(tmp_path)

    assert counts == (1, 1)
    assert db.execute("SELECT run_id, domain, fit_score FROM latest").fetchall() == [("20261101-090000", "acme.com", 82)]
    for skipped in ("beta.io.json", "_run.json: not a JSON object", "acme.com copy.json: ValueError: not named after",
                    "old.dev.json: KeyError", "notes.json: KeyError", "True.json: ValueError: not named after",
                    "folder.dev.json: [Errno", "deep.dev.json: maximum recursion depth", "zz-backup: not a run folder",
                    "20261101-090000 copy: not a run folder", "99999999-999999: not a run folder",
                    "20261101-09000: not a run folder"):
        assert skipped in caplog.text


def test_a_result_duckdb_will_not_load_is_left_out_and_the_rest_loads(tmp_path, caplog):
    odd = assessed("Odd", "odd.dev")
    odd["assessment"]["fit_reasons"] = "edited by hand: text where the column holds a list"
    write_run(tmp_path, "20261101-090000", [assessed("Acme", "acme.com"), odd], manifest={**MANIFEST, "companies": "two"})
    write_run(
        tmp_path, "20261102-090000", [assessed("Beta", "beta.io")],
        manifest={**MANIFEST, "started_at": "yesterday", "saved_at": "20261102T090130", "claude": "x", "exa": []},
    )  # fmt: skip

    counts, db = build(tmp_path)

    assert counts == (2, 2)
    assert db.execute("SELECT domain FROM prospects ORDER BY domain").fetchall() == [("acme.com",), ("beta.io",)]
    assert db.execute("SELECT run_id, mode, companies, started_at, saved_at::TEXT, model FROM runs ORDER BY run_id").fetchall() == [
        ("20261101-090000", None, None, None, None, None),  # its manifest would not load: the run still counts
        ("20261102-090000", "assessed", 2, None, "2026-11-02 09:01:30", None),
    ]
    assert "odd.dev.json: " in caplog.text and "20261101-090000/_run.json: " in caplog.text


def test_text_cut_inside_an_emoji_loads_with_the_stray_half_replaced(tmp_path):
    result = assessed("Acme", "acme.com")
    result["research"]["site"] = {**SITE, "pricing": "Free \ud83d", "scale": None}
    write_run(tmp_path, "20261101-090000", [result])

    _, db = build(tmp_path)

    assert db.execute("SELECT field, value FROM facts WHERE field IN ('pricing', 'scale') ORDER BY field").fetchall() == [
        ("pricing", "Free ?"),
        ("scale", None),  # a field Exa left null stays NULL, not the text "None"
    ]
