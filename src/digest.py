"""Generate v3.0 professional Markdown daily digest.

Target: 媲美 Stratechery / The Information AI / Latent Space 的商业日报。

Structure:
  1. 📰 头部：标题 + Headline + 日期
  2. 📊 今日态势：速读 30 秒（统计 + 1 句总览 + Top 5 事件）
  3. 🔥 头条故事：今日 #1 深度报道（含封面图、5W1H、业内反应、关注什么）
  4. ⚡ 趋势聚焦：4 大跨源共振主题，每个深度展开
  5. 📌 重点新闻（按主题）：用 cluster_id 分组
  6. 🌐 渠道速览：🐦 推特 / 💬 微信 / 💻 GitHub / 🎬 视频
  7. 📡 信号扫描：剩下条目的紧凑列表

数据契约：依赖 LLM 已经生成的字段（title_cn / contribution / why_matters /
key_points / ideas / cluster_id），不增加额外 LLM 调用。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter, defaultdict
from typing import Optional

from . import db
from .og_fetcher import enrich_items_with_images
from .story_writer import StoryWriter, find_related_tweets
from .trend_expander import TrendExpander

OUT_DIR = Path(__file__).parent.parent / "data" / "digests"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

CHANNEL_EMOJI = {
    "推特": "🐦",
    "微信": "💬",
    "YouTube": "🎬",
    "Reddit": "🟠",
    "Bluesky": "🦋",
    "官方/博客/Newsletter": "📄",
}


# ─────────────────────────── helpers ───────────────────────────

def _channel_of(it: dict) -> str:
    """把内部 source + sub_source 映射成用户视角的'逻辑渠道'。"""
    sub = (it.get("sub_source") or "")
    src = it.get("source", "")
    if sub.startswith("X/"): return "推特"
    if sub.startswith("WX/"): return "微信"
    if sub.startswith("BSky/"): return "Bluesky"
    if sub.startswith("YT/") or src == "youtube": return "YouTube"
    if src == "reddit": return "Reddit"
    return "官方/博客/Newsletter"


def _thumbnail_url(it: dict) -> Optional[str]:
    """优先用 DB cache 的 image_url；其次用已知 URL 模式构造缩略图（含微信 logo / X 头像）。"""
    cached = it.get("image_url")
    if cached:  # 非空字符串、非 None
        return cached
    # 用 og_fetcher 的统一逻辑（覆盖 YouTube / GitHub / 微信 / X / Bluesky）
    from .og_fetcher import fetch_image_for_url
    url = it.get("url", "") or ""
    if not url:
        return None
    # 只走构造逻辑，不发 HTTP（HTTP 抓取在 enrich_items_with_images 阶段做了）
    from .og_fetcher import _construct_known_thumb
    return _construct_known_thumb(url, it.get("sub_source", ""))


def _parse_ideas(ideas_text: str) -> list[str]:
    """ideas 字段：可能是 JSON list / Python repr / bullet text，归一化为字符串列表。"""
    if not ideas_text:
        return []
    s = ideas_text.strip()
    if s.startswith("["):
        try:
            val = json.loads(s)
            if isinstance(val, list):
                return [str(x).strip().lstrip("- ") for x in val if str(x).strip()]
        except Exception:
            pass
        import ast
        try:
            val = ast.literal_eval(s)
            if isinstance(val, list):
                return [str(x).strip().lstrip("- ") for x in val if str(x).strip()]
        except Exception:
            pass
    return [ln.strip().lstrip("- ").strip() for ln in s.split("\n") if ln.strip()]


def _parse_key_points(kp_raw) -> list[str]:
    if not kp_raw:
        return []
    try:
        val = json.loads(kp_raw) if isinstance(kp_raw, str) else kp_raw
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    return [s.strip() for s in str(kp_raw).split("\n") if s.strip()]


def _short_score_tag(it: dict) -> str:
    """生成像 'N9 I10 S8' 这种紧凑的评分标记。"""
    n = it.get("novelty"); i = it.get("impact"); s = it.get("signal_score")
    parts = []
    if n is not None: parts.append(f"N{n}")
    if i is not None: parts.append(f"I{i}")
    if s is not None: parts.append(f"S{s}")
    return " ".join(parts) if parts else ""


def _strip_rt_prefix(text: str) -> str:
    """X 推文里 RT 转推的前缀去掉，保留内容主体。"""
    if not text: return ""
    m = re.match(r"^RT\s+[^:]+:\s*", text)
    return m.string[m.end():] if m else text


def _parse_pub_datetime(s: str) -> Optional[datetime]:
    """解析 published_at（可能是 RFC 2822 / ISO 8601 / unix timestamp）。"""
    if not s:
        return None
    s = s.strip()
    # Unix timestamp
    try:
        return datetime.fromtimestamp(float(s))
    except (ValueError, TypeError):
        pass
    # ISO 8601
    for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue
    # RFC 2822 (e.g. "Thu, 14 May 2026 12:11:00 GMT")
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).replace(tzinfo=None)
    except Exception:
        return None


def _escape_mermaid(text: str) -> str:
    """Mermaid 不允许某些字符。"""
    return (text.replace(":", "：").replace("#", "")
                .replace("(", "（").replace(")", "）")
                .replace('"', "'").replace("\n", " "))


# ─────────────────────────── sections ───────────────────────────

def _clean_source_label(it: dict) -> str:
    """把 sub_source 转成适合图表展示的简短标签。"""
    sub = it.get("sub_source") or ""
    src = it.get("source", "")
    if sub.startswith("WX/"): return sub[3:]
    if sub.startswith("YT/"): return sub[3:]
    if sub.startswith("X/"): return "@" + sub[2:]
    if sub: return sub
    return src


def _render_topic_chart(items: list[dict]) -> str:
    """📊 今日主题热度 — quickchart.io 横向柱状图。

    若聚类质量差（>60% 条目无命名 cluster），自动切换为信息源分布图。
    """
    import json, urllib.parse
    cluster_counter = Counter()
    for it in items:
        cluster = (it.get("cluster_id") or "其他").strip()
        if cluster:
            cluster_counter[cluster] += 1

    total_items = len(items)
    other_count = cluster_counter.get("其他", 0)
    use_cluster = (other_count / max(total_items, 1)) < 0.6 and len(cluster_counter) >= 3

    if use_cluster:
        top8 = [(t, c) for t, c in cluster_counter.most_common(8) if t and c > 0]
        chart_title_suffix = "AI 主题热度"
    else:
        # 信息源分布（按 sub_source/source 统计）
        src_counter = Counter()
        for it in items:
            label = _clean_source_label(it)
            if label:
                src_counter[label] += 1
        top8 = [(t[:18], c) for t, c in src_counter.most_common(8) if t and c >= 2]
        chart_title_suffix = "信息来源分布"

    if len(top8) < 3:
        return ""

    labels = [t for t, _ in top8]
    values = [c for _, c in top8]
    total = sum(values)

    # Chart.js 横向柱状图：indexAxis="y" 让标签显示在左侧，不重叠
    config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [{
                "data": values,
                "backgroundColor": [
                    "rgba(91,94,244,0.75)", "rgba(99,179,237,0.75)",
                    "rgba(104,211,145,0.75)", "rgba(246,173,85,0.75)",
                    "rgba(252,129,129,0.75)", "rgba(183,148,246,0.75)",
                    "rgba(246,224,94,0.75)", "rgba(129,230,217,0.75)",
                ],
                "borderWidth": 0,
            }],
        },
        "options": {
            "indexAxis": "y",
            "plugins": {
                "title": {
                    "display": True,
                    "text": f"今日{chart_title_suffix}（共 {total} 条精选）",
                    "font": {"size": 14, "weight": "bold"},
                },
                "legend": {"display": False},
            },
            "scales": {
                "x": {
                    "beginAtZero": True,
                    "title": {"display": True, "text": "条目数"},
                    "ticks": {"stepSize": 1},
                },
                "y": {
                    "ticks": {"font": {"size": 13}},
                },
            },
        },
    }
    c_str = urllib.parse.quote(json.dumps(config, ensure_ascii=False))
    height = max(220, len(top8) * 42)
    url = f"https://quickchart.io/chart?c={c_str}&w=620&h={height}&bkg=white"
    return f"## 📊 今日主题热度\n\n![今日 AI 主题热度分布]({url})\n\n"


def _render_wordcloud(items: list[dict]) -> str:
    """☁️ 今日词云 — 用 quickchart.io 渲染词频图（PNG）。"""
    import urllib.parse
    # 停用词
    stop_en = {"with", "this", "that", "from", "have", "will", "into", "their",
               "than", "more", "your", "when", "what", "been", "they", "AI", "ai"}
    stop_zh = {"我们", "可以", "这个", "通过", "如何", "什么", "提供", "实现", "使用",
               "支持", "包括", "以及", "用于", "目前", "已经", "中文", "英文"}
    tokens: list[str] = []
    for it in items:
        text = ((it.get("title_cn") or "") + " " +
                (it.get("title") or "") + " " +
                (it.get("summary") or ""))
        for m in re.finditer(r'[A-Za-z][A-Za-z0-9_]{2,}|[一-龥]{2,}', text):
            t = m.group(0)
            if t.lower() in stop_en or t in stop_zh:
                continue
            if len(t) >= 3 if t[0].isascii() else len(t) >= 2:
                tokens.append(t)
    cnt = Counter(tokens).most_common(60)
    if len(cnt) < 10:
        return ""
    # quickchart wordcloud：text=word1,word2,word3（直接给词列表，按出现次数加权 → 用 repeat）
    weighted = []
    for w, c in cnt:
        # 取 log 加权避免单词重复太多
        import math
        for _ in range(min(max(1, int(math.log2(c + 1))), 8)):
            weighted.append(w)
    text_param = " ".join(weighted)
    if len(text_param) > 3000:
        text_param = text_param[:3000]
    qs = urllib.parse.urlencode({
        "text": text_param,
        "fontFamily": "sans-serif",
        "width": 800,
        "height": 400,
        "scale": "log",
        "removeStopwords": "false",
        "language": "zh-CN",
    })
    url = f"https://quickchart.io/wordcloud?{qs}"
    return f"## ☁️ 今日词云\n\n![今日 AI 关键词词云]({url})\n\n---\n\n"


def _render_timeline(items: list[dict]) -> str:
    """📅 今日事件线 — 按发布时间排列的表格，分时段展示重要事件。"""
    timed = []
    cutoff = datetime.now() - timedelta(hours=36)
    for it in items:
        dt = _parse_pub_datetime(it.get("published_at") or "")
        if not dt or dt < cutoff:
            continue
        timed.append((dt, it.get("final_score") or 0, it))
    if len(timed) < 4:
        return ""

    # top 14 by score，再按时间升序
    timed.sort(key=lambda x: -x[1])
    top = timed[:14]
    top.sort(key=lambda x: x[0])

    slot_emoji = {0: "🌅", 6: "☀️", 12: "🌆", 18: "🌙"}

    def _slot(h: int) -> str:
        if h < 6: return "🌅 凌晨"
        if h < 12: return "☀️ 上午"
        if h < 18: return "🌆 下午"
        return "🌙 晚上"

    slot_icon = {0: "🌅", 6: "☀️", 12: "🌆", 18: "🌙"}

    def _slot_icon(h: int) -> str:
        if h < 6: return "🌅"
        if h < 12: return "☀️"
        if h < 18: return "🌆"
        return "🌙"

    out = [
        "## 📅 今日事件线",
        "",
        "| 时间 | 渠道 | 事件 |",
        "|:---:|:---:|:---|",
    ]
    for dt, _, it in top:
        time_str = dt.strftime("%H:%M")
        icon = _slot_icon(dt.hour)
        ch = _channel_of(it)
        ch_em = CHANNEL_EMOJI.get(ch, "")
        title = (it.get("title_cn") or it.get("title") or "").strip()
        if len(title) > 38:
            title = title[:38] + "…"
        url = it.get("url", "")
        out.append(f"| {icon} `{time_str}` | {ch_em} {ch} | [{title}]({url}) |")

    out += ["", "---", ""]
    return "\n".join(out)


def _render_header(date_str: str, weekday: str, headline: str) -> str:
    lines = [
        f"# 🤖 AI 前沿日报",
        "",
    ]
    if headline:
        lines += [f"> **{headline}**", ""]
    lines += [f"📅 **{date_str}** · {weekday}", "", "---", ""]
    return "\n".join(lines)


def _render_at_a_glance(items: list[dict], total_collected: int, total_analyzed: int,
                       core_trends: list[dict], headline_item: Optional[dict]) -> str:
    """📊 今日态势 — 速读卡片，包含统计 + Top 5 事件（排除头条避免重复）。"""
    channels = Counter(_channel_of(it) for it in items)
    channel_line = "  ·  ".join(
        f"{CHANNEL_EMOJI.get(k,'')} **{k}** {v}" for k, v in channels.most_common()
    )

    headline_id = headline_item["id"] if headline_item else None
    top10 = [it for it in sorted(items, key=lambda x: x.get("final_score") or 0, reverse=True)
             if it.get("id") != headline_id][:10]

    today_analyzed = total_analyzed
    db_total = db.count_collected()
    # 入选率 = 精选 / 今日已评分条目；优先用传入值，fallback 到 DB 实时查询
    scored_today = total_analyzed if total_analyzed > 0 else db.count_today_analyzed()
    selection_rate = len(items) * 100 / max(scored_today, 1)
    lines = [
        "## 📊 今日态势 · 速读 30 秒",
        "",
        f"| 今日分析 | 精选入选 | 入选率 | 库存总量 |",
        f"|:---:|:---:|:---:|:---:|",
        f"| {today_analyzed:,} | **{len(items)}** | {selection_rate:.1f}% | {db_total:,} |",
        "",
        f"**来源分布**：{channel_line}",
        "",
        "**今日要事 Top 10**（除头条外）：",
        "",
    ]
    for i, it in enumerate(top10, 1):
        title = it.get("title_cn") or it.get("title", "")
        ch = _channel_of(it)
        emoji = CHANNEL_EMOJI.get(ch, "")
        sub = it.get("sub_source", "")
        url = it.get("url", "")
        lines.append(f"{i}. {emoji} **[{title}]({url})** — *{sub}*")
    lines += ["", "---", ""]
    return "\n".join(lines)


def _render_headline_story(top: dict, story: Optional[dict] = None) -> str:
    """🔥 头条故事 — 当日最高分条目，深度商业报道格式。

    如果传入 story（LLM 二次加工的深度报道结果），用 story 字段；
    否则 fallback 到原 item 字段。
    """
    if not top:
        return ""
    title_cn = top.get("title_cn") or top.get("title", "")
    title_en = top.get("title", "")
    url = top.get("url", "")
    sub = top.get("sub_source", "")
    channel = _channel_of(top)
    emoji = CHANNEL_EMOJI.get(channel, "")
    thumb = _thumbnail_url(top)
    score_tag = _short_score_tag(top)
    pub = (top.get("published_at") or "")[:10]

    lines = [
        "## 🔥 头条故事 · 今日最重磅",
        "",
    ]
    if thumb:
        lines += [f"[![{title_cn}]({thumb})]({url})", ""]
    lines += [
        f"### {title_cn}",
        "",
        f"> *{title_en}*",
        "",
        f"{emoji} **{channel}** · {sub}  ·  📅 {pub}  ·  `{score_tag}`",
        "",
    ]

    if story:
        # LLM 深度报道字段
        lead = (story.get("lead") or "").strip()
        background = (story.get("background") or "").strip()
        key_facts = story.get("key_facts") or []
        impact = (story.get("impact") or "").strip()
        reactions = story.get("reactions") or []
        what_to_watch = story.get("what_to_watch") or []

        if lead:
            lines += [f"**📌 一句话讲清**", "", f"**{lead}**", ""]
        if background:
            lines += ["**📖 背景脉络**", "", background, ""]
        if key_facts:
            lines += ["**🔑 关键事实**", ""]
            for kf in key_facts[:5]:
                lines.append(f"- {str(kf).strip()}")
            lines.append("")
        if impact:
            lines += ["**🧠 影响分析**", "", f"> {impact}", ""]
        # 优先用真实推文引用（不是 LLM 编的）
        real_quotes = story.get("real_quotes") or []
        if real_quotes:
            lines += ["**🗣 业内反应 · 来自推特真实推文**", ""]
            for q in real_quotes[:4]:
                handle = q.get("handle", "")
                text = q.get("text", "")
                qurl = q.get("url", "")
                lines.append(f"> *\"{text}\"* — [@{handle}]({qurl})")
                lines.append("")
        elif reactions:
            lines += ["**🗣 业内反应 · 跨源信号**", ""]
            for r in reactions[:5]:
                lines.append(f"- {str(r).strip()}")
            lines.append("")
        if what_to_watch:
            lines += ["**🔭 接下来关注什么**", ""]
            for w in what_to_watch[:4]:
                lines.append(f"- {str(w).strip()}")
            lines.append("")
    else:
        # Fallback：用原 item 字段
        contribution = (top.get("contribution") or "").strip()
        why = (top.get("why_matters") or "").strip()
        summary = (top.get("summary") or "").strip()
        key_points = _parse_key_points(top.get("key_points"))
        ideas = _parse_ideas(top.get("ideas") or "")
        if contribution:
            lines += ["**📌 一句话讲清**", "", f"{contribution}", ""]
        if summary:
            lines += ["**📖 事件回顾**", "", f"{summary}", ""]
        if key_points:
            lines += ["**🔑 关键事实**", ""]
            for kp in key_points[:5]:
                lines.append(f"- {kp}")
            lines.append("")
        if why:
            lines += ["**🧠 Why it matters**", "", f"> {why}", ""]
        if ideas:
            lines += ["**🚀 可探索方向**", ""]
            for idea in ideas[:3]:
                lines.append(f"- {idea}")
            lines.append("")

    lines += [f"🔗 **原文链接**：[阅读全文 →]({url})", "", "---", ""]
    return "\n".join(lines)


def _render_top3_stories(top3_items: list[dict], top3_stories: dict) -> str:
    """🥈🥉 #2 / #3 深度报道 — 精简版（lead + key_facts + what_to_watch）。"""
    secondary = top3_items[1:3] if len(top3_items) >= 2 else []
    if not secondary:
        return ""
    medals = ["🥈", "🥉"]
    lines = ["## 📰 深度关注 · #2 / #3 重磅", ""]
    for medal, it in zip(medals, secondary):
        story = top3_stories.get(it.get("id"))
        title_cn = it.get("title_cn") or it.get("title", "")
        url = it.get("url", "")
        sub = it.get("sub_source", "")
        ch = _channel_of(it)
        ch_em = CHANNEL_EMOJI.get(ch, "")
        thumb = _thumbnail_url(it)
        pub = (it.get("published_at") or "")[:10]
        score_tag = _short_score_tag(it)

        lines += [f"### {medal} {title_cn}", ""]
        if thumb:
            lines += [f"[![{title_cn}]({thumb})]({url})", ""]
        lines += [f"{ch_em} *{sub}*  ·  📅 {pub}  ·  `{score_tag}`", ""]

        if story:
            lead = (story.get("lead") or "").strip()
            key_facts = story.get("key_facts") or []
            impact = (story.get("impact") or "").strip()
            what_to_watch = story.get("what_to_watch") or []
            if lead:
                lines += [f"> **{lead}**", ""]
            if key_facts:
                lines += ["**🔑 关键事实**", ""]
                for kf in key_facts[:4]:
                    lines.append(f"- {str(kf).strip()}")
                lines.append("")
            if impact:
                lines += [f"**🧠 影响**：{impact[:200]}", ""]
            if what_to_watch:
                lines += ["**🔭 接下来关注**：" + " · ".join(str(w)[:60] for w in what_to_watch[:3]), ""]
        else:
            # fallback 到现有字段
            contribution = (it.get("contribution") or it.get("summary") or "").strip()
            why = (it.get("why_matters") or "").strip()
            key_points = _parse_key_points(it.get("key_points"))
            if contribution:
                lines += [contribution, ""]
            if key_points:
                for kp in key_points[:4]:
                    lines.append(f"- {kp}")
                lines.append("")
            if why:
                lines += [f"> 💡 {why}", ""]

        lines += [f"🔗 [阅读全文 →]({url})", "", ""]
    lines += ["---", ""]
    return "\n".join(lines)


