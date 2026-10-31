"""
XP, Levels, Streaks and Achievements (Stage 8, Part C).

Design goals
------------
1. Reward REAL learning events only -- never page views or logins.
2. Be farm-proof: XP is stored as an append-only event LEDGER
   (xp_events table). A partial UNIQUE index on
   (student_id, event_type, reference_type, reference_id) makes every
   one-time event idempotent at the DATABASE level, so re-completing a
   lesson or re-passing a quiz can never mint duplicate XP even under
   race conditions.
3. Be explainable in a viva: every function here is plain Python +
   parameterised SQLite.

XP RULES (documented values)
----------------------------
    lesson_completed   +20   once per lesson (ref: lesson/<id>)
    quiz_passed        +25   once per quiz (ref: quiz/<id>)
    quiz_improved      +10   each time the latest attempt beats the previous
                             best percentage; ref: attempt/<attempt_id> so
                             one attempt can only ever be rewarded once.
                             Bounded naturally: percentages are 0-100.
    revision_reviewed  +10   once per distinct revision item
                             (ref: concept:<tag> | marker | lesson | ...)
    course_completed   +100  once per course when progress hits 100%
                             (ref: course/<id>)

LEVELS (documented thresholds)
------------------------------
    Level 1: 0 XP | Level 2: 100 XP | Level 3: 250 XP
    Level 4: 500 XP | Level 5: 1000 XP

STREAKS
-------
    A "learning day" is any UTC calendar date on which the student did at
    least one MEANFUL activity: lesson progress/completion, quiz attempt,
    note creation, marker creation, or Smart Revision review.
    Logging in alone is NOT an activity (no table records logins).
    All app timestamps are written with SQLite datetime('now') (UTC), so
    streaks are computed on UTC dates for full consistency.

ACHIEVEMENTS
------------
    Eight achievements, each computed from real data by a check function.
    Unlocks are persisted in achievement_unlocks with a UNIQUE constraint
    per (student, achievement) so duplicates are impossible.
"""

from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# XP configuration
# ---------------------------------------------------------------------------

XP_RULES = {
    "lesson_completed": 20,
    "quiz_passed": 25,
    "quiz_improved": 10,
    "revision_reviewed": 10,
    "course_completed": 100,
}

# Events that may only ever be awarded ONCE per reference.
# Enforced by the ux_xp_events_once partial unique index in the schema;
# listed here for documentation and for award_xp's fast pre-check.
ONCE_ONLY_EVENTS = {"lesson_completed", "quiz_passed", "course_completed",
                    "revision_reviewed"}

LEVEL_THRESHOLDS = [
    (1, 0),
    (2, 100),
    (3, 250),
    (4, 500),
    (5, 1000),
]


