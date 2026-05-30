"""YouTube 采集器。需要 YOUTUBE_API_KEY。
按 channels 抓最近视频 + 按 search_queries 关键词搜索；可选抓 Top 评论。

v2.6 新增：velocity 过滤 —— 二次调 videos.list() 拿 viewCount + likeCount，
计算 views/hour 速度，砍掉低播放长尾视频（砍噪音、留信号）。
"""
import os
import json
from datetime import datetime, timedelta, timezone
from .base import Collector, Item


def _parse_iso(s: str):
    """Parse YouTube ISO8601 to UTC datetime; returns None on fail."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class YouTubeCollector(Collector):
    name = "youtube"

    def __init__(self, cfg: dict):
        self.channels = cfg.get("channels", []) or []
        self.queries = cfg.get("search_queries", []) or []
        self.max_results = int(cfg.get("max_results", 15))
        self.fetch_comments = int(cfg.get("fetch_top_comments", 20))
        # velocity 过滤阈值（views/hour）。0 = 不过滤
        # 推荐值：50（频道账号通常 1 小时能拿到 50+ views 才算"有人看"）
        self.min_velocity = float(cfg.get("min_velocity", 0))
        # 绝对播放量底线，太新的视频可能 velocity 算不准
        self.min_views = int(cfg.get("min_views", 0))
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        self.service = self._service()

    def _service(self):
        if not self.api_key:
            return None
        try:
            from googleapiclient.discovery import build

            return build("youtube", "v3", developerKey=self.api_key, cache_discovery=False)
        except ImportError:
            return None

    def collect(self):
        if not self.service:
            return
        published_after = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        for ch in self.channels:
            yield from self._search(channel_id=ch, q=None, published_after=published_after)
        for q in self.queries:
            yield from self._search(channel_id=None, q=q, published_after=published_after)

    def _search(self, channel_id, q, published_after):
        params = dict(
            part="snippet",
            type="video",
            maxResults=self.max_results,
            order="relevance",
            publishedAfter=published_after,
        )
        if channel_id:
            params["channelId"] = channel_id
            params["order"] = "date"
        if q:
            params["q"] = q
        try:
            resp = self.service.search().list(**params).execute()
        except Exception:
            return

        # 收集 video_ids 做一次 batch statistics 查询（每次 1 quota，省得每个视频单独调）
        snippets = []
        for item in resp.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid:
                snippets.append((vid, item.get("snippet", {})))
        if not snippets:
            return

        stats_by_id = self._fetch_stats([vid for vid, _ in snippets])
        now = datetime.now(timezone.utc)

        for vid, sn in snippets:
            st = stats_by_id.get(vid, {})
            view_count = int(st.get("viewCount", 0) or 0)
            like_count = int(st.get("likeCount", 0) or 0)

            # velocity 过滤：低于阈值的视频直接跳过
            pub_dt = _parse_iso(sn.get("publishedAt", ""))
            velocity = 0.0
            if pub_dt and view_count > 0:
                hours = max(1, (now - pub_dt).total_seconds() / 3600)
                velocity = view_count / hours
            # 双门槛：要么 velocity 达标，要么累计 views 达标（旧视频但很火也保留）
            if self.min_velocity > 0 and velocity < self.min_velocity:
                if not (self.min_views and view_count >= self.min_views):
                    continue

            comments = self._top_comments(vid) if self.fetch_comments else []
            yield Item(
                id=f"youtube:{vid}",
                source="youtube",
                sub_source=sn.get("channelTitle", ""),
                title=sn.get("title", ""),
                author=sn.get("channelTitle", ""),
                url=f"https://youtube.com/watch?v={vid}",
                content=sn.get("description", ""),
                comments_blob=json.dumps(comments, ensure_ascii=False),
                published_at=sn.get("publishedAt", ""),
                # score 字段存 view_count，方便 ranker / digest 利用
                score=view_count,
            )

    def _fetch_stats(self, video_ids):
        """Batch fetch view/like/comment counts. Returns {vid: stats_dict}."""
        if not video_ids:
            return {}
        try:
            resp = self.service.videos().list(
                part="statistics",
                id=",".join(video_ids[:50]),  # API 单次最多 50 个 id
            ).execute()
            return {v["id"]: v.get("statistics", {}) for v in resp.get("items", [])}
        except Exception:
            return {}

    def _top_comments(self, video_id):
        try:
            resp = self.service.commentThreads().list(
                part="snippet",
                videoId=video_id,
                maxResults=min(self.fetch_comments, 100),
                order="relevance",
                textFormat="plainText",
            ).execute()
            return [
                t["snippet"]["topLevelComment"]["snippet"].get("textDisplay", "")
                for t in resp.get("items", [])
            ]
        except Exception:
            return []
