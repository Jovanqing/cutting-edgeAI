"""RSS / Atom feed 采集器。覆盖博客、arXiv、以及 RSSHub 桥接的 X/Twitter feed。

本地服务（RSSHub on :1200 / wewe-rss on :4000）失败时会快速跳过，
不会阻塞整个 collect 阶段，所以 Docker 没起也不会挂 pipeline。
"""
import hashlib
import socket
from urllib.parse import urlparse
from .base import Collector, Item

# localhost feed 的探测超时（秒）—— 拒连或没起时秒过
LOCAL_PROBE_TIMEOUT = 1.5
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _local_port_open(url: str) -> bool:
    """对 localhost URL 快速 TCP 探测；非 localhost 总是返回 True（交给 feedparser）。"""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host not in LOCAL_HOSTS:
            return True
        port = p.port or (443 if p.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=LOCAL_PROBE_TIMEOUT):
            return True
    except (socket.error, OSError, ValueError):
        return False


class RSSCollector(Collector):
    name = "rss"

    def __init__(self, cfg: dict):
        self.feeds = cfg.get("feeds", []) or []

    def collect(self):
        try:
            import feedparser
        except ImportError:
            return
        for feed in self.feeds:
            url = feed.get("url")
            name = feed.get("name", url)
            if not url:
                continue
            # localhost 服务（RSSHub / wewe-rss）没起时秒过，避免 feedparser 卡 30s+
            if not _local_port_open(url):
                continue
            try:
                d = feedparser.parse(url)
            except Exception:
                continue
            for entry in d.entries:
                link = entry.get("link", "")
                uid = entry.get("id") or link or entry.get("title", "")
                fid = "rss:" + hashlib.sha1(uid.encode("utf-8", errors="ignore")).hexdigest()[:16]
                yield Item(
                    id=fid,
                    source="rss",
                    sub_source=name,
                    title=entry.get("title", ""),
                    author=entry.get("author", ""),
                    url=link,
                    content=entry.get("summary", "") or entry.get("description", ""),
                    published_at=entry.get("published", "") or entry.get("updated", ""),
                )
