"""
Learning Platform — Flask application entry point.

Stage 7 adds Learning Health scoring and the Smart Revision engine
(recommendations computed live from real data, review activity recorded).
Stage 8 adds the Study Pack, printable notes / revision cards,
XP + levels + streaks + achievements, and instructor learning insights.

Python owns every calculation; JavaScript only handles browser events.
"""

import os
import re
import uuid
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_from_directory, abort, jsonify,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from database.db import init_db, get_db
from utils.auth import login_required, student_required, instructor_required
from utils.formatting import format_duration, format_timestamp
from utils import learning as learning
from utils import gamification as game
from utils import insights as analytics
from utils import playground as playground


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production"),
    DATABASE=os.path.join(app.root_path, "database", "learning_platform.db"),
    UPLOAD_FOLDER=os.path.join(app.root_path, "uploads"),
    MAX_CONTENT_LENGTH=200 * 1024 * 1024,  # 200 MB upload limit
)

# Allowed upload extensions
ALLOWED_VIDEO_EXT = {"mp4", "webm", "ogg"}
ALLOWED_RESOURCE_EXT = {"pdf", "txt", "py", "zip"}
ALLOWED_THUMBNAIL_EXT = {"jpg", "jpeg", "png", "webp"}

init_db(app)

# Ensure upload directories exist at startup
for sub in ("videos", "resources", "thumbnails"):
    os.makedirs(os.path.join(app.config["UPLOAD_FOLDER"], sub), exist_ok=True)

# Populate a brand-new local demo database once.  Later restarts deliberately
# do not reseed: learning history and uploads must never be silently changed.
with app.app_context():
    _existing_users = get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if _existing_users == 0:
        from database.seed_demo import seed_all
        seed_all(get_db(), app.config["UPLOAD_FOLDER"])


# ---------------------------------------------------------------------------
# Context processor
# ---------------------------------------------------------------------------

@app.context_processor
def inject_global_context():
    """Make current_user and current date available in every template."""
    user = None
    if "user_id" in session:
        full_name = session.get("user_name", "")
        first_name = full_name.split()[0] if full_name else ""
        user = {
            "id": session["user_id"],
            "name": full_name,
            "first_name": first_name,
            "role": session.get("role", ""),
        }
    return {"current_user": user, "now": datetime.now()}


@app.template_filter("duration")
def _jinja_duration_filter(seconds):
    """Human-readable duration from stored SECONDS: 420 -> '7 min'."""
    return format_duration(seconds)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


def _save_upload(file_storage, subfolder, allowed_ext):
    """
    Save an uploaded file with a safe unique name.
    Returns the relative path (e.g. 'videos/abc123_video.mp4') or None.
    """
    if not file_storage or file_storage.filename == "":
        return None
    filename = secure_filename(file_storage.filename)
    if "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in allowed_ext:
        return None
    unique_name = f"{uuid.uuid4().hex}_{filename}"
    dest = os.path.join(app.config["UPLOAD_FOLDER"], subfolder)
    os.makedirs(dest, exist_ok=True)
    file_storage.save(os.path.join(dest, unique_name))
    return f"{subfolder}/{unique_name}"


def _get_instructor_course(course_id):
    """Return a course row if it belongs to the logged-in instructor, else None."""
    db = get_db()
    return db.execute(
        "SELECT * FROM courses WHERE id = ? AND instructor_id = ?",
        (course_id, session["user_id"]),
    ).fetchone()


def _get_course_modules(course_id):
    """Return all modules for a course, each with its lessons attached."""
    db = get_db()
    modules = db.execute(
        "SELECT * FROM modules WHERE course_id = ? ORDER BY position, id",
        (course_id,),
    ).fetchall()
    result = []
    for mod in modules:
        lessons = db.execute(
            "SELECT * FROM lessons WHERE module_id = ? ORDER BY position, id",
            (mod["id"],),
        ).fetchall()
        result.append({"module": mod, "lessons": lessons})
    return result


def _count_lessons(course_id):
    """Return total lesson count for a course."""
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchone()
    return row[0]


def _next_position(db, table, column, parent_id, parent_column):
    """Calculate the next position value for an ordered child record."""
    row = db.execute(
        f"SELECT COALESCE(MAX({column}), 0) FROM {table} WHERE {parent_column} = ?",
        (parent_id,),
    ).fetchone()
    return row[0] + 1


# ---------------------------------------------------------------------------
# Progress helpers (Stage 4)
# ---------------------------------------------------------------------------

def _get_lesson_progress(db, student_id, lesson_id):
    """Get or create a lesson progress record for a student."""
    progress = db.execute(
        "SELECT * FROM lesson_progress WHERE student_id = ? AND lesson_id = ?",
        (student_id, lesson_id),
    ).fetchone()
    if not progress:
        db.execute(
            "INSERT INTO lesson_progress (student_id, lesson_id) VALUES (?, ?)",
            (student_id, lesson_id),
        )
        db.commit()
        progress = db.execute(
            "SELECT * FROM lesson_progress WHERE student_id = ? AND lesson_id = ?",
            (student_id, lesson_id),
        ).fetchone()
    return progress


def _calculate_course_progress(db, student_id, course_id):
    """Calculate course progress as completed/total lessons."""
    total = db.execute(
        "SELECT COUNT(*) FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchone()[0]
    if total == 0:
        return {"total": 0, "completed": 0, "percentage": 0}
    completed = db.execute(
        "SELECT COUNT(*) FROM lesson_progress lp "
        "JOIN lessons l ON lp.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND lp.student_id = ? AND lp.is_completed = 1",
        (course_id, student_id),
    ).fetchone()[0]
    return {"total": total, "completed": completed, "percentage": round(completed / total * 100)}


def _calculate_module_progress(db, student_id, module_id):
    """Calculate module progress as completed/total lessons."""
    total = db.execute(
        "SELECT COUNT(*) FROM lessons WHERE module_id = ?", (module_id,)
    ).fetchone()[0]
    if total == 0:
        return {"total": 0, "completed": 0, "percentage": 0}
    completed = db.execute(
        "SELECT COUNT(*) FROM lesson_progress lp "
        "JOIN lessons l ON lp.lesson_id = l.id "
        "WHERE l.module_id = ? AND lp.student_id = ? AND lp.is_completed = 1",
        (module_id, student_id),
    ).fetchone()[0]
    return {"total": total, "completed": completed, "percentage": round(completed / total * 100)}


def _get_continue_lesson(db, student_id, course_id):
    """Find the best lesson to resume: last active incomplete, then first incomplete, then first."""
    lesson = db.execute(
        "SELECT l.id, l.module_id, l.title, l.lesson_type, l.duration "
        "FROM lesson_progress lp "
        "JOIN lessons l ON lp.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND lp.student_id = ? AND lp.is_completed = 0 "
        "ORDER BY lp.last_activity_at DESC LIMIT 1",
        (course_id, student_id),
    ).fetchone()
    if lesson:
        return lesson
    lesson = db.execute(
        "SELECT l.id, l.module_id, l.title, l.lesson_type, l.duration "
        "FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? "
        "AND l.id NOT IN ("
        "  SELECT lp.lesson_id FROM lesson_progress lp "
        "  WHERE lp.student_id = ? AND lp.is_completed = 1"
        ") "
        "ORDER BY m.position, l.position LIMIT 1",
        (course_id, student_id),
    ).fetchone()
    if lesson:
        return lesson
    lesson = db.execute(
        "SELECT l.id, l.module_id, l.title, l.lesson_type, l.duration "
        "FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? "
        "ORDER BY m.position, l.position LIMIT 1",
        (course_id,),
    ).fetchone()
    return lesson


def _get_lesson_progress_map(db, student_id, course_id):
    """Return dict of lesson_id -> progress row for all lessons in a course."""
    rows = db.execute(
        "SELECT lp.* FROM lesson_progress lp "
        "JOIN lessons l ON lp.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND lp.student_id = ?",
        (course_id, student_id),
    ).fetchall()
    return {row["lesson_id"]: row for row in rows}


# ---------------------------------------------------------------------------
# Timestamp & verification helpers (Stage 5)
# ---------------------------------------------------------------------------

def _format_timestamp(seconds):
    """Format numeric seconds into MM:SS or H:MM:SS display."""
    return format_timestamp(seconds)


def _verify_student_lesson_access(student_id, lesson_id):
    """
    Verify a student can access a lesson: lesson exists, is in a published course,
    and student is enrolled. Returns (lesson_row, course_id) or raises (None, None).
    lesson_row includes 'course_id' from the JOIN.
    """
    db = get_db()
    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "JOIN courses c ON m.course_id = c.id "
        "WHERE l.id = ? AND c.status = 'published'",
        (lesson_id,),
    ).fetchone()
    if not lesson:
        return None, None
    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, lesson["course_id"]),
    ).fetchone()
    if not enrolled:
        return None, None
    return lesson, lesson["course_id"]


def _get_student_note_counts(db, student_id):
    """Return dict of note/marker counts for dashboard."""
    notes = db.execute(
        "SELECT COUNT(*) FROM video_notes WHERE student_id = ?", (student_id,)
    ).fetchone()[0]
    important = db.execute(
        "SELECT COUNT(*) FROM video_markers WHERE student_id = ? AND marker_type = 'important'",
        (student_id,),
    ).fetchone()[0]
    confused = db.execute(
        "SELECT COUNT(*) FROM video_markers WHERE student_id = ? AND marker_type = 'confused'",
        (student_id,),
    ).fetchone()[0]
    return {"notes": notes, "important": important, "confused": confused}


