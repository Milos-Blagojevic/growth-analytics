import pytest

import interviews
from dashboard_data import INTERVIEW_TURNS, wispr


def test_wispr_flow_meetings_and_plain_text_become_turns():
    t = interviews.parse_transcript(wispr(*INTERVIEW_TURNS).encode(), "refined.ndjson")
    assert t["source"] == "wispr_flow" and t["me"] is None
    assert t["speakers"]["2"]["label"] == "Speaker 2" and t["speakers"]["2"]["words"] == 35
    assert t["speakers"]["1"]["sample"][0].startswith("Tell me about yourself")

    text = interviews.parse_transcript(
        "[00:05] Ana: Tell me about yourself.\nMilos: I started at PwC,\nthen moved to project44.\n"
        "Ana: Why now?\n[01:02:03] Milos: The pace was not sustainable."
    )
    assert text["source"] == "text"
    assert [(turn["t"], turn["speaker"], turn["text"]) for turn in text["turns"]] == [
        (5, "Ana", "Tell me about yourself."),
        (None, "Milos", "I started at PwC, then moved to project44."),
        (None, "Ana", "Why now?"),
        (3723, "Milos", "The pace was not sustainable."),
    ]

    with pytest.raises(ValueError, match="two speakers"):
        interviews.parse_transcript("Milos: one\nMilos: two\nMilos: three\nMilos: four")


def test_metrics_measure_answers_and_fillers_of_the_chosen_speaker():
    t = interviews.parse_transcript(wispr(*INTERVIEW_TURNS))
    assert interviews.metrics(t, "Serbian") is None  # nobody marked as you yet

    m = interviews.metrics(t | {"me": "2"}, "Serbian")
    # "Okay." at 01:10 is a backchannel: 00:10-03:00 is one answer of 170 s, the longest.
    assert (m["answers"], m["longest_answer_s"], m["longest_answer_at"]) == (2, 170, "0:10")
    assert m["fillers"] == {"mislim": 1, "ovaj": 1, "kako da kažem": 1}
    assert m["fillers_per_100_words"] == 8.6  # 3 fillers in 35 words
    assert (m["time_share"], m["duration_s"]) == (0.863, 197)  # 170 of 197 s; the last turn is estimated

    english = interviews.metrics(t | {"me": "2"}, "German")
    assert english["fillers"] == {} and english["fillers_per_100_words"] is None  # no filler list for the language

    untimed = interviews.parse_transcript("A: Hello there, how are you today?\nB: Fine thanks, and you?\n"
                                          "A: Good, let's start then.\nB: Sure, um, I mean, let's go.")
    m = interviews.metrics(untimed | {"me": "B"}, "English")
    assert (m["time_share"], m["longest_answer_s"], m["duration_s"]) == (None, None, None)
    assert m["fillers"] == {"um": 1, "i mean": 1}


def test_interviews_are_validated_and_get_unique_ids(tmp_path):
    fields = {"company": "SE Ranking", "role": "Senior Data Analyst", "stage": "hr",
              "scheduled_at": "2026-10-20T14:00", "domain": "https://www.SERanking.com/careers"}
    first = interviews.create(tmp_path, fields)
    second = interviews.create(tmp_path, fields)
    assert (first, second) == ("20261020-se-ranking-senior-data-analyst", "20261020-se-ranking-senior-data-analyst-2")
    saved = interviews.load_json(interviews.directory(tmp_path, first), interviews.INTERVIEW)
    assert (saved["domain"], saved["language"]) == ("seranking.com", "English")

    for bad, message in (({"role": ""}, "role is required"), ({"stage": "lunch"}, "stage"),
                         ({"scheduled_at": "next Tuesday"}, "date"), ({"posting_url": "seranking.com/jobs"}, "https://")):
        with pytest.raises(ValueError, match=message):
            interviews.create(tmp_path, fields | bad)
    for bad_id in ("../out", "nope"):
        with pytest.raises(LookupError):
            interviews.directory(tmp_path, bad_id)
