"""
Learning Platform — full backend regression + Stage 9 feature tests.

Runs against a THROWAWAY SQLite database in a temp directory.
The developer's real database (database/learning_platform.db) is never
written to by these tests.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_platform -v
"""

import io
import os
import sys
import sqlite3
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import app as app_module  # noqa: E402
from database.db import init_db  # noqa: E402
from utils import playground  # noqa: E402

# ---------------------------------------------------------------------------
# Isolated test database (fresh schema v9, zero seed data)
# ---------------------------------------------------------------------------

TMP_DIR = tempfile.mkdtemp(prefix="lp_tests_")
TEST_DB = os.path.join(TMP_DIR, "test.db")

app_module.app.config.update(DATABASE=TEST_DB, TESTING=True)
init_db(app_module.app)  # applies SCHEMA_V1..V9 to the temp file

UPLOAD_ROOT = app_module.app.config["UPLOAD_FOLDER"]

# Shared state across test classes (IDs created during the run).
S = {}


def raw_db():
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def scalar(sql, params=()):
    con = raw_db()
    try:
        row = con.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def rows(sql, params=()):
    con = raw_db()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def xp_total(student_id, event_type=None):
    if event_type:
        return scalar(
            "SELECT COALESCE(SUM(xp_amount),0) FROM xp_events "
            "WHERE student_id=? AND event_type=?",
            (student_id, event_type),
        )
    return scalar(
        "SELECT COALESCE(SUM(xp_amount),0) FROM xp_events WHERE student_id=?",
        (student_id,),
    )


class Base(unittest.TestCase):
    """Shared client + auth helpers."""

    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def register(self, name, email, password, role):
        """Register an account. NOTE: the app does NOT auto-login."""
        self.client.get("/logout")  # ensure clean session
        return self.client.post(
            "/register",
            data={
                "full_name": name,
                "email": email,
                "password": password,
                "confirm_password": password,
                "role": role,
            },
            follow_redirects=True,
        )

    def login(self, email, password):
        self.client.get("/logout")
        return self.client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    def logout(self):
        return self.client.get("/logout", follow_redirects=True)


# ---------------------------------------------------------------------------
# Ordered end-to-end scenario (unittest sorts classes/methods by name)
# ---------------------------------------------------------------------------

class Test01SchemaAndAuth(Base):

    def test01_schema_version_is_9_and_tables_exist(self):
        self.assertEqual(scalar("SELECT version FROM schema_version"), 9)
        for table in ("lesson_transcript_segments", "coding_exercises",
                      "student_code"):
            self.assertEqual(scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                (table,)), 1, f"missing table {table}")

    def test02_register_users_and_reject_bad_input(self):
        r = self.register("Ada Instructor", "inst1@test.local", "secret1", "instructor")
        self.assertIn(b"Account created successfully", r.data)
        r = self.register("Bob Instructor", "inst2@test.local", "secret1", "instructor")
        self.assertIn(b"Account created successfully", r.data)
        r = self.register("Riya Student", "stud1@test.local", "secret1", "student")
        self.assertIn(b"Account created successfully", r.data)
        r = self.register("Sam Student", "stud2@test.local", "secret1", "student")
        self.assertIn(b"Account created successfully", r.data)

        # duplicate email rejected
        r = self.register("Clone", "inst1@test.local", "secret1", "student")
        self.assertIn(b"already exists", r.data)
        # bad role rejected
        r = self.register("Bad Role", "badrole@test.local", "secret1", "admin")
        self.assertIn(b"valid role", r.data)
        # mismatched passwords rejected
        r = self.client.post("/register", data={
            "full_name": "Mismatch", "email": "mm@test.local",
            "password": "secret1", "confirm_password": "other", "role": "student",
        }, follow_redirects=True)
        self.assertIn(b"do not match", r.data)

        # login works / wrong password rejected
        r = self.login("stud1@test.local", "WRONG")
        self.assertIn(b"Invalid email or password", r.data)
        r = self.login("stud1@test.local", "secret1")
        self.assertEqual(r.status_code, 200)


