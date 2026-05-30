"""Ranker: cheap keyword-based scoring for first-pass prioritization.

Scores every item that passes PreFilter, then selects Top-K candidates
for LLM analysis. Uses weighted keyword matching rules from config.

v2.6 新增：跨源共振加权 —— 同一关键词在多个不同 sub_source 同时出现
（如 Karpathy 发推 + 机器之心写文章 + Reddit 讨论同一话题），就是真热点。
共振命中的 items 会获得 resonance_bonus 加分。
"""
import re
from collections import defaultdict

# 资源词长度过滤：太短（如 "ai"）和单字噪音多
_MIN_TOKEN_LEN_EN = 4
_MIN_TOKEN_LEN_ZH = 2
# 中英文混合分词（基础版）
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+|[一-龥]+")
# 停用词（不应该当作共振信号）
_STOPWORDS = {
    "this", "that", "with", "from", "they", "have", "will", "your", "what",
    "about", "more", "than", "into", "their", "some", "when", "been", "were",
    "AI", "ai", "the", "and", "for", "you",
    "我们", "可以", "这个", "通过", "如何", "什么", "提供", "进行", "实现",
    "使用", "支持", "包括", "以及", "用于", "目前", "需要", "已经",
}


class Ranker:
    """Rule-based scoring to rank items before LLM analysis."""

    def __init__(self, cfg: dict):
        raw = cfg.get("keyword_weights", {})
        self.weights: list[tuple[str, float]] = []
        for kw, weight in raw.items():
            self.weights.append((kw.lower(), float(weight)))
        self.weights.sort(key=lambda x: -x[1])

        self.top_k = int(cfg.get("top_k", 200))
        self.top_percent = float(cfg.get("top_percent", 0))

        # 跨源共振参数
        # min_sources：一个词需要在多少个不同 sub_source 出现，才算"共振词"
        # bonus_per_hit：命中一个共振词给的加分
        # max_bonus：单个 item 共振加分上限（防止刷分）
        self.resonance_min_sources = int(cfg.get("resonance_min_sources", 3))
        self.resonance_bonus_per_hit = float(cfg.get("resonance_bonus_per_hit", 1.5))
        self.resonance_max_bonus = float(cfg.get("resonance_max_bonus", 8.0))

        # Boost for specific sources
        source_boost = cfg.get("source_boost", {})
        self.source_boost = {k: float(v) for k, v in source_boost.items()}

        # Boost for specific sub_source names (e.g. "OpenAI News", "AINews", "BSky/simonw")
        sub_source_boost = cfg.get("sub_source_boost", {}) or {}
        self.sub_source_boost = {k: float(v) for k, v in sub_source_boost.items()}

        # Boost for specific authors (substring match, case-insensitive)
        author_weights = cfg.get("author_weights", {}) or {}
        self.author_weights = [(k.lower(), float(v)) for k, v in author_weights.items()]

    def score(self, item: dict) -> float:
        text = f"{item.get('title', '')} {item.get('content', '')} {item.get('sub_source', '')}".lower()
        s = 0.0
        for kw, w in self.weights:
            if kw in text:
                s += w

        # Floor so boosts still apply to keyword-empty items (e.g. official press releases)
        if s == 0.0:
            s = 1.0

        src = item.get("source", "")
        if src in self.source_boost:
            s *= self.source_boost[src]

        sub = item.get("sub_source", "")
        if sub in self.sub_source_boost:
            s *= self.sub_source_boost[sub]

        author = (item.get("author", "") or "").lower()
        if author and self.author_weights:
            best = 1.0
            for needle, mult in self.author_weights:
                if needle and needle in author and mult > best:
                    best = mult
            s *= best

        return round(s, 2)

    def select(self, items: list[dict]) -> list[dict]:
        """Score all items, return top candidates.

        Two-pass:
          1) 算每个 item 的基础分（keyword + sub_source_boost + author + source_boost）
          2) 算跨源共振词集合 → 给每个 item 加 resonance_bonus
        """
        if not items:
            return []

        # Pass 1: base score
        for it in items:
            it["pre_score"] = self.score(it)

        # Pass 2: cross-source resonance
        resonance_words = self._compute_resonance_words(items)
        if resonance_words:
            for it in items:
                bonus = self._resonance_bonus(it, resonance_words)
                if bonus > 0:
                    it["pre_score"] = round(it["pre_score"] + bonus, 2)
                    it["resonance_bonus"] = bonus

        items.sort(key=lambda x: x.get("pre_score", 0), reverse=True)
        if self.top_percent > 0:
            n = max(1, int(len(items) * self.top_percent / 100))
        else:
            n = min(self.top_k, len(items))
        return items[:n]

    def _compute_resonance_words(self, items: list[dict]) -> set[str]:
        """Find tokens that appear across ≥ resonance_min_sources different sub_sources."""
        word_to_subs: dict[str, set[str]] = defaultdict(set)
        for it in items:
            text = f"{it.get('title', '')} {it.get('summary', '')}"
            sub = it.get("sub_source", "") or it.get("source", "")
            if not sub:
                continue
            for tok in self._tokenize(text):
                word_to_subs[tok].add(sub)
        return {w for w, subs in word_to_subs.items() if len(subs) >= self.resonance_min_sources}

    def _resonance_bonus(self, item: dict, resonance_words: set[str]) -> float:
        """Bonus = number of resonance tokens this item mentions × bonus_per_hit (capped)."""
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        toks = set(self._tokenize(text))
        hits = len(toks & resonance_words)
        return min(hits * self.resonance_bonus_per_hit, self.resonance_max_bonus)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        if not text:
            return []
        out = []
        for m in _TOKEN_RE.finditer(text):
            tok = m.group(0)
            if tok in _STOPWORDS:
                continue
            # 区分英文/中文长度阈值
            if re.match(r"[A-Za-z]", tok[0]):
                if len(tok) >= _MIN_TOKEN_LEN_EN:
                    out.append(tok.lower())
            else:
                if len(tok) >= _MIN_TOKEN_LEN_ZH:
                    out.append(tok)
        return out
