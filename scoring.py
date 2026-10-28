"""Fit scoring: Jev screens each open role against hard requirements, then Claude judges the company and the roles
that passed against the candidate profile."""

import json
import os
from typing import Literal

import anthropic
from pydantic import BaseModel

import jev

DEFAULT_MODEL = "claude-sonnet-5-5"

# Role screen: a role is dropped before Claude only when Jev is confident it fails a hard requirement of the rubric
# below (target role, seniority, location). Uncertain answers go on to Claude, which applies the full rubric.
SCREEN_YES = 0.9  # Jev's probability that counts as a confident yes
SCREEN_NO = 0.1  # ... and as a confident no
BELOW_SENIOR_MIN = 0.9  # probability of junior or mid-level that drops a role
# Lowest first. SYSTEM_PROMPT's rubric counts senior and above, not junior or mid-level (levels 0 and 1): keep in step.
SENIORITY_LEVELS = ["Intern, junior or entry level", "Mid-level", "Senior", "Lead, staff, principal or above"]
ROLE_QUESTIONS = {
    "target_role": jev.noul(
        "The role's function is one of the candidate's target roles, or its duties clearly overlap with one of them."),
    "seniority": jev.score("The seniority level of the role.", SENIORITY_LEVELS),
}
LOCATION_QUESTIONS = {
    "region_excludes_candidate": jev.noul(
        "The posting limits who can take the role to countries or regions that do not include the candidate's "
        "location (for example remote for US residents only, or candidates must live in Germany)."),
    "onsite_or_hybrid_only": jev.noul("The role is on-site or hybrid only: the posting offers no fully remote option."),
    "office_in_candidate_country": jev.noul("The role's office is in the same country as the candidate's location."),
    "relocation": jev.noul("The posting offers relocation support."),
}

SYSTEM_PROMPT = """\
You assess how well a company and its open roles fit a job seeker. Use ONLY the research evidence \
provided; never add facts from memory. If something is not in the evidence, say it is unknown.

Scoring rubric (0-100):
- Role relevance: roles must be one of the candidate's target roles or clearly equivalent. A different \
function (e.g. generic software engineer, data scientist) only counts when the posting clearly overlaps \
with the target roles, such as a growth-engineering role.
- Seniority: senior, lead, staff and principal level count. Junior and mid-level do not.
- Location: the candidate's location is given. Remote is a fit only if the posting's allowed region includes \
that location; a remote role limited to other countries (e.g. US-only) is not a fit. If the allowed region is \
not stated, say it is unknown and lower the score modestly. On-site or hybrid is a fit only if the posting \
explicitly offers relocation support. If the posting explicitly restricts the role to regions that exclude the \
candidate's location and offers no relocation support, the role is not a fit: the score MUST be below 50 and \
the verdict "skip", however strong the company.
- Business-side orientation: prefer roles close to growth, revenue, marketing, product analytics or business \
reporting over pure infrastructure work.
- Company signal: funding, growth and data-team maturity from the evidence.
- Evidence quality: if identity_verified is false, the verdict MUST be "skip". Low-confidence grounding \
lowers the score.
- If the candidate's experience is empty, score on the criteria above only and note that experience was not \
considered.
- Candidate gaps: the candidate lists areas where their experience is thin. If a posting's core \
responsibilities fall mainly in such an area, the verdict is "stretch" instead of "apply" or "consider": set \
gap_note to one sentence naming the gap and what the candidate actually has, and proof_ideas to at most 2 \
concrete side projects that would build credible proof, reusing the candidate's existing strengths. Reflect \
the gap in the score (typically 10-20 points lower). For roles outside the listed gaps never use "stretch" \
and leave gap_note empty and proof_ideas an empty list.
- Posting links: link_status "unverified" means the page could not be confirmed open; the verdict for such \
a role is at most "consider", and next_step must say to confirm the posting is still live.
- Experience and stack fit: compare the posting's responsibilities and required tools with the candidate's \
CV; the `description` field of a role holds the posting text when available, so prefer it over summaries. A \
strong match on both is the strongest positive signal and can justify scores in the high 80s or 90s. A missing \
core tool is a concern, not an automatic downgrade.
- Weak spots: the candidate lists specific tools or skills where they are weak. If the posting requires one, \
name it in a concern and give one concrete preparation step in next_step. A weak spot alone never changes \
the verdict (unlike gaps).
- Scope: set scope_note to one sentence only when the title and the described responsibilities do not match, \
for example an analyst or BI title whose duties include substantial engineering work (ETL/ELT, data modelling, \
pipelines, dbt) or an engineering title with mostly analysis duties. Say which part of the candidate's \
background fits. Leave it empty when title and duties match (for example a data engineer title with \
data-engineering duties) and when no description is available.
- Compensation: report salary or benefits only as stated in the posting. Never judge whether pay is high or \
low unless the candidate states a pay expectation.
- Preferences: the candidate's notes state preferences (for example individual contributor vs people \
management). Apply them as a modest score adjustment and mention them in reasons or concerns; a preference \
alone never makes a role "skip". Treat facts in the notes (for example past mentoring) as experience.

Verdicts: apply >= 75, consider 50-74, skip < 50 ("stretch" replaces apply or consider for gap-area roles). \
When open roles exist but none are relevant, score below 50 (skip). When the research found NO open roles at \
all, the company may still deserve a manual look: score only the company (signals, data-team maturity, \
overlap with the candidate's background) and use verdict "check_careers" if that score is >= 50, otherwise \
"skip"; never "apply", "consider" or "stretch" without a posting. In that case say in next_step what to look \
for on the careers page.
Be concise: each reason or concern is one short sentence; give at most 3 reasons and at most 3 concerns."""


