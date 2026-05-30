"""微信公众号采集器。通过搜狗微信搜索获取文章。

- 无需 API key，按关键词搜索
- 返回标题、摘要、公众号名称、链接
- 搜索排名作为热度代理（搜狗不返回阅读量）
- 以 (标题+公众号) 哈希去重，避免跨搜索词重复
- 注意：微信无公开 API，搜狗可能偶尔弹出验证码，请合理控制频率
"""
import hashlib
import re
import time
from datetime import datetime, timezone
from .base import Collector, Item

import requests
from bs4 import BeautifulSoup

SOGOU_SEARCH_URL = "https://weixin.sogou.com/weixin"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class WeChatCollector(Collector):
    name = "wechat"

    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled", False))
        self.queries = cfg.get("search_queries", []) or []
        self.max_results = int(cfg.get("max_results", 15))
        self.delay = float(cfg.get("request_delay", 2.0))

    def collect(self):
        if not self.enabled:
            return
        seen = set()
        for q in self.queries:
            items = self._search(q)
            for it in items:
                if it.id not in seen:
                    seen.add(it.id)
                    yield it
            time.sleep(self.delay)

    def _search(self, query: str):
        results = []
        try:
            resp = requests.get(
                SOGOU_SEARCH_URL,
                params={"type": "2", "query": query},
                headers=REQUEST_HEADERS,
                timeout=20,
            )
            resp.raise_for_status()
        except Exception:
            return results

        if "请输入验证码" in resp.text or "antispider" in resp.text.lower():
            return results

        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select(".news-list2 li") or soup.select(".news-list li")

        for rank, li in enumerate(items[: self.max_results]):
            try:
                item = self._parse_item(li, query, rank)
                if item:
                    results.append(item)
            except Exception:
                continue

        return results

    def _parse_item(self, li, query: str, rank: int):
        # Title + Link
        title_el = li.select_one("h3 a")
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if link.startswith("/link"):
            link = f"https://weixin.sogou.com{link}"

        # Summary
        summary_el = li.select_one(".txt-info") or li.select_one("p.txt-info")
        summary = summary_el.get_text(strip=True) if summary_el else ""

        # Account name
        account = ""
        acc_el = li.select_one(".all-time-y2") or li.select_one(".s-p")
        if acc_el:
            account = acc_el.get_text(strip=True)

        # Date: parse from script tag
        published_at = ""
        for script in li.select("script"):
            text = script.get_text(strip=True) if script.string else ""
            m = re.search(r"timeConvert\('(\d+)'\)", text)
            if m:
                try:
                    ts = int(m.group(1))
                    published_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    pass
                break

        # Unique ID: hash by (title + account) for cross-query dedup
        uid = f"{title}_{account}"
        fid = "wechat:" + hashlib.sha1(uid.encode("utf-8", errors="ignore")).hexdigest()[:16]

        # Score: search rank as popularity proxy (position 0 = highest, score 15-max)
        rank_score = max(1, self.max_results - rank)

        return Item(
            id=fid,
            source="wechat",
            sub_source=account or "微信公众号",
            title=title,
            author=account or "",
            url=link,
            content=summary,
            published_at=published_at,
            score=rank_score,
        )