def _get_student_enrolled_course(student_id, course_id):
    """
    Return the course row if the student is enrolled in this PUBLISHED
    course, else None. Used by health / revision / study-pack routes.
    """
    db = get_db()
    return db.execute(
        "SELECT c.* FROM courses c "
        "JOIN enrollments e ON e.course_id = c.id "
        "WHERE c.id = ? AND e.student_id = ? AND c.status = 'published'",
        (course_id, student_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# Quiz helpers (Stage 6)
# ---------------------------------------------------------------------------

def _get_quiz_for_lesson(db, lesson_id):
    """Return the quiz attached to a lesson, or None."""
    return db.execute(
        "SELECT * FROM quizzes WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()


def _get_quiz_with_stats(db, quiz_id):
    """Return quiz row with question_count appended."""
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if not quiz:
        return None
    q = dict(quiz)
    q["question_count"] = db.execute(
        "SELECT COUNT(*) FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)
    ).fetchone()[0]
    return q


def _verify_instructor_quiz_ownership(instructor_id, quiz_id):
    """
    Verify the quiz belongs to a lesson in a course owned by this instructor.
    Returns (quiz_row, lesson_row, course_id) or (None, None, None).
    """
    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if not quiz:
        return None, None, None
    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE l.id = ?",
        (quiz["lesson_id"],),
    ).fetchone()
    if not lesson:
        return None, None, None
    course = db.execute(
        "SELECT id FROM courses WHERE id = ? AND instructor_id = ?",
        (lesson["course_id"], instructor_id),
    ).fetchone()
    if not course:
        return None, None, None
    return quiz, lesson, lesson["course_id"]


def _verify_instructor_question_ownership(instructor_id, question_id):
    """
    Verify the question belongs to a quiz owned by this instructor.
    Returns (question_row, quiz_row, lesson_row, course_id) or (None, None, None, None).
    """
    db = get_db()
    question = db.execute(
        "SELECT * FROM quiz_questions WHERE id = ?", (question_id,)
    ).fetchone()
    if not question:
        return None, None, None, None
    quiz, lesson, course_id = _verify_instructor_quiz_ownership(
        instructor_id, question["quiz_id"]
    )
    if not quiz:
        return None, None, None, None
    return question, quiz, lesson, course_id


def _get_instructor_lesson(lesson_id):
    """
    Ownership chain helper: verify a lesson belongs to a module that
    belongs to a course owned by the logged-in instructor.
    Returns (course_row, module_row, lesson_row) or (None, None, None).
    """
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        return None, None, None
    mod = db.execute(
        "SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)
    ).fetchone()
    if not mod:
        return None, None, None
    course = _get_instructor_course(mod["course_id"])
    if not course:
        return None, None, None
    return course, mod, lesson


def _safe_unlink(rel_path):
    """
    Delete an uploaded file referenced by a relative path under the
    uploads folder. Guards against path traversal: the resolved path
    must stay inside UPLOAD_FOLDER or nothing is touched.
    """
    if not rel_path:
        return
    upload_root = os.path.abspath(app.config["UPLOAD_FOLDER"])
    target = os.path.abspath(os.path.join(upload_root, rel_path))
    try:
        if os.path.commonpath([upload_root, target]) != upload_root:
            return  # traversal attempt — refuse silently
    except ValueError:
        return
    try:
        if os.path.isfile(target):
            os.remove(target)
    except OSError:
        pass


def _delete_lesson_files(lesson):
    """Remove a lesson's video file and every resource file it owns."""
    _safe_unlink(lesson["content_path"])
    db = get_db()
    for res in db.execute(
        "SELECT file_path FROM lesson_resources WHERE lesson_id = ?",
        (lesson["id"],),
    ).fetchall():
        _safe_unlink(res["file_path"])


def _purge_lesson_rows(lesson_id):
    """
    Delete student data attached to one lesson that has no ON DELETE
    CASCADE foreign keys (notes, markers, progress). Safe to call even
    when rows are already gone.
    """
    db = get_db()
    db.execute("DELETE FROM video_notes WHERE lesson_id = ?", (lesson_id,))
    db.execute("DELETE FROM video_markers WHERE lesson_id = ?", (lesson_id,))
    db.execute("DELETE FROM lesson_progress WHERE lesson_id = ?", (lesson_id,))


def _get_student_quiz_stats(db, student_id, quiz_id):
    """
    Return attempt stats for a student on a quiz:
    attempt_count, latest_score, best_score, passed
    """
    attempts = db.execute(
        "SELECT * FROM quiz_attempts "
        "WHERE student_id = ? AND quiz_id = ? "
        "ORDER BY completed_at DESC",
        (student_id, quiz_id),
    ).fetchall()
    if not attempts:
        return {"attempt_count": 0, "latest_score": None, "best_score": None, "passed": False}
    latest = attempts[0]
    best = max(attempts, key=lambda a: a["percentage"])
    any_passed = any(a["passed"] for a in attempts)
    return {
        "attempt_count": len(attempts),
        "latest_score": latest["percentage"],
        "best_score": best["percentage"],
        "passed": any_passed,
    }


def _calculate_concept_performance(db, student_id, course_id=None):
    """
    Calculate per-concept accuracy for a student.
    Optionally filter by course.
    Returns list of dicts:
        {concept_tag, correct, total, accuracy}
    sorted by accuracy ascending (weakest first).
    """
    db_conn = get_db()
    query = (
        "SELECT qa.is_correct, qq.concept_tag "
        "FROM quiz_answers qa "
        "JOIN quiz_questions qq ON qa.question_id = qq.id "
        "JOIN quiz_attempts qat ON qa.attempt_id = qat.id "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE qat.student_id = ? AND qq.concept_tag != '' "
    )
    params = [student_id]
    if course_id:
        query += "AND m.course_id = ? "
        params.append(course_id)
    query += "ORDER BY qq.concept_tag"

    rows = db_conn.execute(query, params).fetchall()

    concepts = {}
    for row in rows:
        tag = row["concept_tag"]
        if tag not in concepts:
            concepts[tag] = {"concept_tag": tag, "correct": 0, "total": 0}
        concepts[tag]["total"] += 1
        if row["is_correct"]:
            concepts[tag]["correct"] += 1

    result = []
    for tag, data in concepts.items():
        data["accuracy"] = round(data["correct"] / data["total"] * 100, 1) if data["total"] > 0 else 0
        result.append(data)

    result.sort(key=lambda x: x["accuracy"])
    return result


def _calculate_question_weakness(db, student_id, course_id=None):
    """
    Identify questions a student repeatedly answers incorrectly.
    Returns list of dicts:
        {question_id, question_text, concept_tag, correct, incorrect, total}
    sorted by incorrect count descending (most problematic first).
    """
    db_conn = get_db()
    query = (
        "SELECT qq.id AS question_id, qq.question_text, qq.concept_tag, "
        "qa.is_correct "
        "FROM quiz_answers qa "
        "JOIN quiz_questions qq ON qa.question_id = qq.id "
        "JOIN quiz_attempts qat ON qa.attempt_id = qat.id "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE qat.student_id = ? "
    )
    params = [student_id]
    if course_id:
        query += "AND m.course_id = ? "
        params.append(course_id)

    rows = db_conn.execute(query, params).fetchall()

    questions = {}
    for row in rows:
        qid = row["question_id"]
        if qid not in questions:
            questions[qid] = {
                "question_id": qid,
                "question_text": row["question_text"],
                "concept_tag": row["concept_tag"],
                "correct": 0,
                "incorrect": 0,
                "total": 0,
            }
        questions[qid]["total"] += 1
        if row["is_correct"]:
            questions[qid]["correct"] += 1
        else:
            questions[qid]["incorrect"] += 1

    result = list(questions.values())
    result.sort(key=lambda x: x["incorrect"], reverse=True)
    return result


def _get_course_quiz_summary(db, student_id, course_id):
    """
    Return quiz summary for a course: total quizzes, attempted, and
    answer-level accuracy.  Accuracy deliberately uses the same canonical
    answer rows as concept performance and Learning Health, so repeated quiz
    attempts cannot make the course summary contradict those surfaces.
    """
    total_quizzes = db.execute(
        "SELECT COUNT(*) FROM quizzes qz "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchone()[0]

    if total_quizzes == 0:
        return {"total_quizzes": 0, "attempted": 0, "average_score": None}

    attempts = db.execute(
        "SELECT DISTINCT qz.id "
        "FROM quiz_attempts qat "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND qat.student_id = ? ",
        (course_id, student_id),
    ).fetchall()

    attempted = len(attempts)
    accuracy = learning.aggregate_answer_accuracy(
        db, student_id=student_id, course_id=course_id
    )["accuracy"]

    return {"total_quizzes": total_quizzes, "attempted": attempted,
            "average_score": accuracy}


# ---------------------------------------------------------------------------
# Demo content seeding (FINAL BACKEND PROMPT 2)
# ---------------------------------------------------------------------------
# Idempotent, restart-safe population of the demo catalog and the demo
# students' learning history. Runs once per boot; every step is guarded
# by stable natural-key lookups. See database/seed_demo.py.

with app.app_context():
    from database.seed_demo import run_startup_seeds
    run_startup_seeds()


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Authentication (unchanged from Stage 2)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "")

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not email:
            errors.append("Email is required.")
        elif not _is_valid_email(email):
            errors.append("Please enter a valid email address.")
        if not password:
            errors.append("Password is required.")
        elif len(password) < 6:
            errors.append("Password must be at least 6 characters long.")
        if password != confirm_password:
            errors.append("Passwords do not match.")
        if role not in ("student", "instructor"):
            errors.append("Please select a valid role.")

        if email and not errors:
            db = get_db()
            if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
                errors.append("An account with this email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", page_title="Register")

        db = get_db()
        db.execute(
            "INSERT INTO users (full_name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (full_name, email, generate_password_hash(password), role),
        )
        db.commit()
        flash("Account created successfully! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", page_title="Register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        if session.get("role") == "instructor":
            return redirect(url_for("instructor_courses"))
        return redirect(url_for("home"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter both email and password.", "error")
            return render_template("login.html", page_title="Log In")

        db = get_db()
        user = db.execute(
            "SELECT id, full_name, password_hash, role FROM users WHERE email = ?",
            (email,),
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", page_title="Log In")

        session["user_id"] = user["id"]
        session["user_name"] = user["full_name"]
        session["role"] = user["role"]

        flash(f"Welcome back, {user['full_name'].split()[0]}!", "success")
        if user["role"] == "instructor":
            return redirect(url_for("instructor_courses"))
        return redirect(url_for("home"))

    return render_template("login.html", page_title="Log In")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Student dashboard
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
@student_required
def home():
    db = get_db()
    student_id = session["user_id"]

    # Find the most recently active enrolled course
    active_enrollment = db.execute(
        "SELECT c.*, u.full_name AS instructor_name "
        "FROM enrollments e "
        "JOIN courses c ON e.course_id = c.id "
        "JOIN users u ON c.instructor_id = u.id "
        "WHERE e.student_id = ? "
        "ORDER BY e.enrolled_at DESC LIMIT 1",
        (student_id,),
    ).fetchone()

    continue_data = None
    if active_enrollment:
        progress = _calculate_course_progress(db, student_id, active_enrollment["id"])
        continue_lesson = _get_continue_lesson(db, student_id, active_enrollment["id"])
        continue_data = {
            "course": dict(active_enrollment),
            "progress": progress,
            "lesson": dict(continue_lesson) if continue_lesson else None,
        }

    return render_template(
        "index.html",
        page_title="Dashboard",
        continue_data=continue_data,
        note_counts=_get_student_note_counts(db, student_id),
        game_state=game.gamification_snapshot(db, student_id),
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Instructor course management
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/instructor")
@instructor_required
def instructor_dashboard():
    return redirect(url_for("instructor_courses"))


@app.route("/instructor/courses")
@instructor_required
def instructor_courses():
    """List courses owned by the current instructor."""
    db = get_db()
    courses = db.execute(
        "SELECT * FROM courses WHERE instructor_id = ? ORDER BY updated_at DESC",
        (session["user_id"],),
    ).fetchall()
    # Attach module/lesson counts
    course_list = []
    for c in courses:
        mod_count = db.execute(
            "SELECT COUNT(*) FROM modules WHERE course_id = ?", (c["id"],)
        ).fetchone()[0]
        les_count = _count_lessons(c["id"])
        course_list.append({**dict(c), "module_count": mod_count, "lesson_count": les_count})
    return render_template(
        "instructor_courses.html",
        page_title="My Courses",
        courses=course_list,
    )


@app.route("/instructor/courses/new", methods=["GET", "POST"])
@instructor_required
def instructor_course_new():
    """Create a new course."""
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        level = request.form.get("level", "beginner")

        if not title:
            flash("Course title is required.", "error")
            return render_template("instructor_course_form.html", page_title="New Course", editing=False)
        if level not in ("beginner", "intermediate", "advanced"):
            level = "beginner"

        # Handle thumbnail upload
        thumbnail = None
        if "thumbnail" in request.files:
            thumbnail = _save_upload(request.files["thumbnail"], "thumbnails", ALLOWED_THUMBNAIL_EXT)

        db = get_db()
        db.execute(
            "INSERT INTO courses (instructor_id, title, description, category, level, thumbnail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session["user_id"], title, description, category, level, thumbnail),
        )
        db.commit()
        flash("Course created successfully!", "success")
        return redirect(url_for("instructor_courses"))

    return render_template("instructor_course_form.html", page_title="New Course", editing=False)


@app.route("/instructor/courses/<int:course_id>")
@instructor_required
def instructor_course_detail(course_id):
    """View course modules and lessons."""
    course = _get_instructor_course(course_id)
    if not course:
        abort(404)
    modules_data = _get_course_modules(course_id)
    total_lessons = _count_lessons(course_id)

    # Attach quiz info + resource lists to lessons
    db = get_db()
    lesson_quiz_map = {}
    for item in modules_data:
        enriched = []
        for l in item["lessons"]:
            ld = dict(l)
            q = _get_quiz_for_lesson(db, ld["id"])
            if q:
                lesson_quiz_map[ld["id"]] = q
            ld["resources"] = db.execute(
                "SELECT * FROM lesson_resources WHERE lesson_id = ? ORDER BY title",
                (ld["id"],),
            ).fetchall()
            enriched.append(ld)
        item["lessons"] = enriched

    return render_template(
        "instructor_course_detail.html",
        page_title=course["title"],
        course=course,
        modules_data=modules_data,
        total_lessons=total_lessons,
        lesson_quiz_map=lesson_quiz_map,
    )


@app.route("/instructor/courses/<int:course_id>/insights")
@instructor_required
def instructor_course_insights(course_id):
    """
    Stage 8 Part D: aggregated learning insights for ONE owned course.

    Every number is computed by utils/insights.py from real learning
    behaviour. Privacy: student-authored free text (notes, marker text)
    never reaches this page -- only counts, percentages and timestamps.
    """
    course = _get_instructor_course(course_id)
    if not course:
        abort(404)
    db = get_db()

    overview = analytics.course_overview(db, course_id)
    concepts = analytics.concept_difficulty(db, course_id)
    questions = analytics.question_difficulty(db, course_id)
    hotspots = analytics.confusion_hotspots(db, course_id)
    confused_lessons = analytics.lesson_confusion(db, course_id)
    lesson_rows = analytics.lesson_analytics(db, course_id)

    # Attach lesson titles to hotspots for display.
    titles = {r["lesson_id"]: r["title"] for r in lesson_rows}
    for h in hotspots:
        h["lesson_title"] = titles.get(h["lesson_id"], f"Lesson #{h['lesson_id']}")

    def _fmt_ts(seconds):
        return _format_timestamp(seconds) if seconds is not None else "--"

    return render_template(
        "instructor_insights.html",
        page_title=f"Insights · {course['title']}",
        course=course,
        overview=overview,
        concepts=concepts,
        questions=questions,
        hotspots=hotspots,
        confused_lessons=confused_lessons,
        lesson_rows=lesson_rows,
        fmt_ts=_fmt_ts,
    )


@app.route("/instructor/courses/<int:course_id>/edit", methods=["GET", "POST"])
@instructor_required
def instructor_course_edit(course_id):
    """Edit course information."""
    course = _get_instructor_course(course_id)
    if not course:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        level = request.form.get("level", "beginner")

        if not title:
            flash("Course title is required.", "error")
            return render_template("instructor_course_form.html", page_title="Edit Course", editing=True, course=course)
        if level not in ("beginner", "intermediate", "advanced"):
            level = "beginner"

        # Handle optional new thumbnail
        thumbnail = course["thumbnail"]
        if "thumbnail" in request.files and request.files["thumbnail"].filename:
            new_thumb = _save_upload(request.files["thumbnail"], "thumbnails", ALLOWED_THUMBNAIL_EXT)
            if new_thumb:
                thumbnail = new_thumb

        db = get_db()
        db.execute(
            "UPDATE courses SET title=?, description=?, category=?, level=?, "
            "thumbnail=?, updated_at=datetime('now') WHERE id=?",
            (title, description, category, level, thumbnail, course_id),
        )
        db.commit()
        flash("Course updated.", "success")
        return redirect(url_for("instructor_course_detail", course_id=course_id))

    return render_template("instructor_course_form.html", page_title="Edit Course", editing=True, course=course)


@app.route("/instructor/courses/<int:course_id>/publish", methods=["POST"])
@instructor_required
def instructor_course_publish(course_id):
    """Toggle course between draft and published."""
    course = _get_instructor_course(course_id)
    if not course:
        abort(404)
    new_status = "published" if course["status"] == "draft" else "draft"
    db = get_db()
    db.execute(
        "UPDATE courses SET status=?, updated_at=datetime('now') WHERE id=?",
        (new_status, course_id),
    )
    db.commit()
    flash(f"Course {'published' if new_status == 'published' else 'unpublished'}.", "success")
    return redirect(url_for("instructor_course_detail", course_id=course_id))


# ---------------------------------------------------------------------------
# Module management
# ---------------------------------------------------------------------------

@app.route("/instructor/courses/<int:course_id>/modules/new", methods=["GET", "POST"])
@instructor_required
def instructor_module_new(course_id):
    course = _get_instructor_course(course_id)
    if not course:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        if not title:
            flash("Module title is required.", "error")
            return render_template("instructor_module_form.html", page_title="New Module", course=course, editing=False)

        db = get_db()
        pos = _next_position(db, "modules", "position", course_id, "course_id")
        db.execute(
            "INSERT INTO modules (course_id, title, description, position) VALUES (?, ?, ?, ?)",
            (course_id, title, description, pos),
        )
        db.commit()
        flash("Module added.", "success")
        return redirect(url_for("instructor_course_detail", course_id=course_id))

    return render_template("instructor_module_form.html", page_title="New Module", course=course, editing=False)


@app.route("/instructor/modules/<int:module_id>/edit", methods=["GET", "POST"])
@instructor_required
def instructor_module_edit(module_id):
    db = get_db()
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (module_id,)).fetchone()
    if not mod:
        abort(404)
    course = _get_instructor_course(mod["course_id"])
    if not course:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        position = request.form.get("position", mod["position"])
        try:
            position = int(position)
        except (ValueError, TypeError):
            position = mod["position"]

        if not title:
            flash("Module title is required.", "error")
            return render_template("instructor_module_form.html", page_title="Edit Module", course=course, module=mod, editing=True)

        db.execute(
            "UPDATE modules SET title=?, description=?, position=? WHERE id=?",
            (title, description, position, module_id),
        )
        db.commit()
        flash("Module updated.", "success")
        return redirect(url_for("instructor_course_detail", course_id=course["id"]))

    return render_template("instructor_module_form.html", page_title="Edit Module", course=course, module=mod, editing=True)


# ---------------------------------------------------------------------------
# Lesson management
# ---------------------------------------------------------------------------

@app.route("/instructor/modules/<int:module_id>/lessons/new", methods=["GET", "POST"])
@instructor_required
def instructor_lesson_new(module_id):
    db = get_db()
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (module_id,)).fetchone()
    if not mod:
        abort(404)
    course = _get_instructor_course(mod["course_id"])
    if not course:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        lesson_type = request.form.get("lesson_type", "text")
        content_text = request.form.get("content_text", "").strip()
        duration = request.form.get("duration", "").strip()

        if not title:
            flash("Lesson title is required.", "error")
            return render_template("instructor_lesson_form.html", page_title="New Lesson", course=course, module=mod, editing=False)
        if lesson_type not in ("video", "text"):
            lesson_type = "text"

        content_path = None
        if lesson_type == "video":
            if "video_file" in request.files and request.files["video_file"].filename:
                content_path = _save_upload(request.files["video_file"], "videos", ALLOWED_VIDEO_EXT)
                if not content_path:
                    flash("Invalid video file. Allowed: MP4, WebM, OGG.", "error")
                    return render_template("instructor_lesson_form.html", page_title="New Lesson", course=course, module=mod, editing=False)
            else:
                flash("Please select a video file for video lessons.", "error")
                return render_template("instructor_lesson_form.html", page_title="New Lesson", course=course, module=mod, editing=False)

        duration_val = None
        if duration:
            try:
                # CMS form collects MINUTES; the database stores SECONDS.
                duration_val = int(round(float(duration) * 60))
                if duration_val <= 0:
                    duration_val = None
            except ValueError:
                pass

        pos = _next_position(db, "lessons", "position", module_id, "module_id")
        db.execute(
            "INSERT INTO lessons (module_id, title, description, lesson_type, content_path, content_text, duration, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (module_id, title, description, lesson_type, content_path, content_text or None, duration_val, pos),
        )
        db.commit()
        flash("Lesson added.", "success")
        return redirect(url_for("instructor_course_detail", course_id=course["id"]))

    return render_template("instructor_lesson_form.html", page_title="New Lesson", course=course, module=mod, editing=False)


@app.route("/instructor/lessons/<int:lesson_id>/edit", methods=["GET", "POST"])
@instructor_required
def instructor_lesson_edit(lesson_id):
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()
    course = _get_instructor_course(mod["course_id"]) if mod else None
    if not course:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        lesson_type = request.form.get("lesson_type", lesson["lesson_type"])
        content_text = request.form.get("content_text", "").strip()
        duration = request.form.get("duration", "").strip()
        position = request.form.get("position", lesson["position"])

        if not title:
            flash("Lesson title is required.", "error")
            return render_template("instructor_lesson_form.html", page_title="Edit Lesson", course=course, module=mod, lesson=lesson, editing=True)
        if lesson_type not in ("video", "text"):
            lesson_type = lesson["lesson_type"]

        # ---- Video file handling (replace / keep / require / drop) ----
        old_path = lesson["content_path"]
        upload = request.files.get("video_file")
        has_new_file = upload is not None and upload.filename != ""

        if lesson_type == "video":
            if has_new_file:
                saved = _save_upload(upload, "videos", ALLOWED_VIDEO_EXT)
                if not saved:
                    flash("Invalid video file. Allowed: MP4, WebM, OGG.", "error")
                    return render_template("instructor_lesson_form.html", page_title="Edit Lesson", course=course, module=mod, lesson=lesson, editing=True)
                if old_path and old_path != saved:
                    _safe_unlink(old_path)   # replaced video: no orphan files
                content_path = saved
            elif old_path and lesson["lesson_type"] == "video":
                content_path = old_path      # keep the existing attachment
            else:
                flash("A video file is required for video lessons. "
                      "Choose an MP4/WebM/OGG file to attach.", "error")
                return render_template("instructor_lesson_form.html", page_title="Edit Lesson", course=course, module=mod, lesson=lesson, editing=True)
            video_attached_now = content_path is not None
        else:
            # Switching to text: the old video file must not linger orphaned
            # on disk or silently stay attached to a text lesson.
            content_path = None
            if old_path:
                _safe_unlink(old_path)
            video_attached_now = False

        duration_val = None
        if duration:
            try:
                # CMS form collects MINUTES; the database stores SECONDS.
                duration_val = int(round(float(duration) * 60))
                if duration_val <= 0:
                    duration_val = None
            except ValueError:
                pass
        try:
            position = int(position)
        except (ValueError, TypeError):
            position = lesson["position"]

        db.execute(
            "UPDATE lessons SET title=?, description=?, lesson_type=?, content_path=?, "
            "content_text=?, duration=?, position=? WHERE id=?",
            (title, description, lesson_type, content_path, content_text or None, duration_val, position, lesson_id),
        )
        db.commit()
        if lesson_type == "video" and video_attached_now:
            flash("Lesson updated. Video attached.", "success")
        else:
            flash("Lesson updated.", "success")
        return redirect(url_for("instructor_course_detail", course_id=course["id"]))

    return render_template("instructor_lesson_form.html", page_title="Edit Lesson", course=course, module=mod, lesson=lesson, editing=True)


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Instructor Quiz Management (Stage 6)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/instructor/lessons/<int:lesson_id>/quiz/new", methods=["GET", "POST"])
@instructor_required
def instructor_quiz_new(lesson_id):
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()
    course = _get_instructor_course(mod["course_id"]) if mod else None
    if not course:
        abort(404)

    existing = _get_quiz_for_lesson(db, lesson_id)
    if existing:
        flash("This lesson already has a quiz. Edit it instead.", "info")
        return redirect(url_for("instructor_quiz_edit", quiz_id=existing["id"]))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        passing = request.form.get("passing_percentage", "50").strip()

        if not title:
            flash("Quiz title is required.", "error")
            return render_template("instructor_quiz_form.html",
                                   page_title="New Quiz", course=course, module=mod,
                                   lesson=lesson, editing=False)

        try:
            passing = float(passing)
            if passing < 0 or passing > 100:
                passing = 50.0
        except (ValueError, TypeError):
            passing = 50.0

        cursor = db.execute(
            "INSERT INTO quizzes (lesson_id, title, description, passing_percentage) "
            "VALUES (?, ?, ?, ?)",
            (lesson_id, title, description, passing),
        )
        db.commit()
        flash("Quiz created. Now add questions.", "success")
        return redirect(url_for("instructor_quiz_edit", quiz_id=cursor.lastrowid))

    return render_template("instructor_quiz_form.html",
                           page_title="New Quiz", course=course, module=mod,
                           lesson=lesson, editing=False)


@app.route("/instructor/quizzes/<int:quiz_id>/edit", methods=["GET", "POST"])
@instructor_required
def instructor_quiz_edit(quiz_id):
    quiz, lesson, course_id = _verify_instructor_quiz_ownership(session["user_id"], quiz_id)
    if not quiz:
        abort(404)

    db = get_db()
    course = _get_instructor_course(course_id)
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()

    questions = db.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY position, id",
        (quiz_id,),
    ).fetchall()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        passing = request.form.get("passing_percentage", "50").strip()

        if not title:
            flash("Quiz title is required.", "error")
            return render_template("instructor_quiz_form.html",
                                   page_title="Edit Quiz", course=course, module=mod,
                                   lesson=lesson, quiz=quiz, questions=questions, editing=True)

        try:
            passing = float(passing)
            if passing < 0 or passing > 100:
                passing = 50.0
        except (ValueError, TypeError):
            passing = 50.0

        db.execute(
            "UPDATE quizzes SET title=?, description=?, passing_percentage=?, "
            "updated_at=datetime('now') WHERE id=?",
            (title, description, passing, quiz_id),
        )
        db.commit()
        flash("Quiz updated.", "success")
        return redirect(url_for("instructor_quiz_edit", quiz_id=quiz_id))

    return render_template("instructor_quiz_form.html",
                           page_title="Edit Quiz", course=course, module=mod,
                           lesson=lesson, quiz=quiz, questions=questions, editing=True)


@app.route("/instructor/quizzes/<int:quiz_id>/questions/new", methods=["GET", "POST"])
@instructor_required
def instructor_question_new(quiz_id):
    quiz, lesson, course_id = _verify_instructor_quiz_ownership(session["user_id"], quiz_id)
    if not quiz:
        abort(404)

    db = get_db()
    course = _get_instructor_course(course_id)
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()

    if request.method == "POST":
        question_text = request.form.get("question_text", "").strip()
        question_type = request.form.get("question_type", "multiple_choice")
        option_a = request.form.get("option_a", "").strip()
        option_b = request.form.get("option_b", "").strip()
        option_c = request.form.get("option_c", "").strip()
        option_d = request.form.get("option_d", "").strip()
        correct_answer = request.form.get("correct_answer", "").strip().upper()
        explanation = request.form.get("explanation", "").strip()
        concept_tag = request.form.get("concept_tag", "").strip()
        position = request.form.get("position", "").strip()

        errors = []
        if not question_text:
            errors.append("Question text is required.")
        if question_type not in ("multiple_choice", "true_false"):
            question_type = "multiple_choice"
        if question_type == "true_false":
            option_c = ""
            option_d = ""
            if correct_answer not in ("A", "B"):
                errors.append("Correct answer must be True (A) or False (B).")
        else:
            if not option_a:
                errors.append("Option A is required.")
            if not option_b:
                errors.append("Option B is required.")
            if correct_answer not in ("A", "B", "C", "D"):
                errors.append("Correct answer must be A, B, C, or D.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("instructor_question_form.html",
                                   page_title="New Question", course=course, module=mod,
                                   lesson=lesson, quiz=quiz, editing=False)

        try:
            pos = int(position) if position else _next_position(
                db, "quiz_questions", "position", quiz_id, "quiz_id"
            )
        except (ValueError, TypeError):
            pos = _next_position(db, "quiz_questions", "position", quiz_id, "quiz_id")

        db.execute(
            "INSERT INTO quiz_questions "
            "(quiz_id, question_text, question_type, option_a, option_b, option_c, option_d, "
            "correct_answer, explanation, concept_tag, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (quiz_id, question_text, question_type, option_a, option_b, option_c, option_d,
             correct_answer, explanation, concept_tag, pos),
        )
        db.commit()
        flash("Question added.", "success")
        return redirect(url_for("instructor_quiz_edit", quiz_id=quiz_id))

    next_pos = _next_position(db, "quiz_questions", "position", quiz_id, "quiz_id")
    return render_template("instructor_question_form.html",
                           page_title="New Question", course=course, module=mod,
                           lesson=lesson, quiz=quiz, editing=False, next_position=next_pos)


@app.route("/instructor/questions/<int:question_id>/edit", methods=["GET", "POST"])
@instructor_required
def instructor_question_edit(question_id):
    question, quiz, lesson, course_id = _verify_instructor_question_ownership(
        session["user_id"], question_id
    )
    if not question:
        abort(404)

    db = get_db()
    course = _get_instructor_course(course_id)
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()

    if request.method == "POST":
        question_text = request.form.get("question_text", "").strip()
        question_type = request.form.get("question_type", question["question_type"])
        option_a = request.form.get("option_a", "").strip()
        option_b = request.form.get("option_b", "").strip()
        option_c = request.form.get("option_c", "").strip()
        option_d = request.form.get("option_d", "").strip()
        correct_answer = request.form.get("correct_answer", "").strip().upper()
        explanation = request.form.get("explanation", "").strip()
        concept_tag = request.form.get("concept_tag", "").strip()
        position = request.form.get("position", question["position"])

        errors = []
        if not question_text:
            errors.append("Question text is required.")
        if question_type not in ("multiple_choice", "true_false"):
            question_type = question["question_type"]
        if question_type == "true_false":
            option_c = ""
            option_d = ""
            if correct_answer not in ("A", "B"):
                errors.append("Correct answer must be True (A) or False (B).")
        else:
            if not option_a:
                errors.append("Option A is required.")
            if not option_b:
                errors.append("Option B is required.")
            if correct_answer not in ("A", "B", "C", "D"):
                errors.append("Correct answer must be A, B, C, or D.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("instructor_question_form.html",
                                   page_title="Edit Question", course=course, module=mod,
                                   lesson=lesson, quiz=quiz, question=question, editing=True)

        try:
            pos = int(position)
        except (ValueError, TypeError):
            pos = question["position"]

        db.execute(
            "UPDATE quiz_questions SET question_text=?, question_type=?, "
            "option_a=?, option_b=?, option_c=?, option_d=?, correct_answer=?, "
            "explanation=?, concept_tag=?, position=?, updated_at=datetime('now') "
            "WHERE id=?",
            (question_text, question_type, option_a, option_b, option_c, option_d,
             correct_answer, explanation, concept_tag, pos, question_id),
        )
        db.commit()
        flash("Question updated.", "success")
        return redirect(url_for("instructor_quiz_edit", quiz_id=quiz["id"]))

    return render_template("instructor_question_form.html",
                           page_title="Edit Question", course=course, module=mod,
                           lesson=lesson, quiz=quiz, question=question, editing=True)


@app.route("/instructor/questions/<int:question_id>/delete", methods=["POST"])
@instructor_required
def instructor_question_delete(question_id):
    question, quiz, lesson, course_id = _verify_instructor_question_ownership(
        session["user_id"], question_id
    )
    if not question:
        abort(404)

    db = get_db()
    db.execute("DELETE FROM quiz_questions WHERE id = ?", (question_id,))
    db.commit()
    flash("Question deleted.", "success")
    return redirect(url_for("instructor_quiz_edit", quiz_id=quiz["id"]))


@app.route("/instructor/quizzes/<int:quiz_id>/delete", methods=["POST"])
@instructor_required
def instructor_quiz_delete(quiz_id):
    quiz, lesson, course_id = _verify_instructor_quiz_ownership(session["user_id"], quiz_id)
    if not quiz:
        abort(404)

    db = get_db()
    lesson_id = quiz["lesson_id"]
    db.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
    db.commit()
    flash("Quiz deleted.", "success")
    return redirect(url_for("instructor_course_detail", course_id=course_id))


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Student: Explore, Course Detail, Enrollment, My Learning, Lessons
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/explore")
@student_required
def explore():
    """Browse published courses with search and filters."""
    db = get_db()

    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    level = request.args.get("level", "").strip()

    query = (
        "SELECT c.*, u.full_name AS instructor_name "
        "FROM courses c JOIN users u ON c.instructor_id = u.id "
        "WHERE c.status = 'published'"
    )
    params = []

    if search:
        query += " AND (c.title LIKE ? OR c.description LIKE ? OR c.category LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like])
    if category:
        query += " AND c.category = ?"
        params.append(category)
    if level:
        query += " AND c.level = ?"
        params.append(level)

    query += " ORDER BY c.created_at DESC"
    courses = db.execute(query, params).fetchall()

    # Get categories for the filter dropdown
    categories = [
        r[0] for r in db.execute(
            "SELECT DISTINCT category FROM courses WHERE status='published' AND category != '' ORDER BY category"
        ).fetchall()
    ]

    # Check enrollment status for each course
    enrolled_ids = set()
    if "user_id" in session:
        rows = db.execute(
            "SELECT course_id FROM enrollments WHERE student_id = ?",
            (session["user_id"],),
        ).fetchall()
        enrolled_ids = {r[0] for r in rows}

    course_list = []
    for c in courses:
        d = dict(c)
        d["enrolled"] = c["id"] in enrolled_ids
        d["lesson_count"] = _count_lessons(c["id"])
        d["module_count"] = db.execute(
            "SELECT COUNT(*) FROM modules WHERE course_id = ?", (c["id"],)
        ).fetchone()[0]
        # Approximate catalog duration: sum of declared lesson durations
        # (stored in SECONDS). Lessons without a duration contribute nothing.
        seconds = db.execute(
            "SELECT COALESCE(SUM(l.duration), 0) FROM lessons l "
            "JOIN modules m ON l.module_id = m.id WHERE m.course_id = ?",
            (c["id"],),
        ).fetchone()[0]
        d["duration_seconds"] = seconds
        # Progress is only meaningful for courses this student is enrolled in.
        d["progress"] = (
            _calculate_course_progress(db, session["user_id"], c["id"])
            if d["enrolled"] else None
        )
        course_list.append(d)

    return render_template(
        "explore.html",
        page_title="Explore",
        courses=course_list,
        categories=categories,
        search=search,
        selected_category=category,
        selected_level=level,
    )


@app.route("/courses/<int:course_id>")
@student_required
def course_detail(course_id):
    """View a published course's details and curriculum."""
    db = get_db()
    course = db.execute(
        "SELECT c.*, u.full_name AS instructor_name "
        "FROM courses c JOIN users u ON c.instructor_id = u.id "
        "WHERE c.id = ? AND c.status = 'published'",
        (course_id,),
    ).fetchone()
    if not course:
        abort(404)

    modules_data = _get_course_modules(course_id)
    total_lessons = _count_lessons(course_id)

    # Check if already enrolled
    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (session["user_id"], course_id),
    ).fetchone()

    # Get lesson progress map if enrolled
    progress_map = {}
    course_progress = {"total": 0, "completed": 0, "percentage": 0}
    quiz_summary = {"total_quizzes": 0, "attempted": 0, "average_score": None}
    if enrolled:
        progress_map = _get_lesson_progress_map(db, session["user_id"], course_id)
        course_progress = _calculate_course_progress(db, session["user_id"], course_id)
        quiz_summary = _get_course_quiz_summary(db, session["user_id"], course_id)

    # Attach quiz info to lessons for curriculum display
    lesson_quiz_map = {}
    for item in modules_data:
        for l in item["lessons"]:
            q = _get_quiz_for_lesson(db, l["id"])
            if q:
                lesson_quiz_map[l["id"]] = q

    return render_template(
        "course_detail.html",
        page_title=course["title"],
        course=course,
        modules_data=modules_data,
        total_lessons=total_lessons,
        enrolled=bool(enrolled),
        progress_map=progress_map,
        course_progress=course_progress,
        quiz_summary=quiz_summary,
        lesson_quiz_map=lesson_quiz_map,
    )


@app.route("/courses/<int:course_id>/enroll", methods=["POST"])
@student_required
def enroll(course_id):
    """Enroll the current student in a published course."""
    db = get_db()
    course = db.execute(
        "SELECT id, status FROM courses WHERE id = ?", (course_id,)
    ).fetchone()

    if not course:
        flash("Course not found.", "error")
        return redirect(url_for("explore"))
    if course["status"] != "published":
        flash("This course is not available for enrollment.", "error")
        return redirect(url_for("explore"))

    existing = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (session["user_id"], course_id),
    ).fetchone()
    if existing:
        flash("You are already enrolled in this course.", "info")
        return redirect(url_for("course_detail", course_id=course_id))

    db.execute(
        "INSERT INTO enrollments (student_id, course_id) VALUES (?, ?)",
        (session["user_id"], course_id),
    )
    db.commit()
    flash("Enrolled successfully! Start learning now.", "success")
    return redirect(url_for("course_detail", course_id=course_id))


@app.route("/my-learning")
@student_required
def my_learning():
    """Show courses the student is enrolled in with real progress data."""
    db = get_db()
    student_id = session["user_id"]
    rows = db.execute(
        "SELECT c.*, u.full_name AS instructor_name, e.enrolled_at "
        "FROM enrollments e "
        "JOIN courses c ON e.course_id = c.id "
        "JOIN users u ON c.instructor_id = u.id "
        "WHERE e.student_id = ? "
        "ORDER BY e.enrolled_at DESC",
        (student_id,),
    ).fetchall()

    enrolled_courses = []
    for r in rows:
        d = dict(r)
        d["module_count"] = db.execute(
            "SELECT COUNT(*) FROM modules WHERE course_id = ?", (r["id"],)
        ).fetchone()[0]
        d["lesson_count"] = _count_lessons(r["id"])
        d["progress"] = _calculate_course_progress(db, student_id, r["id"])
        d["quiz_summary"] = _get_course_quiz_summary(db, student_id, r["id"])
        continue_lesson = _get_continue_lesson(db, student_id, r["id"])
        if continue_lesson:
            d["continue_lesson"] = dict(continue_lesson)
            d["continue_url"] = url_for(
                "lesson_view",
                course_id=r["id"],
                module_id=continue_lesson["module_id"],
                lesson_id=continue_lesson["id"],
            )
        else:
            d["continue_lesson"] = None
            d["continue_url"] = url_for("course_detail", course_id=r["id"])
        enrolled_courses.append(d)

    return render_template(
        "my_learning.html",
        page_title="My Learning",
        courses=enrolled_courses,
    )


@app.route("/courses/<int:course_id>/modules/<int:module_id>/lessons/<int:lesson_id>")
@student_required
def lesson_view(course_id, module_id, lesson_id):
    """View lesson content — enrolled students only."""
    db = get_db()

    # Verify course is published
    course = db.execute(
        "SELECT * FROM courses WHERE id = ? AND status = 'published'",
        (course_id,),
    ).fetchone()
    if not course:
        abort(404)

    # Verify module belongs to course
    mod = db.execute(
        "SELECT * FROM modules WHERE id = ? AND course_id = ?",
        (module_id, course_id),
    ).fetchone()
    if not mod:
        abort(404)

    # Verify lesson belongs to module
    lesson = db.execute(
        "SELECT * FROM lessons WHERE id = ? AND module_id = ?",
        (lesson_id, module_id),
    ).fetchone()
    if not lesson:
        abort(404)

    # Verify enrollment
    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (session["user_id"], course_id),
    ).fetchone()
    if not enrolled:
        flash("You must enroll in this course to access lessons.", "warning")
        return redirect(url_for("course_detail", course_id=course_id))

    is_preview = False

    # Get resources for this lesson
    resources = db.execute(
        "SELECT * FROM lesson_resources WHERE lesson_id = ? ORDER BY title",
        (lesson_id,),
    ).fetchall()

    # Get prev/next lesson for navigation
    all_lessons = db.execute(
        "SELECT l.id, l.title, m.id AS module_id, m.course_id "
        "FROM lessons l JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? ORDER BY m.position, l.position",
        (course_id,),
    ).fetchall()
    lesson_ids = [r["id"] for r in all_lessons]
    current_idx = lesson_ids.index(lesson_id) if lesson_id in lesson_ids else -1
    prev_lesson = all_lessons[current_idx - 1] if current_idx > 0 else None
    next_lesson = all_lessons[current_idx + 1] if current_idx < len(all_lessons) - 1 else None

    # Get lesson progress for resume
    progress = _get_lesson_progress(db, session["user_id"], lesson_id)

    # Get existing notes and markers for this lesson (Stage 5)
    notes = []
    markers = []
    moments = []
    if lesson["lesson_type"] == "video" and not is_preview:
        notes = db.execute(
            "SELECT * FROM video_notes "
            "WHERE student_id = ? AND lesson_id = ? "
            "ORDER BY timestamp_seconds, created_at",
            (session["user_id"], lesson_id),
        ).fetchall()
        markers = db.execute(
            "SELECT * FROM video_markers "
            "WHERE student_id = ? AND lesson_id = ? "
            "ORDER BY timestamp_seconds, created_at",
            (session["user_id"], lesson_id),
        ).fetchall()

        # "My Moments": one merged timeline of every timestamped interaction
        # for THIS lesson (notes + important + confused), sorted by time.
        for n in notes:
            moments.append({
                "kind": "note", "icon": "\U0001F4DD",
                "kind_label": "Note",
                "id": n["id"], "ref_id": n["id"],
                "ts": n["timestamp_seconds"], "text": n["note_text"],
                "created_at": n["created_at"],
                "updated_at": n["updated_at"],
            })
        for mk in markers:
            is_important = mk["marker_type"] == "important"
            moments.append({
                "kind": mk["marker_type"],
                "icon": "\u2B50" if is_important else "\u2753",
                "kind_label": "Important" if is_important else "Didn't Understand",
                "id": mk["id"], "ref_id": mk["id"],
                "ts": mk["timestamp_seconds"], "text": mk["optional_text"],
                "created_at": mk["created_at"],
                "updated_at": None,
            })
        moments.sort(key=lambda mo: (mo["ts"], mo["created_at"]))

    # Handle deep-link seek parameter (from global notes/revision pages).
    # Canonical name is ?seek=<seconds>; legacy ?t= keeps working.
    seek_to = request.args.get("seek", type=float)
    if seek_to is None:
        seek_to = request.args.get("t", type=float)

    # Get quiz for this lesson (Stage 6); stats variant adds question_count
    quiz = None
    quiz_stats = None
    if not is_preview:
        quiz_row = _get_quiz_for_lesson(db, lesson_id)
        if quiz_row:
            quiz = _get_quiz_with_stats(db, quiz_row["id"])
            quiz_stats = _get_student_quiz_stats(db, session["user_id"], quiz["id"])

    # Stage 9: clickable transcript segments (video lessons)
    transcript_segments = []
    if lesson["lesson_type"] == "video":
        transcript_segments = db.execute(
            "SELECT id, start_seconds, text FROM lesson_transcript_segments "
            "WHERE lesson_id = ? ORDER BY position, start_seconds",
            (lesson_id,),
        ).fetchall()

    # Stage 9: instructor-configured Python practice + student's saved code
    exercise = db.execute(
        "SELECT * FROM coding_exercises WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()
    saved_code = None
    if exercise and not is_preview:
        row = db.execute(
            "SELECT code FROM student_code WHERE student_id = ? AND exercise_id = ?",
            (session["user_id"], exercise["id"]),
        ).fetchone()
        saved_code = row["code"] if row else None

    return render_template(
        "lesson_view.html",
        page_title=lesson["title"],
        course=course,
        module=mod,
        lesson=lesson,
        resources=resources,
        prev_lesson=prev_lesson,
        next_lesson=next_lesson,
        progress=progress,
        notes=notes,
        markers=markers,
        moments=moments,
        seek_to=seek_to,
        format_ts=_format_timestamp,
        quiz=quiz,
        quiz_stats=quiz_stats,
        transcript_segments=transcript_segments,
        exercise=exercise,
        saved_code=saved_code,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Progress API (Stage 4)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/progress", methods=["POST"])
@student_required
def api_update_progress():
    """
    Update lesson progress (video position, watched time).
    Expects JSON: { lesson_id, position_seconds, watched_seconds }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    lesson_id = data.get("lesson_id")
    position_seconds = data.get("position_seconds", 0)
    watched_seconds = data.get("watched_seconds", 0)

    if not lesson_id:
        return jsonify({"error": "lesson_id is required"}), 400

    try:
        lesson_id = int(lesson_id)
        position_seconds = max(0, float(position_seconds))
        watched_seconds = max(0, float(watched_seconds))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid numeric values"}), 400

    db = get_db()
    student_id = session["user_id"]

    # Verify enrollment
    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE l.id = ?",
        (lesson_id,),
    ).fetchone()
    if not lesson:
        return jsonify({"error": "Lesson not found"}), 404

    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, lesson["course_id"]),
    ).fetchone()
    if not enrolled:
        return jsonify({"error": "Not enrolled"}), 403

    # Get or create progress row
    progress = _get_lesson_progress(db, student_id, lesson_id)

    # Calculate new watched_seconds (keep the maximum seen, not cumulative sum from client)
    new_watched = max(progress["watched_seconds"], watched_seconds)

    # Calculate completion percentage
    # NOTE: lessons.duration is stored in SECONDS (single platform-wide rule),
    # so the client's watched seconds compare directly against it.
    completion_pct = 0
    if lesson["lesson_type"] == "video" and lesson["duration"] and lesson["duration"] > 0:
        completion_pct = min(100, round(new_watched / lesson["duration"] * 100, 1))
    elif lesson["lesson_type"] == "text":
        completion_pct = progress["completion_percentage"]

    db.execute(
        "UPDATE lesson_progress "
        "SET last_position_seconds = ?, watched_seconds = ?, "
        "completion_percentage = ?, last_activity_at = datetime('now') "
        "WHERE student_id = ? AND lesson_id = ?",
        (position_seconds, new_watched, completion_pct, student_id, lesson_id),
    )
    db.commit()

    return jsonify({
        "ok": True,
        "position_seconds": position_seconds,
        "watched_seconds": new_watched,
        "completion_percentage": completion_pct,
    })


@app.route("/api/progress/complete", methods=["POST"])
@student_required
def api_complete_lesson():
    """
    Mark a lesson as completed.
    Expects JSON: { lesson_id }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    lesson_id = data.get("lesson_id")
    if not lesson_id:
        return jsonify({"error": "lesson_id is required"}), 400

    try:
        lesson_id = int(lesson_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid lesson_id"}), 400

    db = get_db()
    student_id = session["user_id"]

    # Verify lesson exists and get course
    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE l.id = ?",
        (lesson_id,),
    ).fetchone()
    if not lesson:
        return jsonify({"error": "Lesson not found"}), 404

    # Verify enrollment
    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, lesson["course_id"]),
    ).fetchone()
    if not enrolled:
        return jsonify({"error": "Not enrolled"}), 403

    # Get or create progress row
    progress = _get_lesson_progress(db, student_id, lesson_id)

    first_completion = not progress["is_completed"]
    if first_completion:
        db.execute(
            "UPDATE lesson_progress "
            "SET is_completed = 1, completion_percentage = 100, "
            "completed_at = datetime('now'), last_activity_at = datetime('now') "
            "WHERE student_id = ? AND lesson_id = ?",
            (student_id, lesson_id),
        )
        db.commit()

    # Stage 8: XP for FIRST completion only (idempotent — re-completing
    # the same lesson can never award XP again thanks to the unique index).
    xp_awarded = 0
    if first_completion:
        xp_awarded += game.award_xp(
            db, student_id, "lesson_completed", "lesson", lesson_id
        )
        course_progress_now = _calculate_course_progress(db, student_id, lesson["course_id"])
        if (course_progress_now["total"] > 0
                and course_progress_now["completed"] == course_progress_now["total"]):
            xp_awarded += game.award_xp(
                db, student_id, "course_completed", "course", lesson["course_id"]
            )
        game.evaluate_achievements(db, student_id)

    # Return updated course progress
    course_progress = _calculate_course_progress(db, student_id, lesson["course_id"])

    return jsonify({
        "ok": True,
        "is_completed": True,
        "xp_awarded": xp_awarded,
        "total_xp": game.total_xp(db, student_id),
        "course_progress": course_progress,
    })


