"""PreFilter layer: cheap filtering before LLM.

Cuts ~80% of raw items using:
- Keyword matching (title + content)
- Heat threshold (item score, e.g. Reddit upvotes)
- Time window
- Basic dedup already handled by DB upsert
"""
from datetime import datetime, timedelta, timezone


class PreFilter:
    """Fast, rules-based filter to discard obvious noise before ranking."""

    def __init__(self, cfg: dict, keywords: list[str]):
        self.keywords = [k.lower() for k in keywords] if keywords else []
        self.min_score = int(cfg.get("min_heat", 0))
        self.max_age_hours = int(cfg.get("max_age_hours", 72))

    def should_keep(self, item: dict) -> bool:
        # Time window: prefer fetched_at (when we collected), fall back to published_at
        if self.max_age_hours:
            try:
                ts_str = item.get("fetched_at", "") or item.get("published_at", "")
                if ts_str:
                    ts = _parse_ts(ts_str)
                    if ts:
                        age = datetime.now(timezone.utc) - ts
                        if age > timedelta(hours=self.max_age_hours):
                            return False
            except (ValueError, TypeError):
                pass

        # Heat threshold
        score = item.get("score", 0) or 0
        if score < self.min_score:
            # Always keep if it matches a keyword strongly
            if not self._keyword_match(item, threshold=2):
                return False

        # Keyword gate: item must match at least 1 keyword
        if self.keywords and not self._keyword_match(item, threshold=1):
            return False

        return True

    def _keyword_match(self, item: dict, threshold: int = 1) -> bool:
        if not self.keywords:
            return True
        text = f"{item.get('title', '')} {item.get('content', '')}".lower()
        hits = sum(1 for kw in self.keywords if kw in text)
        return hits >= threshold


def _parse_ts(s: str):
    """Try multiple timestamp formats."""
    formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]
    # Handle unix timestamps
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except (ValueError, TypeError):
        pass
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
