"""Builders for pipeline files (out/<run>/, watch_state.json, discovered.csv) used by the dashboard tests."""

import json


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
