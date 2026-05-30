"""周报 / 月报聚合 —— 把多天的日报合并成更高层次的摘要。

用法：
  from src.weekly_digest import write_weekly, write_monthly
  write_weekly()    # 生成最近 7 天的周报
  write_monthly()   # 生成本月的月报

输出：data/digests/weekly-YYYY-Www.md (含 HTML 版本)
      data/digests/monthly-YYYY-MM.md
"""
import os
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from openai import OpenAI

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OUT_DIR = Path(__file__).parent.parent / "data" / "digests"

WEEKLY_PROMPT = """你是顶级 AI 商业刊物总编辑（对标 The Information / Stratechery）。
下面是过去 {days} 天的每日 AI 日报全文。请合并整理出一份{period_name}，
帮读者快速复盘这段时间的关键事件、趋势演变和未来方向。

每日日报全文：
========================================
{digests}
========================================

输出严格 JSON：
{{
  "headline": "<一句话总结{period_name}最关键的变化（30 字以内）>",
  "era_shift": "<200-300 字：这段时间最值得标记的'分水岭'转变，例如某个新范式开始、某个 player 崛起、某个老模式被颠覆>",
  "top_stories": [
    {{
      "title": "<故事名>",
      "summary": "<150 字内：该周期内反复出现的最重要事件，含具体公司/数字/时间>",
      "evidence": ["<日报某日提到的具体证据 1>", "<2>", "<3>"]
    }}
  ],
  "trend_evolution": [
    {{
      "theme": "<主题>",
      "change": "<这段时间该主题如何演变。100 字内>"
    }}
  ],
  "key_numbers": [
    "<这段时间出现的关键数字/数据 1（含上下文）>",
    "<数字 2>",
    "<数字 3>"
  ],
  "winners_losers": {{
    "winners": ["<赢家 1>", "<2>"],
    "losers": ["<承压方 1>", "<2>"]
  }},
  "looking_ahead": ["<未来 7 天关注 1>", "<2>", "<3>"]
}}

写作原则：
- 用专业财经媒体风格
- 多用具体公司/数字/事件名
- 避免 hype，避免空洞修辞
- top_stories 至少 3 个，至多 5 个
- trend_evolution 列 4-6 个主题"""


def _load_recent_digests(days: int) -> list[tuple[str, str]]:
    """读取最近 N 天的日报 markdown 文件。返回 [(date_str, content), ...]"""
    out = []
    now = datetime.now()
    for i in range(days):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        p = OUT_DIR / f"{d}.md"
        if p.exists():
            text = p.read_text(encoding="utf-8")
            # 节流：每天的日报截 5000 字符给 LLM
            out.append((d, text[:5000]))
    out.sort()
    return out


