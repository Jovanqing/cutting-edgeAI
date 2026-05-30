"""OG image fetcher with DB caching + concurrent execution.

策略：
- YouTube / GitHub → 直接构造 URL，不发 HTTP
- 其他 → 抓页面找 <meta og:image>
- 抓不到的 URL 也存"已抓过"标记（避免反复重试）
- 并发抓 top N items，控制总耗时

DB cache：items.image_url 字段
  - NULL = 未尝试
  - "" = 尝试过但抓不到（终态，不再重试）
  - "http://..." = 抓到的 URL
"""
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 微信公众号 logo（一次性从 wechat2rss feed XML 的 <channel><image> 抓取，永久有效）
# 微信文章首图拿不到（腾讯反爬严），用账号 logo 作为视觉锚点替代
WECHAT_LOGOS = {
    # ── wechat2rss.xlab.app（feed XML 含 channel image）──
    'WX/机器之心': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM75UiawQgcdqOcmtYS7Jibug9J7dskxkNicGiadtdKl7mLyiaw/0',
    'WX/量子位': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM6PqxX9Yx8MibJ3nial8ialvVrRs3PxhCJm7eWgR7vFrowLA/0',
    'WX/新智元': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM5Ge0ZibsJqTzd6HdTSHcydlic4TnsmpJicUrIlicD1L9ficFw/0',
    'WX/PaperWeekly': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM4XfZEiaIxTZFPcSMe0laHNlkWJfvNVMcFY7PrIPmKrQjg/0',
    'WX/夕小瑶科技说': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM7mib7yjqnAgq5L1h8OWoUOcxl4bQglNqBttImia4Duyfpw/0',
    'WX/我爱计算机视觉': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM6aYkwkiboia6lA9D7ANy49WBe9icxn5NQqJjvn4Pyntzvfw/0',
    'WX/Datawhale': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM54elYMTg5ONEmPUibdDTx3qsDvunJcoTXF1OEte6lOxGw/0',
    'WX/集智俱乐部': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM4VicMW8sROy4mp4kicsCbgPdujTibKFkG2AUMQt9uwRZ5pA/0',
    'WX/极客公园': 'https://wx.qlogo.cn/mmhead/Q3auHgzwzM6dnlEsZXOPTtkZpEYHJ3nvUviaGTbLGUj4VRaq3vra2icQ/0',
    # ── decemberpei.cyou / 官网 favicon ──
    'WX/虎嗅APP': 'https://www.huxiu.com/favicon.ico',
    'WX/InfoQ': 'https://static001.infoq.cn/static/infoq/frontend/statics/img/favicon.ico',
    'WX/智东西': 'https://zhidx.com/favicon.ico',
    'WX/IT之家': 'https://img.ithome.com/themes/ithome5/img/favicon.ico',
    'WX/晚点LatePost': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f550.png',
    'WX/海外独角兽': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f984.png',
    'WX/FounderPark': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f3d4.png',
    'WX/AI前线': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f916.png',
    'WX/投资实习所': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f4c8.png',
    # ── werss.app ──
    'WX/数字生命卡兹克': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f9ec.png',
    'WX/AGI Hunt': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f52d.png',
    'WX/赛博禅心': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f9d8.png',
    'WX/袋鼠帝AI客栈': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f998.png',
    'WX/AI信息Gap': 'https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f4e1.png',
}


def get_wechat_logo(sub_source: str) -> Optional[str]:
    """根据 sub_source（如 'WX/机器之心'）返回公众号 logo URL。"""
    return WECHAT_LOGOS.get(sub_source)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
}
TIMEOUT = 6
DB_PATH = Path(__file__).parent.parent / "data" / "inspiration.db"