class Fit(BaseModel):
    score: int  # 0-100
    verdict: Literal["apply", "consider", "stretch", "check_careers", "skip"]
    best_role_title: str  # empty string if no relevant role
    best_role_url: str  # empty string if no relevant role
    reasons: list[str]  # up to 3
    concerns: list[str]  # up to 3
    location_assessment: str
    next_step: str  # one concrete action, e.g. who to contact or what to check
    gap_note: str  # only for verdict "stretch": the gap and what the candidate actually has; else empty
    proof_ideas: list[str]  # only for verdict "stretch": up to 2 side-project ideas; else empty
    scope_note: str  # title vs responsibilities mismatch (e.g. analyst title with engineering duties); else empty


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env.")
    return anthropic.Anthropic(api_key=api_key)


def candidate(profile):
    """What Claude is told about the candidate, for fit scoring and interview coaching."""
    return {
        "target_roles": profile["search"]["roles"],
        "seniority": profile["search"]["seniority"],
        "work_mode": profile["search"]["work_mode"],
        "location": profile["candidate"]["location"].strip() or "(unknown)",
        "experience": profile["candidate"]["experience"].strip() or "(empty)",
        "notes": profile["candidate"]["notes"].strip() or "(none)",
        "gaps": profile["candidate"]["gaps"].strip() or "(none)",
        "weak_spots": profile["candidate"]["weak_spots"].strip() or "(none)",
    }


def _role_failures(answers, location):
    """Hard requirements the role confidently fails, as short phrases; empty when it goes on to Claude."""
    failed = []
    if answers["target_role"]["noul"] <= SCREEN_NO:
        failed.append("not one of your target roles")
    levels = answers["seniority"]["probabilities"]
    if levels.get("0", 0) + levels.get("1", 0) >= BELOW_SENIOR_MIN:
        failed.append("below senior level")
    if location and answers["relocation"]["noul"] <= SCREEN_NO:
        if answers["region_excludes_candidate"]["noul"] >= SCREEN_YES:
            failed.append(f"limited to regions that exclude {location}, no relocation")
        elif (answers["onsite_or_hybrid_only"]["noul"] >= SCREEN_YES
              and answers["office_in_candidate_country"]["noul"] <= SCREEN_NO):
            failed.append(f"on-site or hybrid in another country than {location}, no relocation")
    return failed


