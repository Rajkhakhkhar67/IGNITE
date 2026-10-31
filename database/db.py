"""
SQLite database helpers for Learning Platform.

Stage 6 adds quizzes, questions, attempts, and answers for the
understanding measurement layer.
Stage 7 adds revision_activity, a lightweight history of what a student
has reviewed through the Smart Revision engine.
Stage 8 adds xp_events (append-only XP ledger with idempotency index)
and achievement_unlocks (persisted achievement state). Schema is
versioned so future stages can extend it safely.
Stage 9 adds lesson_transcript_segments (clickable video transcripts),
coding_exercises (per-lesson Python practice configured by the
instructor) and student_code (each student's saved playground code).
"""

import sqlite3
from pathlib import Path

from flask import current_app, g

# ---------------------------------------------------------------------------
# Schema definitions (each stage adds its own block)
# ---------------------------------------------------------------------------

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 1,
    initialized_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('student', 'instructor')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SCHEMA_V3 = """
-- Courses created by instructors
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instructor_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'beginner' CHECK(level IN ('beginner', 'intermediate', 'advanced')),
    thumbnail TEXT DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'published')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (instructor_id) REFERENCES users(id)
);

-- Modules within a course (ordered by position)
CREATE TABLE IF NOT EXISTS modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
);

-- Lessons within a module (ordered by position)
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    lesson_type TEXT NOT NULL DEFAULT 'text' CHECK(lesson_type IN ('video', 'text')),
    content_path TEXT DEFAULT NULL,
    content_text TEXT DEFAULT NULL,
    duration INTEGER DEFAULT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (module_id) REFERENCES modules(id) ON DELETE CASCADE
);

-- Student enrollments (unique per student+course)
CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    enrolled_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(student_id, course_id),
    FOREIGN KEY (student_id) REFERENCES users(id),
    FOREIGN KEY (course_id) REFERENCES courses(id)
);

-- Downloadable resources attached to lessons
CREATE TABLE IF NOT EXISTS lesson_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
);
"""

SCHEMA_V4 = """
-- Per-lesson progress for enrolled students
CREATE TABLE IF NOT EXISTS lesson_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    lesson_id INTEGER NOT NULL,
    last_position_seconds REAL NOT NULL DEFAULT 0,
    watched_seconds REAL NOT NULL DEFAULT 0,
    completion_percentage REAL NOT NULL DEFAULT 0,
    is_completed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_activity_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT DEFAULT NULL,
    UNIQUE(student_id, lesson_id),
    FOREIGN KEY (student_id) REFERENCES users(id),
    FOREIGN KEY (lesson_id) REFERENCES lessons(id)
);
"""

SCHEMA_V6 = """
-- Quizzes attached to lessons
CREATE TABLE IF NOT EXISTS quizzes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    passing_percentage REAL NOT NULL DEFAULT 50.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
);

-- Quiz questions (multiple_choice or true_false)
CREATE TABLE IF NOT EXISTS quiz_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER NOT NULL,
    question_text TEXT NOT NULL,
    question_type TEXT NOT NULL DEFAULT 'multiple_choice'
        CHECK(question_type IN ('multiple_choice', 'true_false')),
    option_a TEXT NOT NULL DEFAULT '',
    option_b TEXT NOT NULL DEFAULT '',
    option_c TEXT NOT NULL DEFAULT '',
    option_d TEXT NOT NULL DEFAULT '',
    correct_answer TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    concept_tag TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
);

-- Quiz attempts (one row per submission, never overwritten)
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    total_questions INTEGER NOT NULL DEFAULT 0,
    percentage REAL NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id)
);

-- Individual answers stored per attempt
CREATE TABLE IF NOT EXISTS quiz_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    selected_answer TEXT NOT NULL DEFAULT '',
    is_correct INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    FOREIGN KEY (question_id) REFERENCES quiz_questions(id) ON DELETE CASCADE
);
"""

SCHEMA_V5 = """
-- Timestamped notes on video lessons
CREATE TABLE IF NOT EXISTS video_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    lesson_id INTEGER NOT NULL,
    timestamp_seconds REAL NOT NULL DEFAULT 0,
    note_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (student_id) REFERENCES users(id),
    FOREIGN KEY (lesson_id) REFERENCES lessons(id)
);

-- Important moments and confusion markers on video lessons
CREATE TABLE IF NOT EXISTS video_markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    lesson_id INTEGER NOT NULL,
    timestamp_seconds REAL NOT NULL DEFAULT 0,
    marker_type TEXT NOT NULL CHECK(marker_type IN ('important', 'confused')),
    optional_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (student_id) REFERENCES users(id),
    FOREIGN KEY (lesson_id) REFERENCES lessons(id)
);
"""

