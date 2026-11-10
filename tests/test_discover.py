import pytest

import hunt
from exa_mock import AGENT_URL, agent_run
from prospect_data import SETTINGS
from prospects import discover


def company(name, domain, **fields):
    return {"name": name, "domain": domain, "why": f"{name} sells subscriptions.", "employees": 40, "funding_stage": "seed", **fields}


def test_find_asks_fibers_database_for_the_ideal_customer_and_leaves_out_known_companies(exa):
    exa.on_agent(agent_run({"companies": [company("Acme", "https://www.Acme.com/pricing"), company("Beta", "beta.io")]}))

    found = discover.find(SETTINGS, limit=5, focus="10 to 100 employees", known={"old.com", "gone.io"})

    assert found == [
        {"name": "Acme", "domain": "acme.com", "why": "Acme sells subscriptions.", "employees": 40, "funding_stage": "seed"},
        {"name": "Beta", "domain": "beta.io", "why": "Beta sells subscriptions.", "employees": 40, "funding_stage": "seed"},
    ]
    (sent,) = exa.requests(AGENT_URL)
    assert sent["dataSources"] == [{"provider": "fiber"}]
    assert sent["input"] == {"exclusion": [{"domain": "gone.io"}, {"domain": "old.com"}]}
    assert sent["outputSchema"]["properties"]["companies"]["maxItems"] == 5
    for expected in ("Find up to 5 companies", "Consumer mobile apps that sell subscriptions.",
                     "Narrow it to: 10 to 100 employees.", "Leave out: Agency that builds apps for clients."):  # fmt: skip
        assert expected in sent["query"]


def test_find_keeps_each_new_company_once_and_only_rows_it_can_use(exa):
    exa.on_agent(agent_run({"companies": [
        company("Known", "www.old.com"),  # already a prospect
        company("Acme", "acme.com", why="Sells\n subscriptions \ud83d", employees=None, funding_stage=None),
        company("Acme again", "https://acme.com/about"),
        company("No site", "not a domain"),
        company("", "nameless.com"),
        "not a company",
        company("Odd", "odd.dev", employees="about 40", funding_stage=" Series A "),
        company("Third", "third.io"),
    ]}))  # fmt: skip

    found = discover.find(SETTINGS, limit=2, known={"old.com"})

    assert found == [
        {"name": "Acme", "domain": "acme.com", "why": "Sells subscriptions ?", "employees": None, "funding_stage": None},
        {"name": "Odd", "domain": "odd.dev", "why": "Odd sells subscriptions.", "employees": None, "funding_stage": "Series A"},
    ]  # never more than the limit asked for
    assert "Narrow it to" not in exa.requests(AGENT_URL)[0]["query"]  # no focus given


def test_find_keeps_only_real_domains_and_text_a_prospects_file_can_hold(exa, tmp_path):
    not_domains = (".", "port.com:8080", "sales@mail.com", "see acme.com for more", "query.com?utm=1", "two.com, two.io")
    exa.on_agent(agent_run({"companies": [
        company("#paid", "hashpaid.com", why='=HYPERLINK("http://evil.io")'),
        company("# \t#hash", "hash.io", why="+1 for growth", funding_stage="-"),
        company("@scale", "scale.dev", employees=0),
        company("Minus", "minus.dev", employees=-5),
        *(company("Junk", domain) for domain in not_domains),
    ]}))  # fmt: skip

    found = discover.find(SETTINGS, limit=10)

    # a spreadsheet would run text that starts with = + - or @ as a formula; a prospects file reader would take
    # "#paid" for a comment; nobody has no or fewer than no employees
    assert [(c["name"], c["domain"], c["why"], c["employees"], c["funding_stage"]) for c in found] == [
        ("paid", "hashpaid.com", "'=HYPERLINK(\"http://evil.io\")", 40, "seed"),
        ("hash", "hash.io", "'+1 for growth", 40, "'-"),
        ("'@scale", "scale.dev", "'@scale sells subscriptions.", None, "seed"),
        ("Minus", "minus.dev", "Minus sells subscriptions.", None, "seed"),
    ]
    history = tmp_path / "prospects_discovered.csv"
    discover.save(history, found, "")
    assert [domain for _, domain in hunt.load_companies(history)] == ["hashpaid.com", "hash.io", "scale.dev", "minus.dev"]
    assert discover.known_domains(history) == {"hashpaid.com", "hash.io", "scale.dev", "minus.dev"}


