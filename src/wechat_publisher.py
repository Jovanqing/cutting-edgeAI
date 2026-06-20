"""微信公众号文章生成器。

从当日精选条目中，用 LLM 生成一篇可直接发布的公众号文章。

风格对标：量子位 / 机器之心 / 晚点LatePost / Founder Park
- 移动端优先，段落短、易读
- 每篇聚焦一个核心主题（来自当日头条 + 趋势）
- 标题党但不浮夸：有悬念、有数字、有具体主体
- 结构：钩子开头 → 核心事件 → 深度解读 → 行业影响 → 结尾金句
- 2000-2500 字，6-8 个小节
- 输出格式：微信公众号兼容 HTML（内联样式）+ Markdown 源文

输出路径：
  data/wechat/YYYY-MM-DD.html   可直接粘贴到公众号编辑器
  data/wechat/YYYY-MM-DD.md     本地存档
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from openai import OpenAI

from . import db

OUT_DIR = Path(__file__).parent.parent / "data" / "wechat"
DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

# ── 文章生成 Prompt ───────────────────────────────────────────────────────────

ARTICLE_PROMPT = """你是顶级中文科技自媒体编辑，服务于量子位 / 机器之心级别的公众号。

今天最重要的 AI 事件是：
标题：{title}
核心贡献：{contribution}
关键事实：{key_facts}
背景脉络：{background}
影响分析：{impact}
当日相关事件（同一方向的信号）：
{related_events}

请根据以上信息写一篇 2000-2500 字的公众号文章。

输出严格 JSON：
{{
  "headline": "<标题（15-25字）：有悬念/数字/具体主体，不用感叹号，不夸张>",
  "subtitle": "<副标题（20-30字）：补充说明，抓住读者>",
  "cover_abstract": "<封面摘要（50字以内）：放在图片下方的简介>",
  "sections": [
    {{
      "type": "hook",
      "content": "<开篇钩子（200-300字）：用一个具体场景或悬念开头，引出核心事件>"
    }},
    {{
      "type": "event",
      "title": "<小节标题>",
      "content": "<核心事件（300-400字）：发生了什么，谁做的，具体数字，时间节点>"
    }},
    {{
      "type": "deep_dive",
      "title": "<小节标题>",
      "content": "<深度解读（400-500字）：为什么重要，技术/商业逻辑，与历史对比>"
    }},
    {{
      "type": "impact",
      "title": "<小节标题>",
      "content": "<行业影响（300-400字）：对开发者/企业/投资人/普通用户分别意味着什么>"
    }},
    {{
      "type": "signals",
      "title": "<小节标题>",
      "content": "<行业信号（200-300字）：其他同日出现的相关事件，印证这一判断>"
    }},
    {{
      "type": "closing",
      "content": "<结尾（100-150字）：一个有力的判断或问题，引导读者思考和互动>"
    }}
  ],
  "tags": ["<话题标签1>", "<标签2>", "<标签3>"],
  "key_pullquotes": ["<金句1（适合大字展示）>", "<金句2>"]
}}