def _render_trend_focus(core_trends: list[dict], items: list[dict]) -> str:
    """⚡ 趋势聚焦 — 4 个 trend 深度展开。

    优先用 TrendExpander 加工后的 deep_analysis / drivers / key_players / winner_loser；
    fallback 到原始 description + signals。
    """
    if not core_trends:
        return ""
    lines = ["## ⚡ 趋势聚焦 · 4 大主线深度分析", "",
             "*跨多个渠道同时浮现的关键转变 ——*", ""]
    for i, trend in enumerate(core_trends, 1):
        title = trend.get("title", "").strip()
        desc = trend.get("description", "").strip()
        signals = trend.get("signals", [])

        # 优先用扩展后的字段
        deep_analysis = (trend.get("deep_analysis") or "").strip()
        drivers = trend.get("drivers") or []
        key_players = trend.get("key_players") or []
        winner_loser = (trend.get("winner_loser") or "").strip()

        lines += [f"### {i}️⃣ {title}", ""]
        if deep_analysis:
            # 用深度分析替代简短 description
            lines += [deep_analysis, ""]
        elif desc:
            lines += [desc, ""]

        evidence = trend.get("evidence_signals") or []
        contrarian = (trend.get("contrarian_view") or "").strip()

        meta_parts = []
        if drivers:
            meta_parts.append(f"**🚀 驱动力**：{' · '.join(drivers[:3])}")
        if key_players:
            meta_parts.append(f"**👥 关键玩家**：{' · '.join(key_players[:5])}")
        if winner_loser:
            meta_parts.append(f"**⚖️ 受益 / 承压**：{winner_loser}")
        if meta_parts:
            lines += meta_parts + [""]

        if evidence:
            lines += ["**📎 证据信号**："]
            for ev in evidence[:3]:
                lines.append(f"- {ev}")
            lines.append("")

        if contrarian:
            lines += [f"> ⚠️ **反向视角**：{contrarian}", ""]

        if signals and not evidence:
            lines.append("**📡 共振信号**：")
            for sig in signals[:4]:
                lines.append(f"- {sig}")
            lines.append("")
    lines += ["---", ""]
    return "\n".join(lines)


