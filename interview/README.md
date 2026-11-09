# Interview prep and review

Interview files and talk metrics, plus Claude prep briefs and transcript reviews. You use them from the dashboard's Interviews page or with `hunt.py interview-prep` and `hunt.py interview-review`.

## Files

| File | Purpose |
|---|---|
| `interviews.py` | Interview files, transcript parsing and talk metrics, shared by `hunt.py` and the dashboard |
| `coaching.py` | Claude interview prep and transcript review (structured output) |

## Interviews

Add an interview with **New interview…** on the Interviews page, or with **Prepare an interview…** in a researched company's details (which fills in the company and its best role). Give the company, role, stage (HR / recruiter screen, hiring manager, technical, final), date, interview language, and ideally the posting text: some job pages cannot be fetched, and the brief is only as specific as the posting. "What you want to emphasise" is passed on as is (for example a story to lead with, or your salary range).

- **Prep brief** (**Write prep…**, runs `hunt.py interview-prep`): one Claude call with your profile and CV, the posting (fetched from the posting URL through Exa, about $0.001, if you did not paste it), the company's saved research, and the findings of your last five reviewed interviews. The interview's page shows your positioning for this role, a 60–90-second "tell me about yourself" in the interview language, stories mapped to what the role needs, likely questions with answer points, questions to ask, how to handle salary, requirements against your experience, and gaps with honest ways to handle them. The brief's watch-outs (reminders drawn from your past reviews) are shown on the Interviews page, for your next interview that has a brief; without one, it shows the "next time" actions of your latest review. Claude is told to use only facts from your CV; where a number would help it writes `[add: …]` instead of inventing one. Rewriting replaces the brief.
- **Transcript**: upload a Wispr Flow meeting file (`~/Library/Application Support/Wispr Flow/meetings/<id>/refined.ndjson`; in the file picker press ⌘⇧G and paste the folder path) or paste text with one `Name: text` line per turn, optionally starting with `[mm:ss]`. Then choose which speaker is you. The page measures, for free: your share of the talking (by time when the transcript has timestamps, otherwise by words), the number and median length of your answers, your longest answer and when it started, and filler words per 100 words (English and Serbian lists; approximate, meant for tracking change). A one- to three-word reply from the other side ("okay") does not end your answer.
- **Review** (**Review the interview…**, runs `hunt.py interview-review`): one Claude call with the whole transcript, the measured numbers, the posting and the prep brief you had. It scores six things from 1 to 5 (structure, positioning for the role, evidence, confidence, career moves, questions asked), quotes what worked and what to improve with timestamps and what to say instead, and lists up to five things to do next time. Those "next time" items become the next prep brief's watch-outs. Uploading a new transcript marks the review as outdated until you review again.
- **Progress**: the Interviews page shows the latest value of each review score and of your talk share, longest answer and filler words, with a small bar per reviewed interview (oldest first) for the scores and talk share.

Each Claude call costs a few cents; the saved prep and review record the tokens used. Both send personal data to Anthropic: the prep sends your profile and CV, the review sends the whole transcript including the interviewer's words. Interviews live in `interviews/<id>/` (`interview.json`, `transcript.json`, `prep.json`, `review.json`; git-ignored and kept out of the Docker image). On Railway they are on the `/data` volume with your other files, so you can read the brief on your phone during the call.
