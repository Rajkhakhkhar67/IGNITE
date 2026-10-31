"""
Learning analytics helpers (Stage 7 + Stage 8).

Everything here is computed live in Python from REAL stored data.
Nothing is cached, faked, or AI-generated -- recommendations and health
scores always reflect the student's current notes, markers, quiz answers,
and progress.

LEARNING HEALTH FORMULA (documented for transparency):
    health = weighted average of three components:

      1. Completion (weight 40)  -> percent of course lessons completed
      2. Quiz mastery (weight 40)-> AGGREGATE answer accuracy:
                                    (total correct answers / total answers)
                                    Aggregating raw answers avoids the
                                    statistical trap of averaging percentages.
      3. Clarity (weight 20)     -> 100 - confused_share, where
                                    confused_share = confused / (important + confused)
                                    If the student made NO markers at all this
                                    component is skipped and remaining weights
                                    are renormalised (no invented data).

    Status bands: >= 75 Strong | 50-74 Needs Review | < 50 At Risk

SMART REVISION:
    Recommendations are ranked from weakest signal to strongest material:
      weak_concept > wrong_question > confusion_marker >
      incomplete_lesson > important_marker > note
"""

from datetime import datetime

# Health component weights (must sum to 100 unless components are skipped)
HEALTH_WEIGHT_COMPLETION = 40
HEALTH_WEIGHT_QUIZ = 40
HEALTH_WEIGHT_CLARITY = 20

# Concepts below this accuracy are considered "weak"
WEAK_CONCEPT_THRESHOLD = 70.0


# ---------------------------------------------------------------------------
# Aggregate quiz accuracy (shared by health + insights)
# ---------------------------------------------------------------------------

def aggregate_answer_accuracy(db, student_id=None, course_id=None):
    """
    Aggregate quiz-answer accuracy from raw answers.

    Returns dict {correct, total, accuracy} where accuracy is
    round(correct/total*100, 1) or None when there are no answers.
    Aggregation over raw answer rows (not averages of percentages).
    """
    query = (
        "SELECT COUNT(*) AS total, COALESCE(SUM(qa.is_correct), 0) AS correct "
        "FROM quiz_answers qa "
        "JOIN quiz_attempts qat ON qa.attempt_id = qat.id "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE 1=1"
    )
    params = []
    if student_id is not None:
        query += " AND qat.student_id = ?"
        params.append(student_id)
    if course_id is not None:
        query += " AND m.course_id = ?"
        params.append(course_id)

    row = db.execute(query, params).fetchone()
    total, correct = row["total"], row["correct"]
    if total == 0:
        return {"correct": 0, "total": 0, "accuracy": None}
    return {
        "correct": correct,
        "total": total,
        "accuracy": round(correct / total * 100, 1),
    }


# ---------------------------------------------------------------------------
# Learning Health (Stage 7)
# ---------------------------------------------------------------------------

