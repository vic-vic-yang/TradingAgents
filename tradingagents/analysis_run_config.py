"""Build the graph ``config`` dict for an analysis run (CLI and web share this)."""

from __future__ import annotations

from typing import Any, Optional

from tradingagents.default_config import DEFAULT_CONFIG


def build_analysis_config(
    *,
    research_depth: int,
    shallow_thinker: str,
    deep_thinker: str,
    llm_provider: str,
    backend_url: Optional[str] = None,
    output_language: str = "English",
    google_thinking_level: Optional[str] = None,
    openai_reasoning_effort: Optional[str] = None,
    anthropic_effort: Optional[str] = None,
    checkpoint_enabled: bool = False,
) -> dict[str, Any]:
    """Merge run selections into ``DEFAULT_CONFIG`` the same way as ``cli.main.run_analysis``.

    Uses a shallow copy of ``DEFAULT_CONFIG`` and the same key assignments as
    the interactive CLI after ``get_user_selections()``.
    """
    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = research_depth
    config["max_risk_discuss_rounds"] = research_depth
    config["quick_think_llm"] = shallow_thinker
    config["deep_think_llm"] = deep_thinker
    config["backend_url"] = backend_url
    config["llm_provider"] = llm_provider.lower()
    config["google_thinking_level"] = google_thinking_level
    config["openai_reasoning_effort"] = openai_reasoning_effort
    config["anthropic_effort"] = anthropic_effort
    config["output_language"] = output_language
    config["checkpoint_enabled"] = checkpoint_enabled
    return config
