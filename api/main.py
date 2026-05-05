"""Vercel FastAPI entrypoint.

Vercel auto-detects FastAPI apps from paths like ``api/main.py`` and expects
an ``app`` variable at module level.
"""

from pathlib import Path
import sys

# Ensure repository root is importable in Vercel runtime.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_api.app import app  # noqa: E402,F401

