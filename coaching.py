"""Interview coaching with Claude: a prep brief tailored to one interview, and a review of its transcript.

Both use the scoring model (SCORING_MODEL, default scoring.DEFAULT_MODEL) and the candidate profile, and both
are told to use only the facts they are given: a prep brief that invents achievements is worse than none.
"""

import json
import os

from pydantic import BaseModel, create_model

import interviews
import scoring


class Match(BaseModel):
    requirement: str  # what the role asks for
    evidence: str  # the candidate's matching experience, or how to address it if thin


class Story(BaseModel):
    title: str
    use_when: str  # which question or moment this story answers
    points: list[str]  # 3-5 spoken bullet points: situation, what you did, result, lesson


class Answer(BaseModel):
    question: str
    points: list[str]  # 2-4 bullet points to say


class Gap(BaseModel):
    gap: str
    handle: str  # an honest way to address it


class Prep(BaseModel):
    positioning: str  # 2-3 sentences: who you are for THIS role
    intro: str  # "tell me about yourself", 60-90 seconds spoken, first person
    role_match: list[Match]  # up to 6 key requirements
    stories: list[Story]  # 2-4 stories
    questions: list[Answer]  # 5-8 likely questions for this stage
    gaps: list[Gap]  # up to 4
    ask_them: list[str]  # 3-5 questions to ask
    watch_outs: list[str]  # up to 5 reminders, from past reviews where available
    compensation: str  # how to handle salary and contract questions; never invent numbers


class Moment(BaseModel):
    at: str  # transcript timestamp like "12:01", or "" if none
    quote: str  # verbatim from the transcript, in its original language
    note: str  # why it matters
    better: str  # what to say instead; "" for strengths


Scores = create_model("Scores", **{key: (int, ...) for key in interviews.SCORES})  # 1-5 each


class Review(BaseModel):
    summary: str  # 2-3 sentences
    scores: Scores
    strengths: list[Moment]  # up to 4
    improvements: list[Moment]  # up to 6, most important first
    next_time: list[str]  # up to 5 concrete actions


PREP_PROMPT = """\
You prepare a job candidate for one specific interview. Write a brief they can read just before and glance at \
during the interview.

Rules:
- Use ONLY facts from the candidate profile and CV, the posting, the company notes and the past reviews given. \
Never invent employers, numbers, results or tools. If a story would be stronger with a number the CV does not \
have, write "[add: <what>]" instead of a number.
- Tailor everything to this role and stage. HR / recruiter screens are about motivation, career story, reasons \
for moves, logistics and salary handling, with technical detail kept light. Hiring-manager rounds are about \
impact stories and how the candidate works. Technical rounds go deep on the tools and design choices the posting \
names.
- Address the candidate as "you" everywhere except the intro. Positioning says who you are for THIS role \
("You are…"), using the posting's own priorities, in 2-3 sentences.
- The intro is spoken: plain first-person sentences, no buzzwords, 60-90 seconds (about 150-220 words), ending \
with why this role. Keep the most recent role current only if the CV says so.
- Stories are the candidate's real projects mapped to what this role needs, as short spoken bullet points.
- Gaps are honest: name what is missing and a truthful way to handle it; never suggest claiming experience the \
candidate does not have.
- Watch-outs: when past reviews are given, turn their recurring problems and "next time" actions into short \
reminders and say which interview they come from. Otherwise give the most important reminders for this stage.
- Compensation: how to handle the question at this stage (give a range for this role; do not volunteer past \
pay; ask about the range and contract type). Use a range only if the candidate's focus notes give one.
- Respect the candidate's focus notes: what they want to emphasise.
- Write the intro, story points and answer points in the interview language given; everything else in English."""

REVIEW_PROMPT = """\
You review one job interview transcript for the candidate (the speaker marked YOU), so they present themselves \
better next time. Be direct and specific, kind but not flattering.

Rules:
- Quote the transcript verbatim in its original language, with its timestamp, for every strength and \
improvement. Never invent quotes.
- Judge against this role and stage. The measured metrics (talk share, answer lengths, filler words) are \
given; use them, do not recount them.
- Look for: answers that run long or wander; positioning that argues against the role applied for; stories \
without evidence or numbers; self-undermining comments ("I did not prepare this", "this sounds stupid"); weak \
or risky reasons for joining or leaving jobs; volunteering past salary; missed chances to tell the candidate's \
strongest stories; and the questions the candidate asked.
- If a prep brief is given, say whether the candidate used its positioning, intro and stories.
- For each improvement, "better" is what to say instead, in the interview language, short enough to say aloud.
- Scores are integers 1 (poor) to 5 (excellent) for: {dimensions}.
- next_time: up to 5 concrete actions for the next interview, most important first. Write in English."""


def _usage(response):
    return {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}


def _parse(client, model, system, content, output_format, what):
    response = client.messages.parse(
        model=model or os.environ.get("SCORING_MODEL", scoring.DEFAULT_MODEL),
        # A Serbian prep brief hit 8000 and was cut off mid-JSON. 16000 stays under the SDK's
        # non-streaming limit (about 21k), above which it requires streaming.
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=output_format,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"Claude returned no parsable {what} (stop_reason={response.stop_reason})")
    return response.parsed_output, _usage(response)


def _interview(interview):
    return {
        "company": interview["company"],
        "role": interview["role"],
        "stage": interviews.STAGES.get(interview["stage"], interview["stage"]),
        "language": interview["language"],
        "interviewer": interview.get("interviewer") or "(unknown)",
        "candidate_focus_notes": interview.get("focus") or "(none)",
    }


def prepare(interview, posting, profile, company, lessons, *, client=None, model=None):
    """Returns (Prep, usage). `company` is saved research notes or None; `lessons` is interviews.past_reviews()."""
    content = "\n\n".join([
        "Interview:\n" + json.dumps(_interview(interview), indent=2, ensure_ascii=False),
        "Candidate profile and CV:\n" + json.dumps(scoring.candidate(profile), indent=2, ensure_ascii=False),
        "Job posting:\n" + (posting.strip() or "(not available: say so in positioning and rely on the role title)"),
        "Company notes from earlier research:\n" + (json.dumps(company, indent=2, ensure_ascii=False) if company else "(none)"),
        "Past interview reviews (oldest first):\n" + (json.dumps(lessons, indent=2, ensure_ascii=False) if lessons else "(none yet)"),
    ])
    return _parse(client or scoring._client(), model, PREP_PROMPT, content, Prep, "interview prep")


def review(interview, transcript, metrics, prep, posting, profile, *, client=None, model=None):
    """Returns (Review, usage) with every score clamped to 1-5."""
    content = "\n\n".join([
        "Interview:\n" + json.dumps(_interview(interview), indent=2, ensure_ascii=False),
        "Candidate profile and CV:\n" + json.dumps(scoring.candidate(profile), indent=2, ensure_ascii=False),
        "Job posting:\n" + (posting.strip() or "(not available)"),
        "Prep brief the candidate had:\n" + (json.dumps(prep, indent=2, ensure_ascii=False) if prep else "(none)"),
        "Measured metrics for YOU:\n" + json.dumps(metrics, indent=2, ensure_ascii=False),
        "Transcript:\n" + interviews.render(transcript),
    ])
    system = REVIEW_PROMPT.format(dimensions="; ".join(f"{k} ({v})" for k, v in interviews.SCORES.items()))
    result, usage = _parse(client or scoring._client(), model, system, content, Review, "interview review")
    for key in interviews.SCORES:
        setattr(result.scores, key, min(5, max(1, getattr(result.scores, key))))
    return result, usage
