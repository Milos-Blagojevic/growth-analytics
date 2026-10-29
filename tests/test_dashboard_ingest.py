import json

import pytest

from dashboard import db, ingest, queries
from dashboard_data import fit, result, write_run

ACME_ROLE = ("Senior Analytics Engineer", "https://jobs.ashbyhq.com/acme/1", "live")


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path)
    yield conn
    conn.close()


def two_runs(root):
    """Run 1 (no manifest, saved before it existed): Acme consider, Beta apply. Run 2: Acme apply, Beta fails."""
    write_run(
        root,
        "20260101-090000",
        [
            result("Acme", "acme.com", fit=fit(65, "consider", *ACME_ROLE[:2]), roles=[ACME_ROLE]),
            result("Beta", "beta.io", fit=fit(78, "apply", "Data Engineer", "https://beta.io/jobs/1")),
        ],
    )
    write_run(
        root,
        "20260108-090000",
        [
            result("Acme", "acme.com", fit=fit(82, "apply", *ACME_ROLE[:2]), roles=[ACME_ROLE]),
            result("Beta", "beta.io", error="ValueError: boom"),
        ],
        manifest={
            "mode": "dry_run",
            "started_at": "2026-01-08T08:55:00",
            "saved_at": "2026-01-08T09:00:00",
            "companies": 2,
            "errors": 1,
            "exa": {"requests": 5, "dollars": 0.031},
            "claude": {"model": "m", "input_tokens": 1000, "output_tokens": 200},
        },
    )


def test_ingest_keeps_latest_score_per_company_and_is_idempotent(tmp_path, conn):
    two_runs(tmp_path)

    ingest.ingest(conn, tmp_path)
    ingest.ingest(conn, tmp_path)

    cards = queries.board(conn, verdict="apply")
    # Acme: latest run wins. Beta: its latest run failed, so the last scored result stays on the board.
    assert [(c["name"], c["score"], c["run_id"], c["best_link_status"]) for c in cards] == [
        ("Acme", 82, "20260108-090000", "live"),
        ("Beta", 78, "20260101-090000", None),
    ]
    assert queries.verdict_counts(conn)["consider"] == 0
    runs = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM runs")}
    assert runs["20260101-090000"]["mode"] is None
    assert runs["20260101-090000"]["saved_at"] == "2026-01-01T09:00:00"  # from the directory name
    assert (runs["20260108-090000"]["exa_dollars"], runs["20260108-090000"]["tokens_in"]) == (0.031, 1000)
    assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 4

    beta = queries.company(conn, "beta.io")
    assert beta["result"]["run_id"] == "20260101-090000"
    assert [h["error"] for h in beta["newer"]] == ["ValueError: boom"]


def test_refresh_reingests_only_when_a_source_changes(tmp_path, conn):
    two_runs(tmp_path)
    assert ingest.refresh(conn, tmp_path) is True
    assert ingest.refresh(conn, tmp_path) is False

    write_run(tmp_path, "20260109-090000", [result("Gamma", "gamma.dev", fit=fit(55, "check_careers"))])

    assert ingest.refresh(conn, tmp_path) is True
    assert [c["name"] for c in queries.board(conn, verdict="check_careers")] == ["Gamma"]


def test_unreadable_run_is_skipped_and_reported(tmp_path, conn):
    two_runs(tmp_path)
    broken = write_run(tmp_path, "20260110-090000", [])
    (broken / "half.io.json").write_text('{"name": "Half", "dom')  # a result still being written

    ingest.ingest(conn, tmp_path)

    assert [p.split(":")[0] for p in queries.problems(conn)] == ["20260110-090000"]
    assert queries.verdict_counts(conn)["apply"] == 2