@app.route("/api/progress/text-read", methods=["POST"])
@student_required
def api_text_read_progress():
    """
    Track text lesson reading progress via scroll percentage.
    Expects JSON: { lesson_id, read_percentage }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    lesson_id = data.get("lesson_id")
    read_percentage = data.get("read_percentage", 0)

    if not lesson_id:
        return jsonify({"error": "lesson_id is required"}), 400

    try:
        lesson_id = int(lesson_id)
        read_percentage = max(0, min(100, float(read_percentage)))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid values"}), 400

    db = get_db()
    student_id = session["user_id"]

    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE l.id = ?",
        (lesson_id,),
    ).fetchone()
    if not lesson:
        return jsonify({"error": "Lesson not found"}), 404

    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, lesson["course_id"]),
    ).fetchone()
    if not enrolled:
        return jsonify({"error": "Not enrolled"}), 403

    progress = _get_lesson_progress(db, student_id, lesson_id)

    new_pct = max(progress["completion_percentage"], read_percentage)

    db.execute(
        "UPDATE lesson_progress "
        "SET completion_percentage = ?, last_activity_at = datetime('now') "
        "WHERE student_id = ? AND lesson_id = ?",
        (new_pct, student_id, lesson_id),
    )
    db.commit()

    return jsonify({
        "ok": True,
        "completion_percentage": new_pct,
    })


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Notes API (Stage 5)
# ═══════════════════════════════════════════════════════════════════════════

MAX_NOTE_LENGTH = 2000
MAX_MARKER_TEXT_LENGTH = 500
MARKER_DUPLICATE_WINDOW = 5.0  # seconds — markers within this window are duplicates


@app.route("/api/lessons/<int:lesson_id>/notes", methods=["POST"])
@student_required
def api_create_note(lesson_id):
    """Create a timestamped note on a video lesson."""
    lesson, course_id = _verify_student_lesson_access(session["user_id"], lesson_id)
    if not lesson:
        return jsonify({"error": "Lesson not found or access denied"}), 404
    if lesson["lesson_type"] != "video":
        return jsonify({"error": "Notes are only available for video lessons"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    timestamp_seconds = data.get("timestamp_seconds")
    note_text = (data.get("note_text") or "").strip()

    if timestamp_seconds is None:
        return jsonify({"error": "timestamp_seconds is required"}), 400
    try:
        timestamp_seconds = float(timestamp_seconds)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid timestamp"}), 400
    if timestamp_seconds < 0:
        return jsonify({"error": "Timestamp must be non-negative"}), 400

    if not note_text:
        return jsonify({"error": "Note text is required"}), 400
    if len(note_text) > MAX_NOTE_LENGTH:
        return jsonify({"error": f"Note text must be {MAX_NOTE_LENGTH} characters or fewer"}), 400

    # Reasonable upper bound: if lesson has duration (stored in SECONDS), the
    # timestamp shouldn't exceed it by more than 10% (small grace for overruns)
    if lesson["duration"] and lesson["duration"] > 0:
        if timestamp_seconds > lesson["duration"] * 1.1:
            return jsonify({"error": "Timestamp exceeds video duration"}), 400

    db = get_db()
    cursor = db.execute(
        "INSERT INTO video_notes (student_id, lesson_id, timestamp_seconds, note_text) "
        "VALUES (?, ?, ?, ?)",
        (session["user_id"], lesson_id, timestamp_seconds, note_text),
    )
    db.commit()
    note_id = cursor.lastrowid

    note = db.execute("SELECT * FROM video_notes WHERE id = ?", (note_id,)).fetchone()

    return jsonify({
        "ok": True,
        "note": {
            "id": note["id"],
            "timestamp_seconds": note["timestamp_seconds"],
            "note_text": note["note_text"],
            "created_at": note["created_at"],
            "updated_at": note["updated_at"],
            "formatted_time": _format_timestamp(note["timestamp_seconds"]),
        },
    }), 201


@app.route("/api/notes/<int:note_id>", methods=["PUT"])
@student_required
def api_update_note(note_id):
    """Update a student's own note."""
    db = get_db()
    note = db.execute("SELECT * FROM video_notes WHERE id = ?", (note_id,)).fetchone()
    if not note:
        return jsonify({"error": "Note not found"}), 404
    if note["student_id"] != session["user_id"]:
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    note_text = (data.get("note_text") or "").strip()
    if not note_text:
        return jsonify({"error": "Note text is required"}), 400
    if len(note_text) > MAX_NOTE_LENGTH:
        return jsonify({"error": f"Note text must be {MAX_NOTE_LENGTH} characters or fewer"}), 400

    db.execute(
        "UPDATE video_notes SET note_text = ?, updated_at = datetime('now') WHERE id = ?",
        (note_text, note_id),
    )
    db.commit()

    updated = db.execute("SELECT * FROM video_notes WHERE id = ?", (note_id,)).fetchone()

    return jsonify({
        "ok": True,
        "note": {
            "id": updated["id"],
            "timestamp_seconds": updated["timestamp_seconds"],
            "note_text": updated["note_text"],
            "created_at": updated["created_at"],
            "updated_at": updated["updated_at"],
            "formatted_time": _format_timestamp(updated["timestamp_seconds"]),
        },
    })