def _render_by_theme(items: list[dict], breakthrough_id: Optional[str] = None,
                     shown_ids: Optional[set] = None) -> str:
    """📌 按主题分组（cluster_id），每个主题列条目摘要。

    把展示了的 item id 累加到 shown_ids（caller 传进来的 set），方便
    后续 _render_signal_scan 知道哪些已展示、哪些是真正剩余。
    """
    if shown_ids is None:
        shown_ids = set()
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        if it.get("id") in shown_ids:
            continue  # 已在头条 / 深度关注 等区块展示，跳过
        theme = (it.get("cluster_id") or "其他值得关注").strip()
        by_theme[theme].append(it)

    # 排序：按主题内最高分倒序展示主题；"其他值得关注"放最后
    def theme_max(items_):
        return max((x.get("final_score") or 0) for x in items_)

    sorted_themes = sorted(
        ((t, lst) for t, lst in by_theme.items() if t != "其他值得关注"),
        key=lambda x: -theme_max(x[1])
    )
    if "其他值得关注" in by_theme:
        sorted_themes.append(("其他值得关注", by_theme["其他值得关注"]))

    if not sorted_themes:
        return ""

    lines = ["## 📌 重点新闻 · 按主题", ""]
    theme_emojis = {
        "AI代理与多智能体系统": "🤖",
        "大语言模型与工具调用": "🛠",
        "大语言模型与工具集成": "🛠",
        "AI基础设施与硬件革新": "🏗",
        "计算架构与硬件优化": "🏗",
        "动态基准与评估方法": "📊",
        "基准测试与评估框架": "📊",
        "记忆与上下文管理": "🧠",
        "开源工具与框架": "🔧",
        "垂直领域AI应用": "🎯",
        "语音与视频生成技术": "🎬",
        "多模态推理突破": "👁",
        "安全与治理": "🛡",
        "其他值得关注": "💡",
    }
    for theme, theme_items in sorted_themes:
        emoji = theme_emojis.get(theme, "▪️")
        theme_items.sort(key=lambda x: -(x.get("final_score") or 0))
        top_n = theme_items[:6] if theme != "其他值得关注" else theme_items[:4]
        lines += [f"### {emoji} {theme}  *({len(theme_items)} 条 · 显示 {len(top_n)})*", ""]
        for i, it in enumerate(top_n):
            shown_ids.add(it.get("id"))
            title_cn = (it.get("title_cn") or it.get("title") or "").strip()
            url = it.get("url", "")
            ch = _channel_of(it)
            ch_em = CHANNEL_EMOJI.get(ch, "")
            sub = it.get("sub_source", "")
            contribution = (it.get("contribution") or it.get("summary") or "").strip()
            why = (it.get("why_matters") or "").strip()
            key_points = _parse_key_points(it.get("key_points"))
            score_tag = _short_score_tag(it)
            if len(contribution) > 300:
                contribution = contribution[:300] + "…"
            thumb = _thumbnail_url(it)

            # 第 1 条：大图 + 完整内容
            if i == 0 and thumb:
                lines.append(f"[![{title_cn}]({thumb})]({url})")
                lines.append("")

            lines += [
                f"**[{title_cn}]({url})**  ·  {ch_em} *{sub}*"
                + (f"  `{score_tag}`" if score_tag else ""),
                "",
                contribution,
                "",
            ]
            # 每条都展示 key_points（最多 3 条）
            if key_points:
                for kp in key_points[:3]:
                    lines.append(f"  - {kp}")
                lines.append("")
            # 每条都展示 why_matters
            if why:
                lines += [f"> 💡 **为何重要**：{why}", ""]
        lines.append("")
    lines += ["---", ""]
    return "\n".join(lines)