def test_ingest_reads_watch_state_and_lead_status(tmp_path, conn):
    two_runs(tmp_path)
    (tmp_path / "watch_state.json").write_text(
        json.dumps(
            {
                "acme": {
                    "old": {"title": "Analytics Engineer", "first_seen": "2026-01-01T10:00:00"},  # before full entries
                    "new": {
                        "title": "Growth Engineer",
                        "location": "Berlin",
                        "remote": True,
                        "url": "https://jobs.ashbyhq.com/acme/new",
                        "first_seen": "2026-01-05T10:00:00",
                    },
                }
            }
        )
    )
    (tmp_path / "discovered.csv").write_text(
        "discovered_at,name,domain,why,focus\n"
        "2026-01-01T08:00:00,Acme,acme.com,Hiring AEs,\n"
        "2026-01-02T08:00:00,Delta,delta.ai,Growth team,fintech\n"
        "2026-01-02T08:00:00,Echo,echo.so,Data team,\n"
    )
    (tmp_path / "companies.csv").write_text("# name,domain\nDelta, delta.ai\n#Echo,echo.so\n")

    ingest.ingest(conn, tmp_path)

    postings = {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM watch_postings")}
    assert (postings["old"]["url"], postings["old"]["location"]) == (None, None)
    assert (postings["new"]["url"], postings["new"]["remote"]) == ("https://jobs.ashbyhq.com/acme/new", 1)
    leads = queries.leads(conn)
    assert {i["domain"]: i["status"] for i in leads["items"]} == {
        "acme.com": "researched",
        "delta.ai": "queued",
        "echo.so": "new",  # commented out in companies.csv
    }
    assert [i["domain"] for i in queries.leads(conn, status="new")["items"]] == ["echo.so"]


def test_companies_csv_domains_are_normalised_and_deduplicated_like_hunt_reads_them(tmp_path, conn):
    """`Acme,https://www.Acme.com/` is the company hunt.py saves as acme.com.json, so its lead is queued and
    Add to companies.csv does not append it again."""
    companies = tmp_path / "companies.csv"
    companies.write_text("# name,domain\nAcme,https://www.Acme.com/\nAcme 2,www.acme.com\n")
    (tmp_path / "discovered.csv").write_text("discovered_at,name,domain,why,focus\n2026-01-01T08:00:00,Acme,acme.com,Hiring,\n")

    ingest.ingest(conn, tmp_path)

    assert ingest.read_companies(companies) == [("Acme", "acme.com")]
    assert [i["status"] for i in queries.leads(conn)["items"]] == ["queued"]
    assert ingest.add_companies(companies, [("Acme", "acme.com")]) == []
    assert companies.read_text() == "# name,domain\nAcme,https://www.Acme.com/\nAcme 2,www.acme.com\n"


def test_csvs_saved_by_excel_with_a_byte_order_mark_are_read(tmp_path, conn):
    companies = tmp_path / "companies.csv"
    companies.write_bytes("\ufeffAcme,acme.com\n".encode())
    (tmp_path / "discovered.csv").write_bytes(
        "\ufeffdiscovered_at,name,domain,why,focus\n2026-01-01T08:00:00,Acme,acme.com,Hiring,\n".encode()
    )

    ingest.ingest(conn, tmp_path)

    assert ingest.read_companies(companies) == [("Acme", "acme.com")]
    assert queries.problems(conn) == []
    assert [(i["domain"], i["status"]) for i in queries.leads(conn)["items"]] == [("acme.com", "queued")]


def test_corrupt_watch_state_and_csvs_are_skipped_and_reported(tmp_path, conn):
    two_runs(tmp_path)
    (tmp_path / "watch_state.json").write_text("[]")
    (tmp_path / "discovered.csv").write_text("discovered_at,name,domain,focus\n2026-01-01T08:00:00,Delta,delta.ai,\n")
    (tmp_path / "companies.csv").write_text("Bad\x00Corp," + "x" * 200_000 + "\n")  # a field beyond csv's field limit
    mail_dir = tmp_path / "mail"
    mail_dir.mkdir()
    (mail_dir / "20260108-090000.json").write_text(json.dumps({"updates": [
        {"id": "m1", "received_at": "2026-01-08T08:00:00", "from": "Acme", "from_address": "hr@acme.com",
         "subject": "Interview", "outcome": "interview", "company": "Acme", "role": "AE", "summary": "s"}]}))

    ingest.ingest(conn, tmp_path)

    assert [p.split(":")[0] for p in queries.problems(conn)] == ["watch_state.json", "companies.csv", "discovered.csv"]
    assert queries.verdict_counts(conn)["apply"] == 2  # the sources after the broken ones are still ingested
    assert conn.execute("SELECT COUNT(*) FROM mail_updates").fetchone()[0] == 1
    assert queries.leads(conn)["items"] == []

    (tmp_path / "discovered.csv").write_text("discovered_at,name,domain,why,focus\n2026-01-01T08:00:00,Delta,delta.ai,w,\n")
    ingest.ingest(conn, tmp_path)
    assert [p.split(":")[0] for p in queries.problems(conn)] == ["watch_state.json", "companies.csv"]
    assert [(i["domain"], i["status"]) for i in queries.leads(conn)["items"]] == [("delta.ai", "new")]  # not queued: unknown

    (tmp_path / "companies.csv").write_text("Delta,delta.ai\n")
    ingest.ingest(conn, tmp_path)
    assert [p.split(":")[0] for p in queries.problems(conn)] == ["watch_state.json"]
    assert [(i["domain"], i["status"]) for i in queries.leads(conn)["items"]] == [("delta.ai", "queued")]


def test_schema_version_change_rebuilds_derived_tables(tmp_path):
    conn = db.connect(tmp_path)
    conn.execute("INSERT INTO leads VALUES ('a.com', 'A', '', '', '', 0)")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    conn = db.connect(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