@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
@student_required
def api_delete_note(note_id):
    """Delete a student's own note."""
    db = get_db()
    note = db.execute("SELECT * FROM video_notes WHERE id = ?", (note_id,)).fetchone()
    if not note:
        return jsonify({"error": "Note not found"}), 404
    if note["student_id"] != session["user_id"]:
        return jsonify({"error": "Access denied"}), 403

    db.execute("DELETE FROM video_notes WHERE id = ?", (note_id,))
    db.commit()

    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Markers API (Stage 5)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/lessons/<int:lesson_id>/markers", methods=["POST"])
@student_required
def api_create_marker(lesson_id):
    """Create an important or confused marker at a video timestamp."""
    lesson, course_id = _verify_student_lesson_access(session["user_id"], lesson_id)
    if not lesson:
        return jsonify({"error": "Lesson not found or access denied"}), 404
    if lesson["lesson_type"] != "video":
        return jsonify({"error": "Markers are only available for video lessons"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    timestamp_seconds = data.get("timestamp_seconds")
    marker_type = (data.get("marker_type") or "").strip().lower()
    optional_text = (data.get("optional_text") or "").strip()

    if timestamp_seconds is None:
        return jsonify({"error": "timestamp_seconds is required"}), 400
    try:
        timestamp_seconds = float(timestamp_seconds)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid timestamp"}), 400
    if timestamp_seconds < 0:
        return jsonify({"error": "Timestamp must be non-negative"}), 400

    if marker_type not in ("important", "confused"):
        return jsonify({"error": "marker_type must be 'important' or 'confused'"}), 400

    if optional_text and len(optional_text) > MAX_MARKER_TEXT_LENGTH:
        return jsonify({"error": f"Optional text must be {MAX_MARKER_TEXT_LENGTH} characters or fewer"}), 400

    if lesson["duration"] and lesson["duration"] > 0:
        if timestamp_seconds > lesson["duration"] * 1.1:
            return jsonify({"error": "Timestamp exceeds video duration"}), 400

    db = get_db()
    student_id = session["user_id"]

    # Duplicate-nearby check: prevent same marker_type within MARKER_DUPLICATE_WINDOW seconds
    existing = db.execute(
        "SELECT id FROM video_markers "
        "WHERE student_id = ? AND lesson_id = ? AND marker_type = ? "
        "AND ABS(timestamp_seconds - ?) < ?",
        (student_id, lesson_id, marker_type, timestamp_seconds, MARKER_DUPLICATE_WINDOW),
    ).fetchone()
    if existing:
        return jsonify({"error": "A similar marker already exists near this timestamp"}), 409

    cursor = db.execute(
        "INSERT INTO video_markers (student_id, lesson_id, timestamp_seconds, marker_type, optional_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (student_id, lesson_id, timestamp_seconds, marker_type, optional_text),
    )
    db.commit()
    marker_id = cursor.lastrowid

    marker = db.execute("SELECT * FROM video_markers WHERE id = ?", (marker_id,)).fetchone()

    return jsonify({
        "ok": True,
        "marker": {
            "id": marker["id"],
            "timestamp_seconds": marker["timestamp_seconds"],
            "marker_type": marker["marker_type"],
            "optional_text": marker["optional_text"],
            "created_at": marker["created_at"],
            "formatted_time": _format_timestamp(marker["timestamp_seconds"]),
        },
    }), 201


@app.route("/api/markers/<int:marker_id>", methods=["PUT"])
@student_required
def api_update_marker(marker_id):
    """Update a student's own marker's optional label text."""
    db = get_db()
    marker = db.execute("SELECT * FROM video_markers WHERE id = ?", (marker_id,)).fetchone()
    if not marker:
        return jsonify({"error": "Marker not found"}), 404
    if marker["student_id"] != session["user_id"]:
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    optional_text = (data.get("optional_text") or "").strip()
    if len(optional_text) > MAX_MARKER_TEXT_LENGTH:
        return jsonify({"error": f"Optional text must be {MAX_MARKER_TEXT_LENGTH} characters or fewer"}), 400

    db.execute(
        "UPDATE video_markers SET optional_text = ? WHERE id = ?",
        (optional_text, marker_id),
    )
    db.commit()

    updated = db.execute("SELECT * FROM video_markers WHERE id = ?", (marker_id,)).fetchone()
    return jsonify({
        "ok": True,
        "marker": {
            "id": updated["id"],
            "timestamp_seconds": updated["timestamp_seconds"],
            "marker_type": updated["marker_type"],
            "optional_text": updated["optional_text"],
            "created_at": updated["created_at"],
            "formatted_time": _format_timestamp(updated["timestamp_seconds"]),
        },
    })


@app.route("/api/markers/<int:marker_id>", methods=["DELETE"])
@student_required
def api_delete_marker(marker_id):
    """Delete a student's own marker."""
    db = get_db()
    marker = db.execute("SELECT * FROM video_markers WHERE id = ?", (marker_id,)).fetchone()
    if not marker:
        return jsonify({"error": "Marker not found"}), 404
    if marker["student_id"] != session["user_id"]:
        return jsonify({"error": "Access denied"}), 403

    db.execute("DELETE FROM video_markers WHERE id = ?", (marker_id,))
    db.commit()

    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Global Notes Page (Stage 5)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/notes")
@student_required
def notes_page():
    """Show all notes across enrolled courses, grouped by course."""
    db = get_db()
    student_id = session["user_id"]

    # Fetch all notes with course/module/lesson info
    notes = db.execute(
        "SELECT vn.*, l.title AS lesson_title, l.lesson_type, "
        "m.title AS module_title, m.course_id, c.title AS course_title "
        "FROM video_notes vn "
        "JOIN lessons l ON vn.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "JOIN courses c ON m.course_id = c.id "
        "JOIN enrollments e ON e.course_id = c.id AND e.student_id = vn.student_id "
        "WHERE vn.student_id = ? "
        "ORDER BY c.title, m.position, l.position, vn.timestamp_seconds",
        (student_id,),
    ).fetchall()

    # Group by course
    courses_map = {}
    for n in notes:
        cid = n["course_id"]
        if cid not in courses_map:
            courses_map[cid] = {"title": n["course_title"], "lessons": {}}
        lid = n["lesson_id"]
        if lid not in courses_map[cid]["lessons"]:
            courses_map[cid]["lessons"][lid] = {
                "title": n["lesson_title"],
                "module_title": n["module_title"],
                "course_id": cid,
                "module_id": None,
                "notes": [],
            }
            # Fetch module_id for URL building
            mod_row = db.execute(
                "SELECT id FROM modules WHERE course_id = ? AND title = ?",
                (cid, n["module_title"]),
            ).fetchone()
            if mod_row:
                courses_map[cid]["lessons"][lid]["module_id"] = mod_row["id"]
        courses_map[cid]["lessons"][lid]["notes"].append({
            "id": n["id"],
            "timestamp_seconds": n["timestamp_seconds"],
            "note_text": n["note_text"],
            "created_at": n["created_at"],
            "updated_at": n["updated_at"],
            "formatted_time": _format_timestamp(n["timestamp_seconds"]),
            "lesson_id": lid,
            "module_id": courses_map[cid]["lessons"][lid]["module_id"],
        })

    return render_template(
        "notes.html",
        page_title="My Notes",
        courses_map=courses_map,
        total_notes=len(notes),
        format_ts=_format_timestamp,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Student Quiz (Stage 6)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/courses/<int:course_id>/modules/<int:module_id>/lessons/<int:lesson_id>/quiz")
@student_required
def student_quiz_take(course_id, module_id, lesson_id):
    """Display quiz for an enrolled student (start/retake)."""
    db = get_db()
    student_id = session["user_id"]

    # Verify enrollment
    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, course_id),
    ).fetchone()
    if not enrolled:
        flash("You must enroll in this course.", "warning")
        return redirect(url_for("course_detail", course_id=course_id))

    # Verify lesson exists in course
    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE l.id = ? AND m.course_id = ?",
        (lesson_id, course_id),
    ).fetchone()
    if not lesson:
        abort(404)

    quiz = _get_quiz_for_lesson(db, lesson_id)
    if not quiz:
        flash("No quiz available for this lesson.", "info")
        return redirect(url_for("lesson_view", course_id=course_id,
                                module_id=lesson["module_id"], lesson_id=lesson_id))

    questions = db.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY position, id",
        (quiz["id"],),
    ).fetchall()
    if not questions:
        flash("This quiz has no questions yet.", "info")
        return redirect(url_for("lesson_view", course_id=course_id,
                                module_id=lesson["module_id"], lesson_id=lesson_id))

    quiz_stats = _get_student_quiz_stats(db, student_id, quiz["id"])

    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()

    return render_template(
        "student_quiz_take.html",
        page_title=quiz["title"],
        course_id=course_id,
        module=mod,
        lesson=lesson,
        quiz=quiz,
        questions=questions,
        quiz_stats=quiz_stats,
    )


