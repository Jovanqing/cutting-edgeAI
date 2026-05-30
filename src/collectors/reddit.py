"""Reddit 采集器。
- 有 REDDIT_CLIENT_ID/SECRET → 用 PRAW（含 top 评论）
- 没有 → 降级到匿名 JSON 端点（无评论，限速更严）
"""
import os
import json
import requests
from .base import Collector, Item

USER_AGENT = os.getenv("REDDIT_USER_AGENT", "ai-inspiration-bot/1.0")


class RedditCollector(Collector):
    name = "reddit"

    def __init__(self, cfg: dict):
        self.subreddits = cfg.get("subreddits", [])
        self.limit = cfg.get("limit_per_sub", 25)
        self.time_filter = cfg.get("time_filter", "day")
        self.min_upvotes = int(cfg.get("min_upvotes", 0))
        self.min_upvotes_override = cfg.get("min_upvotes_override", {}) or {}
        self.client = self._init_praw()

    def _threshold(self, sub: str) -> int:
        return int(self.min_upvotes_override.get(sub, self.min_upvotes))

    def _init_praw(self):
        cid = os.getenv("REDDIT_CLIENT_ID")
        csec = os.getenv("REDDIT_CLIENT_SECRET")
        if not (cid and csec):
            return None
        try:
            import praw

            return praw.Reddit(
                client_id=cid,
                client_secret=csec,
                user_agent=USER_AGENT,
            )
        except ImportError:
            return None

    def collect(self):
        for sub in self.subreddits:
            if self.client:
                yield from self._praw_fetch(sub)
            else:
                yield from self._json_fetch(sub)

    def _praw_fetch(self, sub):
        try:
            posts = self.client.subreddit(sub).top(time_filter=self.time_filter, limit=self.limit)
        except Exception:
            return
        threshold = self._threshold(sub)
        for post in posts:
            if threshold and int(post.score or 0) < threshold:
                continue
            try:
                post.comment_sort = "top"
                post.comments.replace_more(limit=0)
                comments = [c.body for c in list(post.comments)[:10] if hasattr(c, "body")]
            except Exception:
                comments = []
            yield Item(
                id=f"reddit:{post.id}",
                source="reddit",
                sub_source=sub,
                title=post.title or "",
                author=str(post.author) if post.author else "",
                url=f"https://reddit.com{post.permalink}",
                content=post.selftext or "",
                comments_blob=json.dumps(comments, ensure_ascii=False),
                published_at=str(post.created_utc),
                score=int(post.score or 0),
            )

    def _json_fetch(self, sub):
        url = f"https://www.reddit.com/r/{sub}/top.json?t={self.time_filter}&limit={self.limit}"
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            r.raise_for_status()
            children = r.json().get("data", {}).get("children", [])
        except Exception:
            return
        threshold = self._threshold(sub)
        for child in children:
            d = child.get("data", {})
            if threshold and int(d.get("score", 0) or 0) < threshold:
                continue
            yield Item(
                id=f"reddit:{d.get('id')}",
                source="reddit",
                sub_source=sub,
                title=d.get("title", ""),
                author=d.get("author", ""),
                url=f"https://reddit.com{d.get('permalink', '')}",
                content=d.get("selftext", ""),
                published_at=str(d.get("created_utc", "")),
                score=int(d.get("score", 0)),
            )