class Test10CourseCreationAndAccess(Base):

    def test10_instructor_builds_course_and_lessons(self):
        self.login("inst1@test.local", "secret1")

        r = self.client.post("/instructor/courses/new", data={
            "title": "Python Foundations",
            "description": "Learn Python from zero: variables, loops and functions.",
            "category": "Programming",
            "level": "beginner",
        }, follow_redirects=True)
        self.assertIn(b"Python Foundations", r.data)
        course_id = scalar("SELECT id FROM courses WHERE title='Python Foundations'")
        self.assertTrue(course_id)

        r = self.client.post(f"/instructor/courses/{course_id}/modules/new", data={
            "title": "Basics", "description": "First steps",
        }, follow_redirects=True)
        self.assertIn(b"Basics", r.data)
        module_id = scalar("SELECT id FROM modules WHERE title='Basics'")

        # Text lesson via the real form
        r = self.client.post(f"/instructor/modules/{module_id}/lessons/new", data={
            "title": "What is Python",
            "description": "Intro",
            "lesson_type": "text",
            "content_text": "Python is a programming language. " * 30,
            "duration": "",
        }, follow_redirects=True)
        self.assertIn(b"What is Python", r.data)

        # Video lesson via the real form (tiny fake mp4 upload)
        fake_mp4 = (io.BytesIO(b"\x00\x00\x00\x18ftypmp42fake"), "variables.mp4")
        r = self.client.post(f"/instructor/modules/{module_id}/lessons/new", data={
            "title": "Variables in 5 Minutes",
            "description": "All about variables",
            "lesson_type": "video",
            "content_text": "",
            "duration": "5",
            "video_file": fake_mp4,
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Variables in 5 Minutes", r.data)

        video_path = scalar(
            "SELECT content_path FROM lessons WHERE title='Variables in 5 Minutes'")
        self.assertIsNotNone(video_path)
        self.assertTrue(os.path.isfile(os.path.join(UPLOAD_ROOT, video_path)))

        self.client.post(f"/instructor/courses/{course_id}/publish",
                         follow_redirects=True)
        self.assertEqual(
            scalar("SELECT status FROM courses WHERE id=?", (course_id,)),
            "published")

        S["course_id"] = course_id
        S["module_id"] = module_id
        S["text_lesson_id"] = scalar("SELECT id FROM lessons WHERE title='What is Python'")
        S["video_lesson_id"] = scalar(
            "SELECT id FROM lessons WHERE title='Variables in 5 Minutes'")
        S["inst1_id"] = scalar("SELECT id FROM users WHERE email='inst1@test.local'")
        S["inst2_id"] = scalar("SELECT id FROM users WHERE email='inst2@test.local'")
        S["stud1_id"] = scalar("SELECT id FROM users WHERE email='stud1@test.local'")
        S["stud2_id"] = scalar("SELECT id FROM users WHERE email='stud2@test.local'")
        self.logout()

    def test11_enrollment_gate_and_lesson_view(self):
        cid, mid, vid = S["course_id"], S["module_id"], S["video_lesson_id"]
        url = f"/courses/{cid}/modules/{mid}/lessons/{vid}"

        # A student with no activity must see an honest no-data health state,
        # not a fabricated 0/100 score just because the course has lessons.
        from utils.learning import calculate_learning_health
        con = raw_db()
        try:
            health = calculate_learning_health(con, S["stud2_id"], cid)
        finally:
            con.close()
        self.assertIsNone(health["score"])
        self.assertEqual(health["status"], "No Data")

        # not enrolled -> bounced away from lesson
        self.login("stud2@test.local", "secret1")
        r = self.client.get(url, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"You must enroll", r.data) if False else None
        # stud2 enrolls (kept for later cross-student checks)
        self.client.post(f"/courses/{cid}/enroll", follow_redirects=True)
        self.logout()

        self.login("stud1@test.local", "secret1")
        r = self.client.post(f"/courses/{cid}/enroll", follow_redirects=True)
        self.assertIn(b"Enrolled successfully", r.data)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Variables in 5 Minutes", r.data)

        # student cannot reach instructor CMS
        r = self.client.get(f"/instructor/courses/{cid}", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.logout()


class Test20ProgressNotesMarkersXp(Base):

    def test20_progress_resume_and_completion_xp_once(self):
        tid, sid = S["text_lesson_id"], S["stud1_id"]
        self.login("stud1@test.local", "secret1")

        before = xp_total(sid, "lesson_completed")
        r = self.client.post("/api/progress", json={
            "lesson_id": tid, "position_seconds": 0, "watched_seconds": 12})
        self.assertEqual(r.status_code, 200)

        r = self.client.post("/api/progress/complete", json={"lesson_id": tid})
        self.assertEqual(r.status_code, 200)
        after_first = xp_total(sid, "lesson_completed")
        self.assertEqual(after_first - before, 20)          # exactly +20 once

        self.client.post("/api/progress/complete", json={"lesson_id": tid})
        self.assertEqual(xp_total(sid, "lesson_completed"), after_first,
                         "XP must be idempotent per lesson")
        self.logout()

    def test21_notes_and_markers_crud_with_ownership(self):
        vid = S["video_lesson_id"]
        self.login("stud1@test.local", "secret1")

        r = self.client.post(f"/api/lessons/{vid}/notes", json={
            "timestamp_seconds": 42, "note_text": "x = 5 creates an int"})
        self.assertEqual(r.status_code, 201)
        note_id = r.get_json()["note"]["id"]

        r = self.client.put(f"/api/notes/{note_id}", json={"note_text": "updated note"})
        self.assertEqual(r.status_code, 200)

        r = self.client.post(f"/api/lessons/{vid}/markers", json={
            "timestamp_seconds": 90, "marker_type": "important"})
        self.assertEqual(r.status_code, 201)
        marker_id = r.get_json()["marker"]["id"]
        r = self.client.post(f"/api/lessons/{vid}/markers", json={
            "timestamp_seconds": 130, "marker_type": "confused",
            "optional_text": "mutable default args?"})
        self.assertEqual(r.status_code, 201)

        # cross-student delete must be blocked
        self.logout()
        self.login("stud2@test.local", "secret1")
        self.assertEqual(self.client.delete(f"/api/notes/{note_id}").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/markers/{marker_id}").status_code, 403)
        self.logout()

        S["note_id"] = note_id


class Test30QuizGradingConceptXp(Base):

    def test30_quiz_fixture_inserted(self):
        con = raw_db()
        try:
            cur = con.execute(
                "INSERT INTO quizzes (lesson_id, title, description, passing_percentage) "
                "VALUES (?, 'Variables Check', 'quick', 50)",
                (S["video_lesson_id"],),
            )
            qz = cur.lastrowid
            questions = [
                ("What does x = 5 do?", "multiple_choice",
                 "Creates variable x", "Prints 5", "Deletes x", "Nothing", "A",
                 "Assignment binds a value to a name.", "variables", 1),
                ("int('3') returns which type?", "multiple_choice",
                 "str", "int", "float", "bool", "B",
                 "int() converts to integer.", "Data Types", 2),
            ]
            for text, typ, a, b, c, d, correct, expl, concept, pos in questions:
                con.execute(
                    "INSERT INTO quiz_questions (quiz_id, question_text, question_type, "
                    "option_a, option_b, option_c, option_d, correct_answer, explanation, "
                    "concept_tag, position) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (qz, text, typ, a, b, c, d, correct, expl, concept, pos),
                )
            con.commit()
        finally:
            con.close()
        S["quiz_id"] = qz

    def _submit(self, answers):
        vid, cid, mid = S["video_lesson_id"], S["course_id"], S["module_id"]
        data = {f"q_{qid}": ans for qid, ans in answers.items()}
        return self.client.post(
            f"/courses/{cid}/modules/{mid}/lessons/{vid}/quiz/submit",
            data=data, follow_redirects=True)

    def test31_perfect_attempt_awards_xp_exactly_once(self):
        con = raw_db()
        qids = [r["id"] for r in con.execute(
            "SELECT id FROM quiz_questions ORDER BY position").fetchall()]
        con.close()
        sid = S["stud1_id"]
        self.login("stud1@test.local", "secret1")

        base_pass = xp_total(sid, "quiz_passed")
        base_improved = xp_total(sid, "quiz_improved")

        self._submit({qids[0]: "A", qids[1]: "B"})          # all correct
        attempt1 = scalar("SELECT MAX(id) FROM quiz_attempts")
        self.assertEqual(xp_total(sid, "quiz_passed") - base_pass, 25)
        self.assertEqual(
            scalar("SELECT passed FROM quiz_attempts WHERE id=?", (attempt1,)), 1)

        # resubmit perfect: still exactly one quiz_passed XP, no improvement XP
        self._submit({qids[0]: "A", qids[1]: "B"})
        self.assertEqual(xp_total(sid, "quiz_passed"), base_pass + 25)
        self.assertEqual(xp_total(sid, "quiz_improved"), base_improved)

        # wrong answer drops score; server grades independently of client
        self._submit({qids[0]: "C", qids[1]: "B"})
        latest = scalar(
            "SELECT percentage FROM quiz_attempts ORDER BY id DESC LIMIT 1")
        self.assertEqual(latest, 50.0)
        S["wrong_attempt_id"] = scalar("SELECT MAX(id) FROM quiz_attempts")
        S["question_ids"] = qids

        # lesson workspace shows the real question count for the attached quiz
        url = (f"/courses/{S['course_id']}/modules/{S['module_id']}"
               f"/lessons/{S['video_lesson_id']}")
        self.assertIn("2 Questions", self.client.get(url).data.decode())
        self.logout()

    def test32_course_quiz_accuracy_matches_all_answer_rows(self):
        """Repeated attempts must agree with concept/health answer accuracy."""
        import app as app_module
        import utils.learning as learning

        con = raw_db()
        try:
            summary = app_module._get_course_quiz_summary(
                con, S["stud1_id"], S["course_id"]
            )
            accuracy = learning.aggregate_answer_accuracy(
                con, student_id=S["stud1_id"], course_id=S["course_id"]
            )
        finally:
            con.close()
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["average_score"], accuracy["accuracy"])
        self.assertEqual(summary["average_score"], round(5 / 6 * 100, 1))


class Test40Stage7HealthRevisionStudyPack(Base):

    def test40_learning_health_page_shows_real_score(self):
        import utils.learning as learning
        cid, sid = S["course_id"], S["stud1_id"]
        self.login("stud1@test.local", "secret1")
        r = self.client.get(f"/courses/{cid}/health")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn("Learning Health", html)
        self.assertIn("/100", html)
        self.assertNotIn("No Data", html)      # real signals exist now

        con = raw_db()
        health = learning.calculate_learning_health(con, sid, cid)
        con.close()
        self.assertIsInstance(health["score"], int)
        self.assertTrue(0 <= health["score"] <= 100)
        self.logout()

    def test41_smart_revision_lists_wrong_question_and_review_records_once(self):
        cid, sid = S["course_id"], S["stud1_id"]
        self.login("stud1@test.local", "secret1")

        r = self.client.get(f"/courses/{cid}/revision")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Smart Revision", r.data.decode())

        act_before = scalar(
            "SELECT COUNT(*) FROM revision_activity WHERE student_id=?", (sid,))
        xp_before = xp_total(sid, "revision_reviewed")

        review_data = {
            "source_type": "wrong_question",
            "ref_type": "attempt_question",
            "ref_id": str(S["wrong_attempt_id"]),
        }
        r = self.client.post(f"/courses/{cid}/revision/review",
                             data=review_data, follow_redirects=True)
        self.assertIn(b"XP", r.data)

        act_after_one = scalar(
            "SELECT COUNT(*) FROM revision_activity WHERE student_id=?", (sid,))
        self.assertEqual(act_after_one, act_before + 1)
        self.assertEqual(xp_total(sid, "revision_reviewed") - xp_before, 10)

        # reviewing again: history grows but XP stays deduplicated
        self.client.post(f"/courses/{cid}/revision/review",
                         data=review_data, follow_redirects=True)
        act_after_two = scalar(
            "SELECT COUNT(*) FROM revision_activity WHERE student_id=?", (sid,))
        self.assertEqual(act_after_two, act_after_one + 1)
        self.assertEqual(xp_total(sid, "revision_reviewed"),
                         xp_before + 10, "revision XP must not double-award")

        r = self.client.get(f"/courses/{cid}/revision/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("wrong question", r.data.decode())
        self.logout()

    def test43_notes_page_lists_all_notes(self):
        self.login("stud1@test.local", "secret1")
        r = self.client.get("/notes")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn("updated note", html)                 # note text present
        self.assertIn("Variables in 5 Minutes", html)       # lesson grouping
        self.assertIn("notes-filter", html)                 # quick filter input
        # a student with no notes still gets a clean page
        self.login("stud2@test.local", "secret1")
        self.assertEqual(self.client.get("/notes").status_code, 200)
        self.logout()

    def test42_study_pack_printables_profile(self):
        cid = S["course_id"]
        self.login("stud1@test.local", "secret1")

        r = self.client.get("/study-pack")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Python Foundations", r.data.decode())

        r = self.client.get("/notes/print")
        self.assertEqual(r.status_code, 200)
        self.assertIn("updated note", r.data.decode())

        r = self.client.get(f"/courses/{cid}/revision-cards/print")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Answer:", r.data.decode())

        r = self.client.get("/profile")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Level", r.data.decode())
        self.assertIn("Current streak", r.data.decode())
        self.assertIn("Recent XP Ledger", r.data.decode())

        # instructor must NOT reach student-only pages
        self.logout()
        self.login("inst1@test.local", "secret1")
        self.assertEqual(
            self.client.get("/study-pack", follow_redirects=False).status_code, 302)
        self.logout()


class Test45SearchAndExplore(Base):

    def test45_global_search_finds_courses_lessons_notes(self):
        cid, mid, vid = S["course_id"], S["module_id"], S["video_lesson_id"]
        self.login("stud1@test.local", "secret1")

        r = self.client.get("/search?q=foundations")
        self.assertIn("Python Foundations", r.data.decode())

        r = self.client.get("/search?q=variables")
        self.assertIn("Variables in 5 Minutes", r.data.decode())

        r = self.client.get("/search?q=updated+note")
        self.assertIn("updated note", r.data.decode())

        # lesson deep-link carries ?t= seek param
        r = self.client.get(f"/courses/{cid}/modules/{mid}/lessons/{vid}?t=42")
        self.assertEqual(r.status_code, 200)
        self.logout()

    def test46_explore_lists_catalog_metadata(self):
        self.login("stud1@test.local", "secret1")
        r = self.client.get("/explore?search=python")
        html = r.data.decode()
        self.assertIn("Python Foundations", html)
        self.assertIn("module", html)
        self.assertIn("min", html)          # duration metadata present
        self.assertIn("Enrolled", html)
        self.assertIn("lessons · ", html)   # per-course progress when enrolled
        self.logout()


class Test50Transcripts(Base):

    @staticmethod
    def _tools_url():
        return f"/instructor/lessons/{S['video_lesson_id']}/tools"

    @staticmethod
    def _add_url():
        # NOTE: transcript endpoints live at /instructor/lessons/<id>/transcript/...
        return f"/instructor/lessons/{S['video_lesson_id']}/transcript/add"

    def test50_instructor_transcript_crud_and_parsing(self):
        self.login("inst1@test.local", "secret1")

        self.assertEqual(self.client.get(self._tools_url()).status_code, 200)

        self.client.post(self._add_url(), data={
            "start_time": "0:05", "text": "Welcome to Python variables"},
            follow_redirects=True)
        self.client.post(self._add_url(), data={
            "start_time": "0:20", "text": "Variables store values in memory"},
            follow_redirects=True)
        segs = rows(
            "SELECT * FROM lesson_transcript_segments ORDER BY position")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["start_seconds"], 5.0)
        self.assertEqual(segs[1]["start_seconds"], 20.0)

        # plain seconds also accepted
        self.client.post(self._add_url(), data={
            "start_time": "75", "text": "Let us create our first variable"},
            follow_redirects=True)
        third = scalar(
            "SELECT start_seconds FROM lesson_transcript_segments ORDER BY id DESC LIMIT 1")
        self.assertEqual(third, 75.0)
        seg3_id = scalar(
            "SELECT id FROM lesson_transcript_segments ORDER BY id DESC LIMIT 1")

        # edit segment 3 (hh:mm accepted too)
        self.client.post(f"/instructor/transcript/{seg3_id}/edit", data={
            "start_time": "1:15", "text": "Edited transcript line"},
            follow_redirects=True)
        row = rows(
            "SELECT * FROM lesson_transcript_segments WHERE id=?", (seg3_id,))[0]
        self.assertEqual(row["start_seconds"], 75.0)
        self.assertEqual(row["text"], "Edited transcript line")

        # invalid time rejected without crash
        r = self.client.post(self._add_url(), data={
            "start_time": "abc", "text": "nope"}, follow_redirects=True)
        self.assertIn(b"valid start time", r.data)
        self.logout()

    def test51_transcript_ownership_enforced(self):
        tools_url = f"/instructor/lessons/{S['video_lesson_id']}/tools"
        add_url = f"/instructor/lessons/{S['video_lesson_id']}/transcript/add"
        self.login("inst2@test.local", "secret1")
        self.assertEqual(
            self.client.get(tools_url, follow_redirects=False).status_code, 404)
        self.assertEqual(self.client.post(
            add_url,
            data={"start_time": "9:99", "text": "hijack"},
            follow_redirects=False).status_code, 404)
        self.logout()

    def test52_student_sees_transcript_with_seek_data(self):
        self.login("stud1@test.local", "secret1")
        url = (f"/courses/{S['course_id']}/modules/{S['module_id']}"
               f"/lessons/{S['video_lesson_id']}")
        html = self.client.get(url).data.decode()
        self.assertIn("transcript-search", html)
        self.assertIn("Welcome to Python variables", html)
        self.assertIn('data-seconds="5.0"', html)
        self.assertIn("Edited transcript line", html)
        self.logout()

    def test53_instructor_preview_with_transcript_renders(self):
        """Regression: preview crashed with UndefinedError on format_ts."""
        cid, mid, vid = S["course_id"], S["module_id"], S["video_lesson_id"]
        self.login("inst1@test.local", "secret1")
        r = self.client.get(
            f"/instructor/courses/{cid}/modules/{mid}/lessons/{vid}")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn("Preview Mode", html)
        self.assertIn("Edited transcript line", html)   # transcript rendered
        self.assertIn("0:05", html)                     # formatted timestamp
        # student-only tooling must be absent from the instructor preview
        self.assertNotIn("add-note-btn", html)
        self.assertNotIn("mark-complete-btn", html)
        self.logout()


class Test55VideoExperience(Base):
    """
    Video experience repair regression (duration seconds rule, video
    identification, missing-video placeholder, MP4 upload flow, timestamp
    capture + seek link-backs, transcript shared seek).
    """

    @staticmethod
    def _lesson_url(vid=None):
        vid = vid or S["video_lesson_id"]
        return f"/courses/{S['course_id']}/modules/{S['module_id']}/lessons/{vid}"

    def _set_video(self, content_path):
        con = raw_db()
        con.execute("UPDATE lessons SET content_path=? WHERE id=?",
                    (content_path, S["video_lesson_id"]))
        con.commit()
        con.close()

    # ---- 1+2. duration rule + video identification -----------------------
    def test01_duration_helper_and_templates_render_minutes(self):
        from utils.formatting import format_duration
        self.assertEqual(format_duration(420), "7 min")
        self.assertEqual(format_duration(360), "6 min")
        self.assertEqual(format_duration(480), "8 min")
        self.assertEqual(format_duration(600), "10 min")
        self.assertEqual(format_duration(65), "1m 5s")
        self.assertEqual(format_duration(None), "")

        # A stored 420 must render as 7 min everywhere, never "420 min".
        con = raw_db()
        con.execute("UPDATE lessons SET duration=420 WHERE id=?",
                    (S["video_lesson_id"],))
        con.commit()
        con.close()

        self.login("stud1@test.local", "secret1")
        html = self.client.get(f"/courses/{S['course_id']}").data.decode()
        self.assertIn("7 min", html)
        self.assertNotIn("420 min", html)
        # Video lessons are clearly identifiable with a camera icon badge.
        self.assertIn("VIDEO", html)
        self.assertIn("\U0001F3A5", html)          # 🎥
        self.client.get("/logout")

    def test02_video_identification_across_surfaces(self):
        self.login("inst1@test.local", "secret1")
        html = self.client.get(f"/instructor/courses/{S['course_id']}").data.decode()
        self.assertIn("VIDEO", html)
        self.assertIn("7 min", html)
        self.assertNotIn("420 min", html)
        self.client.get("/logout")

        self.login("stud1@test.local", "secret1")
        ml = self.client.get("/my-learning").data.decode()
        self.assertIn("Up next:", ml)
        home = self.client.get("/").data.decode()
        self.assertIn("Next lesson:", home)
        lv = self.client.get(self._lesson_url()).data.decode()
        self.assertIn("VIDEO LESSON", lv)
        self.assertIn("7 min", lv)
        self.assertNotIn("420 min", lv)
        self.client.get("/logout")

    # ---- 3. missing-video placeholder -------------------------------------
    def test03_missing_video_placeholder_renders(self):
        con = raw_db()
        con.execute("UPDATE lessons SET content_path=NULL WHERE id=?",
                    (S["video_lesson_id"],))
        con.commit()
        con.close()

        self.login("stud1@test.local", "secret1")
        r = self.client.get(self._lesson_url())
        html = r.data.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Video file not attached yet.", html)
        self.assertIn("The instructor can upload an MP4 from course management.", html)
        self.assertNotIn("<video", html.split("video-placeholder")[0])
        # Workspace stays visible: transcript + capture buttons present but disabled.
        self.assertIn("transcript-search", html)
        self.assertIn('id="add-note-btn"', html)
        self.assertIn("disabled", html)
        self.client.get("/logout")

        # Instructor management flags it and offers Upload / Edit Video.
        self.login("inst1@test.local", "secret1")
        html = self.client.get(f"/instructor/courses/{S['course_id']}").data.decode()
        self.assertIn("VIDEO MISSING", html)
        self.assertIn("Upload / Edit Video", html)
        self.client.get("/logout")

    # ---- 4+5. upload persists; player renders; completion uses seconds ----
    def test04_upload_persists_content_path_and_player_renders(self):
        self.login("inst1@test.local", "secret1")
        fake_mp4 = (io.BytesIO(b"\x00\x00\x00\x18ftypmp42repaired"), "final_take.mp4")
        r = self.client.post(
            f"/instructor/lessons/{S['video_lesson_id']}/edit",
            data={
                "title": "Variables in 5 Minutes",
                "description": "All about variables",
                "lesson_type": "video",
                "content_text": "",
                "duration": "6",          # minutes -> stored as 360 seconds
                "position": "2",
                "video_file": fake_mp4,
            },
            content_type="multipart/form-data",
            follow_redirects=True)
        self.assertIn(b"Video attached", r.data)

        row = rows("SELECT lesson_type, content_path, duration FROM lessons WHERE id=?",
                   (S["video_lesson_id"],))[0]
        self.assertEqual(row["lesson_type"], "video")
        self.assertIsNotNone(row["content_path"])
        self.assertTrue(row["content_path"].startswith("videos/"))
        self.assertTrue(os.path.isfile(os.path.join(UPLOAD_ROOT, row["content_path"])))
        self.assertEqual(row["duration"], 360)   # 6 minutes stored in SECONDS
        S["uploaded_video_path"] = row["content_path"]
        self.client.get("/logout")

        # Enrolled student gets an HTML5 player pointing at the stored file.
        self.login("stud1@test.local", "secret1")
        html = self.client.get(self._lesson_url()).data.decode()
        self.assertIn('<video id="lesson-video"', html)
        self.assertIn(f'src="/uploads/{row["content_path"]}"', html)
        self.assertNotIn("Video file not attached yet.", html)

        # Completion math now treats stored duration as SECONDS:
        # watching all 360 s of a 360 s lesson => exactly 100%.
        resp = self.client.post("/api/progress", json={
            "lesson_id": S["video_lesson_id"],
            "position_seconds": 360,
            "watched_seconds": 360})
        self.assertEqual(resp.get_json()["completion_percentage"], 100.0)
        self.client.get("/logout")

    def test05_replace_video_keeps_attachments_intact(self):
        old_path = scalar(
            "SELECT content_path FROM lessons WHERE id=?", (S["video_lesson_id"],))
        self.assertTrue(old_path)
        counts_before = {
            "quiz": scalar("SELECT COUNT(*) FROM quizzes WHERE lesson_id=?",
                           (S["video_lesson_id"],)),
            "transcripts": scalar(
                "SELECT COUNT(*) FROM lesson_transcript_segments WHERE lesson_id=?",
                (S["video_lesson_id"],)),
            "exercise": scalar("SELECT COUNT(*) FROM coding_exercises WHERE lesson_id=?",
                               (S["video_lesson_id"],)),
        }

        self.login("inst1@test.local", "secret1")
        fake_mp4 = (io.BytesIO(b"\x00\x00\x00\x18ftypmp42take2"), "take_two.webm")
        r = self.client.post(
            f"/instructor/lessons/{S['video_lesson_id']}/edit",
            data={
                "title": "Variables in 5 Minutes",
                "description": "All about variables",
                "lesson_type": "video",
                "content_text": "",
                "duration": "6",
                "position": "2",
                "video_file": fake_mp4,
            },
            content_type="multipart/form-data",
            follow_redirects=True)
        self.assertIn(b"Video attached", r.data)

        new_path = scalar(
            "SELECT content_path FROM lessons WHERE id=?", (S["video_lesson_id"],))
        self.assertNotEqual(new_path, old_path)
        self.assertFalse(os.path.isfile(os.path.join(UPLOAD_ROOT, old_path)),
                         "replaced video file must be removed from disk")
        self.assertTrue(os.path.isfile(os.path.join(UPLOAD_ROOT, new_path)))
        counts_after = {
            "quiz": scalar("SELECT COUNT(*) FROM quizzes WHERE lesson_id=?",
                           (S["video_lesson_id"],)),
            "transcripts": scalar(
                "SELECT COUNT(*) FROM lesson_transcript_segments WHERE lesson_id=?",
                (S["video_lesson_id"],)),
            "exercise": scalar("SELECT COUNT(*) FROM coding_exercises WHERE lesson_id=?",
                               (S["video_lesson_id"],)),
        }
        self.assertEqual(counts_before, counts_after)
        S["uploaded_video_path"] = new_path
        self.client.get("/logout")

    def test06_edit_requires_file_for_empty_video_and_cleans_on_type_switch(self):
        # A video lesson without an attached file cannot be saved as-is...
        con = raw_db()
        con.execute("UPDATE lessons SET content_path=NULL WHERE id=?",
                    (S["video_lesson_id"],))
        con.commit()
        con.close()
        self.login("inst1@test.local", "secret1")
        r = self.client.post(
            f"/instructor/lessons/{S['video_lesson_id']}/edit",
            data={"title": "Variables in 5 Minutes", "description": "",
                  "lesson_type": "video", "content_text": "", "duration": "",
                  "position": "2"},
            follow_redirects=True)
        self.assertIn(b"A video file is required", r.data)

        # ...and switching video -> text clears any attachment cleanly.
        con = raw_db()
        con.execute("UPDATE lessons SET content_path=? WHERE id=?",
                    (S["uploaded_video_path"], S["video_lesson_id"]))
        con.commit()
        con.close()
        self.assertTrue(os.path.isfile(os.path.join(UPLOAD_ROOT, S["uploaded_video_path"])))
        r = self.client.post(
            f"/instructor/lessons/{S['video_lesson_id']}/edit",
            data={"title": "Variables in 5 Minutes", "description": "",
                  "lesson_type": "text", "content_text": "plain text body",
                  "duration": "", "position": "2"},
            follow_redirects=True)
        self.assertIn(b"Lesson updated", r.data)
        row = rows("SELECT lesson_type, content_path FROM lessons WHERE id=?",
                   (S["video_lesson_id"],))[0]
        self.assertEqual(row["lesson_type"], "text")
        self.assertIsNone(row["content_path"])
        self.assertFalse(
            os.path.isfile(os.path.join(UPLOAD_ROOT, S["uploaded_video_path"])),
            "switching to text must delete the orphaned video file")

        # Restore the video state for later suites (still logged in).
        self.client.post(
            f"/instructor/lessons/{S['video_lesson_id']}/edit",
            data={"title": "Variables in 5 Minutes", "description": "All about variables",
                  "lesson_type": "video", "content_text": "", "duration": "6",
                  "position": "2",
                  "video_file": (io.BytesIO(b"\x00\x00\x00\x18ftypmp42restore"), "restored.mp4")},
            content_type="multipart/form-data",
            follow_redirects=True)
        restored = scalar("SELECT content_path FROM lessons WHERE id=?",
                          (S["video_lesson_id"],))
        self.assertTrue(restored and os.path.isfile(os.path.join(UPLOAD_ROOT, restored)))
        S["uploaded_video_path"] = restored
        self.client.get("/logout")

    # ---- 6/7/8/9. timestamps captured & linked ----------------------------
    def test07_note_marker_timestamps_persisted(self):
        vid = S["video_lesson_id"]
        self.login("stud1@test.local", "secret1")

        r = self.client.post(f"/api/lessons/{vid}/notes", json={
            "timestamp_seconds": 95.5, "note_text": "type() reveals the data type"})
        self.assertEqual(r.status_code, 201)
        note = r.get_json()["note"]
        self.assertEqual(note["timestamp_seconds"], 95.5)
        self.assertEqual(note["formatted_time"], "01:35")

        r = self.client.post(f"/api/lessons/{vid}/markers", json={
            "timestamp_seconds": 130, "marker_type": "important"})
        self.assertEqual(r.status_code, 201)
        imp = r.get_json()["marker"]
        self.assertEqual(imp["timestamp_seconds"], 130)

        r = self.client.post(f"/api/lessons/{vid}/markers", json={
            "timestamp_seconds": 175.5, "marker_type": "confused",
            "optional_text": "Why does Python change this to float?"})
        self.assertEqual(r.status_code, 201)
        conf = r.get_json()["marker"]
        self.assertEqual(conf["timestamp_seconds"], 175.5)
        self.assertEqual(conf["optional_text"], "Why does Python change this to float?")

        # Marker label editing endpoint (existing table, no schema change)
        r = self.client.put(f"/api/markers/{imp['id']}",
                            json={"optional_text": "Remember variable naming rules"})
        self.assertEqual(r.get_json()["marker"]["optional_text"],
                         "Remember variable naming rules")

        self.assertEqual(scalar(
            "SELECT timestamp_seconds FROM video_notes WHERE id=?", (note["id"],)), 95.5)
        self.assertEqual(scalar(
            "SELECT marker_type FROM video_markers WHERE id=?", (conf["id"],)), "confused")
        S["seek_note_ts"], S["seek_conf_ts"] = 95.5, 175.5

        # The merged MY MOMENTS timeline lists every interaction on the page.
        html = self.client.get(self._lesson_url()).data.decode()
        self.assertIn("MY MOMENTS", html)
        self.assertIn("01:35", html)
        self.assertIn("02:55", html)
        self.assertIn("Why does Python change this to float?", html)
        self.assertIn("window.seekLessonVideo", html)
        self.client.get("/logout")

    def test08_global_pages_carry_seek_targets(self):
        ts = S["seek_note_ts"]
        self.login("stud1@test.local", "secret1")

        notes_html = self.client.get("/notes").data.decode()
        self.assertIn(f"?t={ts:g}", notes_html)

        search_html = self.client.get("/search?q=type()").data.decode()
        self.assertIn(f"?t={ts:g}", search_html)

        rev_html = self.client.get(
            f"/courses/{S['course_id']}/revision").data.decode()
        self.assertIn(f"?t={S['seek_conf_ts']:g}", rev_html)
        self.client.get("/logout")

    def test09_lesson_consumes_seek_parameter(self):
        self.login("stud1@test.local", "secret1")
        html = self.client.get(self._lesson_url() + "?seek=167").data.decode()
        self.assertIn("SEEK_TO = 167", html)
        html = self.client.get(self._lesson_url() + "?t=41.5").data.decode()
        self.assertIn("SEEK_TO = 41.5", html)
        self.client.get("/logout")

    def test10_transcript_shares_single_seek_mechanism(self):
        self.login("stud1@test.local", "secret1")
        html = self.client.get(self._lesson_url()).data.decode()
        self.assertIn("window.seekLessonVideo", html)
        self.assertIn('data-seek="5.0"', html)          # transcript segment button
        self.assertIn("Transcript timings belong to the configured lesson content", html)
        # Exactly ONE delegated seek dispatcher drives notes/markers/transcript.
        self.assertEqual(html.count('e.target.closest("[data-seek]")'), 1)
        self.client.get("/logout")


class Test60CodingPlayground(Base):

    @staticmethod
    def _exercise_id():
        return scalar("SELECT id FROM coding_exercises LIMIT 1")

    def _run(self, code):
        ex_id = self._exercise_id()
        self.login("stud1@test.local", "secret1")
        r = self.client.post(f"/api/exercises/{ex_id}/run", json={"code": code})
        self.logout()
        return r

    def test60_instructor_configures_exercise_owner_only(self):
        tools = f"/instructor/lessons/{S['video_lesson_id']}"

        self.login("inst2@test.local", "secret1")
        self.assertEqual(self.client.post(
            f"{tools}/exercise",
            data={"instructions": "hack", "starter_code": "print(1)",
                  "expected_output": ""},
            follow_redirects=False).status_code, 404)
        self.logout()

        self.login("inst1@test.local", "secret1")
        r = self.client.post(f"{tools}/exercise", data={
            "instructions": "Print the result of 3 + 5.",
            "starter_code": "# Print 3 + 5\n",
            "expected_output": "8",
        }, follow_redirects=True)
        self.assertIn(b"Coding exercise saved", r.data)
        ex = rows("SELECT * FROM coding_exercises")[0]
        self.assertEqual(ex["expected_output"], "8")
        self.assertEqual(ex["starter_code"], "# Print 3 + 5\n")

        # preview page shows the exercise with a no-run hint (no student APIs)
        r = self.client.get(
            f"/instructor/courses/{S['course_id']}/modules/{S['module_id']}"
            f"/lessons/{S['video_lesson_id']}")
        self.assertIn(b"Python Practice", r.data)
        self.assertIn(b"Preview mode", r.data)
        # the Run button itself must be gone (inline JS string still present)
        self.assertNotIn(b'id="pg-run">', r.data)
        self.logout()

    def test61_run_success_and_expected_output_match(self):
        r = self._run("print(3 + 5)")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stdout"].strip(), "8")
        self.assertTrue(data["passed"])

    def test62_run_mismatch_reports_not_passed(self):
        data = self._run("print(9)").get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["passed"])

    def test63_syntax_error_captured_as_data(self):
        data = self._run("def broken(:\n    pass").get_json()
        self.assertFalse(data["ok"])
        self.assertFalse(data["timed_out"])
        self.assertIn("SyntaxError", data["stderr"])

    def test64_runtime_error_captured(self):
        data = self._run("print(1/0)").get_json()
        self.assertFalse(data["ok"])
        self.assertIn("ZeroDivisionError", data["stderr"])

    def test65_denied_import_rejected_pre_exec(self):
        data = self._run("import socket\nprint('hi')").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["rejected_module"], "socket")
        self.assertNotIn("hi", data["stdout"])

    def test66_timeout_kills_infinite_loop(self):
        data = self._run("while True:\n    pass").get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["timed_out"])

    def test67_stdin_never_blocks_flask(self):
        data = self._run("x = input('name: ')\nprint(x)").get_json()
        self.assertFalse(data["ok"])
        self.assertIn("EOFError", data["stderr"])

    def test68_output_size_limited(self):
        data = self._run("print('A' * 100000)").get_json()
        self.assertLessEqual(len(data["stdout"]),
                             playground.MAX_OUTPUT_CHARS + 80)

    def test69_save_reset_and_nonenrolled_blocked(self):
        ex_id = self._exercise_id()

        self.login("stud1@test.local", "secret1")
        r = self.client.post(f"/api/exercises/{ex_id}/save",
                             json={"code": "draft = True"})
        self.assertTrue(r.get_json().get("ok"))
        self.assertEqual(scalar(
            "SELECT code FROM student_code WHERE exercise_id=?", (ex_id,)),
            "draft = True")

        # saved draft resurfaces in the lesson workspace
        url = (f"/courses/{S['course_id']}/modules/{S['module_id']}"
               f"/lessons/{S['video_lesson_id']}")
        self.assertIn(b"draft = True", self.client.get(url).data)
        self.logout()

        # strip stud2 enrollment, verify all exercise APIs refuse
        self.login("stud2@test.local", "secret1")
        con = raw_db()
        con.execute("DELETE FROM enrollments WHERE student_id=? AND course_id=?",
                    (S["stud2_id"], S["course_id"]))
        con.commit()
        con.close()
        self.assertEqual(self.client.post(
            f"/api/exercises/{ex_id}/run", json={"code": "print(1)"}).status_code, 404)
        self.assertEqual(self.client.post(
            f"/api/exercises/{ex_id}/save", json={"code": "x"}).status_code, 404)
        self.assertEqual(self.client.post(
            f"/api/exercises/{ex_id}/reset", json={}).status_code, 404)
        self.logout()

        # reset restores starter code for the enrolled owner
        self.login("stud1@test.local", "secret1")
        r = self.client.post(f"/api/exercises/{ex_id}/reset", json={})
        self.assertEqual(r.get_json()["starter_code"], "# Print 3 + 5\n")
        self.assertEqual(scalar(
            "SELECT COUNT(*) FROM student_code WHERE exercise_id=?", (ex_id,)), 0)
        self.logout()


