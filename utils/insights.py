"""
Instructor Learning Insights (Stage 8, Part D).

All functions aggregate REAL learning behaviour for ONE course owned by
the calling instructor (ownership is verified in app.py before these
functions run).

PRIVACY RULES
-------------
These queries intentionally never select student-authored free text:
no video_notes.note_text and no video_markers.optional_text ever reach
an instructor. Only counts, timestamps, percentages, and concept tags
are exposed. There is no per-student drill-down.

CONFUSION HOTSPOT ALGORITHM (documented)
----------------------------------------
1. Select all 'confused' markers for the course's lessons.
2. Bucket each marker into a fixed window of BUCKET_SECONDS = 10:
       bucket_index = CAST(timestamp_seconds / 10 AS INTEGER)
   so markers at 08:43 and 08:47 land in the same [08:40-08:50) bucket.
3. GROUP BY lesson + bucket and count DISTINCT student_id -- one student
   marking the same spot five times cannot inflate the result.
4. Rank by distinct-student count descending; ties broken by timestamp.
"""

BUCKET_SECONDS = 10


def course_overview(db, course_id):
    """
    High-level aggregates for one course.

    average_completion : mean of each enrolled student's completion %
                         (per-student percentage, then averaged).
    quiz_accuracy      : AGGREGATE answer accuracy over every answer row
                         in the course (correct/total) -- deliberately
                         not an average of attempt percentages.
    pass_rate          : passed attempts / total attempts.
    """
    enrolled = db.execute(
        "SELECT COUNT(*) FROM enrollments WHERE course_id = ?", (course_id,)
    ).fetchone()[0]

    total_lessons = db.execute(
        "SELECT COUNT(*) FROM lessons l JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchone()[0]

    avg_completion = None
    if enrolled > 0 and total_lessons > 0:
        pct_rows = db.execute(
            "SELECT COALESCE(SUM(lp.is_completed), 0) AS done "
            "FROM enrollments e "
            "LEFT JOIN lesson_progress lp ON lp.student_id = e.student_id "
            "AND lp.is_completed = 1 AND lp.lesson_id IN ("
            "  SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
            "  WHERE m.course_id = ?"
            ") "
            "WHERE e.course_id = ? GROUP BY e.student_id",
            (course_id, course_id),
        ).fetchall()
        if pct_rows:
            percents = [round(r["done"] / total_lessons * 100) for r in pct_rows]
            avg_completion = round(sum(percents) / len(percents))

    accuracy_row = db.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(qa.is_correct), 0) AS correct "
        "FROM quiz_answers qa "
        "JOIN quiz_attempts qat ON qa.attempt_id = qat.id "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchone()
    quiz_accuracy = (
        round(accuracy_row["correct"] / accuracy_row["total"] * 100, 1)
        if accuracy_row["total"] > 0 else None
    )

    attempts_row = db.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(qa.passed), 0) AS passed "
        "FROM quiz_attempts qa "
        "JOIN quizzes qz ON qa.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchone()
    pass_rate = (
        round(attempts_row["passed"] / attempts_row["total"] * 100, 1)
        if attempts_row["total"] > 0 else None
    )

    return {
        "enrolled": enrolled,
        "total_lessons": total_lessons,
        "average_completion": avg_completion,
        "quiz_accuracy": quiz_accuracy,
        "quiz_answers": accuracy_row["total"],
        "pass_rate": pass_rate,
        "attempt_count": attempts_row["total"],
    }


def concept_difficulty(db, course_id, limit=8):
    """
    Concepts ranked weakest-first using AGGREGATE answer accuracy
    across ALL students (correct/total raw answers, with sample size).

    Returns [{concept_tag, correct, total, accuracy}] ascending accuracy.
    """
    rows = db.execute(
        "SELECT qq.concept_tag, COUNT(*) AS total, "
        "COALESCE(SUM(qa.is_correct), 0) AS correct "
        "FROM quiz_answers qa "
        "JOIN quiz_questions qq ON qa.question_id = qq.id "
        "JOIN quiz_attempts qat ON qa.attempt_id = qat.id "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND qq.concept_tag != '' "
        "GROUP BY qq.concept_tag",
        (course_id,),
    ).fetchall()
    concepts = []
    for r in rows:
        concepts.append({
            "concept_tag": r["concept_tag"],
            "correct": r["correct"],
            "total": r["total"],
            "accuracy": round(r["correct"] / r["total"] * 100, 1),
        })
    concepts.sort(key=lambda c: c["accuracy"])
    return concepts[:limit]


def question_difficulty(db, course_id, limit=8):
    """
    Questions with the highest incorrect-answer rate across all attempts.

    Returns [{question_id, question_text, concept_tag, responses,
              incorrect_count, incorrect_rate}] descending incorrect rate.
    """
    rows = db.execute(
        "SELECT qq.id AS question_id, qq.question_text, qq.concept_tag, "
        "COUNT(*) AS responses, COALESCE(SUM(1 - qa.is_correct), 0) AS wrong "
        "FROM quiz_answers qa "
        "JOIN quiz_questions qq ON qa.question_id = qq.id "
        "JOIN quiz_attempts qat ON qa.attempt_id = qat.id "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? "
        "GROUP BY qq.id",
        (course_id,),
    ).fetchall()
    questions = []
    for r in rows:
        rate = round(r["wrong"] / r["responses"] * 100, 1) if r["responses"] else 0.0
        questions.append({
            "question_id": r["question_id"],
            "question_text": r["question_text"],
            "concept_tag": r["concept_tag"],
            "lesson_title": None,
            "responses": r["responses"],
            "incorrect_count": r["wrong"],
            "incorrect_rate": rate,
        })
    questions.sort(key=lambda q: q["incorrect_rate"], reverse=True)
    return questions[:limit]


