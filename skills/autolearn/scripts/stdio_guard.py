"""Console encoding guard for the autolearn CLIs.

Windows consoles often use a legacy code page (e.g. cp1252). CLI output uses
arrows and dashes, so a plain ``print`` can raise UnicodeEncodeError and turn
a successful command into a traceback. Reconfiguring the standard streams to
replace unmappable characters keeps those commands quiet on such consoles.
"""

from __future__ import annotations

import sys


def reconfigure_stdio() -> None:
    """Make stdout/stderr resilient to non-UTF-8 consoles (idempotent)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
