"""Insight Generator: distill today's core AI trends from top-ranked items.

Takes the top 20 analyzed items and produces 3-5 actionable trend insights.
This is the "cognition layer" — it answers "what changed today" rather than
"what scored highest".
"""
import os
import json
import re
import sys
from openai import OpenAI

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

INSIGHT_PROMPT = """You are an AI industry analyst. Read the following top AI articles from today and identify the 3-5 most important TRENDS or SHIFTS happening in AI right now.

Rules:
- DO NOT summarize individual articles — identify cross-cutting trends
- Each trend must describe a CHANGE (what was true before → what's emerging now)
- Back each trend with 2-3 concrete signals from the articles below
- Write in Chinese, professional but concise

Articles:
{articles}

Return ONLY this JSON:
{{
  "trends": [
    {{
      "title": "<trend name in Chinese, max 20 chars>",
      "description": "<2-3 sentence description of the trend and why it matters>",
      "signals": ["<concrete example from articles>", "<another example>"]
    }}
  ],
  "headline": "<one-sentence summary of today's AI landscape in Chinese, max 40 chars>"
}}"""


class InsightGenerator:
    def __init__(self, cfg: dict):
        api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = cfg.get("model", "qwen-max-latest")
        self.enabled = bool(cfg.get("enabled", True))
        self.top_n = int(cfg.get("top_n", 20))

    def generate(self, items: list[dict]) -> dict:
        """Return {headline, trends: [{title, description, signals}]}."""
        if not items or not self.enabled:
            return {"headline": "", "trends": []}

        top_items = sorted(
            items,
            key=lambda x: x.get("final_score") or x.get("relevance", 0),
            reverse=True,
        )[:self.top_n]

        articles_text = self._format(top_items)

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=1500,
                temperature=0.4,
                timeout=90,
                messages=[
                    {"role": "user", "content": INSIGHT_PROMPT.format(articles=articles_text)},
                ],
                response_format={"type": "json_object"},
            )
            finish = resp.choices[0].finish_reason
            raw = (resp.choices[0].message.content or "").strip()
            if finish == "length":
                print(f"[insight] WARN: LLM output truncated (max_tokens hit)", file=sys.stderr)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                raw = m.group(0)
            result = json.loads(raw)
            return {
                "headline": result.get("headline", ""),
                "trends": result.get("trends", []),
            }
        except Exception as e:
            print(f"[insight] LLM call failed: {type(e).__name__}: {e}", file=sys.stderr)
            return {"headline": "", "trends": []}

    def _format(self, top_items: list[dict]) -> str:
        lines = []
        for i, it in enumerate(top_items):
            title = (it.get("title") or "")[:120]
            cn_title = it.get("title_cn") or ""
            summary = (it.get("summary") or "")[:200]
            lines.append(f"[{i}] {title}")
            if cn_title:
                lines.append(f"    CN: {cn_title}")
            if summary:
                lines.append(f"    {summary}")
        return "\n".join(lines)