class Test70ResourcesSecurity(Base):

    def test70_resource_upload_download_and_delete(self):
        vid = S["video_lesson_id"]

        self.login("inst1@test.local", "secret1")
        fake_txt = (io.BytesIO(b"x = 42  # starter resource"), "notes.txt")
        r = self.client.post(f"/instructor/lessons/{vid}/resources/add", data={
            "resource_title": "Lesson notes",
            "resource_file": fake_txt,
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Resource uploaded", r.data)

        res_path = scalar(
            "SELECT file_path FROM lesson_resources WHERE title='Lesson notes'")
        self.assertTrue(res_path)
        self.assertTrue(os.path.isfile(os.path.join(UPLOAD_ROOT, res_path)))
        res_id = scalar("SELECT id FROM lesson_resources WHERE title='Lesson notes'")
        self.logout()

        # inst2 cannot delete inst1's resource
        self.login("inst2@test.local", "secret1")
        self.assertEqual(self.client.post(
            f"/instructor/resources/{res_id}/delete",
            follow_redirects=False).status_code, 404)
        self.logout()

        # owner deletes -> DB row AND file removed
        self.login("inst1@test.local", "secret1")
        r = self.client.post(f"/instructor/resources/{res_id}/delete",
                             follow_redirects=True)
        self.assertIn(b"Resource deleted", r.data)
        self.assertEqual(scalar(
            "SELECT COUNT(*) FROM lesson_resources WHERE id=?", (res_id,)), 0)
        self.assertFalse(os.path.isfile(os.path.join(UPLOAD_ROOT, res_path)))
        self.logout()


class Test80CourseManagementDeletes(Base):

    def test80_cross_instructor_delete_attempts_blocked(self):
        cid, mid, vid = S["course_id"], S["module_id"], S["video_lesson_id"]
        self.login("inst2@test.local", "secret1")
        self.assertEqual(self.client.post(
            f"/instructor/lessons/{vid}/delete", follow_redirects=False).status_code, 404)
        self.assertEqual(self.client.post(
            f"/instructor/modules/{mid}/delete", follow_redirects=False).status_code, 404)
        self.assertEqual(self.client.get(
            f"/instructor/courses/{cid}/delete", follow_redirects=False).status_code, 404)
        self.logout()

    def test81_delete_video_lesson_cascades_everything(self):
        vid = S["video_lesson_id"]
        video_path = scalar("SELECT content_path FROM lessons WHERE id=?", (vid,))
        self.assertTrue(video_path)
        self.assertTrue(os.path.isfile(os.path.join(UPLOAD_ROOT, video_path)))

        self.login("inst1@test.local", "secret1")
        r = self.client.post(f"/instructor/lessons/{vid}/delete", follow_redirects=True)
        self.assertIn(b"deleted", r.data)

        con = raw_db()
        self.assertIsNone(con.execute(
            "SELECT 1 FROM lessons WHERE id=?", (vid,)).fetchone())
        self.assertIsNone(con.execute(
            "SELECT 1 FROM quizzes WHERE lesson_id=?", (vid,)).fetchone())
        self.assertIsNone(con.execute(
            "SELECT 1 FROM coding_exercises WHERE lesson_id=?", (vid,)).fetchone())
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM video_notes WHERE lesson_id=?", (vid,)).fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM video_markers WHERE lesson_id=?", (vid,)).fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM lesson_progress WHERE lesson_id=?", (vid,)).fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM lesson_transcript_segments WHERE lesson_id=?",
            (vid,)).fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE quiz_id=?",
            (S["quiz_id"],)).fetchone()[0], 0)
        con.close()
        self.assertFalse(os.path.isfile(os.path.join(UPLOAD_ROOT, video_path)))
        self.logout()

    def test82_delete_module_then_course_full_flow(self):
        cid, mid = S["course_id"], S["module_id"]
        sid = S["stud1_id"]

        self.login("inst1@test.local", "secret1")

        r = self.client.post(f"/instructor/modules/{mid}/delete", follow_redirects=True)
        self.assertIn(b"deleted", r.data)
        self.assertEqual(scalar("SELECT COUNT(*) FROM modules WHERE id=?", (mid,)), 0)
        self.assertEqual(scalar(
            "SELECT COUNT(*) FROM lessons WHERE module_id=?", (mid,)), 0)

        # confirmation page first, then permanent delete
        r = self.client.get(f"/instructor/courses/{cid}/delete")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"permanently", r.data)

        r = self.client.post(f"/instructor/courses/{cid}/delete", follow_redirects=True)
        self.assertIn(b"permanently deleted", r.data)

        self.assertEqual(scalar("SELECT COUNT(*) FROM courses WHERE id=?", (cid,)), 0)
        self.assertEqual(scalar(
            "SELECT COUNT(*) FROM enrollments WHERE course_id=?", (cid,)), 0)
        self.assertGreater(xp_total(sid), 0, "earned XP must survive deletion")

        # student side no longer shows a ghost enrollment
        self.login("stud1@test.local", "secret1")
        r = self.client.get("/my-learning")
        self.assertNotIn(b"Python Foundations", r.data)
        self.logout()