def _construct_known_thumb(url: str, sub_source: str = "") -> Optional[str]:
    """对已知模式直接构造缩略图 URL，零网络调用。

    sub_source: 用于微信公众号 logo 映射（'WX/机器之心' 等）
    """
    # 微信公众号 logo（先匹配，因为 url 是 mp.weixin.qq.com）
    if sub_source and sub_source.startswith("WX/"):
        logo = get_wechat_logo(sub_source)
        if logo:
            return logo
    # YouTube
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if m:
        return f"https://img.youtube.com/vi/{m.group(1)}/maxresdefault.jpg"
    # GitHub
    m = re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", url)
    if m:
        return f"https://opengraph.githubassets.com/1/{m.group(1)}/{m.group(2)}"
    # X/Twitter → 推主头像（via unavatar.io）
    m = re.match(r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]+)(?:/status/\d+)?", url)
    if m:
        handle = m.group(1)
        # 跳过 /i/ /home /search 这种非用户 URL
        if handle.lower() not in ("i", "home", "search", "explore", "notifications", "messages"):
            return f"https://unavatar.io/twitter/{handle}"
    # Bluesky
    m = re.match(r"https?://bsky\.app/profile/([^/]+)", url)
    if m:
        return f"https://unavatar.io/bluesky/{m.group(1)}"
    return None


def _fetch_og_from_page(url: str) -> Optional[str]:
    """HTTP 抓页面解析 <meta og:image>。失败返回 None。"""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return None
        ct = r.headers.get("Content-Type", "")
        if "html" not in ct and "xml" not in ct:
            return None
        soup = BeautifulSoup(r.text[:200_000], "lxml")  # 只取前 200KB
        for prop in ["og:image:secure_url", "og:image", "twitter:image:src", "twitter:image"]:
            tag = (soup.find("meta", attrs={"property": prop}) or
                   soup.find("meta", attrs={"name": prop}))
            if tag and tag.get("content"):
                img = tag.get("content").strip()
                # 过滤明显错误（占位、太小、相对 URL）
                if img.startswith("//"):
                    img = "https:" + img
                if not img.startswith(("http://", "https://")):
                    continue
                return img
    except Exception:
        return None
    return None


def fetch_image_for_url(url: str, sub_source: str = "") -> Optional[str]:
    """单 URL 抓图入口：先构造已知模式（含微信 logo / X 头像），否则抓 og:image。"""
    known = _construct_known_thumb(url, sub_source)
    if known:
        return known
    return _fetch_og_from_page(url)


def enrich_items_with_images(items: list[dict], max_workers: int = 8) -> int:
    """对 items 列表并发抓图，写回 DB cache。

    跳过：
      - 已有 image_url 字段（非 None）的
      - 没有 url 的

    返回：成功抓到图的条目数（包含构造的 thumb）
    """
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    todo = []  # (id, url, sub_source) tuples needing fetch
    for it in items:
        # 已 cache 过 -> 跳过（NULL = 没试过；"" = 试过没图；URL = 有图）
        if it.get("image_url") is not None:
            continue
        url = it.get("url", "")
        if not url:
            continue
        todo.append((it["id"], url, it.get("sub_source", "")))

    if not todo:
        return 0

    results: dict[str, Optional[str]] = {}

    def _job(tup):
        iid, url, sub = tup
        return iid, fetch_image_for_url(url, sub)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_job, t) for t in todo]
        for fut in as_completed(futures):
            try:
                iid, img = fut.result()
                results[iid] = img
            except Exception:
                pass

    # 写回 DB：抓到的存 URL，没抓到的存 "" 标记"已尝试"
    success = 0
    for iid, img in results.items():
        val = img if img else ""
        cur.execute("UPDATE items SET image_url=? WHERE id=?", (val, iid))
        if img:
            success += 1
    conn.commit()
    conn.close()

    # 把抓到的也同步到内存 items（避免 caller 重读 DB）
    by_id = {it["id"]: it for it in items if "id" in it}
    for iid, img in results.items():
        if iid in by_id:
            by_id[iid]["image_url"] = img if img else ""

    return success
