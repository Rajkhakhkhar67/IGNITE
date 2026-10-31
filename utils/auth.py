"""
Authorization decorators for Learning Platform.

Provides reusable decorators that protect routes based on
authentication status and user roles (student / instructor).
"""

from functools import wraps

from flask import session, redirect, url_for, flash


def login_required(f):
    """Redirect to login page if the user is not authenticated."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def student_required(f):
    """Allow only authenticated students.  Instructors are sent to their dashboard."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        if session.get("role") != "student":
            flash("Access denied. Student privileges required.", "error")
            return redirect(url_for("instructor_dashboard"))
        return f(*args, **kwargs)
    return decorated_function


def instructor_required(f):
    """Allow only authenticated instructors.  Students are sent to their dashboard."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        if session.get("role") != "instructor":
            flash("Access denied. Instructor privileges required.", "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated_function