写作铁律（违反即重写）：
- 每段不超过 5 句话，移动端可读
- 至少 5 个具体数字/数据/时间节点
- 禁用：震惊、颠覆、王炸、里程碑、划时代、引爆
- 开头不能是"近日""随着""在人工智能快速发展的今天"
- 结尾必须有一个留给读者的问题或思考
- 语气：像一个聪明的朋友在跟你聊，不是新闻播报"""


# ── 微信 HTML 模板 ────────────────────────────────────────────────────────────

def _section_html(section: dict, pullquotes: list[str], idx: int) -> str:
    """把一个 section dict 渲染成微信兼容的 HTML。"""
    stype = section.get("type", "")
    title = section.get("title", "")
    content = section.get("content", "")

    # 段落化
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]

    # 每隔 2 段插入一条 pullquote
    pullquote = pullquotes[idx % len(pullquotes)] if pullquotes else ""

    out = []

    if title:
        out.append(
            f'<h2 style="font-size:18px;font-weight:700;color:#1a1a1a;'
            f'margin:28px 0 12px;padding-left:12px;'
            f'border-left:4px solid #576bff;line-height:1.4;">'
            f'{title}</h2>'
        )

    for i, para in enumerate(paragraphs):
        # 强调数字和专有名词（简单处理：数字加粗）
        para_html = re.sub(
            r'(\d+[\d.,]*\s*(?:%|倍|亿|万|TB|GB|亿元|美元|亿美元|参数|token|Token))',
            r'<strong style="color:#576bff;">\1</strong>',
            para
        )
        out.append(
            f'<p style="font-size:16px;line-height:1.85;color:#333;'
            f'margin:0 0 16px;text-align:justify;">{para_html}</p>'
        )
        # 插入 pullquote
        if i == 1 and pullquote and stype in ("deep_dive", "impact"):
            out.append(
                f'<blockquote style="margin:20px 0;padding:16px 20px;'
                f'background:#f5f6ff;border-left:4px solid #576bff;'
                f'border-radius:0 8px 8px 0;">'
                f'<p style="font-size:15px;color:#576bff;font-weight:600;'
                f'line-height:1.7;margin:0;">{pullquote}</p>'
                f'</blockquote>'
            )

    return "\n".join(out)


def _build_wechat_html(article: dict, cover_img: str = "", date_str: str = "") -> str:
    """把 LLM 输出的 article dict 渲染成微信公众号 HTML。"""
    headline = article.get("headline", "")
    subtitle = article.get("subtitle", "")
    cover_abstract = article.get("cover_abstract", "")
    sections = article.get("sections") or []
    tags = article.get("tags") or []
    pullquotes = article.get("key_pullquotes") or []

    if not date_str:
        date_str = datetime.now().strftime("%Y年%m月%d日")

    # 封面区
    cover_html = ""
    if cover_img:
        cover_html = (
            f'<img src="{cover_img}" style="width:100%;border-radius:8px;'
            f'display:block;margin-bottom:16px;" />'
        )

    # 标签
    tags_html = "".join(
        f'<span style="display:inline-block;font-size:12px;color:#576bff;'
        f'background:#eef0ff;padding:2px 10px;border-radius:20px;margin:0 6px 6px 0;">'
        f'#{t}</span>'
        for t in tags[:5]
    )

    # 正文 sections
    body_html = ""
    for i, sec in enumerate(sections):
        body_html += _section_html(sec, pullquotes, i) + "\n"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>{headline}</title>
</head>
<body style="margin:0;padding:0;background:#fff;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB',sans-serif;">
<article style="max-width:680px;margin:0 auto;padding:20px 16px 40px;">

  <!-- 封面图 -->
  {cover_html}

  <!-- 标题区 -->
  <h1 style="font-size:22px;font-weight:800;color:#1a1a1a;line-height:1.4;
             margin:0 0 10px;">{headline}</h1>
  <p style="font-size:15px;color:#666;margin:0 0 16px;line-height:1.6;">{subtitle}</p>

  <!-- 元信息 -->
  <div style="display:flex;align-items:center;justify-content:space-between;
              padding:10px 0;border-top:1px solid #eee;border-bottom:1px solid #eee;
              margin-bottom:24px;">
    <span style="font-size:13px;color:#999;">{date_str} &nbsp;·&nbsp; AI 前沿日报</span>
    <span style="font-size:13px;color:#576bff;font-weight:600;">AI / 深度</span>
  </div>

  <!-- 摘要 -->
  {f'<p style="font-size:15px;color:#555;background:#f8f9ff;padding:14px 16px;border-radius:8px;margin:0 0 24px;line-height:1.8;">{cover_abstract}</p>' if cover_abstract else ''}

  <!-- 正文 -->
  {body_html}

  <!-- 话题标签 -->
  <div style="margin-top:32px;padding-top:20px;border-top:1px solid #eee;">
    {tags_html}
  </div>

  <!-- 结尾署名 -->
  <div style="margin-top:28px;padding:16px;background:#f5f6ff;border-radius:8px;
              text-align:center;font-size:13px;color:#888;line-height:1.8;">
    本文由 AI 前沿日报自动整理生成<br>
    数据来源：Twitter / 微信公众号 / Reddit / YouTube / GitHub
  </div>

</article>
</body>
</html>"""


