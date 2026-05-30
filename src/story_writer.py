"""LLM 深度报道生成器 —— 把 top items 加工成"商业日报"级长内容。

接收：top item + 当日相关 items（同主题）
输出：dict {lead, background, key_facts, impact, reactions, what_to_watch}

设计：
- 用 qwen-max-latest（实测稳定），不要 reasoning model（大 prompt timeout）
- 限制 input ≤ 4k tokens，output 800-1200 tokens
- 错误时 fallback 到原 item 的现有字段（不 break pipeline）
"""
import os
import json
import re
import sys
from typing import Optional
from openai import OpenAI

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

STORY_PROMPT = """你是顶级 AI 商业日报记者（对标 The Information / Stratechery）。
将下面的"主条目"加工成一篇专业商业报道，使用周边"相关条目"补充背景和多源验证。

主条目：
标题: {title}
来源: {source}
作者/账号: {author}
摘要: {summary}
核心贡献: {contribution}
关键事实: {key_points}
Why it matters: {why_matters}

相关条目（同主题的其它报道，供你交叉引用）：
{related}

输出严格 JSON（不要 markdown 标记、不要附加文字）：
{{
  "lead": "<开头钩子段：1-2 句话点出最关键信号，包含具体数字/名字>",
  "background": "<2-3 段背景脉络：这件事的来龙去脉、相关方、历史脉络。150-250 字>",
  "key_facts": ["<关键事实 1，含具体数字>", "<事实 2>", "<事实 3>", "<事实 4>"],
  "impact": "<影响分析：对哪些角色（开发者/企业/研究者/投资人）有什么影响。100-200 字>",
  "reactions": ["<业内人士观点 / 跨源验证信号 1>", "<信号 2>", "<信号 3>"],
  "what_to_watch": ["<接下来要关注的事 1>", "<事 2>", "<事 3>"]
}}

写作原则：
- 中文专业财经媒体风格
- 多用具体数字、时间、人名、公司名
- 避免空洞修辞
- 不要 hype（"震惊""王炸"等）
- 用"事件→背景→影响→展望"的递进结构"""


class StoryWriter:
    def __init__(self, model: str = "qwen-max-latest"):
        api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def write(self, main_item: dict, related_items: list[dict],
              tweet_quotes: Optional[list[dict]] = None) -> Optional[dict]:
        """生成深度报道。

        Args:
          main_item: 头条 item
          related_items: 同主题相关 items（作为 LLM 背景）
          tweet_quotes: 推特真实引用列表 [{handle, text, url}, ...]
                       如果传入，LLM 会用真实推文作为 reactions（不编造）

        失败返回 None（caller 用现有字段 fallback）。
        """
        # 准备 prompt 输入
        title = main_item.get("title_cn") or main_item.get("title", "")
        summary = (main_item.get("summary") or "")[:500]
        contribution = (main_item.get("contribution") or "")[:300]
        why = (main_item.get("why_matters") or "")[:300]
        author = main_item.get("author") or main_item.get("sub_source", "")
        source = main_item.get("sub_source") or main_item.get("source", "")

        # 解析 key_points
        kp_raw = main_item.get("key_points") or "[]"
        try:
            kp = json.loads(kp_raw) if isinstance(kp_raw, str) else kp_raw
            key_points_str = " / ".join(str(p) for p in kp[:5]) if kp else ""
        except Exception:
            key_points_str = str(kp_raw)[:200]

        # 相关条目（最多 6 个）
        related_lines = []
        for i, r in enumerate(related_items[:6], 1):
            r_title = r.get("title_cn") or r.get("title", "")
            r_contribution = (r.get("contribution") or r.get("summary") or "")[:150]
            r_sub = r.get("sub_source", "")
            related_lines.append(f"[{i}] ({r_sub}) {r_title} — {r_contribution}")
        related_text = "\n".join(related_lines) if related_lines else "（无）"

        prompt = STORY_PROMPT.format(
            title=title,
            source=source,
            author=author,
            summary=summary,
            contribution=contribution,
            key_points=key_points_str,
            why_matters=why,
            related=related_text,
        )

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=1500,
                temperature=0.4,
                timeout=60,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                raw = m.group(0)
            result = json.loads(raw)
            # 如果有真实推文引用，覆盖 LLM 编的 reactions
            if tweet_quotes:
                result["real_quotes"] = tweet_quotes[:5]
            return result
        except Exception as e:
            print(f"[story_writer] failed for '{title[:40]}': {type(e).__name__}: {e}",
                  file=sys.stderr)
            return None


def find_related_tweets(main_item: dict, all_items: list[dict], k: int = 5) -> list[dict]:
    """在所有 items 中找与 main_item 相关的推特推文（用 title 关键词重合度）。

    返回 [{handle, text, url, score}, ...] 按相关度排序。
    """
    main_title = ((main_item.get("title_cn") or "") + " " +
                  (main_item.get("title") or "") + " " +
                  (main_item.get("summary") or "")).lower()
    # 简单 tokenize 提取关键词
    tokens = set()
    for t in re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,}|[一-龥]{2,}', main_title):
        if len(t) >= 3:
            tokens.add(t.lower())
    if not tokens:
        return []

    candidates = []
    for it in all_items:
        sub = it.get("sub_source", "") or ""
        if not sub.startswith("X/"):
            continue
        if it.get("id") == main_item.get("id"):
            continue
        text = ((it.get("title") or "") + " " + (it.get("summary") or "")).lower()
        hits = sum(1 for t in tokens if t in text)
        if hits >= 2:  # 至少 2 个关键词重合
            candidates.append({
                "handle": sub[2:],  # X/karpathy → karpathy
                "text": (it.get("title_cn") or it.get("title") or "")[:200],
                "url": it.get("url", ""),
                "score": hits,
            })
    candidates.sort(key=lambda x: -x["score"])
    return candidates[:k]