@app.route("/courses/<int:course_id>/modules/<int:module_id>/lessons/<int:lesson_id>/quiz/submit", methods=["POST"])
@student_required
def student_quiz_submit(course_id, module_id, lesson_id):
    """Grade a quiz submission server-side. NEVER trust client-calculated scores."""
    db = get_db()
    student_id = session["user_id"]

    # Verify enrollment
    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, course_id),
    ).fetchone()
    if not enrolled:
        return jsonify({"error": "Not enrolled"}), 403

    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE l.id = ? AND m.course_id = ?",
        (lesson_id, course_id),
    ).fetchone()
    if not lesson:
        return jsonify({"error": "Lesson not found"}), 404

    quiz = _get_quiz_for_lesson(db, lesson_id)
    if not quiz:
        return jsonify({"error": "No quiz found"}), 404

    # Load questions from DB (never trust client)
    questions = db.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY position, id",
        (quiz["id"],),
    ).fetchall()
    if not questions:
        return jsonify({"error": "No questions"}), 400

    # Collect submitted answers: form field name is q_<question_id>
    total = len(questions)
    correct_count = 0

    # Stage 8: capture previous best BEFORE inserting this attempt so
    # improvement can be measured against history that excludes it.
    prev_attempts = db.execute(
        "SELECT percentage, passed FROM quiz_attempts "
        "WHERE quiz_id = ? AND student_id = ?",
        (quiz["id"], student_id),
    ).fetchall()
    prev_best = max((a["percentage"] for a in prev_attempts), default=None)
    already_passed = any(a["passed"] for a in prev_attempts)

    cursor = db.execute(
        "INSERT INTO quiz_attempts (quiz_id, student_id, score, total_questions, percentage, passed) "
        "VALUES (?, ?, 0, ?, 0, 0)",
        (quiz["id"], student_id, total),
    )
    attempt_id = cursor.lastrowid

    for q in questions:
        selected = request.form.get(f"q_{q['id']}", "").strip().upper()
        is_correct = 1 if selected == q["correct_answer"] else 0
        if is_correct:
            correct_count += 1

        db.execute(
            "INSERT INTO quiz_answers (attempt_id, question_id, selected_answer, is_correct) "
            "VALUES (?, ?, ?, ?)",
            (attempt_id, q["id"], selected, is_correct),
        )

    percentage = round(correct_count / total * 100, 1) if total > 0 else 0
    passed = 1 if percentage >= quiz["passing_percentage"] else 0

    db.execute(
        "UPDATE quiz_attempts SET score=?, percentage=?, passed=?, "
        "completed_at=datetime('now') WHERE id=?",
        (correct_count, percentage, passed, attempt_id),
    )
    db.commit()

    # Stage 8: XP events (all idempotent via the xp_events unique index).
    xp_awarded = 0
    if passed and not already_passed:
        # First-ever pass of this quiz: +25 once per quiz.
        xp_awarded += game.award_xp(db, student_id, "quiz_passed", "quiz", quiz["id"])
    if prev_best is not None and percentage > prev_best:
        # Beating a previous best: +10, referenced to THIS attempt so a
        # single attempt can never be rewarded twice.
        xp_awarded += game.award_xp(db, student_id, "quiz_improved", "attempt", attempt_id)
    if xp_awarded:
        game.evaluate_achievements(db, student_id)
        flash(f"⚡ You earned +{xp_awarded} XP!", "success")

    return redirect(url_for("student_quiz_result",
                            course_id=course_id, module_id=lesson["module_id"],
                            lesson_id=lesson_id, attempt_id=attempt_id))


