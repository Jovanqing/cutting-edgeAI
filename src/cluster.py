"""Topic clustering via LLM.

Takes analyzed items and groups them into 3-5 coherent topics.
Used at the digest layer to answer "what happened today" instead of "what scored high".
"""
import os
import json
import re
import sys
from openai import OpenAI

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

CLUSTER_PROMPT = """You are an AI trend analyst. Group the following articles into **5-8 themes**.

Hard rules — failing any of these breaks the downstream pipeline:
1. **EVERY article MUST be assigned to exactly one theme** — do not skip any index.
2. **NO theme may contain more than 35% of total items.** If a theme grows too large, split it into sub-themes.
3. **AVOID generic catch-all names** like "其他", "杂项", "其它值得关注". If an item doesn't fit existing themes, create a new specific theme for it (e.g. "AI 硬件与基建" instead of "其他").
4. Theme names should be specific Chinese phrases (e.g. "AI Agent 工作流", "开源大模型新进展", "RAG 与检索增强", "多模态推理突破").
5. The union of all `item_indices` MUST equal {{0, 1, ..., N-1}} where N = total articles.

Articles:
{articles}

Return ONLY this JSON (no markdown, no commentary):
{{
  "themes": [
    {{
      "name": "<specific theme name in Chinese>",
      "description": "<1-sentence Chinese description>",
      "item_indices": [0, 3, 5]
    }}
  ]
}}"""


class Clusterer:
    def __init__(self, cfg: dict):
        api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = cfg.get("model", "qwen-max-latest")
        self.num_topics = int(cfg.get("num_topics", 5))
        self.enabled = bool(cfg.get("enabled", True))

    def cluster(self, items: list[dict]) -> dict[str, list[str]]:
        """Return {theme_name: [item_id, ...]} mapping.

        Falls back to source-based grouping if LLM call fails or cluster is disabled.
        """
        if not items:
            return {}

        articles_text = self._format_articles(items)

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                # 200 items × 5 themes × ~50 indices/theme 需要 1500-2500 tokens 输出
                max_tokens=3000,
                temperature=0.2,
                timeout=120,  # 大请求 60-90s 正常，120s 容错；防止 reasoning model 卡 10 分钟
                messages=[
                    {"role": "user", "content": CLUSTER_PROMPT.format(articles=articles_text)},
                ],
                response_format={"type": "json_object"},
            )
            finish = resp.choices[0].finish_reason
            raw = (resp.choices[0].message.content or "").strip()
            if finish == "length":
                print(f"[cluster] WARN: LLM output truncated (max_tokens hit); response={len(raw)} chars", file=sys.stderr)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                raw = m.group(0)
            result = json.loads(raw)
            return self._parse_themes(result, items)
        except Exception as e:
            # Don't silently fallback — log so future bugs are visible.
            print(f"[cluster] LLM clustering failed: {type(e).__name__}: {e}", file=sys.stderr)
            return self._fallback_cluster(items)

    def _format_articles(self, items: list[dict]) -> str:
        # 缩短 summary 节省 input tokens：200 条 × (100+100) ≈ 13k tokens，留足空间给 output
        # SQLite NULL 字段会被 dict_factory 转成 None，要用 `or ""` 兜底
        lines = []
        for i, it in enumerate(items):
            title = (it.get("title") or "")[:100]
            summary = (it.get("summary") or "")[:100]
            lines.append(f"[{i}] {title}")
            if summary:
                lines.append(f"    {summary}")
        return "\n".join(lines)

    def _parse_themes(self, result: dict, items: list[dict]) -> dict[str, list[str]]:
        themes = result.get("themes", [])
        out: dict[str, list[str]] = {}
        for theme in themes:
            name = theme.get("name", "其他")
            indices = theme.get("item_indices", [])
            item_ids = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(items):
                    item_ids.append(items[idx]["id"])
            if item_ids:
                out[name] = item_ids
        # Collect any unassigned items
        assigned = set()
        for ids in out.values():
            assigned.update(ids)
        unassigned = [it["id"] for it in items if it["id"] not in assigned]
        if unassigned:
            if "其他值得关注" in out:
                out["其他值得关注"].extend(unassigned)
            else:
                out["其他值得关注"] = unassigned
        return out

    def _fallback_cluster(self, items: list[dict]) -> dict[str, list[str]]:
        """Group by source when LLM clustering fails."""
        out: dict[str, list[str]] = {}
        for it in items:
            src = it.get("source", "unknown")
            key = f"{src} 来源精选"
            out.setdefault(key, []).append(it["id"])
        return out
