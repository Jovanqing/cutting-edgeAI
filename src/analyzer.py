"""LLM scoring with v2.5 enhanced output fields.

Generates:
- title_cn: rewritten Chinese insight title (NOT translation)
- contribution: one-sentence core contribution
- why_matters: why this matters to the field
- key_points: 2-3 technical key points
- ideas: ≤3 specific, executable exploration directions
- novelty / impact / signal: multi-dimensional scores (internal, not displayed)
"""
import os
import json
import re
import math
from datetime import datetime, timezone
from openai import OpenAI

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

SYSTEM = """You are an elite AI research analyst. Your job is to read cutting-edge AI content and produce structured, high-density analysis for professionals.

Key principles:
- Rewrite titles as Chinese insight headlines, NOT translations. Capture the core finding in natural language, e.g. '让LLM学会说服力的新训练范式' or '稀疏注意力替代全注意力大幅降低计算成本'. Never include literal brackets or meta-text.
- Summaries must answer: what did they do? why is it better than alternatives?
- Ideas must be SPECIFIC and EXECUTABLE. Never say "explore more applications" or "apply to other domains".
- Each idea must have: a technical path, a constraint, or a concrete scenario.
- Max 3 ideas.
- Key points must capture method, technical novelty, and comparative advantage.

Output strictly valid JSON, no prose or markdown fences."""

PROMPT_TEMPLATE = """Analyze this AI research/content:

Source: {source} / {sub_source}
Title: {title}
URL: {url}
Body:
{content}

Top comments:
{comments}

Return ONLY this JSON object:
{{
  "novelty": <int 1-10>,
  "impact": <int 1-10>,
  "signal": <int 1-10>,
  "title_cn": "<Chinese insight headline rewriting the core finding, e.g. '稀疏注意力替代全注意力大幅降低计算成本' or '让LLM学会说服力的新训练范式', NO literal brackets, max 30 chars>",
  "contribution": "<2-3 sentences in Chinese: what is the core contribution, how does it work technically, and what is the key quantitative improvement>",
  "why_matters": "<2 sentences in Chinese: why this matters — what long-standing problem it solves, and what new possibilities it unlocks for practitioners>",
  "key_points": ["<concrete technical detail 1 with specific metric or method name>", "<detail 2>", "<detail 3>", "<detail 4, optional>", "<detail 5, optional>"],
  "summary": "<3-4 sentence detailed Chinese summary covering: what was done, key technical method, quantitative result, and comparative advantage>",,
  "ideas": ["<specific, executable idea 1 in Chinese>", "<specific, executable idea 2 in Chinese>", "<specific, executable idea 3 in Chinese, optional>"]
}}

IMPORTANT:
- title_cn: MUST be a rewritten insight headline, NOT a literal translation of the English title
- ideas: 2-3 max, each must contain a concrete technical approach or specific scenario
- key_points: 2-3 technical specifics about method/architecture/advantage
- No vague suggestions like "explore more applications" or "apply to other domains" """


class Analyzer:
    def __init__(self, model: str = "qwen3.7-max"):
        api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
        if not api_key:
            raise RuntimeError("LLM_API_KEY (or DASHSCOPE_API_KEY) not set in .env")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def score(self, item: dict) -> dict:
        try:
            comments = json.loads(item.get("comments_blob") or "[]")
        except Exception:
            comments = []
        comments_text = "\n".join(f"- {c[:400]}" for c in comments[:15]) or "(none)"
        prompt = PROMPT_TEMPLATE.format(
            source=item.get("source", ""),
            sub_source=item.get("sub_source", ""),
            title=item.get("title", ""),
            url=item.get("url", ""),
            content=(item.get("content") or "")[:4000],
            comments=comments_text[:4000],
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=1500,
            temperature=0.3,
            timeout=90,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {
                "novelty": 0, "impact": 0, "signal": 0,
                "title_cn": item.get("title", "")[:50],
                "contribution": "", "why_matters": "",
                "key_points": [], "summary": raw[:500], "ideas": [],
            }

        novelty = int(parsed.get("novelty", 0) or 0)
        impact = int(parsed.get("impact", 0) or 0)
        signal_score = int(parsed.get("signal", 0) or 0)

        final_score = 0.5 * novelty + 0.3 * impact + 0.2 * signal_score

        relevance = round(final_score)

        # Normalize structured fields
        title_cn = str(parsed.get("title_cn", "") or "").strip()
        if not title_cn:
            title_cn = item.get("title", "")[:50]
        contribution = str(parsed.get("contribution", "") or "").strip()
        why_matters = str(parsed.get("why_matters", "") or "").strip()
        key_points = _normalize_list(parsed.get("key_points", []))
        ideas = _normalize_list(parsed.get("ideas", []))
        summary = str(parsed.get("summary", "") or "").strip()

        return {
            "relevance": max(1, min(10, relevance)),
            "summary": summary,
            "ideas": "\n".join(f"- {x}" for x in ideas) if ideas else "",
            "novelty": max(1, min(10, novelty)),
            "impact": max(1, min(10, impact)),
            "signal_score": max(1, min(10, signal_score)),
            "final_score": final_score,
            "title_cn": title_cn,
            "contribution": contribution,
            "why_matters": why_matters,
            "key_points": json.dumps(key_points, ensure_ascii=False),
        }


def _normalize_list(val):
    """Coerce various input formats into a clean list of strings."""
    if isinstance(val, list):
        return [str(x).strip().lstrip("- ") for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip().lstrip("- ") for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [s] if s else []
    return []


def _age_hours(published_str: str):
    formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]
    try:
        ts = datetime.fromtimestamp(float(published_str), tz=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except (ValueError, TypeError):
        pass
    for fmt in formats:
        try:
            ts = datetime.strptime(published_str, fmt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        except ValueError:
            continue
    return None