def test_find_sends_the_agent_no_more_known_domains_than_the_cap_and_still_leaves_out_every_one(exa):
    known = {f"known{n:04}.com" for n in range(discover.MAX_EXCLUDED + 1)}
    last = sorted(known)[-1]  # the one that is not sent
    exa.on_agent(agent_run({"companies": [company("Known", last), company("New", "new.io")]}))

    found = discover.find(SETTINGS, limit=5, known=known)

    assert [c["domain"] for c in found] == ["new.io"]
    sent = exa.requests(AGENT_URL)[0]["input"]["exclusion"]
    assert len(sent) == discover.MAX_EXCLUDED and {"domain": last} not in sent


def test_save_starts_a_new_line_after_a_history_that_ends_without_one(tmp_path):
    history = tmp_path / "prospects_discovered.csv"
    history.write_text("# name,domain\nOld,old.com")  # edited by hand

    discover.save(history, [company("Acme", "acme.com")], "")

    assert discover.known_domains(history) == {"old.com", "acme.com"}


def test_the_write_check_before_a_paid_run_leaves_the_folder_as_it_was(tmp_path):
    discover.check_writable(tmp_path / "new.csv")
    assert not (tmp_path / "new.csv").exists()

    link = tmp_path / "linked.csv"  # the history kept elsewhere, not written yet
    link.symlink_to(tmp_path / "elsewhere.csv")
    discover.check_writable(link)
    assert link.is_symlink() and not (tmp_path / "elsewhere.csv").exists()

    with pytest.raises(OSError):
        discover.check_writable(tmp_path / "no_such_folder" / "found.csv")


def test_a_focus_that_reached_python_as_broken_bytes_still_searches_and_saves(exa, tmp_path):
    # a shell argument that is not UTF-8 arrives as half a surrogate pair, which no file or request can hold
    exa.on_agent(agent_run({"companies": [company("Acme", "acme.com")]}))

    found = discover.find(SETTINGS, limit=5, focus="fintech \udcff")
    discover.save(tmp_path / "found.csv", found, "fintech \udcff")

    assert "Narrow it to: fintech ?." in exa.requests(AGENT_URL)[0]["query"]
    assert ",fintech ?," in (tmp_path / "found.csv").read_text()


def test_saved_companies_read_as_a_prospects_file_and_as_known_ones(tmp_path):
    history = tmp_path / "prospects_discovered.csv"
    (tmp_path / "prospects.csv").write_text("# name,domain\nMine,https://www.Mine.app/\n\nhalf a row\n")

    discover.save(history, [company("Acme, Inc.", "acme.com")], "fintech")
    discover.save(history, [company("Beta", "beta.io", employees=None)], "")

    lines = history.read_text().splitlines()
    assert lines[0] == "# name,domain,why,employees,funding_stage,focus,discovered_at"
    assert lines[1].startswith('"Acme, Inc.",acme.com,"Acme, Inc. sells subscriptions.",40,seed,fintech,20')
    assert lines[2].startswith("Beta,beta.io,Beta sells subscriptions.,,seed,,20") and len(lines) == 3
    # the history is a prospects file: `run --prospects prospects_discovered.csv` researches what was found
    assert hunt.load_companies(history) == [("Acme, Inc.", "acme.com"), ("Beta", "beta.io")]
    assert discover.known_domains(tmp_path / "prospects.csv", history, tmp_path / "missing.csv") == {
        "mine.app", "acme.com", "beta.io",
    }  # fmt: skip