SCHEMA_V7 = """
-- Stage 7: lightweight revision history.
-- Records WHAT a student reviewed via Smart Revision and WHEN.
-- Recommendations themselves are NEVER stored here -- they are always
-- recomputed in Python from current live data, so they can never go stale.
CREATE TABLE IF NOT EXISTS revision_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    lesson_id INTEGER DEFAULT NULL,
    concept_tag TEXT DEFAULT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN (
        'weak_concept', 'review_concept', 'confusion_marker',
        'important_marker', 'note', 'wrong_question', 'incomplete_lesson'
    )),
    source_id INTEGER DEFAULT NULL,
    reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (student_id) REFERENCES users(id),
    FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
);
"""

SCHEMA_V8 = """
-- Stage 8 Part C: XP event ledger.
-- Total XP is always SUM(xp_amount); there is no mutable counter.
-- The partial UNIQUE index makes every one-time award idempotent at the
-- database level: re-completing a lesson, re-passing a quiz, or
-- re-reviewing the same revision item can never mint duplicate XP.
-- quiz_improved references a specific attempt, so one attempt can only
-- ever be rewarded once even though improvements may happen repeatedly.
CREATE TABLE IF NOT EXISTS xp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'lesson_completed', 'quiz_passed', 'quiz_improved',
        'revision_reviewed', 'course_completed'
    )),
    reference_type TEXT NOT NULL DEFAULT '',
    reference_id INTEGER NOT NULL DEFAULT 0,
    xp_amount INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (student_id) REFERENCES users(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_xp_events_once
    ON xp_events(student_id, event_type, reference_type, reference_id);

-- Stage 8 Part C: persisted achievement unlocks.
-- UNIQUE(student_id, achievement_key) guarantees no duplicate unlocks.
CREATE TABLE IF NOT EXISTS achievement_unlocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    achievement_key TEXT NOT NULL,
    unlocked_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(student_id, achievement_key),
    FOREIGN KEY (student_id) REFERENCES users(id)
);
"""

SCHEMA_V9 = """
-- Stage 9: clickable transcript segments for VIDEO lessons.
-- Instructors add/edit/delete segments manually (no speech-to-text).
CREATE TABLE IF NOT EXISTS lesson_transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL,
    start_seconds REAL NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_transcript_lesson
    ON lesson_transcript_segments(lesson_id, position);

-- Stage 9: instructor-configured Python practice attached to ONE lesson.
-- expected_output is optional; when present the runner compares it with
-- the program's trimmed stdout for a friendly pass/fail hint.
CREATE TABLE IF NOT EXISTS coding_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL UNIQUE,
    instructions TEXT NOT NULL DEFAULT '',
    starter_code TEXT NOT NULL DEFAULT '',
    expected_output TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
);

-- Stage 9: each student's latest saved playground code per exercise.
CREATE TABLE IF NOT EXISTS student_code (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    exercise_id INTEGER NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    last_output TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(student_id, exercise_id),
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (exercise_id) REFERENCES coding_exercises(id) ON DELETE CASCADE
);
"""

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Return a SQLite connection for the current request."""
    if "db" not in g:
        db_path = current_app.config["DATABASE"]
        g.db = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exception=None) -> None:
    """Close the SQLite connection at the end of a request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_db(app) -> None:
    """
    Register database teardown and ensure the schema is up to date.
    Called once when the Flask application starts.
    Safe to run repeatedly -- existing data is never erased.
    """
    app.teardown_appcontext(close_db)

    with app.app_context():
        _ensure_schema(app.config["DATABASE"])


def _ensure_schema(db_path: str) -> None:
    """Create the database file and apply any pending schema migrations."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)

        cursor = connection.execute("SELECT version FROM schema_version WHERE id = 1")
        row = cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version < 2:
            connection.executescript(SCHEMA_V2)
            connection.execute("UPDATE schema_version SET version = 2 WHERE id = 1")

        if current_version < 3:
            connection.executescript(SCHEMA_V3)
            connection.execute("UPDATE schema_version SET version = 3 WHERE id = 1")

        if current_version < 4:
            connection.executescript(SCHEMA_V4)
            connection.execute("UPDATE schema_version SET version = 4 WHERE id = 1")

        if current_version < 5:
            connection.executescript(SCHEMA_V5)
            connection.execute("UPDATE schema_version SET version = 5 WHERE id = 1")

        if current_version < 6:
            connection.executescript(SCHEMA_V6)
            connection.execute("UPDATE schema_version SET version = 6 WHERE id = 1")

        if current_version < 7:
            connection.executescript(SCHEMA_V7)
            connection.execute("UPDATE schema_version SET version = 7 WHERE id = 1")

        if current_version < 8:
            connection.executescript(SCHEMA_V8)
            connection.execute("UPDATE schema_version SET version = 8 WHERE id = 1")

        if current_version < 9:
            connection.executescript(SCHEMA_V9)
            connection.execute("UPDATE schema_version SET version = 9 WHERE id = 1")

        connection.commit()
    finally:
        connection.close()