def calculate_learning_health(db, student_id, course_id):
    """
    Compute the Learning Health snapshot for one student in one course.

    Returns dict:
        {
          score: 0-100 int or None when the course has no lessons AND
                 no quiz answers AND no markers (truly nothing to measure),
          status: 'Strong' | 'Needs Review' | 'At Risk' | 'No Data',
          completion: {...},       # lessons completed / total
          quiz: {...},             # aggregate answer accuracy
          clarity: {...} or None,  # marker balance
          components: [...],       # human-friendly breakdown for the UI
        }
    """
    total_lessons = db.execute(
        "SELECT COUNT(*) FROM lessons l JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchone()[0]
    completed = db.execute(
        "SELECT COUNT(*) FROM lesson_progress lp "
        "JOIN lessons l ON lp.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND lp.student_id = ? AND lp.is_completed = 1",
        (course_id, student_id),
    ).fetchone()[0]
    completion_pct = round(completed / total_lessons * 100) if total_lessons else None

    quiz = aggregate_answer_accuracy(db, student_id=student_id, course_id=course_id)

    # Enrolment alone is not evidence of learning.  A brand-new student used
    # to receive a fabricated 0/100 "At Risk" health score simply because
    # the course contained lessons.  Wait until the student has actually
    # opened/progressed through a lesson, attempted a quiz, or used a marker.
    progress_activity = db.execute(
        "SELECT COUNT(*) FROM lesson_progress lp "
        "JOIN lessons l ON lp.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND lp.student_id = ?",
        (course_id, student_id),
    ).fetchone()[0]

    important = db.execute(
        "SELECT COUNT(*) FROM video_markers vm "
        "JOIN lessons l ON vm.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND vm.student_id = ? AND vm.marker_type = 'important'",
        (course_id, student_id),
    ).fetchone()[0]
    confused = db.execute(
        "SELECT COUNT(*) FROM video_markers vm "
        "JOIN lessons l ON vm.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND vm.student_id = ? AND vm.marker_type = 'confused'",
        (course_id, student_id),
    ).fetchone()[0]

    if not progress_activity and quiz["total"] == 0 and not (important + confused):
        return {
            "score": None,
            "status": "No Data",
            "completion": {"completed": completed, "total": total_lessons,
                           "percentage": completion_pct},
            "quiz": quiz,
            "clarity": None,
            "components": [],
        }

    components = []          # (label, value_text, weight_applied)
    weighted_sum = 0.0
    weight_total = 0

    if completion_pct is not None:
        components.append({
            "label": "Completion",
            "value": completion_pct,
            "suffix": "%",
            "detail": f"{completed}/{total_lessons} lessons completed",
            "weight": HEALTH_WEIGHT_COMPLETION,
        })
        weighted_sum += completion_pct * HEALTH_WEIGHT_COMPLETION
        weight_total += HEALTH_WEIGHT_COMPLETION

    if quiz["accuracy"] is not None:
        components.append({
            "label": "Quiz Accuracy",
            "value": quiz["accuracy"],
            "suffix": "%",
            "detail": f"{quiz['correct']}/{quiz['total']} answers correct",
            "weight": HEALTH_WEIGHT_QUIZ,
        })
        weighted_sum += quiz["accuracy"] * HEALTH_WEIGHT_QUIZ
        weight_total += HEALTH_WEIGHT_QUIZ

    clarity = None
    if (important + confused) > 0:
        confused_share = confused / (important + confused) * 100
        clarity = {
            "important": important,
            "confused": confused,
            "score": round(100 - confused_share, 1),
        }
        components.append({
            "label": "Clarity",
            "value": clarity["score"],
            "suffix": "%",
            "detail": f"{important} important / {confused} confused markers",
            "weight": HEALTH_WEIGHT_CLARITY,
        })
        weighted_sum += clarity["score"] * HEALTH_WEIGHT_CLARITY
        weight_total += HEALTH_WEIGHT_CLARITY

    if weight_total == 0:
        return {
            "score": None,
            "status": "No Data",
            "completion": {"completed": 0, "total": 0, "percentage": None},
            "quiz": quiz,
            "clarity": None,
            "components": [],
        }

    score = round(weighted_sum / weight_total)
    if score >= 75:
        status = "Strong"
    elif score >= 50:
        status = "Needs Review"
    else:
        status = "At Risk"

    return {
        "score": score,
        "status": status,
        "completion": {
            "completed": completed,
            "total": total_lessons,
            "percentage": completion_pct,
        },
        "quiz": quiz,
        "clarity": clarity,
        "components": components,
    }


# ---------------------------------------------------------------------------
# Concept performance for one student (weak concepts)
# ---------------------------------------------------------------------------

def student_concept_performance(db, student_id, course_id):
    """
    Per-concept AGGREGATE accuracy for one student in one course.
    Returns list [{concept_tag, correct, total, accuracy}] sorted weakest-first.
    """
    rows = db.execute(
        "SELECT qq.concept_tag, "
        "COUNT(*) AS total, COALESCE(SUM(qa.is_correct), 0) AS correct "
        "FROM quiz_answers qa "
        "JOIN quiz_questions qq ON qa.question_id = qq.id "
        "JOIN quiz_attempts qat ON qa.attempt_id = qat.id "
        "JOIN quizzes qz ON qat.quiz_id = qz.id "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE qat.student_id = ? AND m.course_id = ? AND qq.concept_tag != '' "
        "GROUP BY qq.concept_tag",
        (student_id, course_id),
    ).fetchall()
    result = []
    for r in rows:
        result.append({
            "concept_tag": r["concept_tag"],
            "correct": r["correct"],
            "total": r["total"],
            "accuracy": round(r["correct"] / r["total"] * 100, 1),
        })
    result.sort(key=lambda x: x["accuracy"])
    return result


# ---------------------------------------------------------------------------
# Smart Revision (Stage 7)
# ---------------------------------------------------------------------------

def build_revision_items(db, student_id, course_id, limit=8):
    """
    Build ranked Smart Revision recommendations for one student in one course.

    Every item comes from REAL stored data. Returns list of dicts:
        { source_type, source_id, ref_type, ref_id, title, detail,
          lesson_id, module_id, priority }
    The caller turns them into links. Deduplication key is
    (source_type, source_id/ref identity).
    """
    items = []
    seen = set()

    def _add(source_type, key, title, detail, lesson_id=None, module_id=None,
             ref_type="", ref_id=0, priority=0, timestamp_seconds=None):
        if key in seen:
            return
        seen.add(key)
        items.append({
            "source_type": source_type,
            "key": key,
            "title": title,
            "detail": detail,
            "lesson_id": lesson_id,
            "module_id": module_id,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "priority": priority,
            # When set, the caller deep-links straight to this moment in the
            # lesson video (lesson_view?...&t=<seconds>).
            "timestamp_seconds": timestamp_seconds,
        })

    # 1. Weak concepts (aggregate accuracy below threshold) -------------------
    for c in student_concept_performance(db, student_id, course_id):
        if c["accuracy"] < WEAK_CONCEPT_THRESHOLD:
            _add(
                "weak_concept",
                ("weak_concept", c["concept_tag"]),
                c["concept_tag"],
                f"Quiz accuracy {c['accuracy']}% ({c['correct']}/{c['total']} answers)",
                ref_type=f"concept:{c['concept_tag']}", ref_id=0, priority=1,
            )

    # 2. Wrong questions from the LATEST attempt of each quiz -----------------
    quizzes = db.execute(
        "SELECT qz.id, qz.lesson_id, l.title AS lesson_title, l.id AS lid, m.id AS mid "
        "FROM quizzes qz "
        "JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ?",
        (course_id,),
    ).fetchall()
    for qz in quizzes:
        latest = db.execute(
            "SELECT id FROM quiz_attempts WHERE quiz_id = ? AND student_id = ? "
            "ORDER BY completed_at DESC, id DESC LIMIT 1",
            (qz["id"], student_id),
        ).fetchone()
        if not latest:
            continue
        wrong = db.execute(
            "SELECT qa.question_id, qa.selected_answer, qq.question_text, "
            "qq.concept_tag "
            "FROM quiz_answers qa JOIN quiz_questions qq ON qa.question_id = qq.id "
            "WHERE qa.attempt_id = ? AND qa.is_correct = 0",
            (latest["id"],),
        ).fetchall()
        for w in wrong:
            _add(
                "wrong_question",
                ("wrong_question", latest["id"], w["question_id"]),
                (w["question_text"][:90] + "…") if len(w["question_text"]) > 90
                else w["question_text"],
                (f"Missed in “{qz['lesson_title']}”"
                 + (f" · Concept: {w['concept_tag']}" if w["concept_tag"] else "")),
                lesson_id=qz["lid"], module_id=qz["mid"],
                ref_type="attempt_question", ref_id=latest["id"], priority=2,
            )

    # 3. Confusion markers -----------------------------------------------------
    confusions = db.execute(
        "SELECT vm.id, vm.timestamp_seconds, vm.optional_text, vm.lesson_id, "
        "l.title AS lesson_title, m.id AS module_id "
        "FROM video_markers vm "
        "JOIN lessons l ON vm.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND vm.student_id = ? AND vm.marker_type = 'confused' "
        "ORDER BY vm.created_at DESC",
        (course_id, student_id),
    ).fetchall()
    for cm in confusions:
        mmss = _fmt_ts(cm["timestamp_seconds"])
        detail = f"You flagged {mmss} in “{cm['lesson_title']}”"
        if cm["optional_text"]:
            detail += f" — “{cm['optional_text'][:60]}”"
        _add(
            "confusion_marker",
            ("confusion_marker", cm["id"]),
            cm["lesson_title"],
            detail,
            lesson_id=cm["lesson_id"], module_id=cm["module_id"],
            ref_type="marker", ref_id=cm["id"], priority=3,
            timestamp_seconds=cm["timestamp_seconds"],
        )

    # 4. Started-but-incomplete lessons ----------------------------------------
    incomplete = db.execute(
        "SELECT l.id, l.title, m.id AS module_id, lp.completion_percentage "
        "FROM lesson_progress lp "
        "JOIN lessons l ON lp.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND lp.student_id = ? AND lp.is_completed = 0 "
        "ORDER BY lp.last_activity_at DESC",
        (course_id, student_id),
    ).fetchall()
    for l in incomplete:
        _add(
            "incomplete_lesson",
            ("incomplete_lesson", l["id"]),
            l["title"],
            f"Lesson {round(l['completion_percentage'])}% complete — finish it",
            lesson_id=l["id"], module_id=l["module_id"],
            ref_type="lesson", ref_id=l["id"], priority=4,
        )

    # 5. Important moments worth revisiting ------------------------------------
    importants = db.execute(
        "SELECT vm.id, vm.timestamp_seconds, vm.lesson_id, "
        "l.title AS lesson_title, m.id AS module_id "
        "FROM video_markers vm "
        "JOIN lessons l ON vm.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND vm.student_id = ? AND vm.marker_type = 'important' "
        "ORDER BY vm.created_at DESC",
        (course_id, student_id),
    ).fetchall()
    for im in importants:
        _add(
            "important_marker",
            ("important_marker", im["id"]),
            im["lesson_title"],
            f"Revisit your ⭐ moment at {_fmt_ts(im['timestamp_seconds'])}",
            lesson_id=im["lesson_id"], module_id=im["module_id"],
            ref_type="marker", ref_id=im["id"], priority=5,
            timestamp_seconds=im["timestamp_seconds"],
        )

    # 6. Lessons with notes ------------------------------------------------------
    noted = db.execute(
        "SELECT l.id, l.title, m.id AS module_id, "
        "MAX(vn.timestamp_seconds) AS last_note_ts "
        "FROM video_notes vn "
        "JOIN lessons l ON vn.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND vn.student_id = ? "
        "GROUP BY l.id, l.title, m.id",
        (course_id, student_id),
    ).fetchall()
    for l in noted:
        _add(
            "note",
            ("note", l["id"]),
            l["title"],
            "Review your notes on this lesson",
            lesson_id=l["id"], module_id=l["module_id"],
            ref_type="lesson_note", ref_id=l["id"], priority=6,
            timestamp_seconds=l["last_note_ts"],
        )

    items.sort(key=lambda x: x["priority"])
    return items[:limit]


def _fmt_ts(seconds):
    """Format seconds as MM:SS / H:MM:SS."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def record_revision_activity(db, student_id, course_id, item):
    """
    Persist one revision_activity row for a reviewed recommendation.
    Returns the inserted row id.
    """
    cursor = db.execute(
        "INSERT INTO revision_activity "
        "(student_id, course_id, lesson_id, concept_tag, source_type, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            student_id,
            course_id,
            item.get("lesson_id"),
            item.get("concept_tag"),
            item["source_type"],
            item.get("ref_id") or None,
        ),
    )
    db.commit()
    return cursor.lastrowid


def revision_stats(db, student_id, course_id=None):
    """Count of revision reviews, optionally scoped to a course."""
    if course_id is None:
        row = db.execute(
            "SELECT COUNT(*) FROM revision_activity WHERE student_id = ?",
            (student_id,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT COUNT(*) FROM revision_activity "
            "WHERE student_id = ? AND course_id = ?",
            (student_id, course_id),
        ).fetchone()
    return row[0]