def confusion_hotspots(db, course_id, bucket=BUCKET_SECONDS, limit=8):
    """
    Confusion hotspots: 10-second windows where the most DISTINCT students
    marked 'confused'. See module docstring for the full algorithm.
    """
    rows = db.execute(
        "SELECT vm.lesson_id, "
        "CAST(vm.timestamp_seconds / ? AS INTEGER) AS bucket_idx, "
        "MIN(vm.timestamp_seconds) AS bucket_start, "
        "COUNT(DISTINCT vm.student_id) AS students "
        "FROM video_markers vm "
        "JOIN lessons l ON vm.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND vm.marker_type = 'confused' "
        "GROUP BY vm.lesson_id, bucket_idx "
        "ORDER BY students DESC, bucket_start ASC "
        "LIMIT ?",
        (bucket, course_id, limit),
    ).fetchall()
    hotspots = []
    for r in rows:
        start = int(r["bucket_start"])
        # Window is [start - offset_within_bucket, ...+bucket). Snap to bucket edge.
        window_start = r["bucket_idx"] * bucket
        window_end = window_start + bucket
        hotspots.append({
            "lesson_id": r["lesson_id"],
            "window_start": window_start,
            "window_end": window_end,
            "students": r["students"],
        })
    return hotspots


def lesson_confusion(db, course_id, limit=6):
    """Lessons ranked by DISTINCT confused students."""
    rows = db.execute(
        "SELECT l.id, l.title, COUNT(DISTINCT vm.student_id) AS students "
        "FROM lessons l "
        "JOIN modules m ON l.module_id = m.id "
        "LEFT JOIN video_markers vm ON vm.lesson_id = l.id "
        "AND vm.marker_type = 'confused' "
        "WHERE m.course_id = ? "
        "GROUP BY l.id "
        "HAVING students > 0 "
        "ORDER BY students DESC LIMIT ?",
        (course_id, limit),
    ).fetchall()
    return [
        {"lesson_id": r["id"], "title": r["title"], "students": r["students"]}
        for r in rows
    ]


def lesson_analytics(db, course_id):
    """
    Per-lesson metrics table for the insights page.

    started          : students with a progress row for the lesson
    completed        : students with is_completed = 1
    completion_rate  : completed / enrolled (None when nobody enrolled)
    avg_best_quiz    : mean over students of their BEST attempt percentage
                       (aggregate-friendly: one number per student first)
    confused_students: distinct students who flagged confusion here
    top_confusion_ts : start of the hottest 10s window, or None
    """
    enrolled = db.execute(
        "SELECT COUNT(*) FROM enrollments WHERE course_id = ?", (course_id,)
    ).fetchone()[0]

    lessons = db.execute(
        "SELECT l.id, l.title, l.lesson_type, m.title AS module_title, m.id AS mid "
        "FROM lessons l JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? ORDER BY m.position, l.position",
        (course_id,),
    ).fetchall()

    results = []
    for l in lessons:
        started = db.execute(
            "SELECT COUNT(DISTINCT lp.student_id) FROM lesson_progress lp "
            "WHERE lp.lesson_id = ?", (l["id"],),
        ).fetchone()[0]
        completed = db.execute(
            "SELECT COUNT(DISTINCT lp.student_id) FROM lesson_progress lp "
            "WHERE lp.lesson_id = ? AND lp.is_completed = 1", (l["id"],),
        ).fetchone()[0]
        completion_rate = round(completed / enrolled * 100) if enrolled else None

        best_rows = db.execute(
            "SELECT student_id, MAX(percentage) AS best FROM quiz_attempts "
            "WHERE quiz_id IN (SELECT id FROM quizzes WHERE lesson_id = ?) "
            "GROUP BY student_id",
            (l["id"],),
        ).fetchall()
        has_quiz = db.execute(
            "SELECT COUNT(*) FROM quizzes WHERE lesson_id = ?", (l["id"],),
        ).fetchone()[0] > 0
        avg_best_quiz = (
            round(sum(r["best"] for r in best_rows) / len(best_rows), 1)
            if best_rows else None
        )

        confused_students = db.execute(
            "SELECT COUNT(DISTINCT student_id) FROM video_markers "
            "WHERE lesson_id = ? AND marker_type = 'confused'", (l["id"],),
        ).fetchone()[0]
        top_hotspot = db.execute(
            "SELECT CAST(timestamp_seconds / ? AS INTEGER) AS bucket_idx "
            "FROM video_markers WHERE lesson_id = ? AND marker_type = 'confused' "
            "GROUP BY bucket_idx ORDER BY COUNT(DISTINCT student_id) DESC "
            "LIMIT 1",
            (BUCKET_SECONDS, l["id"]),
        ).fetchone()
        top_confusion_start = (
            top_hotspot["bucket_idx"] * BUCKET_SECONDS if top_hotspot else None
        )

        results.append({
            "lesson_id": l["id"],
            "module_id": l["mid"],
            "title": l["title"],
            "module_title": l["module_title"],
            "lesson_type": l["lesson_type"],
            "has_quiz": has_quiz,
            "started": started,
            "completed": completed,
            "completion_rate": completion_rate,
            "avg_best_quiz": avg_best_quiz,
            "quiz_participants": len(best_rows),
            "confused_students": confused_students,
            "top_confusion_start": top_confusion_start,
        })
    return results
