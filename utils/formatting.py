"""
Shared human-readable formatting helpers.

ONE consistent rule for the whole platform:
    lessons.duration is stored in SECONDS in the database.
    All display formatting goes through format_duration() so a stored
    420 can never render as "420 min" again.
"""

from datetime import datetime


def format_duration(seconds):
    """
    Convert a duration in SECONDS into compact human-readable text.

        360  -> "6 min"
        420  -> "7 min"
        600  -> "10 min"
         65  -> "1m 5s"
         45  -> "45 sec"
       3660  -> "1h 01m"

    Returns "" for None/zero/negative so templates can skip empty values.
    """
    if seconds is None:
        return ""
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes and secs == 0:
        return f"{minutes} min"
    if minutes == 0:
        return f"{secs} sec"
    return f"{minutes}m {secs}s"


def duration_label(lesson_type, duration):
    """
    One-line lesson kind label used across course/lesson listings:

        ("video", 360) -> "VIDEO · 6 min"
        ("text", 480)  -> "READ · 8 min"
    """
    kind = "VIDEO" if (lesson_type or "").lower() == "video" else "READ"
    dur = format_duration(duration)
    return f"{kind} · {dur}" if dur else kind


def format_timestamp(seconds):
    """
    Format numeric seconds as a playback timestamp: MM:SS or H:MM:SS.
    Used for video notes/markers/transcripts (NOT the same as durations).
    """
    try:
        seconds = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
