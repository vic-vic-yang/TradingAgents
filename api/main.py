"""Vercel FastAPI entrypoint.

Vercel auto-detects FastAPI apps from paths like ``api/main.py`` and expects
an ``app`` variable at module level.
"""

import os
from pathlib import Path
import sys

# Ensure repository root is importable in Vercel runtime.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _configure_writable_paths_for_vercel() -> None:
    """Vercel has a read-only filesystem except ``/tmp``."""
    if os.getenv("VERCEL") != "1":
        return

    base = "/tmp/tradingagents"
    os.environ.setdefault("TRADINGAGENTS_RESULTS_DIR", f"{base}/logs")
    os.environ.setdefault("TRADINGAGENTS_CACHE_DIR", f"{base}/cache")
    os.environ.setdefault("TRADINGAGENTS_MEMORY_LOG_PATH", f"{base}/memory/trading_memory.md")
    os.environ.setdefault("TRADINGAGENTS_CLI_REPORTS_DIR", f"{base}/reports")

    Path(f"{base}/logs").mkdir(parents=True, exist_ok=True)
    Path(f"{base}/cache").mkdir(parents=True, exist_ok=True)
    Path(f"{base}/memory").mkdir(parents=True, exist_ok=True)
    Path(f"{base}/reports").mkdir(parents=True, exist_ok=True)


_configure_writable_paths_for_vercel()

from web_api.app import app  # noqa: E402,F401