def total_xp(db, student_id):
    """Total XP = SUM over the append-only ledger. Reliable by construction."""
    row = db.execute(
        "SELECT COALESCE(SUM(xp_amount), 0) FROM xp_events WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    return row[0]


def level_info(xp):
    """
    Convert total XP into level information.

    Returns dict {level, xp, current_floor, next_threshold, xp_to_next,
    progress_pct}. When max level is reached next_threshold/xp_to_next
    are None and progress_pct is 100.
    """
    level, floor = LEVEL_THRESHOLDS[0]
    for lvl, threshold in LEVEL_THRESHOLDS:
        if xp >= threshold:
            level, floor = lvl, threshold
    nxt = None
    for lvl, threshold in LEVEL_THRESHOLDS:
        if threshold > floor:
            nxt = threshold
            break
    if nxt is None:
        return {
            "level": level, "xp": xp, "current_floor": floor,
            "next_threshold": None, "xp_to_next": None, "progress_pct": 100,
        }
    span = nxt - floor
    return {
        "level": level,
        "xp": xp,
        "current_floor": floor,
        "next_threshold": nxt,
        "xp_to_next": nxt - xp,
        "progress_pct": min(100, round((xp - floor) / span * 100)) if span else 100,
    }


def award_xp(db, student_id, event_type, ref_type="", ref_id=0):
    """
    Insert an XP event. Returns the XP amount awarded, or 0 when the
    event was a duplicate (already recorded for this reference).

    The database UNIQUE index is the real guard; the SELECT below is just
    a cheap pre-check so the common duplicate path avoids an exception.
    """
    if event_type not in XP_RULES:
        raise ValueError(f"Unknown XP event type: {event_type}")

    existing = db.execute(
        "SELECT 1 FROM xp_events WHERE student_id = ? AND event_type = ? "
        "AND reference_type = ? AND reference_id = ?",
        (student_id, event_type, ref_type, ref_id),
    ).fetchone()
    if existing:
        return 0

    try:
        db.execute(
            "INSERT INTO xp_events (student_id, event_type, reference_type, "
            "reference_id, xp_amount) VALUES (?, ?, ?, ?, ?)",
            (student_id, event_type, ref_type, ref_id, XP_RULES[event_type]),
        )
        db.commit()
    except Exception:
        # Concurrent duplicate lost the race with the unique index.
        db.rollback()
        return 0
    return XP_RULES[event_type]


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

# Every table column holding a meaningful-activity timestamp.
# Login is NOT in this list by design -- logging in never feeds streaks.
_ACTIVITY_SOURCES = [
    "SELECT last_activity_at FROM lesson_progress WHERE student_id = ?",
    "SELECT completed_at FROM lesson_progress WHERE student_id = ? "
    "AND completed_at IS NOT NULL",
    "SELECT completed_at FROM quiz_attempts WHERE student_id = ?",
    "SELECT created_at FROM video_notes WHERE student_id = ?",
    "SELECT created_at FROM video_markers WHERE student_id = ?",
    "SELECT reviewed_at FROM revision_activity WHERE student_id = ?",
]


def activity_dates(db, student_id):
    """
    Return the set of UTC calendar dates on which the student performed
    any meaningful learning activity. Login is not recorded anywhere in
    the database, therefore it cannot inflate this set.
    """
    dates = set()
    for sql in _ACTIVITY_SOURCES:
        for row in db.execute(sql, (student_id,)).fetchall():
            raw = row[0]
            if not raw:
                continue
            try:
                dates.add(datetime.strptime(str(raw)[:10], "%Y-%m-%d").date())
            except ValueError:
                continue
    return dates


def current_streak(dates, today=None):
    """
    Count consecutive active days ending today (or yesterday if today has
    no activity yet -- the standard convention so a streak does not show
    as broken before the student has studied today).
    """
    today = today or date.today()
    day = today
    if day not in dates:
        day -= timedelta(days=1)
        if day not in dates:
            return 0
    streak = 0
    while day in dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


def longest_streak(dates):
    """Longest run of consecutive active dates across all history."""
    streak = 0
    best = 0
    prev = None
    for day in sorted(dates):
        if prev is not None and (day - prev).days == 1:
            streak += 1
        else:
            streak = 1
        best = max(best, streak)
        prev = day
    return best


def streak_info(db, student_id, today=None):
    dates = activity_dates(db, student_id)
    return {
        "current": current_streak(dates, today=today),
        "longest": longest_streak(dates),
        "active_days": len(dates),
    }


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------

def _check_note_taker(db, sid):
    return db.execute(
        "SELECT COUNT(*) FROM video_notes WHERE student_id = ?", (sid,)
    ).fetchone()[0] >= 10


def _check_keen_eye(db, sid):
    return db.execute(
        "SELECT COUNT(*) FROM video_markers WHERE student_id = ? "
        "AND marker_type = 'important'", (sid,),
    ).fetchone()[0] >= 10


def _check_comeback(db, sid):
    """Improve a quiz score significantly: +30 points over prior best."""
    attempts = db.execute(
        "SELECT quiz_id, percentage FROM quiz_attempts WHERE student_id = ? "
        "ORDER BY completed_at, id", (sid,),
    ).fetchall()
    best_per_quiz = {}
    for a in attempts:
        qid = a["quiz_id"]
        prior_best = best_per_quiz.get(qid)
        if prior_best is not None and a["percentage"] - prior_best >= 30:
            return True
        if prior_best is None or a["percentage"] > prior_best:
            best_per_quiz[qid] = a["percentage"]
    return False


def _check_consistent(db, sid):
    return longest_streak(activity_dates(db, sid)) >= 7


def _check_quiz_master(db, sid):
    row = db.execute(
        "SELECT COUNT(*) FROM quiz_attempts WHERE student_id = ? "
        "AND total_questions > 0 AND percentage >= 100", (sid,),
    ).fetchone()
    return row[0] > 0


def _check_course_finisher(db, sid):
    row = db.execute(
        "SELECT COUNT(*) FROM enrollments e WHERE e.student_id = ? AND EXISTS ("
        "  SELECT 1 FROM modules m JOIN lessons l ON l.module_id = m.id "
        "  WHERE m.course_id = e.course_id"
        ") AND ("
        "  SELECT COUNT(*) FROM lessons l JOIN modules m ON l.module_id = m.id "
        "  WHERE m.course_id = e.course_id"
        ") = ("
        "  SELECT COUNT(*) FROM lesson_progress lp "
        "  JOIN lessons l ON lp.lesson_id = l.id "
        "  JOIN modules m ON l.module_id = m.id "
        "  WHERE m.course_id = e.course_id AND lp.student_id = e.student_id "
        "  AND lp.is_completed = 1"
        ") AND ("
        "  SELECT COUNT(*) FROM lessons l JOIN modules m ON l.module_id = m.id "
        "  WHERE m.course_id = e.course_id"
        ") > 0", (sid,),
    ).fetchone()
    return row[0] > 0


def _check_self_aware(db, sid):
    """
    Marked a lesson confusing AND later passed that lesson's quiz --
    evidence the confusion was resolved through real study.
    """
    row = db.execute(
        "SELECT COUNT(*) FROM video_markers vm "
        "JOIN quizzes qz ON qz.lesson_id = vm.lesson_id "
        "WHERE vm.student_id = ? AND vm.marker_type = 'confused' AND EXISTS ("
        "  SELECT 1 FROM quiz_attempts qa "
        "  WHERE qa.quiz_id = qz.id AND qa.student_id = vm.student_id "
        "  AND qa.passed = 1 AND qa.completed_at >= vm.created_at"
        ")", (sid,),
    ).fetchone()
    return row[0] > 0


def _check_revision_warrior(db, sid):
    return db.execute(
        "SELECT COUNT(*) FROM revision_activity WHERE student_id = ?", (sid,)
    ).fetchone()[0] >= 10


ACHIEVEMENTS = [
    {
        "key": "note_taker",
        "icon": "\U0001F4DD",
        "name": "Note Taker",
        "description": "Save your first 10 timestamped notes.",
        "check": _check_note_taker,
    },
    {
        "key": "keen_eye",
        "icon": "\u2B50",
        "name": "Keen Eye",
        "description": "Mark 10 important moments.",
        "check": _check_keen_eye,
    },
    {
        "key": "comeback",
        "icon": "\U0001F9E0",
        "name": "Comeback",
        "description": "Improve a quiz score by 30+ points over your previous best.",
        "check": _check_comeback,
    },
    {
        "key": "consistent",
        "icon": "\U0001F525",
        "name": "Consistent",
        "description": "Keep a 7-day learning streak.",
        "check": _check_consistent,
    },
    {
        "key": "quiz_master",
        "icon": "\U0001F3AF",
        "name": "Quiz Master",
        "description": "Score 100% on a quiz.",
        "check": _check_quiz_master,
    },
    {
        "key": "course_finisher",
        "icon": "\U0001F4DA",
        "name": "Course Finisher",
        "description": "Complete every lesson in a course.",
        "check": _check_course_finisher,
    },
    {
        "key": "self_aware",
        "icon": "\u2753",
        "name": "Self-Aware Learner",
        "description": "Flag confusion, then pass that lesson's quiz afterwards.",
        "check": _check_self_aware,
    },
    {
        "key": "revision_warrior",
        "icon": "\U0001F4AA",
        "name": "Revision Warrior",
        "description": "Review 10 Smart Revision items.",
        "check": _check_revision_warrior,
    },
]

_ACH_BY_KEY = {a["key"]: a for a in ACHIEVEMENTS}


def evaluate_achievements(db, student_id):
    """
    Check every achievement against REAL data and persist any new unlocks.
    Returns list of achievement dicts newly unlocked (may be empty).
    Safe to call repeatedly -- UNIQUE(student_id, achievement_key) prevents
    duplicates and already-unlocked achievements skip their checks entirely.
    """
    unlocked_rows = {
        r["achievement_key"]
        for r in db.execute(
            "SELECT achievement_key FROM achievement_unlocks WHERE student_id = ?",
            (student_id,),
        ).fetchall()
    }
    new_unlocks = []
    for ach in ACHIEVEMENTS:
        if ach["key"] in unlocked_rows:
            continue
        if ach["check"](db, student_id):
            db.execute(
                "INSERT INTO achievement_unlocks (student_id, achievement_key) "
                "VALUES (?, ?)",
                (student_id, ach["key"]),
            )
            new_unlocks.append(ach)
    if new_unlocks:
        db.commit()
    return new_unlocks


def achievement_summary(db, student_id):
    """
    Full achievement grid state for display:
    [{key, icon, name, description, unlocked, unlocked_at}]
    """
    rows = {
        r["achievement_key"]: r["unlocked_at"]
        for r in db.execute(
            "SELECT achievement_key, unlocked_at FROM achievement_unlocks "
            "WHERE student_id = ?",
            (student_id,),
        ).fetchall()
    }
    result = []
    for ach in ACHIEVEMENTS:
        result.append({
            "key": ach["key"],
            "icon": ach["icon"],
            "name": ach["name"],
            "description": ach["description"],
            "unlocked": ach["key"] in rows,
            "unlocked_at": rows.get(ach["key"]),
        })
    return result


def unlocked_count(db, student_id):
    row = db.execute(
        "SELECT COUNT(*) FROM achievement_unlocks WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    return row[0]


def recent_xp_events(db, student_id, limit=15):
    """Latest ledger entries for the profile page."""
    return db.execute(
        "SELECT * FROM xp_events WHERE student_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (student_id, limit),
    ).fetchall()


def gamification_snapshot(db, student_id):
    """One-call bundle used by dashboard/profile headers."""
    xp = total_xp(db, student_id)
    evaluate_achievements(db, student_id)
    return {
        "xp": xp,
        "level": level_info(xp),
        "streaks": streak_info(db, student_id),
        "achievements": unlocked_count(db, student_id),
        "achievements_total": len(ACHIEVEMENTS),
    }
