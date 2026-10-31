"""
Demo content + demo activity seeder (FINAL BACKEND PROMPT 2 OF 3).

Turns the functional platform into a convincing educational product by
populating a SMALL, HIGH-QUALITY course catalog and a realistic learning
history for clearly-identifiable demo accounts.

DESIGN RULES (enforced below)
-----------------------------
1. Idempotent + restart-safe: every insert is preceded by a stable
   natural-key lookup, so running ``python app.py`` any number of times
   never duplicates courses, lessons, quizzes, transcripts, exercises,
   notes, attempts or XP events.
2. Never damages real user data: deletions are limited to exact-match
   development artifacts ("Test Course Updated", its module/lesson) and
   to synthetic rows that belong to the @demo.com seed accounts
   themselves. Real registrations are never touched.
3. Deterministic where practical: fixed file names for seeded resources,
   canonical concept tags, and quiz answers that mirror the app's own
   grading formula (percentage = round(correct/total*100, 1)).
4. Analytics are NEVER hardcoded. We seed UNDERLYING ACTIVITY (progress,
   notes, markers, attempts, XP via the ledger rules) and let the
   existing calculation engines produce Learning Health, Concept
   Mastery, Smart Revision and instructor insights naturally.

Canonical concept tags (Learning Health / Mastery / Revision depend on
these being consistent):
    variables, data-types, conditionals, for-loops, while-loops,
    lists, dictionaries, strings, functions
"""

import os
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

from utils.gamification import XP_RULES, evaluate_achievements

# ---------------------------------------------------------------------------
# Stable identities
# ---------------------------------------------------------------------------

DEMO_INSTRUCTOR = "instructor@demo.com"
DEMO_STUDENT = "student@demo.com"
SIDE_STUDENTS = [("Alex Demo", "alex@demo.com", "alex123"),
                 ("Maya Demo", "maya@demo.com", "maya123")]
ALL_DEMO_EMAILS = [DEMO_INSTRUCTOR, DEMO_STUDENT] + [e for _, e, _ in SIDE_STUDENTS]

GARBAGE_COURSE_TITLES = {"Test Course Updated"}

