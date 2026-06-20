"""TrendExpander —— 把简短的 trend 描述深度加工成 200-300 字商业分析。

输入：一个 trend dict（title / description / signals）+ 当日相关 items
输出：扩展后的 trend dict，新增字段：
  - deep_analysis: 200-300 字深度分析
  - drivers: 主要驱动力 list
  - key_players: 关键玩家/公司 list
  - winner_loser: 谁受益 / 谁承压

并发执行：4 个 trend 并行调 LLM，总耗时 ~30s（单条 ~30s）
"""
import os
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from openai import OpenAI

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

TREND_PROMPT = """你是顶级 AI 商业日报分析师（对标 The Information / Stratechery）。
将下面的"主题"扩展为一篇有洞察力的深度分析（250-350 字）。

主题：{title}
简短描述：{description}

跨源共振信号：
{signals}

当日相关报道（背景素材，可引用具体细节）：
{related}

输出严格 JSON（不要 markdown 标记）：
{{
  "deep_analysis": "<250-350 字。结构：①现状与证据（引用具体公司/产品/数字） ②为什么现在（驱动力与时间节点） ③谁在押注（关键行动方及动机） ④对行业的非显而易见影响 ⑤一个反直觉或值得警惕的视角>",
  "evidence_signals": ["<来自相关报道的具体证据 1（含公司名/数字/时间）>", "<证据 2>", "<证据 3>"],
  "drivers": ["<核心驱动力 1（具体、可量化）>", "<驱动力 2>", "<驱动力 3>"],
  "key_players": ["<公司/人物 1 及其角色>", "<2>", "<3>"],
  "winner_loser": "<具体说明：A 受益（原因），B 承压（原因）>",
  "contrarian_view": "<一句话：主流判断之外，值得警惕或反驳的视角>"
}}

写作要求（违反即重写）：
- 每个论点必须有来自相关报道的具体事实支撑，不得泛泛而谈
- 至少包含 2 个具体数字（百分比、金额、时间、参数规模等）
- 避免"快速发展""持续推进""重要意义"等空洞措辞
- contrarian_view 必须是真正的反向视角，不是对主观点的重复"""


class TrendExpander:
    def __init__(self, model: str = "qwen3.7-max"):
        api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def _expand_one(self, trend: dict, related_items: list[dict]) -> Optional[dict]:
        title = (trend.get("title") or "").strip()
        desc = (trend.get("description") or "").strip()
        signals = trend.get("signals") or []
        signals_text = "\n".join(f"- {s}" for s in signals) if signals else "（无）"

        related_lines = []
        for i, r in enumerate(related_items[:6], 1):
            r_title = r.get("title_cn") or r.get("title", "")
            r_contribution = (r.get("contribution") or r.get("summary") or "")[:120]
            r_sub = r.get("sub_source", "")
            related_lines.append(f"[{i}] ({r_sub}) {r_title} — {r_contribution}")
        related_text = "\n".join(related_lines) if related_lines else "（无）"

        prompt = TREND_PROMPT.format(
            title=title,
            description=desc,
            signals=signals_text,
            related=related_text,
        )

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=800,
                temperature=0.4,
                timeout=150,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                raw = m.group(0)
            return json.loads(raw)
        except Exception as e:
            print(f"[trend_expander] failed for '{title}': {type(e).__name__}: {e}",
                  file=sys.stderr)
            return None

    def expand_all(self, trends: list[dict], items: list[dict]) -> list[dict]:
        """并发扩展所有 trend。把 deep_analysis 等字段 merge 到原 trend dict 里。"""
        if not trends:
            return trends

        # 给每个 trend 选相关 items（用 title 关键词粗匹配）
        def related_for(trend):
            title = trend.get("title", "").lower()
            tokens = [t for t in re.findall(r'[A-Za-z]+|[一-龥]+', title) if len(t) >= 2]
            scored = []
            for it in items:
                text = ((it.get("title_cn") or it.get("title") or "") + " " +
                        (it.get("summary") or "")).lower()
                hits = sum(1 for t in tokens if t in text)
                if hits > 0:
                    scored.append((hits, it))
            scored.sort(key=lambda x: (-x[0], -(x[1].get("final_score") or 0)))
            return [it for _, it in scored[:6]]

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(self._expand_one, t, related_for(t)): t for t in trends}
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    result = fut.result()
                    if result:
                        t.update(result)
                except Exception:
                    pass

        return trends