def screen_roles(research, profile):
    """Jev checks each open role against the rubric's hard requirements, one request per role.

    Returns {"model", "roles": [{"title", "url", "passed", "failed", "answers"}]} in open_roles order: "failed" names
    what a dropped role confidently fails, "answers" keeps Jev's probabilities for tuning the thresholds. The location
    questions are skipped when the profile has no location. Raises when a Jev request fails.
    """
    location = profile["candidate"]["location"].strip()
    questions = ROLE_QUESTIONS | (LOCATION_QUESTIONS if location else {})
    seeker = {"target_roles": profile["search"]["roles"], **({"location": location} if location else {})}
    roles = []
    for role in research["content"]["open_roles"]:
        posting = {k: role[k] for k in ("title", "location", "remote_or_relocation", "description") if role.get(k)}
        answers = jev.ask({"candidate": seeker, "role": posting}, questions)
        failed = _role_failures(answers, location)
        compact = {key: a["noul"] if "noul" in a else {"score": a["score"], "probabilities": a["probabilities"]}
                   for key, a in answers.items()}
        roles.append({"title": role["title"], "url": role["url"], "passed": not failed, "failed": failed,
                      "answers": compact})
    return {"model": jev.model(), "roles": roles}


def _screened_out(screen):
    """The skip a company gets when Jev drops every open role; Claude is not asked. At most 3 concerns, like Claude's."""
    concerns = [f"{r['title']}: {', '.join(r['failed'])}" for r in screen["roles"]]
    if len(concerns) > 3:
        concerns = concerns[:2] + [f"{len(concerns) - 2} more open roles also failed the screen (see screen in out/)"]
    return Fit(
        score=0,
        verdict="skip",
        best_role_title="",
        best_role_url="",
        reasons=[],
        concerns=concerns,
        location_assessment="Not assessed: every open role was screened out before scoring.",
        next_step="Nothing: no open role meets your hard requirements (see concerns).",
        gap_note="",
        proof_ideas=[],
        scope_note="",
    )


def score_company(research, profile, *, screen=None, model=None, client=None):
    """Returns (Fit, usage) where usage is {"input_tokens", "output_tokens"}.

    With a `screen` (screen_roles), Claude sees only the roles that passed it, and a company whose roles all failed
    gets a skip with score 0 without a Claude call.
    """
    if screen and screen["roles"]:
        if not any(r["passed"] for r in screen["roles"]):
            return _screened_out(screen), {"input_tokens": 0, "output_tokens": 0}
        passed = [role for role, r in zip(research["content"]["open_roles"], screen["roles"], strict=True) if r["passed"]]
        research = {**research, "content": {**research["content"], "open_roles": passed}}
    client = client or _client()
    user_content = (
        "Candidate:\n"
        + json.dumps(candidate(profile), indent=2)
        + "\n\nCompany research (with source grounding):\n"
        + json.dumps({"research": research["content"], "grounding": research["grounding"]}, indent=2)
    )
    response = client.messages.parse(
        model=model or os.environ.get("SCORING_MODEL", DEFAULT_MODEL),
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=Fit,
    )
    fit = response.parsed_output
    if fit is None:
        raise RuntimeError(f"Claude returned no parsable output for {research['name']} (stop_reason={response.stop_reason})")
    if not research["content"]["open_roles"] and fit.verdict in ("apply", "consider", "stretch"):
        fit.verdict = "check_careers"  # never recommend applying without a posting
    if fit.verdict == "apply":
        best = next((r for r in research["content"]["open_roles"] if r["url"] == fit.best_role_url), None)
        if best and best.get("link_status") == "unverified":
            fit.verdict = "consider"  # a posting that could not be confirmed open is never an "apply"
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return fit, usage
