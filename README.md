# Learning Platform

A local Flask and SQLite learning platform for a college project. It supports
the complete student learning loop: discover a course, enrol, learn from video
or text, practise, check understanding, review weak areas, and continue.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5001`. A brand-new database is populated once with the
demo catalog. Later restarts do not reseed or alter learning history.

Demo accounts:

| Role | Email | Password |
|---|---|---|
| Student | `student@demo.com` | `student123` |
| Instructor | `instructor@demo.com` | `instructor123` |

## What the project includes

- Flask, Jinja templates, vanilla JavaScript, and SQLite with additive,
  versioned migrations (schema version 9).
- Published and draft courses, ordered modules/lessons, enrolment, progress,
  course completion, and video resume.
- A local H.264 video lesson for **Python Fundamentals → Getting Started →
  Variables and Data Types**, with a synchronized clickable transcript.
- **My Moments**: private timestamped notes, Important markers, and Didn't
  Understand questions. They persist and seek the shared lesson player.
- Local Python practice using a bounded subprocess sandbox, saved work, reset,
  expected-output feedback, and no client-side fake execution.
- Lesson quizzes, stored attempts/answers, best-score statistics, and tagged
  concept performance.
- Learning Health, Smart Revision, Study Pack, printable notes/revision cards,
  XP, streaks, achievements, search, and privacy-safe instructor analytics.
- Instructor ownership checks for course, lesson, transcript, exercise, quiz,
  question, and resource mutations.

## Data and file model

SQLite stores users, courses, modules, lessons, enrollments, resources,
progress, notes, markers, quizzes/attempts/answers, revision activity, XP,
achievements, transcripts, exercises, and saved student code. Foreign keys are
enabled for every request. Uploads live locally in `uploads/videos`,
`uploads/resources`, and `uploads/thumbnails`; filenames are sanitized and
UUID-prefixed.

## Safety and limitations

- Passwords use Werkzeug hashing; queries are parameterized; routes verify
  authentication, enrolment, and instructor ownership.
- The Python runner has timeout, output, import, and subprocess safeguards. It
  is appropriate for this trusted local classroom demo, not production-grade
  untrusted-code isolation. Use OS/container isolation before public hosting.
- Learning Health and Smart Revision are deterministic rules based only on
  stored progress, quiz answers, notes, and markers. The project makes no AI
  claims and uses no external services.
- Local uploaded files are intentionally served by the app for this demo.

## Verification

Run the regression suite with:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_platform -v
```

The suite uses a temporary SQLite database and does not modify the real demo
database.

## License

College mini-project — educational use.