def _render_channel_section(items: list[dict], shown_ids: Optional[set] = None) -> str:
    """🌐 渠道速览 — 推特、微信、GitHub、视频各自精选。

    shown_ids：已在头条/深度关注/主题区展示的条目 id，渠道速览中不重复出现。
    """
    _seen = shown_ids or set()
    out = ["## 🌐 渠道速览", ""]

    # 🐦 推特
    x_items = [it for it in items
               if (it.get("sub_source") or "").startswith("X/")
               and it.get("id") not in _seen]
    x_items.sort(key=lambda x: -(x.get("final_score") or 0))
    if x_items:
        out += ["### 🐦 推特圈热议", "",
                "| 头像 | 推主 | 推文要点 | 链接 |",
                "|:---:|:---|:---|:---:|"]
        seen_handles = set()
        for it in x_items[:8]:
            handle = (it.get("sub_source", "") or "")[2:]
            if handle in seen_handles:
                continue
            seen_handles.add(handle)
            text = _strip_rt_prefix(it.get("title_cn") or it.get("title", ""))
            if len(text) > 80: text = text[:80] + "…"
            url = it.get("url", "")
            avatar = f"https://unavatar.io/twitter/{handle}"
            avatar_md = f"![]({avatar})"
            out.append(f"| {avatar_md} | **@{handle}** | {text} | [→]({url}) |")
        out.append("")

    # 💬 微信
    wx_items = [it for it in items
                if (it.get("sub_source") or "").startswith("WX/")
                and it.get("id") not in _seen]
    wx_items.sort(key=lambda x: -(x.get("final_score") or 0))
    if wx_items:
        out += ["### 💬 中文圈速览（微信公众号）", "",
                "| Logo | 媒体 | 标题 | 摘要 |",
                "|:---:|:---|:---|:---|"]
        from .og_fetcher import get_wechat_logo
        for it in wx_items[:6]:
            sub = it.get("sub_source", "") or ""
            account = sub[3:]
            title_cn = it.get("title_cn") or it.get("title", "")
            contribution = (it.get("contribution") or it.get("summary") or "")[:80]
            url = it.get("url", "")
            logo = get_wechat_logo(sub) or ""
            logo_md = f"![]({logo})" if logo else "💬"
            out.append(f"| {logo_md} | **{account}** | [{title_cn}]({url}) | {contribution} |")
        out.append("")

    # 💻 GitHub
    gh_items = [it for it in items
                if "GitHub" in (it.get("sub_source") or "")
                and it.get("id") not in _seen]
    gh_items.sort(key=lambda x: -(x.get("final_score") or 0))
    if gh_items:
        out += ["### 💻 GitHub Trending · 今日工具", "",
                "| Repo | 用途 |",
                "|:---|:---|"]
        for it in gh_items[:6]:
            # title 通常是 owner/repo
            title = it.get("title", "")
            contribution = (it.get("contribution") or it.get("summary") or "")[:100]
            url = it.get("url", "")
            out.append(f"| [{title}]({url}) | {contribution} |")
        out.append("")

    # 🎬 视频
    yt_items = [it for it in items
                if _channel_of(it) == "YouTube"
                and it.get("id") not in _seen]
    yt_items.sort(key=lambda x: -(x.get("final_score") or 0))
    if yt_items:
        out += ["### 🎬 视频深度推荐", ""]
        for it in yt_items[:4]:
            title_cn = it.get("title_cn") or it.get("title", "")
            url = it.get("url", "")
            sub = it.get("sub_source", "")
            contribution = (it.get("contribution") or it.get("summary") or "")[:200]
            thumb = _thumbnail_url(it)
            if thumb:
                out.append(f"[![{title_cn}]({thumb})]({url})")
                out.append("")
            out.append(f"**[{title_cn}]({url})**  ·  *{sub}*")
            out.append("")
            out.append(f"{contribution}")
            out.append("")
        out.append("")

    out += ["---", ""]
    return "\n".join(out)