CANONICAL_TAGS = {
    "variables", "data-types", "conditionals", "for-loops", "while-loops",
    "lists", "dictionaries", "strings", "functions",
    # non-flagship courses may use these:
    "complexity", "sql-basics", "flask-basics",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _one(db, sql, params=()):
    return db.execute(sql, params).fetchone()


def _scalar(db, sql, params=()):
    row = _one(db, sql, params)
    return row[0] if row else None


def _ts(days_ago, hour=18, minute=0):
    """Deterministic 'YYYY-MM-DD HH:MM:SS' timestamp N days before now."""
    dt = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    from datetime import timedelta
    return (dt - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _award_xp_at(db, student_id, event_type, ref_type, ref_id, when):
    """
    award_xp() with an explicit timestamp so demo history spans real days.

    Amounts come from game.XP_RULES (single source of truth) and the
    ux_xp_events_once unique index makes this idempotent exactly like the
    runtime helper. A dedicated helper is required because the runtime
    one stamps datetime('now'), which would collapse all demo history
    into a single day.
    """
    cur = db.execute(
        "INSERT OR IGNORE INTO xp_events "
        "(student_id, event_type, reference_type, reference_id, xp_amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (student_id, event_type, ref_type, ref_id, XP_RULES[event_type], when),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# 1) Demo accounts
# ---------------------------------------------------------------------------

def ensure_demo_accounts(db):
    """Create missing @demo.com accounts with hashed passwords. Never resets."""
    demos = [
        ("Demo Student", DEMO_STUDENT, "student123", "student"),
        ("Demo Instructor", DEMO_INSTRUCTOR, "instructor123", "instructor"),
        *[(n, e, p, "student") for n, e, p in SIDE_STUDENTS],
    ]
    created = []
    for name, email, pw, role in demos:
        if not _one(db, "SELECT id FROM users WHERE email = ?", (email,)):
            db.execute(
                "INSERT INTO users (full_name, email, password_hash, role) "
                "VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(pw), role),
            )
            created.append(email)
    db.commit()
    return created


# ---------------------------------------------------------------------------
# 2) Development-garbage cleanup (exact-match only)
# ---------------------------------------------------------------------------

def remove_test_garbage(db, uploads_root):
    """
    Remove confidently-identifiable automated-test artifacts:

    * courses whose title EXACTLY matches GARBAGE_COURSE_TITLES
      (deleted in the same order as the app's own delete route);
    * unreferenced junk upload files from earlier manual upload tests.
    Real accounts and anything else are never touched.
    """
    removed = []

    for title in sorted(GARBAGE_COURSE_TITLES):
        course = _one(db, "SELECT * FROM courses WHERE title = ?", (title,))
        if not course:
            continue
        cid = course["id"]
        lesson_ids = [r[0] for r in db.execute(
            "SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
            "WHERE m.course_id = ?", (cid,)).fetchall()]
        # same purge order as instructor_delete_course()
        db.execute(
            "DELETE FROM lesson_progress WHERE lesson_id IN "
            "(SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
            " WHERE m.course_id = ?)", (cid,))
        db.execute(
            "DELETE FROM video_notes WHERE lesson_id IN "
            "(SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
            " WHERE m.course_id = ?)", (cid,))
        db.execute(
            "DELETE FROM video_markers WHERE lesson_id IN "
            "(SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
            " WHERE m.course_id = ?)", (cid,))
        db.execute("DELETE FROM revision_activity WHERE course_id = ?", (cid,))
        db.execute("DELETE FROM enrollments WHERE course_id = ?", (cid,))
        db.execute("DELETE FROM courses WHERE id = ?", (cid,))
        removed.append(f"course '{title}' (+{len(lesson_ids)} lesson(s))")
    db.commit()

    # Junk upload files: tiny placeholder files from upload-feature tests,
    # referenced by NO row anywhere.
    referenced = set()
    for (p,) in db.execute(
            "SELECT content_path FROM lessons WHERE content_path IS NOT NULL"):
        referenced.add(os.path.basename(p))
    for (p,) in db.execute("SELECT file_path FROM lesson_resources"):
        referenced.add(os.path.basename(p))
    for (p,) in db.execute(
            "SELECT thumbnail FROM courses WHERE thumbnail IS NOT NULL"):
        referenced.add(os.path.basename(p))

    for sub in ("videos", "resources", "thumbnails"):
        folder = os.path.join(uploads_root, sub)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name in referenced:
                continue
            try:
                if os.path.isfile(path) and os.path.getsize(path) < 10_000:
                    os.remove(path)
                    removed.append(f"junk upload {sub}/{name}")
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# 3) Flagship course — Python Fundamentals
# ---------------------------------------------------------------------------

FLAGSHIP_TITLE = "Python Fundamentals"
FLAGSHIP_DESCRIPTION = (
    "Build a practical foundation in Python through guided lessons, hands-on "
    "coding exercises, quick checks, and focused revision. Covers variables, "
    "control flow, collections, and functions with runnable examples."
)

# Lesson bodies are PLAIN TEXT: the renderer converts \n to <br>.
L = {}


def _flagship_lessons():
    """Curriculum definitions. `old` = previous placeholder title to upgrade."""
    return [
        {
            "module": "Getting Started", "module_desc":
                "What Python is, how to run it, and your first programs.",
            "title": "Welcome to Python", "old": "Introduction to Python",
            "desc": "What Python is and why it is worth learning",
            "type": "text", "duration": 480,
            "body": (
                "Python is a high-level, interpreted programming language "
                "designed to be readable first. Instead of compiling a whole "
                "program, you can run it line by line, which makes it ideal "
                "for beginners.\n\n"
                "Why developers pick Python:\n"
                "- Simple, English-like syntax\n"
                "- Huge standard library (batteries included)\n"
                "- Used everywhere: web apps, data science, automation, AI\n\n"
                "Example — your first program:\n"
                ">>> print(\"Hello, world!\")\n"
                "Hello, world!\n\n"
                "Common mistake: mixing up Python versions. This course uses "
                "Python 3 syntax throughout.\n\n"
                "Takeaway: Python trades a little speed for a lot of clarity, "
                "which is why it is a great first language."
            ),
        },
        {
            "module": "Getting Started",
            "title": "Variables and Data Types",
            "desc": "The flagship video walkthrough of variables and core types",
            "type": "video", "duration": 360,
            # A locally generated H.264 slide lesson shipped with the demo.
            # Keep any instructor replacement; only attach this canonical
            # asset when a legacy/demo row has no video yet.
            "video_path": "videos/python_variables_data_types.mp4",
            "convert_from_text": True,
            "old_content_marker": "Core data types:",
        },
        {
            "module": "Getting Started", "title": "Input and Output",
            "desc": "Reading user input and printing clean output",
            "type": "text", "duration": 420,
            "body": (
                "Programs become useful when they can talk to users. print() "
                "writes output; input() reads what the user types.\n\n"
                "Printing values:\n"
                ">>> print(\"Age:\", 20)\n"
                "Age: 20\n\n"
                "f-strings embed values inside text:\n"
                ">>> name = \"Riya\"\n"
                ">>> print(f\"Welcome, {name}!\")\n"
                "Welcome, Riya!\n\n"
                "Reading input (always returns text):\n"
                ">>> age = int(input(\"Your age: \"))\n\n"
                "Practical example:\n"
                ">>> price = 49\n"
                ">>> qty = 3\n"
                ">>> print(f\"Total: {price * qty}\")\n"
                "Total: 147\n\n"
                "Common mistake: forgetting int() around numeric input — "
                "\"20\" plus 1 raises a TypeError because \"20\" is text.\n\n"
                "Takeaway: f-strings are the modern, readable way to build "
                "output from variables."
            ),
        },
        {
            "module": "Decisions and Repetition", "old_module": "Control Flow",
            "module_desc": "Teach programs to choose with if/elif/else and repeat with loops.",
            "title": "Conditional Statements",
            "old": "Conditional Statements",
            "desc": "Making decisions with if, elif and else",
            "type": "text", "duration": 540,
            "rewrite_body": True,
            "body": (
                "Conditionals let a program choose between paths based on a "
                "question whose answer is True or False.\n\n"
                "Syntax:\n"
                "if score >= 50:\n"
                "    print(\"Pass\")\n"
                "elif score >= 40:\n"
                "    print(\"Retake\")\n"
                "else:\n"
                "    print(\"Fail\")\n\n"
                "Comparison operators: == != < > <= >=\n"
                "Combine questions with and / or / not.\n\n"
                "Practical example:\n"
                ">>> temp = 31\n"
                ">>> if temp > 30:\n"
                "...     print(\"Hot day\")\n"
                "Hot day\n\n"
                "Common mistake: using = (assignment) instead of == "
                "(comparison) inside an if condition — Python rejects it.\n\n"
                "Takeaway: elif chains stop at the FIRST matching branch; "
                "order your checks from most specific to most general."
            ),
        },
        {
            "module": "Decisions and Repetition",
            "title": "For Loops", "old": "Loops",
            "desc": "Repeating work over sequences with for",
            "type": "text", "duration": 540,
            "rewrite_body": True,
            "body": (
                "A for loop repeats an action once per item of a sequence — "
                "a range of numbers, a list, even the letters of a string.\n\n"
                "Looping over a range:\n"
                "for i in range(1, 6):\n"
                "    print(i)\n"
                "# prints 1 2 3 4 5\n\n"
                "range(start, stop) includes start but EXCLUDES stop — the "
                "most common beginner surprise.\n\n"
                "Looping over a list:\n"
                "for color in [\"red\", \"green\", \"blue\"]:\n"
                "    print(color)\n\n"
                "Building a total:\n"
                "total = 0\n"
                "for n in [2, 4, 6]:\n"
                "    total += n\n"
                "# total is now 12\n\n"
                "Useful keywords: break exits early, continue skips to the "
                "next item.\n\n"
                "Takeaway: prefer for-loops over while-loops whenever you "
                "already know WHAT you are looping over."
            ),
        },
        {
            "module": "Decisions and Repetition",
            "title": "While Loops",
            "desc": "Repeating while a condition remains true",
            "type": "text", "duration": 420,
            "rewrite_body": True,
            "body": (
                "A while loop repeats while its condition is True. Use it when "
                "you do not know in advance how many repetitions you need.\n\n"
                "A countdown:\n"
                "count = 3\n"
                "while count > 0:\n"
                "    print(count)\n"
                "    count -= 1\n"
                "print(\"Go!\")\n\n"
                "The update count -= 1 is essential: it eventually makes the "
                "condition False. Without an update, a while loop can run forever.\n\n"
                "Use a while loop for input validation or a game that continues "
                "until the player chooses to stop. Use a for loop when you already "
                "have a sequence or range to visit.\n\n"
                "Takeaway: every while loop needs a clear condition and a path that "
                "changes that condition."
            ),
        },
        {
            "module": "Working with Collections", "old_module": "Functions",
            "module_desc": "Lists, tuples, dictionaries and strings — storing groups of data.",
            "title": "Lists", "old": "Understanding Functions",
            "desc": "Ordered, changeable collections",
            "type": "text", "duration": 540,
            "rewrite_body": True,
            "body": (
                "A list stores many values in order, and you can add, remove "
                "or replace items at any time (lists are mutable).\n\n"
                "Creating and indexing (counting starts at 0):\n"
                "colors = [\"red\", \"blue\", \"green\"]\n"
                "colors[0]     # 'red'\n"
                "colors[-1]    # 'green' — negative index counts from the end\n\n"
                "Growing a list:\n"
                "colors.append(\"yellow\")\n\n"
                "Slicing copies a section:\n"
                "colors[0:2]   # ['red', 'blue']\n\n"
                "len(colors) tells you how many items exist.\n\n"
                "Common mistake: colors[3] on the list above raises IndexError "
                "— valid indexes are 0..2.\n\n"
                "Takeaway: lists are the default container in Python; master "
                "indexing and append() before anything else."
            ),
        },
        {
            "module": "Working with Collections",
            "title": "Tuples and Dictionaries",
            "desc": "Fixed pairs and key-based lookups",
            "type": "text", "duration": 480,
            "body": (
                "Tuples are lists that cannot change after creation — perfect "
                "for fixed pairs like coordinates:\n"
                "point = (3, 7)\n"
                "point[0]      # 3\n\n"
                "Dictionaries store values under keys instead of positions, "
                "so lookups read like sentences:\n"
                "student = {\"name\": \"Riya\", \"age\": 20}\n"
                "student[\"age\"]        # 20\n"
                "student[\"grade\"] = \"A\"   # add a new pair\n\n"
                "Looping over pairs:\n"
                "for key, value in student.items():\n"
                "    print(key, value)\n\n"
                "Choosing between them:\n"
                "- Ordered collection that changes? list\n"
                "- Fixed shape? tuple\n"
                "- Lookup by label? dictionary\n\n"
                "Common mistake: student[0] fails on dictionaries — you must "
                "use the KEY, not a position.\n\n"
                "Takeaway: dictionaries turn \"find the value for X\" into a "
                "one-line operation."
            ),
        },
        {
            "module": "Working with Collections",
            "title": "Working with Strings",
            "desc": "Everyday string operations every program needs",
            "type": "text", "duration": 420,
            "body": (
                "Strings are sequences of characters, so much of what you "
                "learned about lists applies here too.\n\n"
                "Handy built-ins:\n"
                ">>> name = \"riya shah\"\n"
                ">>> name.upper()\n"
                "'RIYA SHAH'\n"
                ">>> name.title()\n"
                "'Riya Shah'\n"
                ">>> len(name)\n"
                "9\n\n"
                "Checking contents:\n"
                ">>> \"ya\" in name\n"
                "True\n\n"
                "Splitting and joining:\n"
                ">>> parts = \"a,b,c\".split(\",\")\n"
                ">>> \"-\".join(parts)\n"
                "'a-b-c'\n\n"
                "Common mistake: strings are immutable — methods return NEW "
                "strings, so name.upper() alone changes nothing unless you "
                "store the result.\n\n"
                "Takeaway: chain small string tools together instead of "
                "writing manual character loops."
            ),
        },
        {
            "module": "Reusable Programs",
            "module_desc": "Package logic into functions and combine everything you have learned.",
            "title": "Functions",
            "desc": "Naming a block of code so you can reuse it",
            "type": "text", "duration": 480,
            "body": (
                "A function wraps steps under a NAME so you can run them "
                "whenever needed without repeating code.\n\n"
                "Defining and calling:\n"
                "def greet(name):\n"
                "    return f\"Hello, {name}!\"\n\n"
                "message = greet(\"Riya\")\n"
                "print(message)      # Hello, Riya!\n\n"
                "Anatomy:\n"
                "- def starts the definition\n"
                "- parameters (name) are inputs\n"
                "- return hands a result back to the caller\n\n"
                "Why bother?\n"
                "- Write once, call many times\n"
                "- Easier testing and debugging\n"
                "- Code reads like a story of named steps\n\n"
                "Common mistake: calling greet(\"Riya\") but ignoring the "
                "returned value — printing happens only if YOU print it.\n\n"
                "Takeaway: if you copy-paste code twice, it wants to be a "
                "function."
            ),
        },
        {
            "module": "Reusable Programs",
            "title": "Parameters and Return Values",
            "desc": "Feeding data in and getting answers out",
            "type": "text", "duration": 420,
            "body": (
                "Parameters let one function serve many situations; return "
                "values let results flow back into your program.\n\n"
                "Default parameters make arguments optional:\n"
                "def power(base, exponent=2):\n"
                "    return base ** exponent\n\n"
                "power(3)       # 9  (uses default)\n"
                "power(3, 3)    # 27\n\n"
                "Returning multiple values packs them into a tuple:\n"
                "def min_max(numbers):\n"
                "    return min(numbers), max(numbers)\n\n"
                "low, high = min_max([4, 8, 15])\n\n"
                "Common mistake: expecting a function WITHOUT a return "
                "statement to hand something back — it returns None, and "
                "printing None is usually the symptom.\n\n"
                "Takeaway: think of parameters as the question and the "
                "return value as the answer."
            ),
        },
        {
            "module": "Reusable Programs",
            "title": "Mini Practice Challenge",
            "desc": "Combine variables, loops, conditionals and functions in one program",
            "type": "text", "duration": 600,
            "body": (
                "Time to combine the whole course in one small program.\n\n"
                "Task: given a list of scores, write a function that returns "
                "how many passed (50 or more), then print a report.\n\n"
                "scores = [45, 82, 91, 38, 67]\n\n"
                "def count_passing(marks):\n"
                "    passed = 0\n"
                "    for m in marks:\n"
                "        if m >= 50:\n"
                "            passed += 1\n"
                "    return passed\n\n"
                "passed = count_passing(scores)\n"
                "print(f\"{passed} of {len(scores)} students passed\")\n"
                "# 3 of 5 students passed\n\n"
                "Notice the layers: a variable holds the data, the loop "
                "repeats the check, the conditional decides, and the function "
                "names the whole idea.\n\n"
                "Try extending it: also count how many scored above 80, or "
                "return the average too.\n\n"
                "Takeaway: real programs are just these small ideas, "
                "composed carefully."
            ),
        },
    ]


FLAGSHIP_TRANSCRIPT = [
    (0,   "Hi everyone, welcome to the course. In this lesson we look at "
          "variables and Python's core data types."),
    (20,  "A variable is simply a name that refers to a value stored in memory."),
    (55,  "Creating one is a single assignment: name equals Riya. No type "
          "declaration is needed."),
    (95,  "Strings hold text. Wrap characters in quotes, like greeting equals "
          "quote Hello quote."),
    (130, "Numbers come in two flavours: int for whole numbers and float for "
          "decimals such as 3.14."),
    (175, "Booleans are True or False. They power every decision your program makes."),
    (210, "Not sure what type a value is? Call type on it and Python tells you."),
    (255, "Naming rules: letters, digits and underscores, but never start with "
          "a digit. Names are case sensitive."),
    (300, "Quick example: store an age, add one to it, and print the result."),
    (345, "Recap: variables reference values, and str, int, float and bool "
          "are the types you will meet everywhere."),
]

FLAGSHIP_EXERCISES = {
    # lesson title -> (instructions, starter_code, expected_output)
    "Variables and Data Types": (
        "Create two variables: name should hold \"Riya\" and age should hold "
        "20. Print name first, then age — each on its own line.",
        "# 1. create the variables\n"
        "# name = ...\n"
        "# age = ...\n\n"
        "# 2. print them in order\n",

        "Riya\n20",
    ),
    "Conditional Statements": (
        "score already holds 72. Write ONE if/else statement that prints "
        "\"Pass\" when score is 50 or higher, otherwise prints \"Fail\".",
        "score = 72\n\n"
        "# your if / else check goes below\n",

        "Pass",
    ),
    "For Loops": (
        "Use a for loop with range() to print the numbers 1 to 5 — each on "
        "its own line.",
        "# hint: range(1, 6) produces 1..5\n",

        "1\n2\n3\n4\n5",
    ),
    "Lists": (
        "The list colors has three items. Print the FIRST item and then the "
        "LAST item, each on its own line.",
        "colors = [\"red\", \"blue\", \"green\"]\n\n"
        "# print colors[0] first, then the last item\n",

        "red\ngreen",
    ),
    "Functions": (
        "Define a function square(n) that RETURNS the square of n, then "
        "print square(6).",
        "def square(n):\n"
        "    # return the square of n\n"
        "    pass\n\n"
        "print(square(6))\n",

        "36",
    ),
    "Mini Practice Challenge": (
        "The list scores contains five numbers. Use a loop to count how many "
        "are 50 or higher, then print exactly: Passed X of 5  (with X replaced "
        "by your count).",
        "scores = [45, 82, 91, 38, 67]\n"
        "passed = 0\n\n"
        "# loop, check >= 50, increase passed\n\n"
        "# print the final message\n",

        "Passed 3 of 5",
    ),
}

# Reference solutions — used by tests/verification, never stored in DB.
EXERCISE_SOLUTIONS = {
    "Variables and Data Types": "name = \"Riya\"\nage = 20\nprint(name)\nprint(age)",
    "Conditional Statements": "score = 72\nif score >= 50:\n    print(\"Pass\")\nelse:\n    print(\"Fail\")",
    "For Loops": "for i in range(1, 6):\n    print(i)",
    "Lists": "colors = [\"red\", \"blue\", \"green\"]\nprint(colors[0])\nprint(colors[-1])",
    "Functions": "def square(n):\n    return n * n\n\nprint(square(6))",
    "Mini Practice Challenge": (
        "scores = [45, 82, 91, 38, 67]\npassed = 0\nfor s in scores:\n"
        "    if s >= 50:\n        passed += 1\nprint(f\"Passed {passed} of 5\")"
    ),
}

# New quizzes added to flagship lessons (existing Variables quiz is retagged).
NEW_FLAGSHIP_QUIZZES = {
    "Conditional Statements": (
        "Conditional Statements — Quick Check", 60.0,
        [
            ("score = 65. What does this print?\nif score >= 50:\n    print(\"Pass\")\nelse:\n    print(\"Fail\")",
             "Pass", "Fail", "Error", "None", "A",
             "65 >= 50 is True, so the if branch runs.", "conditionals"),
            ("Which value of x makes this print \"B\"?\nif x > 10:\n    print(\"A\")\nelif x > 5:\n    print(\"B\")\nelse:\n    print(\"C\")",
             "12", "7", "3", "-2", "B",
             "x=7 fails x > 10 but passes x > 5, reaching the elif branch.",
             "conditionals"),
            ("Which operator CHECKS whether two values are equal?",
             "=", "==", "===", "=>", "B",
             "= assigns; == compares.", "conditionals"),
        ],
    ),
    "For Loops": (
        "For Loops — Quick Check", 60.0,
        [
            ("How many times does this loop run?\nfor i in range(3):",
             "2", "3", "4", "Forever", "B",
             "range(3) yields three numbers: 0, 1, 2.", "for-loops"),
            ("Which values does range(1, 6) produce?",
             "0 to 4", "1 to 5", "1 to 6", "0 to 5", "B",
             "start is included, stop is excluded.", "for-loops"),
            ("total = 0\nfor n in [2, 4, 6]:\n    total += n\nWhat is printed by print(total)?",
             "6", "10", "12", "24", "C",
             "2+4+6 adds up to 12.", "for-loops"),
            ("Which keyword exits a loop IMMEDIATELY?",
             "stop", "exit", "break", "pass", "C",
             "break leaves the loop at once; continue only skips one step.",
             "for-loops"),
        ],
    ),
    "Lists": (
        "Lists — Quick Check", 60.0,
        [
            ("colors = [\"red\", \"blue\", \"green\"]\nWhat is colors[1]?",
             "red", "blue", "green", "IndexError", "B",
             "Indexing starts at 0, so [1] is the second item.", "lists"),
            ("colors[-1] on the same list returns…",
             "red", "blue", "green", "Nothing", "C",
             "Negative indexes count from the end; -1 is the last item.",
             "lists"),
            ("After colors.append(\"yellow\"), what does len(colors) return? (started with 3 items)",
             "3", "4", "5", "It errors", "B",
             "append adds one item to the end.", "lists"),
            ("Which bracket pair creates a LIST?",
             "[ ]", "{ }", "( )", "< >", "A",
             "{} creates dictionaries/sets, () tuples, [] lists.", "lists"),
        ],
    ),
    "Tuples and Dictionaries": (
        "Tuples & Dictionaries — Quick Check", 60.0,
        [
            ("student = {\"name\": \"Riya\", \"age\": 20}\nWhich expression gives 20?",
             "student[1]", "student[\"age\"]", "student.age", "student(2)", "B",
             "Dictionary values are fetched by key.", "dictionaries"),
            ("What is the key difference between a tuple and a list?",
             "Tuples are slower", "Tuples are immutable",
             "Lists cannot hold strings", "There is none", "B",
             "Tuples cannot be modified after creation.", "data-types"),
            ("What does len({\"a\": 1, \"b\": 2}) return?",
             "1", "2", "4", "It errors", "B",
             "len counts the key-value PAIRS.", "dictionaries"),
        ],
    ),
    "Functions": (
        "Functions — Quick Check", 60.0,
        [
            ("def add(a, b):\n    return a + b\nWhat does add(2, 3) evaluate to?",
             "23", "5", "None", "An error", "B",
             "add receives 2 and 3 and returns their sum.", "functions"),
            ("Which keyword DEFINES a function?",
             "func", "define", "def", "function", "C",
             "def name(parameters): starts a definition.", "functions"),
            ("A function finishes without hitting a return statement. What does it give back?",
             "0", "\"\"", "None", "The last variable", "C",
             "No return means the call evaluates to None.", "functions"),
            ("def power(base, exponent=2):\n    return base ** exponent\nWhat is power(3)?",
             "6", "9", "27", "Error — missing argument", "B",
             "exponent defaults to 2, so power(3) is 3**2 = 9.", "functions"),
        ],
    ),
}

# Retagging for the pre-existing Variables & Data Types quiz (position -> tag).
EXISTING_QUIZ_RETAG = {
    "Variables and Data Types": {
        1: "variables", 2: "data-types", 3: "data-types",
        4: "data-types", 5: "dictionaries",
    },
}


def _get_or_create_module(db, course_id, title, desc, position):
    mod = _one(db, "SELECT * FROM modules WHERE course_id = ? AND title = ?",
               (course_id, title))
    if mod:
        return mod["id"]
    cur = db.execute(
        "INSERT INTO modules (course_id, title, description, position) "
        "VALUES (?, ?, ?, ?)", (course_id, title, desc, position))
    return cur.lastrowid


def _rename_module_if(db, course_id, old_title, new_title, desc):
    """Rename a legacy module only while it still carries the legacy title."""
    if not old_title or old_title == new_title:
        return
    mod = _one(db, "SELECT * FROM modules WHERE course_id = ? AND title = ?",
               (course_id, old_title))
    if mod:
        db.execute(
            "UPDATE modules SET title = ?, description = ? WHERE id = ?",
            (new_title, desc, mod["id"]))


def seed_python_fundamentals(db, uploads_root=None):
    """Upgrade the existing flagship course to the full 4-module curriculum."""
    instructor_id = _scalar(db, "SELECT id FROM users WHERE email = ?",
                            (DEMO_INSTRUCTOR,))
    if not instructor_id:
        return None
    course = _one(db, "SELECT * FROM courses WHERE title = ? AND instructor_id = ?",
                  (FLAGSHIP_TITLE, instructor_id))
    if not course:
        # Course absent entirely (fresh database): create the shell.
        cur = db.execute(
            "INSERT INTO courses (instructor_id, title, description, category, "
            "level, status) VALUES (?, ?, ?, ?, ?, ?)",
            (instructor_id, FLAGSHIP_TITLE, FLAGSHIP_DESCRIPTION,
             "Programming", "beginner", "published"))
        course_id = cur.lastrowid
    else:
        course_id = course["id"]
        # Refresh catalog metadata ONLY while the old auto-seeded text is
        # still there (an edited description is user content and stays).
        if course["description"].startswith(
                "A comprehensive introduction to Python programming."):
            db.execute(
                "UPDATE courses SET description = ?, category = ?, level = ? "
                "WHERE id = ?",
                (FLAGSHIP_DESCRIPTION, "Programming", "beginner", course_id))

    module_order = {}
    seen_modules = set()
    for spec in _flagship_lessons():
        m_title = spec["module"]
        if m_title not in module_order:
            if spec.get("old_module"):
                _rename_module_if(db, course_id, spec["old_module"],
                                  m_title, spec.get("module_desc", ""))
            pos = len(module_order) + 1
            module_order[m_title] = _get_or_create_module(
                db, course_id, m_title, spec.get("module_desc", ""), pos)
            seen_modules.add(m_title)

    for idx, spec in enumerate(_flagship_lessons(), start=1):
        mod_id = module_order[spec["module"]]
        existing = _one(db, "SELECT * FROM lessons WHERE module_id = ? AND title = ?",
                        (mod_id, spec["title"]))
        if not existing and spec.get("old"):
            existing = _one(db, "SELECT * FROM lessons WHERE module_id = ? AND title = ?",
                            (mod_id, spec["old"]))
        if existing:
            lid = existing["id"]
            updates, params = [], []
            if existing["title"] != spec["title"]:
                updates.append("title = ?"); params.append(spec["title"])
            if existing["description"] != spec["desc"]:
                updates.append("description = ?"); params.append(spec["desc"])
            if spec["type"] == "video" and existing["lesson_type"] != "video":
                # Convert the old TEXT version of this lesson to the flagship
                # VIDEO lesson (content_path stays NULL => clean pending state).
                updates.append("lesson_type = ?"); params.append("video")
                updates.append("content_path = NULL")
                updates.append("content_text = NULL")
            if spec["type"] == "text" and existing["lesson_type"] != "text":
                # The legacy While Loops placeholder had no actual media.
                # Convert this canonical demo lesson to complete text content
                # rather than leaving students a broken video player.
                updates.append("lesson_type = ?"); params.append("text")
                updates.append("content_path = NULL")
            if spec["type"] == "video" and not existing["duration"]:
                updates.append("duration = ?"); params.append(spec["duration"])
            video_path = spec.get("video_path")
            if (video_path and uploads_root and not existing["content_path"]
                    and os.path.isfile(os.path.join(uploads_root, video_path))):
                updates.append("content_path = ?"); params.append(video_path)
            if spec["type"] == "text":
                # Rewrite bodies only for known auto-seed placeholders
                # (`old` match) or lessons we ourselves seeded empty.
                known_placeholder = bool(spec.get("old")) and \
                    spec["old"] != spec["title"]
                if (known_placeholder or spec.get("rewrite_body")
                        or not existing["content_text"]):
                    updates.append("content_text = ?"); params.append(spec["body"])
                    updates.append("duration = ?"); params.append(spec["duration"])
            if updates:
                updates.append("position = ?"); params.append(idx)
                params.append(lid)
                db.execute(f"UPDATE lessons SET {', '.join(updates)} WHERE id = ?",
                           params)
        else:
            cur = db.execute(
                "INSERT INTO lessons (module_id, title, description, lesson_type, "
                "content_path, content_text, duration, position) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                (mod_id, spec["title"], spec["desc"], spec["type"],
                 spec["body"] if spec["type"] == "text" else None,
                 spec["duration"], idx))
            lid = cur.lastrowid
    db.commit()

    # Transcript segments (ascending, deduped per start time).
    video_lid = _scalar(db,
        "SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND l.title = 'Variables and Data Types'",
        (course_id,))
    if video_lid:
        for pos, (sec, text) in enumerate(FLAGSHIP_TRANSCRIPT, start=1):
            exists = _one(db,
                "SELECT id FROM lesson_transcript_segments "
                "WHERE lesson_id = ? AND start_seconds = ?", (video_lid, sec))
            if not exists:
                db.execute(
                    "INSERT INTO lesson_transcript_segments "
                    "(lesson_id, start_seconds, text, position) "
                    "VALUES (?, ?, ?, ?)", (video_lid, sec, text, pos))
        db.commit()

    # Coding exercises (one per lesson, UNIQUE constraint guards reruns).
    for title, (instr, starter, expected) in FLAGSHIP_EXERCISES.items():
        lid = _scalar(db,
            "SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
            "WHERE m.course_id = ? AND l.title = ?", (course_id, title))
        if lid and not _one(db, "SELECT id FROM coding_exercises WHERE lesson_id = ?",
                            (lid,)):
            db.execute(
                "INSERT INTO coding_exercises "
                "(lesson_id, instructions, starter_code, expected_output) "
                "VALUES (?, ?, ?, ?)", (lid, instr, starter, expected))
    db.commit()

    # Retag the pre-existing Variables quiz with canonical tags.
    retag = EXISTING_QUIZ_RETAG.get("Variables and Data Types", {})
    var_qz = _one(db,
        "SELECT qz.id FROM quizzes qz JOIN lessons l ON qz.lesson_id = l.id "
        "JOIN modules m ON l.module_id = m.id "
        "WHERE m.course_id = ? AND l.title = 'Variables and Data Types'",
        (course_id,))
    if var_qz and retag:
        for pos, tag in retag.items():
            db.execute(
                "UPDATE quiz_questions SET concept_tag = ? "
                "WHERE quiz_id = ? AND position = ?", (tag, var_qz["id"], pos))
        db.commit()

    # New quizzes (skip any lesson that already has one).
    for title, (qtitle, passing, questions) in NEW_FLAGSHIP_QUIZZES.items():
        lid = _scalar(db,
            "SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
            "WHERE m.course_id = ? AND l.title = ?", (course_id, title))
        if not lid or _one(db, "SELECT id FROM quizzes WHERE lesson_id = ?", (lid,)):
            continue
        cur = db.execute(
            "INSERT INTO quizzes (lesson_id, title, description, passing_percentage) "
            "VALUES (?, ?, ?, ?)",
            (lid, qtitle, "Five-minute self-check on this lesson.", passing))
        quiz_id = cur.lastrowid
        for pos, (text, a, b, c, d, correct, expl, tag) in enumerate(
                questions, start=1):
            db.execute(
                "INSERT INTO quiz_questions (quiz_id, question_text, question_type, "
                "option_a, option_b, option_c, option_d, correct_answer, explanation, "
                "concept_tag, position) VALUES (?, ?, 'multiple_choice', "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                (quiz_id, text, a, b, c, d, correct, expl, tag, pos))
    db.commit()
    return course_id


# ---------------------------------------------------------------------------
# 4) Supporting courses
# ---------------------------------------------------------------------------

OTHER_COURSES = [
    {
        "title": "Data Structures Essentials",
        "category": "Computer Science", "level": "intermediate",
        "description": (
            "Understand the structures behind working programs — lists, stacks, "
            "queues — and learn to reason about search, sorting and Big-O "
            "without heavy mathematics."
        ),
        "modules": [
            ("Arrays and Python Lists", "How dynamic arrays behave under Python's favourite container.", [
                ("Lists Under the Hood", "Contiguous storage, indexing and growth",
                 "Python lists are dynamic arrays: items live side by side in "
                 "memory, which is why indexing is instant.\n\n"
                 "Index cost:\n"
                 ">>> nums = [10, 20, 30]\n"
                 ">>> nums[1]\n"
                 "20        # computed address, O(1)\n\n"
                 "Growth: appending is usually O(1) thanks to spare capacity; "
                 "occasionally Python must relocate the block.\n\n"
                 "Costly operations: inserting at the FRONT shifts every item "
                 "(O(n)). When you need fast front-inserts, a deque fits "
                 "better.\n\n"
                 "Takeaway: know the cheap direction of your container — end "
                 "operations are cheap for arrays, front operations are not."),
                ("List Operations and Their Cost", "append vs insert vs pop",
                 "Rough costs to remember:\n"
                 "- append(x): O(1) average\n"
                 "- pop(): O(1) — removes LAST item\n"
                 "- insert(0, x): O(n) — shifts everything right\n"
                 "- x in lst: O(n) linear scan\n\n"
                 "Example — building reversed order cheaply:\n"
                 "out = []\n"
                 "for n in [1, 2, 3]:\n"
                 "    out.insert(0, n)   # works, but O(n^2) overall\n\n"
                 "Better: out.append(n) then reverse once.\n\n"
                 "Common mistake: measuring with feelings instead of growth. "
                 "Double the input, watch the time — quadratic shows itself "
                 "immediately.\n\n"
                 "Takeaway: choosing WHERE to add/remove matters as much as "
                 "the algorithm above it."),
            ]),
            ("Stacks and Queues", "Two disciplines, countless programs.", [
                ("Stacks — Last In, First Out", "push, pop and the undo button",
                 "A stack lets you touch only the TOP: push adds, pop removes "
                 "the most recent item (LIFO).\n\n"
                 "In Python a plain list is the idiomatic stack:\n"
                 "stack = []\n"
                 "stack.append(\"page1\")\n"
                 "stack.append(\"page2\")\n"
                 "stack.pop()      # 'page2'\n\n"
                 "Where LIFO shines: undo history, browser back, matching "
                 "brackets, depth-first search.\n\n"
                 "Bracket checker sketch:\n"
                 "for ch in text:\n"
                 "    if ch == '(': stack.append(ch)\n"
                 "    elif ch == ')': stack.pop()\n\n"
                 "Common mistake: popping an empty stack — IndexError. Always "
                 "check truthiness first.\n\n"
                 "Takeaway: if the MOST RECENT item must be handled first, you "
                 "want a stack."),
                ("Queues — First In, First Out", "Fair ordering for shared resources",
                 "Queues serve items in arrival order (FIFO) — printers, task "
                 "schedulers, ticket lines.\n\n"
                 "from collections import deque\n"
                 "jobs = deque()\n"
                 "jobs.append(\"print-a\")\n"
                 "jobs.popleft()     # oldest job first\n\n"
                 "deque gives O(1) operations at BOTH ends; lists only at the "
                 "right end.\n\n"
                 "Breadth-first search is the classic algorithm built on a "
                 "queue: explore neighbours layer by layer.\n\n"
                 "Takeaway: queue = fairness, stack = recency. Pick the "
                 "discipline that matches your problem's promise."),
            ]),
            ("Search and Sort Basics", "Finding things and putting them in order.", [
                ("Linear vs Binary Search", "The payoff of keeping data sorted",
                 "Linear search checks items one by one: O(n). Works on any "
                 "list, sorted or not.\n\n"
                 "Binary search halves the remaining window each step: O(log n) "
                 "— but REQUIRES sorted data.\n\n"
                 "One million sorted items need at most ~20 comparisons. That "
                 "is the entire selling point of sorting upfront.\n\n"
                 "Sketch:\n"
                 "while low <= high:\n"
                 "    mid = (low + high) // 2\n"
                 "    ...compare and move a boundary...\n\n"
                 "Common mistake: binary searching an unsorted list and "
                 "trusting the wrong answer.\n\n"
                 "Takeaway: sorted data turns lookup cost from 'size of data' "
                 "into 'digits of size'."),
                ("Sorting and Growth Intuition", "From bubble sort to Timsort",
                 "Bubble sort repeatedly swaps neighbours: simple, instructive, "
                 "and O(n^2) — doubling the data QUADRUPLES the work.\n\n"
                 "Python's sorted() uses Timsort: O(n log n), highly tuned.\n\n"
                 "sorted([3, 1, 2])              # new list\n"
                 "nums.sort(key=len)             # in place, custom key\n\n"
                 "Sorting stability (equal items keep their order) enables "
                 "multi-pass sorting tricks: sort by secondary key first, then "
                 "primary.\n\n"
                 "Big-O cheatline: O(1) < O(log n) < O(n) < O(n log n) < O(n^2).\n\n"
                 "Takeaway: reach for built-ins first; they embody decades of "
                 "engineering."),
            ]),
        ],
    },
    {
        "title": "SQL & Database Foundations",
        "category": "Databases", "level": "beginner",
        "description": (
            "Store, query and protect data with SQL. From tables and primary "
            "keys to everyday SELECT queries and safe INSERT/UPDATE patterns."
        ),
        "modules": [
            ("Database Basics", "Tables, rows, columns and identity.", [
                ("Tables, Rows and Columns", "Spreadsheets with discipline",
                 "A relational database stores data in TABLES: each ROW is one "
                 "record, each COLUMN one attribute with a fixed type.\n\n"
                 "-- a students table\n"
                 "CREATE TABLE students (\n"
                 "    id INTEGER PRIMARY KEY,\n"
                 "    name TEXT NOT NULL,\n"
                 "    age INTEGER\n"
                 ");\n\n"
                 "Unlike spreadsheets, the schema enforces rules: wrong types "
                 "or missing NOT NULL values are rejected.\n\n"
                 "SQLite note: one FILE is a whole database — that is what "
                 "powers this very platform.\n\n"
                 "Takeaway: model entities (Student, Course) as tables and "
                 "relationships as columns referencing other tables."),
                ("Primary Keys", "Every record needs an identity",
                 "A PRIMARY KEY uniquely identifies each row. It is NOT NULL, "
                 "unique, and usually an auto-incrementing integer id.\n\n"
                 "Why it matters:\n"
                 "- updates/deletes target exact rows\n"
                 "- other tables reference it (foreign keys)\n"
                 "- no accidental duplicate records\n\n"
                 "Natural vs surrogate keys: emails feel natural but people "
                 "change emails; a meaningless id never needs to change.\n\n"
                 "Takeaway: give every table an id INTEGER PRIMARY KEY and let "
                 "the database manage uniqueness."),
            ]),
            ("Reading Data", "Querying is the daily bread of SQL.", [
                ("SELECT Fundamentals", "Asking precise questions",
                 "SELECT chooses COLUMNS, FROM picks the table, and the result "
                 "is always a new table-shaped answer.\n\n"
                 "SELECT name, age FROM students;\n"
                 "SELECT * FROM students;         -- all columns\n"
                 "SELECT DISTINCT city FROM students;\n\n"
                 "Order rows and limit output:\n"
                 "SELECT name FROM students ORDER BY age DESC LIMIT 5;\n\n"
                 "Common mistake: SELECT * in production code — fetch only the "
                 "columns you use.\n\n"
                 "Takeaway: read queries OUT LOUD — 'select name from students "
                 "ordered by age' is literally the sentence."),
                ("Filtering with WHERE", "Rows in, fewer rows out",
                 "WHERE keeps only rows matching a condition:\n\n"
                 "SELECT * FROM students WHERE age >= 18;\n"
                 "SELECT * FROM students WHERE city = 'Surat' AND age < 25;\n\n"
                 "Operators: = <> > < BETWEEN LIKE IN IS NULL.\n\n"
                 "LIKE pattern: 'ra%' matches names starting with Ra.\n\n"
                 "Common mistake: NULL comparisons — age = NULL is never true; "
                 "write IS NULL.\n\n"
                 "Takeaway: combine simple conditions with AND/OR instead of "
                 "hunting for one clever expression."),
            ]),
            ("Changing Data", "Write operations done safely.", [
                ("INSERT, UPDATE and DELETE", "Writing rows responsibly",
                 "Three verbs change data:\n\n"
                 "INSERT INTO students (name, age) VALUES ('Riya', 20);\n"
                 "UPDATE students SET age = 21 WHERE id = 1;\n"
                 "DELETE FROM students WHERE id = 1;\n\n"
                 "Golden rule: UPDATE/DELETE without WHERE hits EVERY row. Run "
                 "the matching SELECT first to preview affected rows.\n\n"
                 "Transactions wrap multi-step writes: COMMIT to save, "
                 "ROLLBACK to undo — all or nothing.\n\n"
                 "Takeaway: in real systems, writes are wrapped in "
                 "transactions and previews; treat them with respect."),
                ("Relationships and JOIN Basics", "Tables talking to tables",
                 "Relationships connect tables: enrollments reference a student "
                 "id and a course id instead of copying names around.\n\n"
                 "JOIN stitches them back for display:\n\n"
                 "SELECT students.name, courses.title\n"
                 "FROM enrollments\n"
                 "JOIN students ON students.id = enrollments.student_id\n"
                 "JOIN courses ON courses.id = enrollments.course_id;\n\n"
                 "This very platform uses exactly this pattern for My Learning.\n\n"
                 "Common mistake: forgetting the ON clause — you get a giant "
                 "cross-product of every combination.\n\n"
                 "Takeaway: normalise data into tables, then JOIN for answers."),
            ]),
        ],
    },
    {
        "title": "Web Development with Flask",
        "category": "Web Development", "level": "intermediate",
        "description": (
            "Build dynamic websites in pure Python. Requests and responses, "
            "routes, Jinja templates, forms, SQLite persistence and a complete "
            "CRUD pattern — the same architecture this platform uses."
        ),
        "modules": [
            ("How the Web Talks", "Requests, responses and your first server.", [
                ("Requests and Responses", "Everything is an HTTP conversation",
                 "Browsers send HTTP REQUESTS (method + path + headers); "
                 "servers reply with RESPONSES (status + body).\n\n"
                 "GET /courses → 200 OK, HTML body\n"
                 "POST /enroll → 302 redirect\n\n"
                 "Methods map to intent: GET reads, POST changes state. Forms "
                 "submit POST; links navigate GET.\n\n"
                 "Status families: 200 ok, 302 redirect, 404 missing, 403 "
                 "forbidden, 500 server error.\n\n"
                 "Takeaway: when a web bug appears, ask 'what request was sent "
                 "and what came back?' before touching code."),
                ("Your First Flask App", "Six lines to a website",
                 "Flask maps URLs to ordinary Python functions.\n\n"
                 "from flask import Flask\n"
                 "app = Flask(__name__)\n\n"
                 "@app.route(\"/\")\n"
                 "def home():\n"
                 "    return \"Hello, web!\"\n\n"
                 "app.run(debug=True)\n\n"
                 "The decorator REGISTERS home() for path '/'. Return value "
                 "becomes the response body.\n\n"
                 "debug=True auto-reloads on save — development only, never in "
                 "production.\n\n"
                 "Takeaway: a Flask app is just a routing table plus functions "
                 "that return text (usually HTML)."),
            ]),
            ("Pages and Data", "Dynamic HTML and clean URL design.", [
                ("Routes and View Functions", "Dynamic segments and redirects",
                 "Routes can capture URL parts as arguments:\n\n"
                 "@app.route(\"/courses/<int:course_id>\")\n"
                 "def show(course_id):\n"
                 "    ...lookup...\n"
                 "    if not found:\n"
                 "        abort(404)\n"
                 "    return render_template(...)\n\n"
                 "<int:> converts and validates the segment for free.\n\n"
                 "url_for('show', course_id=3) builds links from function "
                 "names, so renaming routes never breaks templates.\n\n"
                 "After a successful POST, redirect (Post/Redirect/Get) so "
                 "refresh does not resubmit forms.\n\n"
                 "Takeaway: views stay thin — validate, delegate to helpers, "
                 "render."),
                ("Templates with Jinja", "HTML with escape hatches",
                 "Templates keep HTML in .html files; Python data flows in as "
                 "variables.\n\n"
                 "<h1>{{ course.title }}</h1>\n"
                 "{% for lesson in lessons %}\n"
                 "  <li>{{ lesson.title }}</li>\n"
                 "{% endfor %}\n\n"
                 "{{ }} prints values (auto-escaped against XSS); {% %} holds "
                 "logic like for/if blocks.\n\n"
                 "Template inheritance: base.html holds header/footer once; "
                 "child pages fill named blocks — exactly how this site stays "
                 "consistent.\n\n"
                 "Takeaway: logic belongs in views, presentation decisions in "
                 "templates, and never trust raw |safe without reason."),
            ]),
            ("Forms and Data", "Closing the loop: input to storage.", [
                ("Handling Forms", "Reading POST data safely",
                 "A form posts fields to a route; Flask exposes them via "
                 "request.form.\n\n"
                 "@app.route(\"/register\", methods=[\"GET\", \"POST\"])\n"
                 "def register():\n"
                 "    if request.method == \"POST\":\n"
                 "        email = request.form.get(\"email\", \"\").strip()\n"
                 "        ...validate...\n\n"
                 "Validate EVERYTHING: presence, length, format, uniqueness. "
                 "Flash friendly errors and re-render.\n\n"
                 "CSRF and password storage matter too — passwords go through "
                 "hashing libraries, never plain text.\n\n"
                 "Takeaway: client-side hints are UX; server-side validation "
                 "is the actual security boundary."),
                ("SQLite + CRUD Pattern", "A complete mini feature",
                 "CRUD = Create Read Update Delete, the heartbeat of most web "
                 "apps. Flask pairs with SQLite through parameterised SQL:\n\n"
                 "db.execute(\n"
                 "    \"INSERT INTO notes (text) VALUES (?)\", (text,))\n"
                 "row = db.execute(\n"
                 "    \"SELECT * FROM notes WHERE id = ?\", (note_id,)\n"
                 ").fetchone()\n\n"
                 "Question marks are NON-NEGOTIABLE: they prevent SQL "
                 "injection. Never format strings into SQL.\n\n"
                 "Pattern used across this platform: route validates access → "
                 "helper mutates DB → commit → redirect with a flash message.\n\n"
                 "Takeaway: master one honest CRUD loop and you can read most "
                 "web codebases."),
            ]),
        ],
    },
]


def seed_other_courses(db):
    instructor_id = _scalar(db, "SELECT id FROM users WHERE email = ?",
                            (DEMO_INSTRUCTOR,))
    if not instructor_id:
        return []
    created = []
    for spec in OTHER_COURSES:
        course = _one(db, "SELECT id FROM courses WHERE title = ? AND instructor_id = ?",
                      (spec["title"], instructor_id))
        if course:
            cid = course["id"]
        else:
            cur = db.execute(
                "INSERT INTO courses (instructor_id, title, description, category, "
                "level, status) VALUES (?, ?, ?, ?, ?, 'published')",
                (instructor_id, spec["title"], spec["description"],
                 spec["category"], spec["level"]))
            cid = cur.lastrowid
            created.append(spec["title"])

        for m_pos, (m_title, m_desc, lessons) in enumerate(spec["modules"], 1):
            mid = _get_or_create_module(db, cid, m_title, m_desc, m_pos)
            for l_pos, (l_title, l_desc, body) in enumerate(lessons, 1):
                if _one(db, "SELECT id FROM lessons WHERE module_id = ? AND title = ?",
                        (mid, l_title)):
                    continue
                db.execute(
                    "INSERT INTO lessons (module_id, title, description, "
                    "lesson_type, content_path, content_text, duration, position) "
                    "VALUES (?, ?, ?, 'text', NULL, ?, 420, ?)",
                    (mid, l_title, l_desc, body, l_pos))

        # One quick check on the FIRST lesson of the middle module of each
        # course keeps discovery meaningful without bloating seeding.
        quiz_specs = {
            "Data Structures Essentials": (
                "Complexity Quick Check", 60.0, "complexity",
                [("What is the worst-case time of LINEAR search on n items?",
                  "O(1)", "O(log n)", "O(n)", "O(n^2)", "C",
                  "Every item may be checked once."),
                 ("What is the cost of accessing a list element BY INDEX?",
                  "O(1)", "O(log n)", "O(n)", "O(n log n)", "A",
                  "Arrays compute the address directly."),
                 ("A stack removes items in which order?",
                  "FIFO", "LIFO", "Sorted order", "Random", "B",
                  "Last pushed is popped first.")],
            ),
            "SQL & Database Foundations": (
                "SELECT Fundamentals Quick Check", 60.0, "sql-basics",
                [("Which statement fetches EVERY column from students?",
                  "SELECT * FROM students;", "GET students;",
                  "SHOW students;", "FETCH ALL students;", "A",
                  "* expands to all columns."),
                 ("Which clause FILTERS which rows are returned?",
                  "GROUP BY", "ORDER BY", "WHERE", "LIMIT", "C",
                  "WHERE applies a row-level condition."),
                 ("What does a PRIMARY KEY guarantee?",
                  "Sorted rows", "Unique row identification",
                  "Encrypted values", "Faster inserts only", "B",
                  "Uniqueness is its entire purpose.")],
            ),
            "Web Development with Flask": (
                "Routing Quick Check", 60.0, "flask-basics",
                [("Which decorator connects a URL to a view function?",
                  "@app.link", "@app.route", "@url.map", "@bind", "B",
                  "@app.route registers the path."),
                 ("By default, @app.route responds to which HTTP method(s)?",
                  "POST only", "GET only", "PUT and DELETE", "All methods", "B",
                  "GET is the default; others must be listed."),
                 ("What does url_for('show', course_id=3) do?",
                  "Fetches course 3", "Builds the URL for view 'show'",
                  "Renders JSON", "Redirects immediately", "B",
                  "url_for builds links from endpoint names.")],
            ),
        }
        qspec = quiz_specs.get(spec["title"])
        if qspec:
            qtitle, passing, tag, questions = qspec
            # attach to the FIRST lesson overall for predictable discovery
            anchor = _one(db,
                "SELECT l.id FROM lessons l JOIN modules m ON l.module_id = m.id "
                "WHERE m.course_id = ? ORDER BY m.position ASC, l.position ASC "
                "LIMIT 1", (cid,))
            if anchor and not _one(db, "SELECT id FROM quizzes WHERE lesson_id = ?",
                                   (anchor["id"],)):
                cur = db.execute(
                    "INSERT INTO quizzes (lesson_id, title, description, "
                    "passing_percentage) VALUES (?, ?, ?, ?)",
                    (anchor["id"], qtitle, "Short self-check.", passing))
                qid = cur.lastrowid
                for pos, (t, a, b, c, d, corr, expl) in enumerate(questions, 1):
                    db.execute(
                        "INSERT INTO quiz_questions (quiz_id, question_text, "
                        "question_type, option_a, option_b, option_c, option_d, "
                        "correct_answer, explanation, concept_tag, position) "
                        "VALUES (?, ?, 'multiple_choice', ?, ?, ?, ?, ?, ?, ?, ?)",
                        (qid, t, a, b, c, d, corr, expl, tag, pos))
    db.commit()
    return created


# ---------------------------------------------------------------------------
# 5) Local downloadable resources (generated, not downloaded)
# ---------------------------------------------------------------------------

RESOURCES_DIRNAME = "resources"


RESOURCE_FILES = {
    "python_quick_reference.txt": (
        "PYTHON QUICK REFERENCE — Python Fundamentals\n"
        "=============================================\n\n"
        "VARIABLES\n"
        "  name = \"Riya\"        # str\n"
        "  age = 20              # int\n"
        "  height = 1.62         # float\n"
        "  is_ready = True       # bool\n\n"
        "OUTPUT / INPUT\n"
        "  print(f\"Hi {name}\")\n"
        "  age_text = input(\"age: \")   # always str!\n"
        "  age = int(age_text)\n\n"
        "CONDITIONALS\n"
        "  if score >= 50:\n"
        "      print(\"Pass\")\n"
        "  elif score >= 40:\n"
        "      print(\"Retake\")\n"
        "  else:\n"
        "      print(\"Fail\")\n\n"
        "LOOPS\n"
        "  for i in range(1, 6):   # 1..5\n"
        "      print(i)\n"
        "  while count > 0:\n"
        "      count -= 1\n\n"
        "LISTS / DICTS / TUPLES\n"
        "  colors = [\"red\", \"blue\"]\n"
        "  colors.append(\"green\")\n"
        "  point = (3, 7)                 # tuple: fixed\n"
        "  student = {\"name\": \"Riya\"}\n"
        "  student[\"age\"] = 20\n\n"
        "FUNCTIONS\n"
        "  def power(base, exponent=2):\n"
        "      return base ** exponent\n\n"
        "USEFUL BUILTINS\n"
        "  len(x)  sum(xs)  max(xs)  min(xs)\n"
        "  sorted(xs)  range(a, b)  type(x)\n"),
    "variables_examples.py": (
        "# variables_examples.py — runnable companion to the\n"
        "# 'Variables and Data Types' lesson.\n\n"
        "name = \"Riya\"          # str\n"
        "age = 20                # int\n"
        "height = 1.62           # float\n"
        "is_enrolled = True      # bool\n\n"
        "print(type(name), type(age), type(height), type(is_enrolled))\n\n"
        "# Variables can be reassigned to a new value\n"
        "streak = 3\n"
        "streak = streak + 1\n"
        "print(\"streak:\", streak)\n\n"
        "# Naming rules demo\n"
        "user_name_2 = \"valid\"    # letters/digits/_ , no leading digit\n"
        "print(user_name_2)\n\n"
        "# Common mistake (uncomment to see the error):\n"
        "# 2nd_place = \"oops\"    # SyntaxError: invalid decimal literal\n"),
    "loops_practice.py": (
        "# loops_practice.py — try these in the playground!\n\n"
        "# 1. Count 1..5\n"
        "for i in range(1, 6):\n"
        "    print(i)\n\n"
        "# 2. Sum a list\n"
        "total = 0\n"
        "for n in [2, 4, 6, 8]:\n"
        "    total += n\n"
        "print(\"total:\", total)\n\n"
        "# 3. Times table corner\n"
        "for i in range(1, 4):\n"
        "    for j in range(1, 4):\n"
        "        print(i * j, end=\" \")\n"
        "    print()\n\n"
        "# 4. While countdown\n"
        "count = 3\n"
        "while count > 0:\n"
        "    print(count)\n"
        "    count -= 1\n"
        "print(\"Go!\")\n"),
    "collections_cheatsheet.txt": (
        "COLLECTIONS CHEATSHEET\n"
        "=====================\n\n"
        "LIST (ordered, mutable)\n"
        "  nums = [3, 1, 2]\n"
        "  nums[0]        # 3\n"
        "  nums[-1]       # last\n"
        "  nums.append(9)\n"
        "  nums.sort()\n"
        "  len(nums)\n\n"
        "TUPLE (ordered, FIXED)\n"
        "  point = (3, 7)\n"
        "  x, y = point   # unpacking\n\n"
        "DICTIONARY (key -> value)\n"
        "  student = {\"name\": \"Riya\", \"age\": 20}\n"
        "  student[\"age\"]\n"
        "  student[\"grade\"] = \"A\"\n"
        "  for k, v in student.items(): print(k, v)\n\n"
        "STRING TOOLS\n"
        "  \"hi\".upper()  \"a,b\".split(\",\")  \"-\".join([\"a\",\"b\"])\n\n"
        "WHEN TO USE WHICH\n"
        "  changing sequence ......... list\n"
        "  fixed shape ............... tuple\n"
        "  lookup by label ........... dict\n"),
    "functions_practice.py": (
        "# functions_practice.py — finish each function, then run.\n\n"
        "def square(n):\n"
        "    return n * n\n\n"
        "def greet(name, greeting=\"Hello\"):\n"
        "    return f\"{greeting}, {name}? No wait — {greeting}, {name}.\"\n\n"
        "def min_max(nums):\n"
        "    return min(nums), max(nums)\n\n"
        "def count_passing(marks, threshold=50):\n"
        "    passed = 0\n"
        "    for m in marks:\n"
        "        if m >= threshold:\n"
        "            passed += 1\n"
        "    return passed\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    print(square(6))\n"
        "    print(greet(\"Riya\"))\n"
        "    low, high = min_max([4, 8, 15, 16])\n"
        "    print(low, high)\n"
        "    print(count_passing([45, 82, 91]))\n"),
    "sql_select_basics.txt": (
        "SQL SELECT BASICS — SQL & Database Foundations\n"
        "==============================================\n\n"
        "READ A QUERY OUT LOUD:\n"
        "  SELECT name, age      -- which columns\n"
        "  FROM students          -- which table\n"
        "  WHERE age >= 18        -- keep which rows\n"
        "  ORDER BY age DESC      -- sort\n"
        "  LIMIT 5;               -- cap results\n\n"
        "OPERATORS\n"
        "  =  <>  >  <  >=  <=\n"
        "  BETWEEN 10 AND 20\n"
        "  IN ('Surat', 'Baroda')\n"
        "  LIKE 'ra%'      -- starts with Ra\n"
        "  IS NULL / IS NOT NULL\n\n"
        "AGGREGATES\n"
        "  SELECT COUNT(*) FROM students;\n"
        "  SELECT city, COUNT(*) FROM students GROUP BY city;\n\n"
        "SAFETY RULE\n"
        "  Always write the WHERE before running UPDATE/DELETE.\n"),
    "flask_project_structure.txt": (
        "FLASK PROJECT STRUCTURE NOTES — Web Development with Flask\n"
        "==========================================================\n\n"
        "learning_platform/\n"
        "  app.py               # routes (thin controllers)\n"
        "  database/db.py       # schema + connection helpers\n"
        "  utils/               # business logic helpers\n"
        "  templates/           # Jinja HTML\n"
        "    base.html          # shared layout (blocks)\n"
        "  static/css|js        # stylesheets & scripts\n"
        "  uploads/             # user files (never in git!)\n\n"
        "REQUEST FLOW\n"
        "  Browser -> route decorator -> view function -> helpers/SQL\n"
        "          -> render_template -> response\n\n"
        "GOLDEN RULES\n"
        "  1. Parameterised SQL always (? placeholders).\n"
        "  2. Validate on the SERVER, decorate for the client.\n"
        "  3. Redirect after successful POST.\n"
        "  4. Keep secrets out of code (environment variables).\n"),
}


def seed_resources(db, uploads_root):
    """Write useful local files once, then attach rows keyed by (lesson,title)."""
    res_dir = os.path.join(uploads_root, RESOURCES_DIRNAME)
    os.makedirs(res_dir, exist_ok=True)
    for fname, content in RESOURCE_FILES.items():
        path = os.path.join(res_dir, fname)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)

    attachments = [
        # (lesson title, resource title, filename)
        ("Welcome to Python", "Python Quick Reference (TXT)",
         "python_quick_reference.txt"),
        ("Variables and Data Types", "variables_examples.py",
         "variables_examples.py"),
        ("For Loops", "loops_practice.py", "loops_practice.py"),
        ("Lists", "Collections Cheatsheet (TXT)", "collections_cheatsheet.txt"),
        ("Functions", "functions_practice.py", "functions_practice.py"),
        ("SELECT Fundamentals", "SQL SELECT Basics (TXT)",
         "sql_select_basics.txt"),
        ("Routes and View Functions", "Flask Project Structure (TXT)",
         "flask_project_structure.txt"),
    ]
    for lesson_title, r_title, fname in attachments:
        lesson = _one(db,
            "SELECT l.id FROM lessons l WHERE l.title = ? ORDER BY l.id LIMIT 1",
            (lesson_title,))
        if not lesson:
            continue
        if _one(db, "SELECT id FROM lesson_resources WHERE lesson_id = ? AND title = ?",
                (lesson["id"], r_title)):
            continue
        db.execute(
            "INSERT INTO lesson_resources (lesson_id, title, file_path, file_type) "
            "VALUES (?, ?, ?, ?)",
            (lesson["id"], r_title, f"{RESOURCES_DIRNAME}/{fname}",
             fname.rsplit(".", 1)[-1]))
    db.commit()


# ---------------------------------------------------------------------------
# 6) Demo learning history
# ---------------------------------------------------------------------------

def reset_demo_student_artifacts(db):
    """
    Clear PRIOR-STAGE synthetic activity belonging ONLY to the main demo
    student so the new realistic history starts from a clean slate.
    Real user rows are never touched.
    """
    sid = _scalar(db, "SELECT id FROM users WHERE email = ?", (DEMO_STUDENT,))
    if not sid:
        return
    db.execute(
        "DELETE FROM quiz_answers WHERE attempt_id IN "
        "(SELECT id FROM quiz_attempts WHERE student_id = ?)", (sid,))
    db.execute("DELETE FROM quiz_attempts WHERE student_id = ?", (sid,))
    db.execute("DELETE FROM achievement_unlocks WHERE student_id = ?", (sid,))
    db.execute("DELETE FROM xp_events WHERE student_id = ?", (sid,))
    db.execute("DELETE FROM revision_activity WHERE student_id = ?", (sid,))
    db.execute("DELETE FROM video_notes WHERE student_id = ?", (sid,))
    db.execute("DELETE FROM video_markers WHERE student_id = ?", (sid,))
    db.execute("DELETE FROM lesson_progress WHERE student_id = ?", (sid,))
    db.commit()


def demo_history_exists(db):
    """
    One-time gate: the pre-Prompt-2 database had ZERO notes/markers for the
    demo student, while the seeded history always creates both. Once the
    new history exists we NEVER purge or rewrite it again — a restart must
    not clobber anything the demo account did after seeding.
    """
    sid = _scalar(db, "SELECT id FROM users WHERE email = ?", (DEMO_STUDENT,))
    if not sid:
        return False
    notes = _scalar(db, "SELECT COUNT(*) FROM video_notes WHERE student_id=?",
                    (sid,))
    markers = _scalar(db, "SELECT COUNT(*) FROM video_markers WHERE student_id=?",
                      (sid,))
    return bool(notes or markers)


class DemoHistory:
    """Bundle of ids the activity seeders need."""

    def __init__(self, db):
        self.db = db
        self.course_id = _scalar(db,
            "SELECT id FROM courses WHERE title = ?", (FLAGSHIP_TITLE,))
        self.instructor_id = _scalar(db,
            "SELECT id FROM users WHERE email = ?", (DEMO_INSTRUCTOR,))
        self.demo_sid = _scalar(db,
            "SELECT id FROM users WHERE email = ?", (DEMO_STUDENT,))
        # Frozen identity of the MAIN demo student. Callers must NEVER
        # mutate self.demo_sid to reuse this object for other accounts
        # (that bug silently disabled side-student dedup + XP guards).
        # Side students are always passed via author_sid= instead.
        self.main_demo_sid = self.demo_sid
        self.lesson_ids = {}
        self.quiz_ids = {}
        self.question_ids = {}   # (quiz title, position) -> id
        if not self.course_id:
            return
        for row in db.execute(
                "SELECT l.id, l.title FROM lessons l "
                "JOIN modules m ON l.module_id = m.id WHERE m.course_id = ?",
                (self.course_id,)):
            self.lesson_ids[row["title"]] = row["id"]
        for row in db.execute(
                "SELECT qz.id, qz.title, l.title AS lesson_title FROM quizzes qz "
                "JOIN lessons l ON qz.lesson_id = l.id "
                "JOIN modules m ON l.module_id = m.id WHERE m.course_id = ?",
                (self.course_id,)):
            self.quiz_ids[row["lesson_title"]] = row["id"]
        for row in db.execute(
                "SELECT qq.id, qq.position, l.title AS lesson_title FROM "
                "quiz_questions qq JOIN quizzes qz ON qq.quiz_id = qz.id "
                "JOIN lessons l ON qz.lesson_id = l.id "
                "JOIN modules m ON l.module_id = m.id WHERE m.course_id = ?",
                (self.course_id,)):
            self.question_ids[(row["lesson_title"], row["position"])] = row["id"]

    def lesson(self, title):
        return self.lesson_ids.get(title)

    def ensure_progress(self, lesson_title, *, completed=False, pct=None,
                        pos=None, watched=None, days_ago, hour=18):
        db = self.db
        lid = self.lesson(lesson_title)
        sid = self.demo_sid
        if not lid or not sid:
            return
        pct = pct if pct is not None else (100 if completed else 0)
        row = _one(db, "SELECT * FROM lesson_progress "
                       "WHERE student_id = ? AND lesson_id = ?", (sid, lid))
        started = _ts(days_ago, hour - 1 if hour else hour)
        last_act = _ts(days_ago, hour)
        completed_at = _ts(days_ago, hour + 0) if completed else None
        if row:
            db.execute(
                "UPDATE lesson_progress SET last_position_seconds = ?, "
                "watched_seconds = ?, completion_percentage = ?, "
                "is_completed = ?, last_activity_at = ?, completed_at = COALESCE(?, completed_at) "
                "WHERE id = ?",
                (pos or 0, watched or pos or 0, pct, 1 if completed else 0,
                 last_act, completed_at, row["id"]))
        else:
            db.execute(
                "INSERT INTO lesson_progress (student_id, lesson_id, "
                "last_position_seconds, watched_seconds, completion_percentage, "
                "is_completed, started_at, last_activity_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, lid, pos or 0, watched or pos or 0, pct,
                 1 if completed else 0, started, last_act, completed_at))
            # Platform rule: +20 XP is a COMPLETION reward. In-progress
            # lessons must never mint lesson_completed events.
            if completed:
                self._xp("lesson_completed", "lesson", lid, days_ago, hour)

    def _xp(self, etype, rt, rid, days_ago, hour=18):
        _award_xp_at(self.db, self.demo_sid, etype, rt, rid, _ts(days_ago, hour))

    def add_note(self, lesson_title, seconds, text, days_ago, hour=19):
        lid = self.lesson(lesson_title)
        if not lid:
            return None
        if _one(self.db,
                "SELECT id FROM video_notes WHERE student_id=? AND lesson_id=? "
                "AND timestamp_seconds=?", (self.demo_sid, lid, seconds)):
            return None
        cur = self.db.execute(
            "INSERT INTO video_notes (student_id, lesson_id, timestamp_seconds, "
            "note_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self.demo_sid, lid, seconds, text, _ts(days_ago, hour),
             _ts(days_ago, hour)))
        return cur.lastrowid

    def add_marker(self, lesson_title, seconds, m_type, text, days_ago,
                   hour=20, author_sid=None):
        lid = self.lesson(lesson_title)
        sid = author_sid or self.demo_sid
        if not lid:
            return None
        if _one(self.db,
                "SELECT id FROM video_markers WHERE student_id=? AND lesson_id=? "
                "AND timestamp_seconds=? AND marker_type=?",
                (sid, lid, seconds, m_type)):
            return None
        cur = self.db.execute(
            "INSERT INTO video_markers (student_id, lesson_id, timestamp_seconds, "
            "marker_type, optional_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, lid, seconds, m_type, text, _ts(days_ago, hour)))
        return cur.lastrowid

    def submit_attempt(self, lesson_title, answers_by_pos, days_ago, hour=17,
                       author_sid=None):
        """
        Insert a graded attempt mirroring the app's own formula:
        percentage = round(correct/total*100, 1); passed = pct >= quiz.passing.
        answers_by_pos: {position: 'A'..'D'} — missing positions count wrong.
        Idempotency: any student OTHER than the main demo student gets a
        same-result lookup first (quiz+student+score+percentage), so restarts
        never duplicate side-student attempts. The demo student's history is
        rebuilt from scratch by the one-time reset gate instead. XP is only
        ever awarded to the main demo student (side students keep a small
        footprint). Returns attempt id.
        """
        db = self.db
        target_sid = author_sid or self.demo_sid
        is_main = target_sid == self.main_demo_sid
        qid = self.quiz_ids.get(lesson_title)
        if not qid or not target_sid:
            return None

        questions = db.execute(
            "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY position, id",
            (qid,)).fetchall()
        total = len(questions)
        correct_count = sum(
            1 for q in questions
            if answers_by_pos.get(q["position"], "").upper() == q["correct_answer"])
        percentage = round(correct_count / total * 100, 1) if total else 0

        if not is_main:
            prior = _one(db,
                "SELECT id FROM quiz_attempts WHERE quiz_id=? AND student_id=? "
                "AND score=? AND percentage=? ORDER BY id LIMIT 1",
                (qid, target_sid, correct_count, percentage))
            if prior:
                return prior["id"]

        passing = _scalar(db, "SELECT passing_percentage FROM quizzes WHERE id=?",
                          (qid,)) or 50.0
        passed = 1 if percentage >= passing else 0

        prev_best = already_passed = None
        if is_main:
            prev_best = _scalar(db,
                "SELECT MAX(percentage) FROM quiz_attempts WHERE quiz_id=? "
                "AND student_id=?", (qid, target_sid))
            already_passed = _scalar(db,
                "SELECT COUNT(*) FROM quiz_attempts WHERE quiz_id=? AND student_id=? "
                "AND passed=1", (qid, target_sid)) > 0

        when = _ts(days_ago, hour)
        cur = db.execute(
            "INSERT INTO quiz_attempts (quiz_id, student_id, score, total_questions, "
            "percentage, passed, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (qid, target_sid, correct_count, total, percentage, passed, when, when))
        attempt_id = cur.lastrowid
        for q in questions:
            selected = answers_by_pos.get(q["position"], "")
            is_correct = 1 if selected.upper() == q["correct_answer"] else 0
            db.execute(
                "INSERT INTO quiz_answers (attempt_id, question_id, selected_answer, "
                "is_correct) VALUES (?, ?, ?, ?)",
                (attempt_id, q["id"], selected, is_correct))

        if is_main:
            if passed and not already_passed:
                self._xp("quiz_passed", "quiz", qid, days_ago, hour)
            if prev_best is not None and percentage > prev_best:
                self._xp("quiz_improved", "attempt", attempt_id, days_ago, hour)
        return attempt_id


def seed_demo_activity(db):
    """Realistic mixed-strength history for the main demo student."""
    h = DemoHistory(db)
    if not h.course_id or not h.demo_sid:
        return {}

    # --- Progress narrative: strong start, wobble in the middle ----------
    h.ensure_progress("Welcome to Python", completed=True, days_ago=8, hour=16)
    h.ensure_progress("Input and Output", completed=True, days_ago=7, hour=17)
    h.ensure_progress("Variables and Data Types", completed=False, pct=42,
                      pos=150, watched=160, days_ago=0, hour=9)   # continue card
    h.ensure_progress("Conditional Statements", completed=True, days_ago=6, hour=18)
    h.ensure_progress("For Loops", completed=True, days_ago=5, hour=19)
    h.ensure_progress("While Loops", completed=False, pct=25, pos=75,
                      watched=75, days_ago=5, hour=20)
    h.ensure_progress("Lists", completed=True, days_ago=2, hour=18)
    h.ensure_progress("Functions", completed=False, pct=30, days_ago=1, hour=17)

    # --- Timestamped notes (aligned with the demo transcript) ------------
    v = "Variables and Data Types"
    h.add_note(v, 55,  "Variables are names that reference values.", 6)
    h.add_note(v, 130, "int = whole number, float = decimal number.", 4)
    h.add_note(v, 255, "Names can't start with a digit — case sensitive too.", 3)
    h.add_note(v, 95,  "Quotes: single or double both fine for strings.", 4, 19)
    h.add_note(v, 175, "True/False are capitalised in Python.", 2)
    h.add_note(v, 345, "Rewatch recap before the Functions quiz.", 0)

    # --- Markers ----------------------------------------------------------
    conf_v = h.add_marker(v, 215, "confused",
                          "type() output looks confusing — what is class 'float'?",
                          5)
    h.add_marker(v, 130, "important", "", 4)
    imp_naming = h.add_marker(v, 255, "important", "", 3)
    h.add_marker("While Loops", 150, "confused",
                 "When does the condition actually get re-checked?", 5)
    h.add_marker("While Loops", 240, "important", "", 4)

    # --- Quiz attempts (mixed performance, dated) -------------------------
    cond_att = h.submit_attempt("Conditional Statements",
                                {1: "A", 2: "B", 3: "B"}, 6, 18)
    loops_att1 = h.submit_attempt("For Loops", {1: "B", 2: "A", 3: "D", 4: "C"},
                                  5, 19)                      # 2/4 fail
    vars_att1 = h.submit_attempt(v, {1: "B", 2: "C", 3: "C", 4: "B", 5: "B"},
                                 4, 18)                       # 3/5 pass
    loops_att2 = h.submit_attempt("For Loops", {1: "B", 2: "B", 3: "D", 4: "C"},
                                  4, 19)                      # 3/4 pass+
    vars_att2 = h.submit_attempt(v, {1: "B", 2: "C", 3: "C", 4: "A", 5: "C"},
                                 3, 17)                       # 5/5 pass+
    lists_att = h.submit_attempt("Lists", {1: "B", 2: "C", 3: "A", 4: "A"},
                                 2, 18)                       # 3/4 pass
    func_att = h.submit_attempt("Functions", {1: "B", 2: "A", 3: "A", 4: "A"},
                                1, 17)                        # 1/4 fail

    # --- Smart Revision review history -----------------------------------
    # (source_type, source_id, lesson_title_or_None, concept_tag_or_None,
    #  xp_ref_type, xp_ref_id, days_ago, hour)
    # concept_tag is ALWAYS a canonical lowercase tag; the lesson is looked
    # up separately by title so both fields stay independently correct.
    reviews = [
        ("weak_concept", None, None, "for-loops", "concept:for-loops", 0, 2, 18),
        ("confusion_marker", conf_v, v, None, "marker", conf_v, 2, 19),
        ("wrong_question", func_att, "Functions", "functions",
         "attempt_question", func_att, 1, 18),
        ("important_marker", imp_naming, v, None, "marker", imp_naming, 1, 19),
    ]
    for src_type, src_id, lesson_t, tag, xp_rt, xp_rid, d_ago, hr in reviews:
        if src_id is None:
            exists = _one(db, "SELECT id FROM revision_activity WHERE "
                              "student_id=? AND source_type=?",
                          (h.demo_sid, src_type))
        else:
            exists = _one(db, "SELECT id FROM revision_activity WHERE "
                              "student_id=? AND source_type=? AND source_id=?",
                          (h.demo_sid, src_type, src_id))
        if exists:
            continue
        db.execute(
            "INSERT INTO revision_activity (student_id, course_id, lesson_id, "
            "concept_tag, source_type, source_id, reviewed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (h.demo_sid, h.course_id,
             h.lesson(lesson_t) if lesson_t else None,
             tag, src_type, src_id, _ts(d_ago, hr)))
        _award_xp_at(db, h.demo_sid, "revision_reviewed", xp_rt, xp_rid or 0,
                     _ts(d_ago, hr))
    db.commit()

    evaluate_achievements(db, h.demo_sid)
    return {"demo_student_id": h.demo_sid, "vars_attempt2": vars_att2}


def seed_supporting_students(db):
    """Small footprints for alex/maya so hotspots aggregate distinct students."""
    course_id = _scalar(db, "SELECT id FROM courses WHERE title = ?",
                        (FLAGSHIP_TITLE,))
    if not course_id:
        return
    plans = [
        {
            "email": "alex@demo.com",
            "welcome_days": 4,
            "cond_answers": {1: "A", 2: "B", 3: "A"},     # 2/3 pass
            "cond_days": 3,
            "markers": [
                ("Variables and Data Types", 213, "confused", "", 3),
                ("Variables and Data Types", 300, "important", "", 3, 21),
                ("While Loops", 148, "confused", "", 3),
            ],
        },
        {
            "email": "maya@demo.com",
            "welcome_days": 2,
            "cond_answers": {1: "A", 2: "C", 3: "A"},     # 1/3 fail
            "cond_days": 2,
            "markers": [
                ("Variables and Data Types", 217, "confused", "", 2),
                ("Variables and Data Types", 303, "confused", "", 2, 21),
                ("While Loops", 152, "confused", "", 2),
            ],
        },
    ]
    for plan in plans:
        sid = _scalar(db, "SELECT id FROM users WHERE email = ?", (plan["email"],))
        if not sid:
            continue
        if not _one(db, "SELECT id FROM enrollments WHERE student_id=? AND course_id=?",
                    (sid, course_id)):
            db.execute(
                "INSERT INTO enrollments (student_id, course_id, enrolled_at) "
                "VALUES (?, ?, ?)", (sid, course_id, _ts(plan["welcome_days"], 10)))

        welcome = _scalar(db,
            "SELECT l.id FROM lessons l JOIN modules m ON l.module_id=m.id "
            "WHERE m.course_id=? AND l.title='Welcome to Python'", (course_id,))
        if welcome and not _one(db, "SELECT id FROM lesson_progress WHERE "
                                    "student_id=? AND lesson_id=?", (sid, welcome)):
            db.execute(
                "INSERT INTO lesson_progress (student_id, lesson_id, "
                "completion_percentage, is_completed, started_at, "
                "last_activity_at, completed_at) VALUES (?, ?, 100, 1, ?, ?, ?)",
                (sid, welcome, _ts(plan["welcome_days"], 11),
                 _ts(plan["welcome_days"], 12), _ts(plan["welcome_days"], 12)))

        h = DemoHistory(db)
        if plan["cond_answers"]:
            h.submit_attempt("Conditional Statements", plan["cond_answers"],
                             plan["cond_days"], 14, author_sid=sid)

        for mk in plan["markers"]:
            title, sec, mtype, text = mk[0], mk[1], mk[2], mk[3]
            days = mk[4]
            h.add_marker(title, sec, mtype, text, days, author_sid=sid)
    db.commit()
    evaluate_achievements_all_demo(db)


def evaluate_achievements_all_demo(db):
    for email in ALL_DEMO_EMAILS:
        sid = _scalar(db, "SELECT id FROM users WHERE email = ?", (email,))
        if sid:
            evaluate_achievements(db, sid)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def seed_all(db, uploads_root):
    """Run every seeder in dependency order. Fully idempotent."""
    summary = {}
    summary["accounts_created"] = ensure_demo_accounts(db)
    # Never delete content during ordinary seed/restart operations.  A file
    # can be deliberately uploaded before it is attached to a lesson, so an
    # automatic "orphan" cleanup is not safe for real student/instructor data.
    summary["garbage_removed"] = []
    summary["flagship_course_id"] = seed_python_fundamentals(db, uploads_root)
    summary["other_courses_created"] = seed_other_courses(db)
    seed_resources(db, uploads_root)
    if demo_history_exists(db):
        # History already seeded (or the demo account has real activity
        # of its own) — never purge, never rewrite.
        summary["demo_history"] = "already-present"
    else:
        reset_demo_student_artifacts(db)
        seed_demo_activity(db)
        summary["demo_history"] = "seeded-now"
    seed_supporting_students(db)
    evaluate_achievements_all_demo(db)
    return summary


def run_startup_seeds():
    """Called by app.py inside app.app_context() after init_db()."""
    from flask import current_app
    from database.db import get_db
    db = get_db()
    seed_all(db, current_app.config["UPLOAD_FOLDER"])


if __name__ == "__main__":
    # Standalone usage: python -m database.seed_demo
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conn = sqlite3.connect(os.path.join(root, "database", "learning_platform.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    result = seed_all(conn, os.path.join(root, "uploads"))
    conn.close()
    print("Seeding complete:", result)