def _aggregate_with_llm(digests: list[tuple[str, str]], period_name: str, days: int) -> Optional[dict]:
    if not digests:
        return None
    digest_text = "\n\n".join(
        f"### {d}\n{content}" for d, content in digests
    )
    if len(digest_text) > 40000:
        digest_text = digest_text[:40000]  # qwen-max 32k 上限留些 margin

    api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
    client = OpenAI(api_key=api_key, base_url=base_url)

    prompt = WEEKLY_PROMPT.format(
        days=days, period_name=period_name, digests=digest_text
    )
    try:
        resp = client.chat.completions.create(
            model="qwen-max-latest",
            max_tokens=3500,
            temperature=0.3,
            timeout=120,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        return json.loads(raw)
    except Exception as e:
        print(f"[weekly] LLM failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _render_weekly_md(result: dict, period_name: str,
                      start_date: str, end_date: str, days_with_data: int) -> str:
    headline = (result.get("headline") or "").strip()
    era = (result.get("era_shift") or "").strip()
    top_stories = result.get("top_stories") or []
    trends = result.get("trend_evolution") or []
    numbers = result.get("key_numbers") or []
    wl = result.get("winners_losers") or {}
    winners = wl.get("winners") or []
    losers = wl.get("losers") or []
    ahead = result.get("looking_ahead") or []

    lines = [f"# 📊 AI 前沿{period_name}", ""]
    if headline:
        lines += [f"> **{headline}**", ""]
    lines += [
        f"📅 **{start_date} ~ {end_date}**  ·  覆盖 {days_with_data} 天日报",
        "", "---", "",
    ]
    if era:
        lines += [
            "## 🎯 本期标志性转变",
            "",
            era,
            "", "---", "",
        ]
    if top_stories:
        lines += ["## 🔥 本期 Top 故事", ""]
        for i, s in enumerate(top_stories, 1):
            t = s.get("title", "")
            summary = s.get("summary", "")
            evidence = s.get("evidence") or []
            lines += [f"### {i}. {t}", "", summary, ""]
            if evidence:
                lines.append("**证据线索**：")
                for e in evidence:
                    lines.append(f"- {e}")
                lines.append("")
        lines += ["---", ""]
    if trends:
        lines += ["## 📈 主题演变", ""]
        for t in trends:
            lines += [f"### {t.get('theme', '')}", "", t.get("change", ""), ""]
        lines += ["---", ""]
    if numbers:
        lines += ["## 🔢 关键数字", ""]
        for n in numbers:
            lines.append(f"- {n}")
        lines += ["", "---", ""]
    if winners or losers:
        lines += ["## 🔍 行业影响格局", ""]
        if winners:
            lines.append(f"**受益方**：{' · '.join(winners)}")
        if losers:
            lines.append(f"**挑战方**：{' · '.join(losers)}")
        lines += ["", "---", ""]
    if ahead:
        lines += ["## 🔭 接下来 7 天关注", ""]
        for a in ahead:
            lines.append(f"- {a}")
        lines += ["", "---", ""]
    lines.append(f"*🤖 AI Inspiration Pipeline · {period_name}聚合 · "
                 f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    return "\n".join(lines)


def write_weekly(days: int = 7) -> Optional[Path]:
    """生成最近 N 天周报，输出 weekly-YYYY-Www.md + .html。"""
    digests = _load_recent_digests(days)
    if len(digests) < 1:
        print("[weekly] no digest found in window", file=sys.stderr)
        return None
    result = _aggregate_with_llm(digests, "周报", days)
    if not result:
        return None
    start_d = digests[0][0]
    end_d = digests[-1][0]
    today = datetime.now()
    week_num = today.isocalendar().week
    year = today.isocalendar().year
    md = _render_weekly_md(result, "周报", start_d, end_d, len(digests))
    out_path = OUT_DIR / f"weekly-{year}-W{week_num:02d}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[weekly] written: {out_path}")
    # HTML 版本
    try:
        from .email_renderer import md_to_html_email
        html_path = out_path.with_suffix(".html")
        html_path.write_text(
            md_to_html_email(md, date_str=f"{start_d} ~ {end_d}"),
            encoding="utf-8",
        )
        print(f"[weekly] HTML: {html_path}")
    except Exception as e:
        print(f"[weekly] HTML render failed: {e}", file=sys.stderr)
    return out_path


def write_monthly() -> Optional[Path]:
    """生成本月月报（取最近 30 天）。"""
    digests = _load_recent_digests(30)
    if len(digests) < 5:
        print(f"[monthly] only {len(digests)} digest(s) found, need >= 5", file=sys.stderr)
        return None
    result = _aggregate_with_llm(digests, "月报", 30)
    if not result:
        return None
    start_d = digests[0][0]
    end_d = digests[-1][0]
    today = datetime.now()
    md = _render_weekly_md(result, "月报", start_d, end_d, len(digests))
    out_path = OUT_DIR / f"monthly-{today.strftime('%Y-%m')}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[monthly] written: {out_path}")
    try:
        from .email_renderer import md_to_html_email
        html_path = out_path.with_suffix(".html")
        html_path.write_text(
            md_to_html_email(md, date_str=f"{start_d} ~ {end_d}"),
            encoding="utf-8",
        )
        print(f"[monthly] HTML: {html_path}")
    except Exception as e:
        print(f"[monthly] HTML render failed: {e}", file=sys.stderr)
    return out_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["weekly", "monthly"], default="weekly", nargs="?")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    if args.mode == "weekly":
        write_weekly(args.days)
    else:
        write_monthly()
