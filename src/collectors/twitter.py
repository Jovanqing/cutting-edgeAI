"""Twitter/X 采集器。需要 TWITTER_BEARER_TOKEN。免费档每月 ~1500 条限额，慎用。"""
import os
from .base import Collector, Item


class TwitterCollector(Collector):
    name = "twitter"

    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", False)
        self.users = cfg.get("users", []) or []
        self.query = cfg.get("query", "")
        self.max_results = min(int(cfg.get("max_results", 50)), 100)
        self.token = os.getenv("TWITTER_BEARER_TOKEN")
        self.client = self._client()

    def _client(self):
        if not (self.enabled and self.token):
            return None
        try:
            import tweepy

            return tweepy.Client(bearer_token=self.token, wait_on_rate_limit=False)
        except ImportError:
            return None

    def collect(self):
        if not self.client:
            return
        q = self.query
        if self.users:
            user_part = " OR ".join(f"from:{u}" for u in self.users)
            q = f"({user_part}) {q}".strip()
        if not q:
            return
        try:
            resp = self.client.search_recent_tweets(
                query=q,
                max_results=self.max_results,
                tweet_fields=["created_at", "public_metrics", "author_id", "text"],
                expansions=["author_id"],
                user_fields=["username"],
            )
        except Exception:
            return
        users = {u.id: u.username for u in (resp.includes or {}).get("users", [])}
        for t in resp.data or []:
            uname = users.get(t.author_id, "")
            metrics = t.public_metrics or {}
            yield Item(
                id=f"twitter:{t.id}",
                source="twitter",
                sub_source=uname,
                title=(t.text or "")[:140],
                author=uname,
                url=f"https://twitter.com/{uname}/status/{t.id}",
                content=t.text or "",
                published_at=str(t.created_at) if t.created_at else "",
                score=int(metrics.get("like_count", 0)),
            )
