"""Email HTML Renderer — 把 Markdown 日报转成可直接发送的 HTML 邮件。

策略：
- 用 Python markdown 库解析（支持 tables/fenced_code/nl2br）
- 注入内联 CSS（Gmail/Outlook 兼容，max-width 640px）
- Mermaid 代码块 → mermaid.ink 图片 URL（无需 JS，在 HTML/邮件里直接显示图表）
- quickchart.io 词云图保留（外部 PNG，同样可在 HTML 里直接显示）
- 生成单个 .html 文件，可直接在浏览器打开或通过 SMTP 发送

使用：
  python -m src.main email            # 渲染今日 digest
  python -m src.main email 2026-05-15 # 渲染指定日期
"""
import base64
import re
from datetime import datetime
from pathlib import Path

import markdown as md_lib

OUT_DIR = Path(__file__).parent.parent / "data" / "digests"

# ─── Professional CSS ────────────────────────────────────────────────────────
_STYLE = """
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  background: #f0f2f8;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
               'Hiragino Sans GB', Arial, sans-serif;
  font-size: 15px; color: #1c1e2d; line-height: 1.72;
}
.wrapper {
  max-width: 700px; margin: 24px auto 40px;
  background: #ffffff; border-radius: 12px; overflow: hidden;
  box-shadow: 0 4px 24px rgba(20,20,60,0.10);
}

/* ── Header ── */
.site-header {
  background: linear-gradient(135deg, #0d0d2b 0%, #1a1060 50%, #0d1a3a 100%);
  padding: 32px 36px 28px; color: #fff; position: relative; overflow: hidden;
}
.site-header::before {
  content: ''; position: absolute; top: -60px; right: -60px;
  width: 220px; height: 220px;
  background: radial-gradient(circle, rgba(100,120,255,0.18) 0%, transparent 70%);
  border-radius: 50%;
}
.header-badge {
  display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
  text-transform: uppercase; color: #8899ff; background: rgba(100,120,255,0.15);
  border: 1px solid rgba(100,120,255,0.3); padding: 3px 10px; border-radius: 20px;
  margin-bottom: 10px;
}
.header-title { margin: 0 0 6px; font-size: 26px; font-weight: 800; line-height: 1.2; }
.header-headline {
  font-size: 14px; color: #c8d0f0; margin: 8px 0 0;
  border-left: 3px solid #6478ff; padding-left: 10px;
  font-style: italic;
}
.header-meta { margin-top: 16px; font-size: 12px; color: #8090b8; }

/* ── Body ── */
.body { padding: 28px 36px; }

/* ── Section headers ── */
h2 {
  font-size: 16px; font-weight: 800; color: #0d0d2b;
  display: flex; align-items: center; gap: 8px;
  margin: 36px 0 16px; padding: 10px 16px;
  background: #f4f5ff; border-radius: 8px;
  border-left: 4px solid #5b5ef4;
}
h3 {
  font-size: 15px; font-weight: 700; color: #1c1e2d;
  margin: 22px 0 8px; padding-bottom: 4px;
  border-bottom: 1px solid #eeeeff;
}
h4 { font-size: 14px; font-weight: 600; color: #3a3d60; margin: 14px 0 6px; }

/* ── Text ── */
p { margin: 0 0 12px; }
a { color: #4a5cf0; text-decoration: none; font-weight: 500; }
a:hover { text-decoration: underline; color: #3347e0; }
strong { color: #0d0d2b; }
ul, ol { margin: 8px 0 14px; padding-left: 22px; }
li { margin-bottom: 7px; line-height: 1.6; }

/* ── Blockquotes ── */
blockquote {
  border-left: 4px solid #6478ff; margin: 14px 0;
  padding: 10px 18px; background: #f5f6ff;
  border-radius: 0 8px 8px 0; color: #3a4070;
}
blockquote p { margin: 0; }

/* ── Code ── */
code {
  font-family: 'SF Mono', 'Fira Code', Consolas, monospace;
  background: #f0f0ff; padding: 2px 6px; border-radius: 4px;
  font-size: 12.5px; color: #c0357a;
}
pre {
  background: #141428; color: #e2e8f0; padding: 16px 18px;
  border-radius: 8px; overflow-x: auto; font-size: 13px;
  line-height: 1.55; margin: 14px 0;
}
pre code { background: none; color: inherit; padding: 0; font-size: inherit; }

/* ── Tables ── */
table {
  border-collapse: collapse; width: 100%; margin: 14px 0;
  font-size: 14px; border-radius: 8px; overflow: hidden;
  border: 1px solid #e8eaf6;
}
th {
  background: #f0f1fc; color: #2d3060; font-weight: 700;
  padding: 10px 14px; border-bottom: 2px solid #d8daee;
  text-align: left; font-size: 13px;
}
td {
  padding: 9px 14px; border-bottom: 1px solid #edeef8;
  vertical-align: top;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f8f9ff; }

/* ── Images ── */
img {
  max-width: 100%; height: auto; display: block;
  border-radius: 8px; margin: 10px 0;
}
.img-avatar {
  width: 32px; height: 32px; border-radius: 50%;
  display: inline-block; vertical-align: middle; margin: 0;
  border: 2px solid #e8eaf6;
}
.mermaid-chart {
  max-width: 100%; display: block; margin: 14px auto;
  border-radius: 8px; border: 1px solid #e8eaf6;
}

/* ── HR ── */
hr {
  border: none;
  border-top: 1px solid #edeef8;
  margin: 28px 0;
}

/* ── Score / badge tags ── */
.score-tag {
  display: inline-block; font-size: 11px; color: #6478ff;
  background: #eef0ff; padding: 1px 7px; border-radius: 20px;
  font-family: monospace; font-weight: 600; white-space: nowrap;
}

/* ── Headline card ── */
.headline-card {
  background: linear-gradient(135deg, #f5f6ff 0%, #eff0ff 100%);
  border: 1px solid #d8daee; border-radius: 10px;
  padding: 20px 22px; margin: 16px 0;
}
.headline-card h3 { border-bottom: none; margin-top: 0; color: #1a1060; }

/* ── Trend cards ── */
.trend-meta {
  display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 6px;
  font-size: 13px;
}
.trend-tag {
  background: #f0f1fc; color: #3a3d80; padding: 2px 10px;
  border-radius: 20px; font-size: 12px; font-weight: 600;
}

/* ── Footer ── */
.site-footer {
  background: #f4f5ff; padding: 18px 36px;
  border-top: 1px solid #e4e6f8; text-align: center;
  color: #8090b8; font-size: 12px; line-height: 1.8;
}

/* ── 表格横向滚动容器 ── */
.table-scroll {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  margin: 14px 0;
  border-radius: 8px;
}
.table-scroll table { margin: 0; min-width: 420px; }

/* ── 移动端响应式 ── */
@media (max-width: 640px) {
  .wrapper {
    margin: 0;
    border-radius: 0;
    box-shadow: none;
  }
  .site-header {
    padding: 20px 16px 16px;
  }
  .header-title { font-size: 20px; }
  .header-headline { font-size: 13px; }
  .body {
    padding: 16px 14px;
  }
  .site-footer {
    padding: 14px 16px;
  }
  h2 {
    font-size: 14px;
    padding: 8px 12px;
    margin: 24px 0 12px;
  }
  h3 { font-size: 14px; }
  h4 { font-size: 13px; }
  p, li { font-size: 14px; }
  th { font-size: 12px; padding: 8px 10px; }
  td { font-size: 13px; padding: 7px 10px; }
  blockquote { padding: 8px 12px; font-size: 13px; }
  pre { font-size: 12px; padding: 12px; }
}
"""

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<div class="wrapper">
  <div class="site-header">
    <div class="header-badge">AI Daily · {date_str}</div>
    <h1 class="header-title">🤖 AI 前沿日报</h1>
    {headline_block}
    <div class="header-meta">每日精选 · 深度分析 · 趋势洞察</div>
  </div>
  <div class="body">
    {body}
  </div>
  <div class="site-footer">
    {footer}
  </div>
