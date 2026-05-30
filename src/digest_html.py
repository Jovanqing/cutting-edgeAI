"""Markdown digest → HTML 邮件版本。

特点：
- 内联 CSS（邮件客户端兼容性）
- Mermaid 块替换为 mermaid.ink 渲染的 SVG/PNG（邮件不支持 JS）
- 图片宽度自适应
- 表格响应式
"""
import re
import base64
import urllib.parse
from pathlib import Path
from typing import Optional

import markdown as md_lib

EMAIL_CSS = """
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    line-height: 1.65; color: #1a1a1a;
    max-width: 800px; margin: 0 auto;
    padding: 24px;
    background: #ffffff;
}
h1 { font-size: 28px; border-bottom: 3px solid #2563eb; padding-bottom: 12px; margin-top: 32px; }
h2 { font-size: 22px; color: #1e40af; border-left: 4px solid #3b82f6; padding-left: 12px; margin-top: 32px; }
h3 { font-size: 18px; color: #0f172a; margin-top: 24px; }
h4 { font-size: 16px; color: #334155; margin-top: 16px; }
p { margin: 8px 0; }
blockquote {
    border-left: 4px solid #94a3b8;
    margin: 12px 0; padding: 8px 16px;
    background: #f8fafc; color: #475569;
    border-radius: 0 6px 6px 0;
}
img { max-width: 100%; height: auto; border-radius: 8px; margin: 8px 0; }
table {
    border-collapse: collapse; width: 100%; margin: 12px 0;
    font-size: 14px;
}
th, td {
    border: 1px solid #e2e8f0; padding: 8px 12px; text-align: left;
}
th { background: #f1f5f9; font-weight: 600; }
tr:nth-child(even) { background: #f8fafc; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
code {
    background: #f1f5f9; padding: 2px 6px; border-radius: 4px;
    font-family: "SF Mono", Monaco, Consolas, monospace; font-size: 13px;
}
pre {
    background: #0f172a; color: #e2e8f0; padding: 16px;
    border-radius: 8px; overflow-x: auto;
}
hr { border: none; border-top: 1px solid #e2e8f0; margin: 24px 0; }
ul, ol { padding-left: 24px; }
li { margin: 4px 0; }
strong { color: #0f172a; }
"""


def _mermaid_to_image_url(mermaid_code: str) -> str:
    """把 Mermaid 源码用 mermaid.ink 渲染成 SVG URL（邮件可见的图片）。"""
    encoded = base64.urlsafe_b64encode(mermaid_code.encode("utf-8")).decode("ascii").rstrip("=")
    return f"https://mermaid.ink/img/{encoded}?type=png&theme=neutral"


def _replace_mermaid_blocks(markdown_text: str) -> str:
    """把 ```mermaid ... ``` 替换为 ![](mermaid.ink/...)。"""
    def repl(m):
        code = m.group(1).strip()
        url = _mermaid_to_image_url(code)
        return f"![diagram]({url})"
    return re.sub(r"```mermaid\s*\n(.*?)```", repl, markdown_text, flags=re.DOTALL)


def markdown_to_html(markdown_text: str, title: str = "AI 前沿日报") -> str:
    """Markdown 转 HTML 邮件版（含 Mermaid 渲染回退）。"""
    # 先把 mermaid 块转成图片 URL
    processed = _replace_mermaid_blocks(markdown_text)
    # 然后用 markdown 库转 HTML
    html_body = md_lib.markdown(
        processed,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{EMAIL_CSS}</style>
</head>
<body>
{html_body}
</body>
</html>"""


def write_html_for_today(md_path: Path) -> Optional[Path]:
    """读取今日 markdown digest，输出 HTML 版本到同目录。"""
    if not md_path.exists():
        return None
    md_text = md_path.read_text(encoding="utf-8")
    html_text = markdown_to_html(md_text, title=f"AI 前沿日报 {md_path.stem}")
    html_path = md_path.with_suffix(".html")
    html_path.write_text(html_text, encoding="utf-8")
    return html_path