def _render_signal_scan(items: list[dict], already_shown_ids: set[str]) -> str:
    """📡 信号扫描 — 剩余条目，含摘要片段。"""
    remaining = [it for it in items if it.get("id") not in already_shown_ids]
    remaining.sort(key=lambda x: -(x.get("final_score") or 0))
    if not remaining:
        return ""
    out = ["## 📡 信号扫描", "",
           "*以下条目同样值得关注，按评分排序：*", ""]
    for it in remaining[:20]:
        title_cn = (it.get("title_cn") or it.get("title") or "").strip()
        ch = _channel_of(it)
        sub = it.get("sub_source", "")
        url = it.get("url", "")
        snippet = (it.get("contribution") or it.get("summary") or "").strip()
        if len(snippet) > 100:
            snippet = snippet[:100] + "…"
        em = CHANNEL_EMOJI.get(ch, "")
        out.append(f"**{em} [{title_cn}]({url})** · *{sub}*")
        if snippet:
            out.append(f"  {snippet}")
        out.append("")
    out += ["---", ""]
    return "\n".join(out)


def _generate_research_ideas(items: list[dict]) -> Optional[str]:
    """🔬 调用 LLM，基于今日精选内容生成前沿研究方向 & Idea。"""
    import os
    from openai import OpenAI
    api_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("LLM_BASE_URL") or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    client = OpenAI(api_key=api_key, base_url=base_url)

    # 取 top 15 条内容作为输入
    top15 = sorted(items, key=lambda x: -(x.get("final_score") or 0))[:15]
    lines = []
    for it in top15:
        t = (it.get("title_cn") or it.get("title", "")).strip()
        c = (it.get("contribution") or "").strip()[:120]
        lines.append(f"- {t}：{c}")
    content_summary = "\n".join(lines)

    prompt = f"""你是顶级 AI 研究员，今天阅读了以下 AI 前沿动态：

{content_summary}

基于上述内容，识别出 3-5 个**有价值的前沿研究方向或可探索的 Idea**。

输出严格 JSON：
{{
  "ideas": [
    {{
      "title": "<研究方向/Idea 标题（10-20字）>",
      "rationale": "<为什么值得探索：结合今日动态的1-2句依据>",
      "hypothesis": "<具体假设或可验证的问题（1句话）>",
      "difficulty": "<easy | medium | hard>"
    }}
  ]
}}

要求：
- 必须是基于今日内容延伸出来的，不是泛泛的"大方向"
- hypothesis 要具体可操作，不是"探索XXX的可能性"
- 优先标注 medium/hard 级别的有价值问题，不要只给 easy 的"""

    try:
        resp = client.chat.completions.create(
            model="qwen-max-latest",
            max_tokens=1200,
            temperature=0.6,
            timeout=60,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        return json.loads(raw)
    except Exception as e:
        print(f"[digest] research ideas failed: {e}")
        return None


def _render_research_ideas(ideas_result: Optional[dict]) -> str:
    """🔬 前沿研究方向 & Idea — 日报收尾。"""
    if not ideas_result:
        return ""
    ideas = ideas_result.get("ideas") or []
    if not ideas:
        return ""

    diff_label = {"easy": "🟢 入门", "medium": "🟡 进阶", "hard": "🔴 挑战"}
    lines = [
        "## 🔬 前沿研究方向 & Idea",
        "",
        "*基于今日内容，以下方向值得深入探索：*",
        "",
    ]
    for i, idea in enumerate(ideas[:5], 1):
        title = (idea.get("title") or "").strip()
        rationale = (idea.get("rationale") or "").strip()
        hypothesis = (idea.get("hypothesis") or "").strip()
        diff = diff_label.get(idea.get("difficulty", "medium"), "🟡 进阶")
        lines += [
            f"### {i}. {title}  {diff}",
            "",
        ]
        if rationale:
            lines += [rationale, ""]
        if hypothesis:
            lines += [f"> 💡 **可验证假设**：{hypothesis}", ""]
    lines += ["---", ""]
    return "\n".join(lines)


# ─────────────────────────── orchestration ───────────────────────────

def render(
    min_score: int = 5,
    hours: int = 24,
    limit: int = 50,
    total_collected: int = 0,
    total_analyzed: int = 0,
    headline: str = "",
    core_trends: list[dict] = None,
) -> str:
    # quotas 放宽：24h 内顶级 (≥8) 通常有 50-80 条，按主题分组消化得了
    # 完全去掉 youtube/reddit 限制（让 Stratified 内部按 score 排序）
    quotas = {"reddit": 25, "youtube": 12, "rss": 80}
    items = db.top_stratified(min_score=min_score, hours=hours, quotas=quotas, today_only=True)
    if not items:
        items = db.top_recent(min_score=min_score, hours=hours, limit=limit, today_only=True)

    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    weekday = WEEKDAYS[today.weekday()]

    # ── 排序确定 #1（头条） ──
    top_sorted = sorted(items, key=lambda x: -(x.get("final_score") or 0))
    headline_item = top_sorted[0] if top_sorted else None
    breakthrough_id = headline_item["id"] if headline_item else None
    shown_ids: set[str] = set()
    if breakthrough_id:
        shown_ids.add(breakthrough_id)

    # ── og:image 并发抓取（best-effort，仅对 top 30）──
    try:
        enrich_items_with_images(items[:30], max_workers=8)
    except Exception as e:
        print(f"[digest] og fetch error: {e}")

    # ── LLM 深度报道：Top 3 条目并发生成深度报道 ──
    top3_items = top_sorted[:3]
    top3_stories: dict[str, Optional[dict]] = {it["id"]: None for it in top3_items}

    def _write_story(it: dict) -> tuple[str, Optional[dict]]:
        same_cluster = [x for x in items
                        if x.get("cluster_id") == it.get("cluster_id")
                        and x.get("id") != it.get("id")]
        tweets = find_related_tweets(it, items, k=5) if it == top3_items[0] else []
        return it["id"], StoryWriter().write(it, same_cluster[:6], tweet_quotes=tweets)

    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_write_story, it): it for it in top3_items}
            for fut in as_completed(futures):
                try:
                    iid, story = fut.result()
                    top3_stories[iid] = story
                except Exception as e:
                    print(f"[digest] story writer error: {e}")
        headline_story = top3_stories.get(top3_items[0]["id"]) if top3_items else None
        print(f"[digest] {sum(1 for v in top3_stories.values() if v)} / {len(top3_items)} stories written")
    except Exception as e:
        print(f"[digest] story writer error: {e}")
        headline_story = None

    # ── TrendExpander：4 个 trend 各自深度加工（并发） ──
    if core_trends:
        try:
            print(f"[digest] expanding {len(core_trends)} trends...")
            core_trends = TrendExpander().expand_all(core_trends, items)
            print("[digest] trends expanded.")
        except Exception as e:
            print(f"[digest] trend expander error: {e}")

    # top3 都已单独展示，加入 shown_ids 避免主题区重复
    for it in top3_items:
        shown_ids.add(it.get("id"))
    # _render_by_theme 会把展示的 item ids 继续累加
    theme_section = _render_by_theme(items, breakthrough_id=breakthrough_id, shown_ids=shown_ids)

    # ── 前沿研究方向 & Idea ──
    print("[digest] generating research ideas...")
    research_ideas = _generate_research_ideas(items)

    parts = [
        _render_header(date_str, weekday, headline),
        _render_at_a_glance(items, total_collected, total_analyzed, core_trends or [], headline_item),
        _render_timeline(items),
        _render_topic_chart(items),
        _render_wordcloud(items),
        _render_headline_story(headline_item, story=headline_story) if headline_item else "",
        _render_top3_stories(top3_items, top3_stories),
        _render_trend_focus(core_trends or [], items),
        theme_section,
        _render_channel_section(items, shown_ids=shown_ids),
        _render_signal_scan(items, shown_ids),
        _render_research_ideas(research_ideas),
        f"*🤖 AI Inspiration Pipeline v5.0 · {today.strftime('%Y-%m-%d %H:%M')} · "
        f"powered by Qwen-Max + Qwen3.6-Plus*",
    ]
    return "\n".join(p for p in parts if p)


