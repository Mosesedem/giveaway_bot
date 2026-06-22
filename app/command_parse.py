"""Parse structured fields from host giveaway-start tweets."""

import re

WINNERS_RE = re.compile(r"winners?\s*:\s*(\d+)", re.IGNORECASE)
PRIZE_RE = re.compile(r"prize\s*:\s*(.+?)(?:\s*/\s*|$)", re.IGNORECASE)
TITLE_RE = re.compile(r"\b(giveaway|start|begin)\b\s*(.*)", re.IGNORECASE | re.DOTALL)


def parse_start_command(text: str) -> dict:
    """
    Extract title, prize, and winner count from a mention like:
      @bot giveaway Friday cash drop / prize: ₦50k / winners: 3
    """
    num_winners = 1
    prize_description = None
    title = text.strip()

    winners_match = WINNERS_RE.search(text)
    if winners_match:
        num_winners = max(1, int(winners_match.group(1)))

    prize_match = PRIZE_RE.search(text)
    if prize_match:
        prize_description = prize_match.group(1).strip().rstrip("/").strip() or None

    title_match = TITLE_RE.search(text)
    if title_match:
        remainder = title_match.group(2).strip()
        if remainder:
            cleaned = WINNERS_RE.sub("", remainder)
            cleaned = PRIZE_RE.sub("", cleaned)
            cleaned = re.sub(r"\s*/\s*", " ", cleaned).strip()
            if cleaned:
                title = cleaned[:200]

    return {
        "title": title[:200] or "Untitled giveaway",
        "prize_description": prize_description,
        "num_winners": num_winners,
    }