@app.route("/courses/<int:course_id>/modules/<int:module_id>/lessons/<int:lesson_id>/quiz/result/<int:attempt_id>")
@student_required
def student_quiz_result(course_id, module_id, lesson_id, attempt_id):
    """Show quiz result with question-by-question feedback."""
    db = get_db()
    student_id = session["user_id"]

    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, course_id),
    ).fetchone()
    if not enrolled:
        flash("You must enroll in this course.", "warning")
        return redirect(url_for("course_detail", course_id=course_id))

    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE id = ? AND student_id = ? AND quiz_id IN "
        "(SELECT id FROM quizzes WHERE lesson_id = ?)",
        (attempt_id, student_id, lesson_id),
    ).fetchone()
    if not attempt:
        abort(404)

    quiz = _get_quiz_for_lesson(db, lesson_id)
    if not quiz:
        abort(404)

    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE l.id = ? AND m.course_id = ?",
        (lesson_id, course_id),
    ).fetchone()

    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()

    # Load questions + answers for this attempt
    results = db.execute(
        "SELECT qq.*, qa.selected_answer, qa.is_correct "
        "FROM quiz_questions qq "
        "JOIN quiz_answers qa ON qq.id = qa.question_id "
        "WHERE qa.attempt_id = ? "
        "ORDER BY qq.position, qq.id",
        (attempt_id,),
    ).fetchall()

    quiz_stats = _get_student_quiz_stats(db, student_id, quiz["id"])

    return render_template(
        "student_quiz_result.html",
        page_title=f"Quiz Result — {quiz['title']}",
        course_id=course_id,
        module=mod,
        lesson=lesson,
        quiz=quiz,
        attempt=attempt,
        results=results,
        quiz_stats=quiz_stats,
    )