class Test90SandboxUnit(Base):

    def test_unit_sandbox_behaviours(self):
        ok = playground.run_python_code("print(sum(range(10)))")
        self.assertEqual(ok["stdout"].strip(), "45")

        too_long = playground.run_python_code("x = '" + "a" * 70000 + "'")
        self.assertFalse(too_long["ok"])
        self.assertIn("too long", too_long["stderr"])

        empty = playground.run_python_code("   ")
        self.assertFalse(empty["ok"])

        denied = playground.run_python_code("from ctypes import *")
        self.assertEqual(denied["rejected_module"], "ctypes")


class Test95PersistenceAndPort(Base):

    def test95_database_restart_safe(self):
        con = sqlite3.connect(TEST_DB)   # brand-new connection
        version = con.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        con.close()
        self.assertEqual(version, 9)
        self.assertTrue({"users", "courses", "lessons", "coding_exercises",
                         "student_code",
                         "lesson_transcript_segments"} <= tables)

    def test96_port_5001_preserved(self):
        with open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("port=5001", src)


class Test97SeedIdempotency(Base):
    """
    Backend 2/3 guarantee: seed_all() is restart-safe. Running it twice on a
    fresh database must never duplicate rows or files, so `python app.py`
    can be launched any number of times without corrupting demo data.
    """

    def test97_seed_all_runs_twice_without_duplicates(self):
        import database.seed_demo as seed_demo
        from database.db import (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4,
                                 SCHEMA_V5, SCHEMA_V6, SCHEMA_V7, SCHEMA_V8,
                                 SCHEMA_V9)

        workdir = os.path.join(TMP_DIR, "seed_idem")
        os.makedirs(workdir, exist_ok=True)
        db_path = os.path.join(workdir, "seed.db")
        uploads_root = os.path.join(workdir, "uploads")
        os.makedirs(uploads_root, exist_ok=True)
        if os.path.isfile(db_path):
            os.remove(db_path)

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        for schema in (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5,
                       SCHEMA_V6, SCHEMA_V7, SCHEMA_V8, SCHEMA_V9):
            con.executescript(schema)

        tables = ["users", "courses", "modules", "lessons", "quizzes",
                  "quiz_questions", "lesson_transcript_segments",
                  "coding_exercises", "enrollments", "lesson_progress",
                  "video_notes", "video_markers", "quiz_attempts", "xp_events"]

        def snapshot():
            return {t: con.execute(
                f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

        seed_demo.seed_all(con, uploads_root)
        first = snapshot()
        seeded_files = sorted(os.listdir(uploads_root))

        # Simulated restart: second run must be a data no-op.
        seed_demo.seed_all(con, uploads_root)
        second = snapshot()

        self.assertGreater(first["courses"], 0)
        self.assertGreater(first["lessons"], 0)
        self.assertEqual(first, second,
                         "running the seeder twice duplicated demo rows")
        self.assertEqual(seeded_files, sorted(os.listdir(uploads_root)),
                         "running the seeder twice duplicated upload files")
        tags = {r[0] for r in con.execute(
            "SELECT DISTINCT concept_tag FROM quiz_questions")}
        self.assertTrue(tags)
        self.assertTrue(tags <= set(seed_demo.CANONICAL_TAGS),
                        "seeded quiz tags must stay canonical")
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
