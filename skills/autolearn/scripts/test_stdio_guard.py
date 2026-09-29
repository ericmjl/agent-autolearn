"""Tests for the console-encoding guard used by the autolearn CLIs.

On Windows, stdout often uses a legacy code page (cp1252); printing the
arrows/dashes in CLI output then raises UnicodeEncodeError and turns a
successful command into a traceback. reconfigure_stdio() replaces unmappable
characters instead of raising.
"""

import io
import sys

import stdio_guard


def test_guard_prevents_cp1252_crash():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    old_stdout = sys.stdout
    sys.stdout = stream
    try:
        stdio_guard.reconfigure_stdio()
        # The arrow is not encodable in cp1252; the guard must degrade it
        # instead of raising UnicodeEncodeError.
        print("RECORDED (count=1) \u2192 apply in-session only")
    finally:
        sys.stdout = old_stdout
    stream.flush()
    assert b"?" in raw.getvalue()