def _build_article_md(article: dict, date_str: str = "") -> str:
    """把 article dict 渲染成 Markdown 存档。"""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    headline = article.get("headline", "")
    subtitle = article.get("subtitle", "")
    sections = article.get("sections") or []
    tags = article.get("tags") or []
    pullquotes = article.get("key_pullquotes") or []

    lines = [
        f"# {headline}",
        "",
        f"> {subtitle}",
        "",
        f"*{date_str} · AI 前沿日报*",
        "",
        "---",
        "",
    ]
    if pullquotes:
        lines += [f"> **💬 {pullquotes[0]}**", ""]

    for sec in sections:
        title = sec.get("title", "")
        content = sec.get("content", "")
        if title:
            lines += [f"## {title}", ""]
        for para in content.split("\n"):
            if para.strip():
                lines += [para.strip(), ""]

    if tags:
        lines += ["", "---", "", f"话题：{' · '.join('#' + t for t in tags)}"]
    return "\n".join(lines)


# ── 主函数 ────────────────────────────────────────────────────────────────────

def generate_wechat_article(
    min_score: int = 6,
    hours: int = 48,
    model: str = "qwen3.7-max",
) -> tuple[Path, Path]:
    """生成今日微信公众号文章。

    Returns:
        (html_path, md_path)
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 取今日精选 top 20
    items = db.top_stratified(min_score=min_score, hours=hours,
                              quotas={"reddit": 8, "youtube": 4, "rss": 30})
    if not items:
        items = db.top_recent(min_score=min_score, hours=hours, limit=20)
    if not items:
        raise ValueError("No items found for today")

    top_sorted = sorted(items, key=lambda x: -(x.get("final_score") or 0))
    headline_item = top_sorted[0]

    # 2. 准备文章素材
    title = headline_item.get("title_cn") or headline_item.get("title", "")
    contribution = (headline_item.get("contribution") or "").strip()

    # key_facts 来自 LLM 已生成的 key_points
    kp_raw = headline_item.get("key_points") or "[]"
    try:
        kps = json.loads(kp_raw) if isinstance(kp_raw, str) else kp_raw
        key_facts = " · ".join(str(k) for k in kps[:5])
    except Exception:
        key_facts = str(kp_raw)[:200]

    # background / impact 用 story_writer 生成（如果当天已跑过，复用 DB 字段）
    background = (headline_item.get("summary") or "").strip()
    impact = (headline_item.get("why_matters") or "").strip()

    # 相关事件：同日 top 5（排除头条）
    related_items = [it for it in top_sorted[1:6]]
    related_lines = []
    for it in related_items:
        t = it.get("title_cn") or it.get("title", "")
        c = (it.get("contribution") or "")[:80]
        related_lines.append(f"- {t}：{c}")
    related_events = "\n".join(related_lines) if related_lines else "（无）"

    cover_img = headline_item.get("image_url") or ""

    # 3. LLM 写文章
    api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
    client = OpenAI(api_key=api_key, base_url=base_url)

    prompt = ARTICLE_PROMPT.format(
        title=title,
        contribution=contribution,
        key_facts=key_facts,
        background=background[:600],
        impact=impact[:400],
        related_events=related_events,
    )

    print(f"[wechat] writing article on: {title[:50]}...")
    resp = client.chat.completions.create(
        model=model,
        max_tokens=4000,
        temperature=0.55,
        timeout=120,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    raw = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    article = json.loads(raw)

    # 4. 渲染输出
    date_cn = datetime.now().strftime("%Y年%m月%d日")
    html_content = _build_wechat_html(article, cover_img=cover_img, date_str=date_cn)
    md_content = _build_article_md(article, date_str=today_str)

    html_path = OUT_DIR / f"{today_str}.html"
    md_path = OUT_DIR / f"{today_str}.md"
    html_path.write_text(html_content, encoding="utf-8")
    md_path.write_text(md_content, encoding="utf-8")

    print(f"[wechat] HTML: {html_path}")
    print(f"[wechat] MD:   {md_path}")
    print(f"[wechat] 标题：{article.get('headline', '')}")
    return html_path, md_path