def write_for_date(
    date_str: str,
    min_score: int = 4,
    headline: str = "",
    core_trends: list[dict] = None,
) -> Path:
    """Generate digest for a specific past date using existing DB data."""
    from datetime import datetime as _dt
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{date_str}.md"

    # Items for that specific date
    quotas = {"reddit": 25, "youtube": 12, "rss": 80}
    items = db.top_stratified_for_date(date_str, min_score=min_score, quotas=quotas)
    _collected, total_analyzed = db.count_for_date(date_str)
    total_collected = _collected

    if not items:
        print(f"[digest] No items found for {date_str}")
        return path

    dt = _dt.strptime(date_str, "%Y-%m-%d")
    weekday = WEEKDAYS[dt.weekday()]

    # Top 3 stories
    top_sorted = sorted(items, key=lambda x: -(x.get("final_score") or 0))
    breakthrough_id = top_sorted[0]["id"] if top_sorted else None
    shown_ids: set[str] = set()
    if breakthrough_id:
        shown_ids.add(breakthrough_id)

    try:
        enrich_items_with_images(items[:30], max_workers=8)
    except Exception as e:
        print(f"[digest] og fetch error: {e}")

    top3_items = top_sorted[:3]
    top3_stories: dict[str, Optional[dict]] = {it["id"]: None for it in top3_items}

    def _write_story(it: dict) -> tuple[str, Optional[dict]]:
        same_cluster = [x for x in items if x.get("cluster_id") == it.get("cluster_id") and x.get("id") != it.get("id")]
        tweets = find_related_tweets(it, items, k=5) if it == top3_items[0] else []
        return it["id"], StoryWriter().write(it, same_cluster[:6], tweet_quotes=tweets)

    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_write_story, it): it for it in top3_items}
            for fut in as_completed(futures):
                try:
                    iid, story = fut.result()
                    top3_stories[iid] = story
                except Exception as e:
                    print(f"[digest] story writer error: {e}")
        headline_story = top3_stories.get(top3_items[0]["id"]) if top3_items else None
        print(f"[digest] {sum(1 for v in top3_stories.values() if v)} / {len(top3_items)} stories written")
    except Exception as e:
        print(f"[digest] story writer error: {e}")
        headline_story = None

    for it in top3_items:
        shown_ids.add(it.get("id"))
    theme_section = _render_by_theme(items, breakthrough_id=breakthrough_id, shown_ids=shown_ids)

    print("[digest] generating research ideas...")
    research_ideas = _generate_research_ideas(items)

    parts = [
        _render_header(date_str, weekday, headline),
        _render_at_a_glance(items, total_collected, total_analyzed, core_trends or [], top_sorted[0] if top_sorted else None),
        _render_timeline(items),
        _render_topic_chart(items),
        _render_wordcloud(items),
        _render_headline_story(top_sorted[0] if top_sorted else None, story=headline_story),
        _render_top3_stories(top3_items, top3_stories),
        _render_trend_focus(core_trends or [], items),
        theme_section,
        _render_channel_section(items, shown_ids=shown_ids),
        _render_signal_scan(items, shown_ids),
        _render_research_ideas(research_ideas),
        f"*🤖 AI Inspiration Pipeline v5.0 · {date_str} · powered by Qwen-Max + Qwen3.6-Plus*",
    ]
    content = "\n".join(p for p in parts if p)
    path.write_text(content, encoding="utf-8")

    try:
        from .email_renderer import md_to_html_email
        html_path = path.with_suffix(".html")
        html_path.write_text(md_to_html_email(content, date_str=date_str), encoding="utf-8")
        print(f"[digest] HTML version: {html_path}")
    except Exception as e:
        print(f"[digest] HTML render failed: {e}")
    return path


def write_today(
    min_score: int = 5,
    hours: int = 24,
    limit: int = 50,
    total_collected: int = 0,
    total_analyzed: int = 0,
    headline: str = "",
    core_trends: list[dict] = None,
) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = OUT_DIR / f"{today}.md"
    path.write_text(
        render(
            min_score=min_score,
            hours=hours,
            limit=limit,
            total_collected=total_collected,
            total_analyzed=total_analyzed,
            headline=headline,
            core_trends=core_trends or [],
        ),
        encoding="utf-8",
    )
    # 同步生成 HTML 邮件版（统一用 email_renderer）
    try:
        from .email_renderer import md_to_html_email
        html_path = path.with_suffix(".html")
        html_path.write_text(
            md_to_html_email(path.read_text(encoding="utf-8"), date_str=today),
            encoding="utf-8",
        )
        print(f"[digest] HTML version: {html_path}")
    except Exception as e:
        print(f"[digest] HTML render failed: {e}")
    return path
