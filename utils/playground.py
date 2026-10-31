"""
Local educational Python sandbox (Stage 9).

SECURITY MODEL — READ THIS BEFORE DEPLOYING ANYWHERE PUBLIC
-----------------------------------------------------------
This is a deliberately LIMITED code runner for a local college demo.
It is NOT production-grade remote-code-execution infrastructure.

What it does:
  * writes the student's code to an isolated temporary directory,
  * runs it with `python -I -B` (isolated mode: ignores PYTHONPATH and
    user site-packages; -B skips .pyc writing) as a SUBPROCESS,
  * never uses shell=True and never interpolates user input into a
    command line -- the code travels via a FILE, not argv,
  * feeds an empty stdin so `input()` raises EOFError immediately
    instead of blocking Flask forever,
  * enforces a hard wall-clock timeout (default 3 s, capped at 10 s)
    via subprocess.run(timeout=...) which kills the child process,
  * caps captured stdout/stderr at MAX_OUTPUT_CHARS so a runaway print
    loop cannot exhaust memory or flood the browser,
  * statically rejects a small denylist of clearly dangerous stdlib
    modules (network / process spawning) BEFORE execution,
  * removes the temporary directory in a finally block.

What it does NOT do:
  * OS-level user isolation, cgroups/containers, seccomp, CPU or RAM
    quotas. A determined student could still write a program that
    touches the filesystem of the machine running this server. For a
    trusted classroom environment that trade-off is acceptable; for
    anything else run this behind Docker/gVisor or use a hosted judge.

Return contract: run_python_code() -> dict(ok, stdout, stderr,
timed_out, rejected_module, duration_ms). It NEVER raises for student
mistakes -- syntax errors, runtime errors and timeouts all come back
as data.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Hard limits (kept generous enough for beginner exercises).
DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_TIMEOUT_SECONDS = 10.0
MAX_CODE_LENGTH = 64_000          # ~64 KB of source is plenty
MAX_OUTPUT_CHARS = 8_000          # per stream; larger output is truncated

# Modules refused before execution. Beginner courses never need these;
# they are exactly the ones useful for attacking the host or network.
_DENIED_MODULES = {
    "ctypes", "socket", "subprocess", "multiprocessing", "http",
    "urllib", "urllib2", "urllib3", "ftplib", "telnetlib", "smtplib",
    "webbrowser", "requests", "shutil", "importlib",
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE
)


def _scan_denied_imports(code):
    """Return the first denylisted top-level module imported by `code`."""
    for match in _IMPORT_RE.finditer(code):
        module = match.group(1).split(".")[0]
        if module in _DENIED_MODULES:
            return module
    return None


def _truncate(text):
    """Cap a stream at MAX_OUTPUT_CHARS, appending a visible marker."""
    if text is None:
        return ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return (
        text[:MAX_OUTPUT_CHARS]
        + f"\n…[output truncated after {MAX_OUTPUT_CHARS} characters]"
    )


def run_python_code(code, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    """
    Execute beginner Python in a temporary directory.

    Returns dict:
        ok              True when the program exited with code 0
        stdout          decoded stdout (truncated)
        stderr          decoded stderr / traceback (truncated)
        timed_out       True when the timeout killed the program
        rejected_module denylisted module name, or None
        duration_ms     wall-clock runtime in milliseconds
    """
    result = {
        "ok": False,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "rejected_module": None,
        "duration_ms": 0,
    }

    if not isinstance(code, str) or not code.strip():
        result["stderr"] = "Nothing to run — the editor is empty."
        return result
    if len(code) > MAX_CODE_LENGTH:
        result["stderr"] = (
            f"Program too long ({len(code)} characters); "
            f"limit is {MAX_CODE_LENGTH}."
        )
        return result

    denied = _scan_denied_imports(code)
    if denied:
        result["rejected_module"] = denied
        result["stderr"] = (
            f"ImportError: module '{denied}' is not allowed in the "
            "learning sandbox."
        )
        return result

    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = max(0.5, min(timeout, MAX_TIMEOUT_SECONDS))

    workdir = tempfile.mkdtemp(prefix="lp_playground_")
    script_path = os.path.join(workdir, "main.py")
    try:
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(code)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) \
            if os.name == "nt" else 0

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "main.py"],
                cwd=workdir,
                input=b"",
                capture_output=True,
                timeout=timeout,
                shell=False,          # never a shell — argv only
                creationflags=creationflags,
            )
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
            result["stdout"] = _truncate(
                completed.stdout.decode("utf-8", errors="replace"))
            result["stderr"] = _truncate(
                completed.stderr.decode("utf-8", errors="replace"))
            result["ok"] = completed.returncode == 0
        except subprocess.TimeoutExpired:
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
            result["timed_out"] = True
            result["stderr"] = _truncate(
                f"TimeoutExpired: your program ran longer than "
                f"{timeout:g}s and was stopped.")
    except Exception as exc:  # defensive: never crash the request
        result["stderr"] = f"Sandbox error: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return result
