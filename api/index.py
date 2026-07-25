"""Vercel entry point.

Vercel's Python runtime looks for a symbol named ``handler`` that subclasses
``BaseHTTPRequestHandler`` — which is exactly what the local server already is, so the
deployed site and ``python web/server.py`` run the same code down to the routing table.

``vercel.json`` rewrites every path here, and ships ``src``, ``web`` and ``params`` with the
function: the parameter store is read from disk at request time and the pages are served
from ``web/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from web.server import Handler as handler  # noqa: E402,F401,N813