@app.route("/courses/<int:course_id>/quiz-stats")
@student_required
def student_course_quiz_stats(course_id):
    """Show quiz statistics for a course."""
    db = get_db()
    student_id = session["user_id"]

    enrolled = db.execute(
        "SELECT id FROM enrollments WHERE student_id = ? AND course_id = ?",
        (student_id, course_id),
    ).fetchone()
    if not enrolled:
        flash("You must enroll in this course.", "warning")
        return redirect(url_for("course_detail", course_id=course_id))

    summary = _get_course_quiz_summary(db, student_id, course_id)
    concept_perf = _calculate_concept_performance(db, student_id, course_id)

    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()

    return render_template(
        "student_course_quiz_stats.html",
        page_title=f"Quiz Stats — {course['title']}",
        course=course,
        summary=summary,
        concept_perf=concept_perf,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Instructor lesson preview
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/instructor/courses/<int:course_id>/modules/<int:module_id>/lessons/<int:lesson_id>")
@instructor_required
def instructor_lesson_preview(course_id, module_id, lesson_id):
    """Instructor can preview their own lessons without enrollment."""
    course = _get_instructor_course(course_id)
    if not course:
        abort(404)
    db = get_db()
    mod = db.execute(
        "SELECT * FROM modules WHERE id = ? AND course_id = ?",
        (module_id, course_id),
    ).fetchone()
    if not mod:
        abort(404)
    lesson = db.execute(
        "SELECT * FROM lessons WHERE id = ? AND module_id = ?",
        (lesson_id, module_id),
    ).fetchone()
    if not lesson:
        abort(404)

    resources = db.execute(
        "SELECT * FROM lesson_resources WHERE lesson_id = ? ORDER BY title",
        (lesson_id,),
    ).fetchall()

    transcript_segments = []
    if lesson["lesson_type"] == "video":
        transcript_segments = db.execute(
            "SELECT id, start_seconds, text FROM lesson_transcript_segments "
            "WHERE lesson_id = ? ORDER BY position, start_seconds",
            (lesson_id,),
        ).fetchall()
    exercise = db.execute(
        "SELECT * FROM coding_exercises WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()

    return render_template(
        "lesson_view.html",
        page_title=f"[Preview] {lesson['title']}",
        course=course,
        module=mod,
        lesson=lesson,
        resources=resources,
        prev_lesson=None,
        next_lesson=None,
        is_preview=True,
        progress=None,
        notes=[],
        markers=[],
        moments=[],
        seek_to=None,
        format_ts=_format_timestamp,
        quiz=None,
        quiz_stats=None,
        transcript_segments=transcript_segments,
        exercise=exercise,
        saved_code=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Lesson resources (instructor upload)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/instructor/lessons/<int:lesson_id>/resources/add", methods=["POST"])
@instructor_required
def instructor_add_resource(lesson_id):
    """Upload a resource file for a lesson."""
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)).fetchone()
    course = _get_instructor_course(mod["course_id"]) if mod else None
    if not course:
        abort(404)

    title = request.form.get("resource_title", "").strip()
    if not title:
        flash("Resource title is required.", "error")
        return redirect(url_for("instructor_course_detail", course_id=course["id"]))

    if "resource_file" not in request.files or not request.files["resource_file"].filename:
        flash("Please select a file.", "error")
        return redirect(url_for("instructor_course_detail", course_id=course["id"]))

    file_path = _save_upload(request.files["resource_file"], "resources", ALLOWED_RESOURCE_EXT)
    if not file_path:
        flash("Invalid file type. Allowed: PDF, TXT, PY, ZIP.", "error")
        return redirect(url_for("instructor_course_detail", course_id=course["id"]))

    file_type = file_path.rsplit(".", 1)[-1].lower()
    db.execute(
        "INSERT INTO lesson_resources (lesson_id, title, file_path, file_type) VALUES (?, ?, ?, ?)",
        (lesson_id, title, file_path, file_type),
    )
    db.commit()
    flash("Resource uploaded.", "success")
    return redirect(url_for("instructor_course_detail", course_id=course["id"]))


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Learning Health + Smart Revision (Stage 7 integration)
# ═══════════════════════════════════════════════════════════════════════════

def _enrolled_course_or_404(student_id, course_id):
    """Return the enrolled course row for a student, or None."""
    db = get_db()
    return db.execute(
        "SELECT c.*, u.full_name AS instructor_name "
        "FROM enrollments e "
        "JOIN courses c ON e.course_id = c.id "
        "JOIN users u ON c.instructor_id = u.id "
        "WHERE e.student_id = ? AND c.id = ?",
        (student_id, course_id),
    ).fetchone()


@app.route("/courses/<int:course_id>/health")
@student_required
def course_health(course_id):
    """Learning Health dashboard for one enrolled course."""
    db = get_db()
    student_id = session["user_id"]
    course = _enrolled_course_or_404(student_id, course_id)
    if not course:
        flash("You must enroll in this course first.", "warning")
        return redirect(url_for("course_detail", course_id=course_id))

    health = learning.calculate_learning_health(db, student_id, course_id)
    concepts = learning.student_concept_performance(db, student_id, course_id)

    return render_template(
        "course_health.html",
        page_title=f"Learning Health · {course['title']}",
        course=course,
        health=health,
        concepts=concepts,
    )


@app.route("/courses/<int:course_id>/revision")
@student_required
def smart_revision(course_id):
    """
    Smart Revision recommendations, recomputed live from real data.
    Nothing about the recommendations is ever stored.
    """
    db = get_db()
    student_id = session["user_id"]
    course = _enrolled_course_or_404(student_id, course_id)
    if not course:
        flash("You must enroll in this course first.", "warning")
        return redirect(url_for("course_detail", course_id=course_id))

    items = learning.build_revision_items(db, student_id, course_id, limit=10)

    # Turn items into links the template can render directly. Timestamped
    # sources (notes/markers) deep-link straight to that moment in the video.
    for item in items:
        if item["lesson_id"] and item["module_id"]:
            url = url_for(
                "lesson_view",
                course_id=course_id,
                module_id=item["module_id"],
                lesson_id=item["lesson_id"],
            )
            ts = item.get("timestamp_seconds")
            if ts is not None:
                url += f"?t={float(ts):g}"
            item["url"] = url
        else:
            item["url"] = url_for("my_learning")

    history_count = learning.revision_stats(db, student_id, course_id)
    health = learning.calculate_learning_health(db, student_id, course_id)

    return render_template(
        "smart_revision.html",
        page_title=f"Smart Revision · {course['title']}",
        course=course,
        items=items,
        history_count=history_count,
        health=health,
        format_ts=_format_timestamp,
    )


@app.route("/courses/<int:course_id>/revision/review", methods=["POST"])
@student_required
def record_revision_review(course_id):
    """
    Record that a student reviewed one recommendation and award XP.

    The server RE-DERIVES the recommendation list and matches on
    (source_type, ref_type, ref_id) from the form -- the client can
    never inject an arbitrary activity row or XP event.
    """
    db = get_db()
    student_id = session["user_id"]
    course = _enrolled_course_or_404(student_id, course_id)
    if not course:
        abort(403)

    source_type = request.form.get("source_type", "")
    ref_type = request.form.get("ref_type", "")
    try:
        ref_id = int(request.form.get("ref_id", 0))
    except ValueError:
        ref_id = 0

    items = learning.build_revision_items(db, student_id, course_id, limit=50)
    match = next(
        (i for i in items if i["source_type"] == source_type
         and i["ref_type"] == ref_type and i["ref_id"] == ref_id),
        None,
    )
    if not match:
        flash("That revision item is no longer on your list.", "info")
        return redirect(url_for("smart_revision", course_id=course_id))

    learning.record_revision_activity(
        db, student_id, course_id,
        {"lesson_id": match.get("lesson_id"), "concept_tag": None,
         "source_type": match["source_type"], "ref_id": match["ref_id"]},
    )
    xp = game.award_xp(db, student_id, "revision_reviewed",
                       match["ref_type"], match["ref_id"])
    game.evaluate_achievements(db, student_id)
    if xp:
        flash(f"Reviewed “{match['title']}” · +{xp} XP ⚡", "success")
    else:
        flash(f"Reviewed “{match['title']}”.", "success")
    return redirect(url_for("smart_revision", course_id=course_id))


@app.route("/courses/<int:course_id>/revision/history")
@student_required
def revision_history(course_id):
    """When did this student review what? Lightweight audit trail."""
    db = get_db()
    student_id = session["user_id"]
    course = _enrolled_course_or_404(student_id, course_id)
    if not course:
        abort(404)

    rows = db.execute(
        "SELECT ra.*, l.title AS lesson_title "
        "FROM revision_activity ra "
        "LEFT JOIN lessons l ON ra.lesson_id = l.id "
        "WHERE ra.student_id = ? AND ra.course_id = ? "
        "ORDER BY ra.reviewed_at DESC, ra.id DESC LIMIT 100",
        (student_id, course_id),
    ).fetchall()

    return render_template(
        "revision_history.html",
        page_title=f"Revision History · {course['title']}",
        course=course,
        rows=rows,
        total=learning.revision_stats(db, student_id, course_id),
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Study Pack + Printables (Stage 8 integration)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/study-pack")
@student_required
def study_pack():
    """One printable-friendly hub across every enrolled course."""
    db = get_db()
    student_id = session["user_id"]

    courses = []
    rows = db.execute(
        "SELECT c.* FROM enrollments e JOIN courses c ON e.course_id = c.id "
        "WHERE e.student_id = ? ORDER BY c.title",
        (student_id,),
    ).fetchall()
    for c in rows:
        entry = dict(c)
        entry["health"] = learning.calculate_learning_health(db, student_id, c["id"])
        entry["revision_count"] = learning.revision_stats(db, student_id, c["id"])
        entry["notes"] = db.execute(
            "SELECT COUNT(*) FROM video_notes vn "
            "JOIN lessons l ON vn.lesson_id = l.id "
            "JOIN modules m ON l.module_id = m.id "
            "WHERE vn.student_id = ? AND m.course_id = ?",
            (student_id, c["id"]),
        ).fetchone()[0]
        entry["markers"] = db.execute(
            "SELECT COUNT(*) FROM video_markers vm "
            "JOIN lessons l ON vm.lesson_id = l.id "
            "JOIN modules m ON l.module_id = m.id "
            "WHERE vm.student_id = ? AND m.course_id = ?",
            (student_id, c["id"]),
        ).fetchone()[0]
        entry["weak_concepts"] = [
            cpt for cpt in
            learning.student_concept_performance(db, student_id, c["id"])
            if cpt["accuracy"] < learning.WEAK_CONCEPT_THRESHOLD
        ]
        courses.append(entry)

    return render_template(
        "study_pack.html",
        page_title="Study Pack",
        courses=courses,
    )


@app.route("/notes/print")
@login_required
def print_notes():
    """Printable version of all timestamped notes (browser print)."""
    db = get_db()
    student_id = session["user_id"]

    notes = db.execute(
        "SELECT vn.*, l.title AS lesson_title, l.lesson_type, "
        "m.title AS module_title, m.id AS module_id, m.course_id, "
        "c.title AS course_title "
        "FROM video_notes vn "
        "JOIN lessons l ON vn.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "JOIN courses c ON m.course_id = c.id "
        "JOIN enrollments e ON e.course_id = c.id AND e.student_id = vn.student_id "
        "WHERE vn.student_id = ? "
        "ORDER BY c.title, m.position, l.position, vn.timestamp_seconds",
        (student_id,),
    ).fetchall()

    grouped = {}
    for n in notes:
        grouped.setdefault(n["course_title"], []).append(dict(n))

    return render_template(
        "print_notes.html",
        page_title="Printable Notes",
        grouped=grouped,
        format_ts=_format_timestamp,
    )


@app.route("/courses/<int:course_id>/revision-cards/print")
@student_required
def print_revision_cards(course_id):
    """
    Printable revision cards generated deterministically from the same
    real signals Smart Revision uses (weak concepts, wrong questions,
    confusion markers). Front = prompt, back = detail.
    """
    db = get_db()
    student_id = session["user_id"]
    course = _enrolled_course_or_404(student_id, course_id)
    if not course:
        abort(404)

    cards = []
    # Weak concepts -> drill cards.
    for c in learning.student_concept_performance(db, student_id, course_id):
        if c["accuracy"] >= learning.WEAK_CONCEPT_THRESHOLD:
            continue
        cards.append({
            "kind": "Concept",
            "front": f"What do you know about “{c['concept_tag']}”?",
            "back": f"Quiz accuracy {c['accuracy']}% — "
                    f"{c['correct']}/{c['total']} answers correct.",
        })
    # Wrong questions from latest attempts -> recall cards.
    quizzes = db.execute(
        "SELECT DISTINCT qz.id, qz.lesson_id FROM quizzes qz "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id WHERE m.course_id = ?",
        (course_id,),
    ).fetchall()
    seen_q = set()
    for qz in quizzes:
        latest = db.execute(
            "SELECT id FROM quiz_attempts WHERE quiz_id = ? AND student_id = ? "
            "ORDER BY completed_at DESC, id DESC LIMIT 1",
            (qz["id"], student_id),
        ).fetchone()
        if not latest:
            continue
        wrong = db.execute(
            "SELECT qq.question_text, qq.correct_answer, qq.concept_tag, qq.id "
            "FROM quiz_answers qa JOIN quiz_questions qq ON qa.question_id = qq.id "
            "WHERE qa.attempt_id = ? AND qa.is_correct = 0",
            (latest["id"],),
        ).fetchall()
        for w in wrong:
            if w["id"] in seen_q:
                continue
            seen_q.add(w["id"])
            front = w["question_text"]
            back = f"Answer: {w['correct_answer']}"
            if w["concept_tag"]:
                back += f" · Concept: {w['concept_tag']}"
            cards.append({"kind": "Question", "front": front, "back": back})
    # Confusion markers -> rewatch cards.
    confusions = db.execute(
        "SELECT vm.timestamp_seconds, vm.optional_text, l.title AS lesson_title "
        "FROM video_markers vm JOIN lessons l ON vm.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND vm.student_id = ? AND vm.marker_type = 'confused'",
        (course_id, student_id),
    ).fetchall()
    for cm in confusions:
        detail = f"Rewatch {_format_timestamp(cm['timestamp_seconds'])} of “{cm['lesson_title']}”."
        if cm["optional_text"]:
            detail += f" You noted: {cm['optional_text']}"
        cards.append({
            "kind": "Confusion",
            "front": cm["optional_text"] or f"Confused at {cm['lesson_title']}",
            "back": detail,
        })

    return render_template(
        "print_revision_cards.html",
        page_title=f"Revision Cards · {course['title']}",
        course=course,
        cards=cards,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Profile: XP / levels / streaks / achievements (Stage 8 integration)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/profile")
@student_required
def profile():
    """Full gamification profile: level progress, streaks, ledger, badges."""
    db = get_db()
    student_id = session["user_id"]

    snapshot = game.gamification_snapshot(db, student_id)
    achievements = game.achievement_summary(db, student_id)
    ledger = game.recent_xp_events(db, student_id, limit=20)

    return render_template(
        "profile.html",
        page_title="My Progress",
        snapshot=snapshot,
        achievements=achievements,
        ledger=ledger,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Search (Stage 9)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/search")
@login_required
def search_page():
    """
    SQLite LIKE search across published courses, lesson titles (own
    enrolled courses only) and the student's own notes. No external
    search engine required at this scale.
    """
    db = get_db()
    q = request.args.get("q", "").strip()
    results_courses = []
    results_lessons = []
    results_notes = []

    if q:
        like = f"%{q}%"

        results_courses = db.execute(
            "SELECT c.*, u.full_name AS instructor_name "
            "FROM courses c JOIN users u ON c.instructor_id = u.id "
            "WHERE c.status='published' AND "
            "(c.title LIKE ? OR c.description LIKE ? OR c.category LIKE ?) "
            "ORDER BY c.title LIMIT 20",
            (like, like, like),
        ).fetchall()

        if session.get("role") == "student":
            results_lessons = db.execute(
                "SELECT l.title AS lesson_title, l.id AS lesson_id, "
                "l.lesson_type, m.id AS module_id, m.course_id, c.title AS course_title "
                "FROM lessons l "
                "JOIN modules m ON l.module_id = m.id "
                "JOIN courses c ON m.course_id = c.id "
                "JOIN enrollments e ON e.course_id = c.id AND e.student_id = ? "
                "WHERE c.status='published' AND (l.title LIKE ? OR l.description LIKE ?) "
                "ORDER BY c.title, m.position, l.position LIMIT 30",
                (session["user_id"], like, like),
            ).fetchall()

            results_notes = db.execute(
                "SELECT vn.note_text, vn.timestamp_seconds, vn.lesson_id, "
                "m.id AS module_id, m.course_id, l.title AS lesson_title "
                "FROM video_notes vn "
                "JOIN lessons l ON vn.lesson_id = l.id "
                "JOIN modules m ON l.module_id = m.id "
                "WHERE vn.student_id = ? AND vn.note_text LIKE ? "
                "ORDER BY vn.created_at DESC LIMIT 30",
                (session["user_id"], like),
            ).fetchall()

    return render_template(
        "search_results.html",
        page_title=f"Search · {q}" if q else "Search",
        q=q,
        courses=results_courses,
        lessons=results_lessons,
        notes=results_notes,
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Coding Playground API (Stage 9)
#
# SECURITY: execution happens ONLY through utils/playground.run_python_code
# which runs `python -I -B main.py` in a temp dir with timeout, no shell,
# output caps and cleanup. See that module's security notes. This is a
# LOCAL EDUCATIONAL SANDBOX, not production RCE infrastructure.
# ═══════════════════════════════════════════════════════════════════════════

def _exercise_for_student(exercise_id):
    """
    Load a coding exercise and verify the logged-in student is enrolled
    in its course. Returns (exercise, lesson) or (None, None).
    """
    db = get_db()
    exercise = db.execute(
        "SELECT * FROM coding_exercises WHERE id = ?", (exercise_id,)
    ).fetchone()
    if not exercise:
        return None, None
    lesson = db.execute(
        "SELECT l.*, m.course_id FROM lessons l "
        "JOIN modules m ON l.module_id = m.id WHERE l.id = ?",
        (exercise["lesson_id"],),
    ).fetchone()
    if not lesson:
        return None, None
    enrolled = db.execute(
        "SELECT 1 FROM enrollments WHERE student_id = ? AND course_id = ?",
        (session["user_id"], lesson["course_id"]),
    ).fetchone()
    if not enrolled:
        return None, None
    return exercise, lesson


def _save_student_code(db, exercise_id, code, output=""):
    """Upsert the student's latest playground code + last output."""
    db.execute(
        "INSERT INTO student_code (student_id, exercise_id, code, last_output) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(student_id, exercise_id) DO UPDATE SET "
        "code=excluded.code, last_output=excluded.last_output, "
        "updated_at=datetime('now')",
        (session["user_id"], exercise_id, code, output),
    )
    db.commit()


@app.route("/api/exercises/<int:exercise_id>/run", methods=["POST"])
@student_required
def api_run_exercise(exercise_id):
    """
    Run student Python code through the local sandbox, persist it, and
    compare stdout with expected_output when the instructor set one.
    """
    exercise, lesson = _exercise_for_student(exercise_id)
    if not exercise:
        return jsonify({"error": "Exercise not found or not enrolled"}), 404

    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not isinstance(code, str):
        return jsonify({"error": "Invalid code payload"}), 400

    result = playground.run_python_code(code)

    passed = None
    if result["ok"] and exercise["expected_output"]:
        # Python on Windows writes CRLF while seeded expected output uses LF.
        # Compare logical lines so a correct learner solution is never marked
        # wrong merely because of the host operating system's newline style.
        expected = exercise["expected_output"].replace("\r\n", "\n").replace("\r", "\n").strip()
        actual = result["stdout"].replace("\r\n", "\n").replace("\r", "\n").strip()
        passed = (actual == expected)

    _save_student_code(get_db(), exercise_id, code,
                       result["stdout"] or result["stderr"])

    response = {
        "ok": result["ok"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "timed_out": result["timed_out"],
        "rejected_module": result["rejected_module"],
        "duration_ms": result["duration_ms"],
        "passed": passed,
    }
    if passed is not None:
        response["expected_output"] = exercise["expected_output"]
    return jsonify(response)


@app.route("/api/exercises/<int:exercise_id>/save", methods=["POST"])
@student_required
def api_save_exercise(exercise_id):
    """Persist the student's code WITHOUT running it (draft save)."""
    exercise, lesson = _exercise_for_student(exercise_id)
    if not exercise:
        return jsonify({"error": "Exercise not found or not enrolled"}), 404

    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not isinstance(code, str) or len(code) > playground.MAX_CODE_LENGTH:
        return jsonify({"error": "Invalid code payload"}), 400

    _save_student_code(get_db(), exercise_id, code)
    return jsonify({"ok": True})


@app.route("/api/exercises/<int:exercise_id>/reset", methods=["POST"])
@student_required
def api_reset_exercise(exercise_id):
    """Restore starter code (clears saved draft)."""
    exercise, lesson = _exercise_for_student(exercise_id)
    if not exercise:
        return jsonify({"error": "Exercise not found or not enrolled"}), 404

    db = get_db()
    db.execute(
        "DELETE FROM student_code WHERE student_id = ? AND exercise_id = ?",
        (session["user_id"], exercise_id),
    )
    db.commit()
    return jsonify({"ok": True, "starter_code": exercise["starter_code"]})


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Instructor lesson tools: transcript CRUD + coding exercise (Stage 9)
# ═══════════════════════════════════════════════════════════════════════════

_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})$")


def _parse_timestamp(raw):
    """
    Accept '75', '1:15' or '01:02:03'; return seconds as float, or None.
    Bare seconds are also accepted so instructors can paste either way.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return float(raw)
    m = _TS_RE.match(raw)
    if not m:
        return None
    h, mnt, sec = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec)


@app.route("/instructor/lessons/<int:lesson_id>/tools")
@instructor_required
def instructor_lesson_tools(lesson_id):
    """Manage transcript segments + coding exercise for ONE owned lesson."""
    course, mod, lesson = _get_instructor_lesson(lesson_id)
    if not course:
        abort(404)
    db = get_db()

    segments = db.execute(
        "SELECT * FROM lesson_transcript_segments WHERE lesson_id = ? "
        "ORDER BY position, start_seconds",
        (lesson_id,),
    ).fetchall()
    exercise = db.execute(
        "SELECT * FROM coding_exercises WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()

    return render_template(
        "instructor_lesson_tools.html",
        page_title=f"Lesson Tools · {lesson['title']}",
        course=course,
        module=mod,
        lesson=lesson,
        segments=segments,
        exercise=exercise,
        format_ts=_format_timestamp,
    )


@app.route("/instructor/lessons/<int:lesson_id>/transcript/add", methods=["POST"])
@instructor_required
def instructor_transcript_add(lesson_id):
    course, mod, lesson = _get_instructor_lesson(lesson_id)
    if not course:
        abort(404)
    db = get_db()

    seconds = _parse_timestamp(request.form.get("start_time", ""))
    text = request.form.get("text", "").strip()
    if seconds is None or not text:
        flash("A valid start time and segment text are required.", "error")
        return redirect(url_for("instructor_lesson_tools", lesson_id=lesson_id))

    position = db.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM lesson_transcript_segments "
        "WHERE lesson_id = ?",
        (lesson_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO lesson_transcript_segments (lesson_id, start_seconds, text, position) "
        "VALUES (?, ?, ?, ?)",
        (lesson_id, seconds, text[:2000], position),
    )
    db.commit()
    flash("Transcript segment added.", "success")
    return redirect(url_for("instructor_lesson_tools", lesson_id=lesson_id))


@app.route("/instructor/transcript/<int:segment_id>/edit", methods=["POST"])
@instructor_required
def instructor_transcript_edit(segment_id):
    db = get_db()
    segment = db.execute(
        "SELECT * FROM lesson_transcript_segments WHERE id = ?", (segment_id,)
    ).fetchone()
    if not segment:
        abort(404)
    course, mod, lesson = _get_instructor_lesson(segment["lesson_id"])
    if not course:
        abort(404)

    seconds = _parse_timestamp(request.form.get("start_time", ""))
    text = request.form.get("text", "").strip()
    if seconds is None or not text:
        flash("A valid start time and segment text are required.", "error")
    else:
        db.execute(
            "UPDATE lesson_transcript_segments SET start_seconds = ?, text = ? "
            "WHERE id = ?",
            (seconds, text[:2000], segment_id),
        )
        db.commit()
        flash("Segment updated.", "success")
    return redirect(url_for("instructor_lesson_tools", lesson_id=segment["lesson_id"]))


@app.route("/instructor/transcript/<int:segment_id>/delete", methods=["POST"])
@instructor_required
def instructor_transcript_delete(segment_id):
    db = get_db()
    segment = db.execute(
        "SELECT * FROM lesson_transcript_segments WHERE id = ?", (segment_id,)
    ).fetchone()
    if not segment:
        abort(404)
    course, mod, lesson = _get_instructor_lesson(segment["lesson_id"])
    if not course:
        abort(404)

    db.execute("DELETE FROM lesson_transcript_segments WHERE id = ?", (segment_id,))
    db.commit()
    flash("Segment deleted.", "success")
    return redirect(url_for("instructor_lesson_tools", lesson_id=segment["lesson_id"]))


@app.route("/instructor/lessons/<int:lesson_id>/exercise", methods=["POST"])
@instructor_required
def instructor_exercise_save(lesson_id):
    """Create or update the coding exercise attached to one owned lesson."""
    course, mod, lesson = _get_instructor_lesson(lesson_id)
    if not course:
        abort(404)
    db = get_db()

    instructions = request.form.get("instructions", "").strip()
    starter = request.form.get("starter_code", "")
    expected = request.form.get("expected_output", "").strip() or None

    if len(starter) > playground.MAX_CODE_LENGTH:
        flash("Starter code is too long.", "error")
        return redirect(url_for("instructor_lesson_tools", lesson_id=lesson_id))

    existing = db.execute(
        "SELECT id FROM coding_exercises WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE coding_exercises SET instructions=?, starter_code=?, "
            "expected_output=?, updated_at=datetime('now') WHERE id=?",
            (instructions, starter, expected, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO coding_exercises (lesson_id, instructions, starter_code, expected_output) "
            "VALUES (?, ?, ?, ?)",
            (lesson_id, instructions, starter, expected),
        )
    db.commit()
    flash("Coding exercise saved. Students see it inside the lesson.", "success")
    return redirect(url_for("instructor_lesson_tools", lesson_id=lesson_id))


@app.route("/instructor/exercise/<int:exercise_id>/delete", methods=["POST"])
@instructor_required
def instructor_exercise_delete(exercise_id):
    db = get_db()
    exercise = db.execute(
        "SELECT * FROM coding_exercises WHERE id = ?", (exercise_id,)
    ).fetchone()
    if not exercise:
        abort(404)
    course, mod, lesson = _get_instructor_lesson(exercise["lesson_id"])
    if not course:
        abort(404)

    db.execute("DELETE FROM coding_exercises WHERE id = ?", (exercise_id,))
    db.commit()
    flash("Coding exercise removed from this lesson.", "success")
    return redirect(url_for("instructor_lesson_tools", lesson_id=exercise["lesson_id"]))


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Course management deletions (Stage 9)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/instructor/resources/<int:resource_id>/delete", methods=["POST"])
@instructor_required
def instructor_delete_resource(resource_id):
    db = get_db()
    res = db.execute(
        "SELECT * FROM lesson_resources WHERE id = ?", (resource_id,)
    ).fetchone()
    if not res:
        abort(404)
    lesson = db.execute(
        "SELECT * FROM lessons WHERE id = ?", (res["lesson_id"],)
    ).fetchone()
    mod = db.execute(
        "SELECT * FROM modules WHERE id = ?", (lesson["module_id"],)
    ).fetchone() if lesson else None
    course = _get_instructor_course(mod["course_id"]) if mod else None
    if not course:
        abort(404)

    _safe_unlink(res["file_path"])
    db.execute("DELETE FROM lesson_resources WHERE id = ?", (resource_id,))
    db.commit()
    flash("Resource deleted.", "success")
    return redirect(url_for("instructor_course_detail", course_id=course["id"]))


@app.route("/instructor/lessons/<int:lesson_id>/delete", methods=["POST"])
@instructor_required
def instructor_delete_lesson(lesson_id):
    course, mod, lesson = _get_instructor_lesson(lesson_id)
    if not course:
        abort(404)
    db = get_db()

    _delete_lesson_files(lesson)      # videos + resources off disk
    _purge_lesson_rows(lesson_id)     # notes/markers/progress (no cascade)
    db.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
    db.commit()
    flash(f"Lesson “{lesson['title']}” deleted.", "success")
    return redirect(url_for("instructor_course_detail", course_id=course["id"]))


@app.route("/instructor/modules/<int:module_id>/delete", methods=["POST"])
@instructor_required
def instructor_delete_module(module_id):
    db = get_db()
    mod = db.execute("SELECT * FROM modules WHERE id = ?", (module_id,)).fetchone()
    if not mod:
        abort(404)
    course = _get_instructor_course(mod["course_id"])
    if not course:
        abort(404)

    lessons = db.execute(
        "SELECT * FROM lessons WHERE module_id = ?", (module_id,)
    ).fetchall()
    for lesson in lessons:
        _delete_lesson_files(lesson)
        _purge_lesson_rows(lesson["id"])

    # modules cascade to lessons/quizzes/resources/transcripts/exercises;
    # revision_activity references lessons without FK — clean manually.
    db.execute(
        "DELETE FROM revision_activity WHERE lesson_id IN "
        "(SELECT id FROM lessons WHERE module_id = ?)",
        (module_id,),
    )
    db.execute("DELETE FROM modules WHERE id = ?", (module_id,))
    db.commit()
    flash(f"Module “{mod['title']}” and its lessons were deleted.", "success")
    return redirect(url_for("instructor_course_detail", course_id=course["id"]))


@app.route("/instructor/courses/<int:course_id>/delete", methods=["GET", "POST"])
@instructor_required
def instructor_delete_course(course_id):
    """
    GET shows a confirmation page listing what will be removed;
    POST performs the deletion (ownership enforced both times).
    """
    course = _get_instructor_course(course_id)
    if not course:
        abort(404)
    db = get_db()

    if request.method == "GET":
        stats = {
            "modules": db.execute(
                "SELECT COUNT(*) FROM modules WHERE course_id = ?", (course_id,)
            ).fetchone()[0],
            "lessons": _count_lessons(course_id),
            "students": db.execute(
                "SELECT COUNT(*) FROM enrollments WHERE course_id = ?", (course_id,)
            ).fetchone()[0],
        }
        return render_template(
            "instructor_course_delete.html",
            page_title=f"Delete · {course['title']}",
            course=course,
            stats=stats,
        )

    # POST: collect every file owned by every lesson, purge non-cascading
    # student rows, then let cascades handle the rest.
    lessons = db.execute(
        "SELECT l.* FROM lessons l JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchall()
    for lesson in lessons:
        _delete_lesson_files(lesson)

    db.execute(
        "DELETE FROM lesson_progress WHERE lesson_id IN "
        "(SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
        " WHERE m.course_id = ?)",
        (course_id,),
    )
    db.execute(
        "DELETE FROM video_notes WHERE lesson_id IN "
        "(SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
        " WHERE m.course_id = ?)",
        (course_id,),
    )
    db.execute(
        "DELETE FROM video_markers WHERE lesson_id IN "
        "(SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
        " WHERE m.course_id = ?)",
        (course_id,),
    )
    db.execute(
        "DELETE FROM revision_activity WHERE course_id = ?", (course_id,)
    )
    db.execute("DELETE FROM enrollments WHERE course_id = ?", (course_id,))
    thumbnail = course["thumbnail"]
    db.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    db.commit()
    _safe_unlink(thumbnail)

    flash(f"Course “{course['title']}” was permanently deleted.", "success")
    return redirect(url_for("instructor_courses"))


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES — Static file serving for uploads
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """Serve files from the uploads directory."""
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, port=5001)
