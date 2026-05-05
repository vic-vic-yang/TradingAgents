"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Overweight, Hold, Underweight, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re
from typing import Tuple


# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: Tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# Matches "Rating: X" / "rating - X" / "Rating: **X**" — tolerates markdown
# bold wrappers and either a colon or hyphen separator.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)

# Line looks like a formal rating label (PM / research manager / research plan).
# Include ``**评级**：**增持（Overweight）**`` (short 评级) — not only 最终评级 / 投资评级.
_RATING_LINE_HINT = re.compile(
    r"(?i)(?:\bRating\b|\*\*Rating\*\*|Recommendation|最终评级|投资评级|组合评级|"
    r"最终交易决策|投资建议|"
    r"\*\*评级\*\*\s*[：:]|(?<![\w/])评级\s*[：:])",
)

# Chinese prose on the same line as a rating hint → canonical tier (checked in order).
_CN_TIER_ON_HINT_LINE: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"卖出|清仓|抛售|离场"), "Sell"),
    (re.compile(r"减持|减仓|低配"), "Underweight"),
    (re.compile(r"增持|超配|加码"), "Overweight"),
    (re.compile(r"买入|买进|建仓"), "Buy"),
    (re.compile(r"持有|观望|中性"), "Hold"),
)

# English tier as its own token, including before fullwidth parens or punctuation
# (e.g. ``**Underweight（减仓/回避）**`` — not ``Underweight`` in ASCII parens only).
_EN_TIER_TOKEN = re.compile(
    r"(?<![A-Za-z])(Buy|Overweight|Hold|Underweight|Sell)(?![A-Za-z])",
)

def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text.

    Strategy:
    1. Explicit ``Rating: X`` (ASCII label).
    2. Same line as ``最终评级`` / ``Rating`` with an English tier (incl. before ``（``).
    3. Whitespace tokens whose leading Latin run is a tier (markdown / CJK suffix).
    4. Isolated English tier words per line (legacy).
    5. Chinese keywords on the same line as a rating-style hint (PM Chinese output).
    """
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    for line in text.splitlines():
        if _RATING_LINE_HINT.search(line):
            m = _EN_TIER_TOKEN.search(line)
            if m:
                return m.group(1).capitalize()

    for line in text.splitlines():
        for raw in line.split():
            w = raw.strip('*:.，。；;、（）()[]【】""''「」')
            m = _EN_TIER_TOKEN.match(w)
            if m:
                return m.group(1).capitalize()

    for line in text.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,")
            if clean in _RATING_SET:
                return clean.capitalize()

    for line in text.splitlines():
        if not _RATING_LINE_HINT.search(line):
            continue
        for pat, tier in _CN_TIER_ON_HINT_LINE:
            if pat.search(line):
                return tier

    return default
