"""Force UTF-8 console output on Windows.

The CLI scripts print emoji and box-drawing characters. On Windows the default
console encoding is often cp1252, which cannot encode those characters and makes
Python raise UnicodeEncodeError mid-print.

Importing this module reconfigures stdout/stderr to UTF-8 (with a safe fallback
that replaces un-encodable characters rather than crashing). Import it as the
very first thing in an entry-point script:

    import console_utf8  # noqa: F401  (side-effect import)
"""

import sys


def _enable():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        # Python 3.7+ TextIOWrapper supports reconfigure().
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # Stream may not support reconfiguration (e.g. redirected pipe);
                # leave it as-is rather than failing the whole program.
                pass


_enable()