</div>
</body>
</html>
"""


def _mermaid_to_ink_url(diagram_def: str) -> str:
    """把 Mermaid 图表定义编码成 mermaid.ink 图片 URL（服务端渲染，无需 JS）。"""
    encoded = base64.urlsafe_b64encode(diagram_def.encode("utf-8")).decode("ascii")
    return f"https://mermaid.ink/img/{encoded}"


def _preprocess_md(text: str) -> str:
    """预处理 Markdown：把 Mermaid 代码块转成 mermaid.ink 图片（可在 HTML 里直接显示）。"""
    chart_labels = {
        "timeline": "今日事件时间线",
        "xychart-beta": "今日主题热度分布",
    }

    def replace_mermaid(m):
        diagram_def = m.group(1)
        first_line = diagram_def.strip().split("\n")[0].strip()
        label = chart_labels.get(first_line, "AI 数据图表")
        img_url = _mermaid_to_ink_url(diagram_def)
        # 返回 Markdown 图片语法，markdown 库会转成 <img> 标签
        return f"\n![{label}]({img_url})\n"

    text = re.sub(
        r"```mermaid\n(.*?)```",
        replace_mermaid,
        text,
        flags=re.DOTALL,
    )
    return text


def _postprocess_html(html: str) -> str:
    """后处理 HTML：给头像 img 加 class，给 mermaid 图表加样式。"""
    # mermaid.ink 图表加 class
    html = re.sub(
        r'(<img\s[^>]*mermaid\.ink[^>]*>)',
        lambda m: m.group(1).replace('<img ', '<img class="mermaid-chart" '),
        html,
    )
    # 把表格里的头像图片加 .img-avatar class
    html = re.sub(
        r'(<img\s[^>]*unavatar\.io[^>]*>)',
        lambda m: m.group(1).replace('<img ', '<img class="img-avatar" '),
        html,
    )
    html = re.sub(
        r'(<img\s[^>]*wx\.qlogo\.cn[^>]*>)',
        lambda m: m.group(1).replace('<img ', '<img class="img-avatar" '),
        html,
    )
    # code 里的 score tag 加样式
    html = re.sub(
        r'<code>([NIS\d ]+)</code>',
        r'<span class="score-tag">\1</span>',
        html,
    )
    # 所有表格包裹横向滚动容器（移动端防溢出）
    html = html.replace('<table>', '<div class="table-scroll"><table>')
    html = html.replace('</table>', '</table></div>')
    return html


def md_to_html_email(
    markdown_text: str,
    date_str: str = "",
    footer: str = "",
) -> str:
    """把 Markdown 日报文本转换成 HTML。"""
    # 提取 header 中的今日导语（> **...** 形式）
    headline_block = ""
    m = re.search(r'^>\s*\*\*(.+?)\*\*', markdown_text, re.MULTILINE)
    if m:
        headline_block = (
            f'<div class="header-headline">{m.group(1)}</div>'
        )

    preprocessed = _preprocess_md(markdown_text)
    # 去掉 H1 标题和导语行（已移至 header）
    preprocessed = re.sub(r'^# .+\n', '', preprocessed, count=1)
    preprocessed = re.sub(r'^> \*\*.+\*\*\n', '', preprocessed, count=1, flags=re.MULTILINE)

    body_html = md_lib.markdown(
        preprocessed,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )
    body_html = _postprocess_html(body_html)

    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    if not footer:
        footer = (
            f"🤖 AI Inspiration Pipeline &nbsp;·&nbsp; "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}&nbsp;·&nbsp;"
            "Powered by Qwen-Max + Qwen3.6-Plus"
        )

    title = f"AI 前沿日报 {date_str}"
    return _HTML_TEMPLATE.format(
        title=title,
        style=_STYLE,
        date_str=date_str,
        headline_block=headline_block,
        body=body_html,
        footer=footer,
    )


def render_digest_as_email(date_str: str = "") -> Path:
    """读取指定日期（默认今日）的 .md 日报，渲染成 HTML 邮件文件。

    Returns:
        Path to the generated .html file.
    Raises:
        FileNotFoundError if the .md digest doesn't exist.
    """
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    md_path = OUT_DIR / f"{date_str}.md"
    if not md_path.exists():
        raise FileNotFoundError(f"Digest not found: {md_path}")

    markdown_text = md_path.read_text(encoding="utf-8")
    html = md_to_html_email(markdown_text, date_str=date_str)

    html_path = OUT_DIR / f"{date_str}.html"
    html_path.write_text(html, encoding="utf-8")
    return html_path
