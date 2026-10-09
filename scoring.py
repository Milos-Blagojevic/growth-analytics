"""Fit scoring: Claude judges researched companies against the candidate profile."""

import json
import os
from typing import Literal

import anthropic
from pydantic import BaseModel

DEFAULT_MODEL = "claude-sonnet-5-5"

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


def score_company(research, profile, *, model=None, client=None):
    """Returns (Fit, usage) where usage is {"input_tokens", "output_tokens"}."""
    client = client or _client()
    candidate = {
        "target_roles": profile["search"]["roles"],
        "seniority": profile["search"]["seniority"],
        "work_mode": profile["search"]["work_mode"],
        "location": profile["candidate"]["location"].strip() or "(unknown)",
        "experience": profile["candidate"]["experience"].strip() or "(empty)",
        "notes": profile["candidate"]["notes"].strip() or "(none)",
        "gaps": profile["candidate"]["gaps"].strip() or "(none)",
        "weak_spots": profile["candidate"]["weak_spots"].strip() or "(none)",
    }
    user_content = (
        "Candidate:\n"
        + json.dumps(candidate, indent=2)
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
