"""SQLite storage with v2.5 schema migration support.

v2.5 columns: title_cn, contribution, why_matters, key_points.
Migration runs automatically on init().
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "inspiration.db"

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    sub_source TEXT,
    title TEXT,
    author TEXT,
    url TEXT,
    content TEXT,
    comments_blob TEXT,
    published_at TEXT,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    score INTEGER DEFAULT 0,
    relevance INTEGER,
    summary TEXT,
    ideas TEXT,
    processed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_processed ON items(processed);
CREATE INDEX IF NOT EXISTS idx_relevance ON items(relevance);
CREATE INDEX IF NOT EXISTS idx_fetched_at ON items(fetched_at);
"""

MIGRATIONS = [
    "ALTER TABLE items ADD COLUMN pre_score REAL",
    "ALTER TABLE items ADD COLUMN final_score REAL",
    "ALTER TABLE items ADD COLUMN cluster_id TEXT",
    "ALTER TABLE items ADD COLUMN novelty INTEGER",
    "ALTER TABLE items ADD COLUMN impact INTEGER",
    "ALTER TABLE items ADD COLUMN signal_score INTEGER",
    # v2.5
    "ALTER TABLE items ADD COLUMN title_cn TEXT",
    "ALTER TABLE items ADD COLUMN contribution TEXT",
    "ALTER TABLE items ADD COLUMN why_matters TEXT",
    "ALTER TABLE items ADD COLUMN key_points TEXT",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pre_score ON items(pre_score)",
    "CREATE INDEX IF NOT EXISTS idx_final_score ON items(final_score)",
    "CREATE INDEX IF NOT EXISTS idx_cluster_id ON items(cluster_id)",
]


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")  # 允许多线程并发写
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA_V1)
        existing = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
        for sql in MIGRATIONS:
            col = sql.split("ADD COLUMN ")[-1].split(" ")[0]
            if col not in existing:
                try:
                    c.execute(sql)
                except sqlite3.OperationalError:
                    pass
        for sql in INDEXES:
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                pass


def upsert_item(item: dict):
    keys = list(item.keys())
    placeholders = ",".join("?" * len(keys))
    cols = ",".join(keys)
    sql = f"INSERT OR IGNORE INTO items ({cols}) VALUES ({placeholders})"
    with conn() as c:
        c.execute(sql, [item[k] for k in keys])


def get_unprocessed(limit: int = 100) -> list[dict]:
    order_col = "pre_score" if _has_column("pre_score") and _has_pre_scores() else "RANDOM()"
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT * FROM (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY source, sub_source
                           ORDER BY fetched_at DESC, {order_col} DESC
                       ) AS rn
                FROM items
                WHERE processed = 0
            )
            ORDER BY rn ASC, {order_col} DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _has_column(col: str) -> bool:
    with conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
        return col in cols


def _has_pre_scores() -> bool:
    with conn() as c:
        r = c.execute("SELECT COUNT(*) FROM items WHERE pre_score IS NOT NULL").fetchone()
        return (r[0] or 0) > 0


def update_pre_score(item_id: str, pre_score: float):
    with conn() as c:
        c.execute("UPDATE items SET pre_score=? WHERE id=?", (pre_score, item_id))


def update_analysis_v2(
    item_id: str,
    relevance: int,
    summary: str,
    ideas: str,
    novelty: int = 0,
    impact: int = 0,
    signal_score: int = 0,
    final_score: float = 0,
    title_cn: str = "",
    contribution: str = "",
    why_matters: str = "",
    key_points: str = "",
):
    with conn() as c:
        c.execute(
            """UPDATE items SET relevance=?, summary=?, ideas=?, processed=1,
               novelty=?, impact=?, signal_score=?, final_score=?,
               title_cn=?, contribution=?, why_matters=?, key_points=?
               WHERE id=?""",
            (relevance, summary, ideas, novelty, impact, signal_score, final_score,
             title_cn, contribution, why_matters, key_points, item_id),
        )


def update_analysis(item_id: str, relevance: int, summary: str, ideas: str):
    """Backward-compat wrapper for v1 callers."""
    update_analysis_v2(item_id=item_id, relevance=relevance, summary=summary, ideas=ideas)


def set_cluster(item_id: str, cluster_id: str):
    with conn() as c:
        c.execute("UPDATE items SET cluster_id=? WHERE id=?", (cluster_id, item_id))


def mark_skipped(item_id: str):
    with conn() as c:
        c.execute("UPDATE items SET processed=1, relevance=0 WHERE id=?", (item_id,))


def top_recent(min_score: int = 5, hours: int = 24, limit: int = 50,
               today_only: bool = False) -> list[dict]:
    since = "date('now')" if today_only else f"datetime('now', '-{hours} hours')"
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT * FROM items
            WHERE relevance >= ?
              AND fetched_at >= {since}
            ORDER BY COALESCE(final_score, relevance) DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def top_by_cluster(cluster_id: str, min_score: int = 5, hours: int = 24,
                   limit: int = 30, today_only: bool = False) -> list[dict]:
    since = "date('now')" if today_only else f"datetime('now', '-{hours} hours')"
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT * FROM items
            WHERE relevance >= ?
              AND fetched_at >= {since}
              AND cluster_id = ?
            ORDER BY COALESCE(final_score, relevance) DESC
            LIMIT ?
            """,
            (min_score, cluster_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_clusters(hours: int = 24, today_only: bool = False) -> list[dict]:
    since = "date('now')" if today_only else f"datetime('now', '-{hours} hours')"
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT cluster_id, COUNT(*) as cnt, AVG(final_score) as avg_score
            FROM items
            WHERE fetched_at >= {since}
              AND cluster_id IS NOT NULL
            GROUP BY cluster_id
            ORDER BY AVG(final_score) DESC
            """,
        ).fetchall()
        return [dict(r) for r in rows]


def top_stratified_for_date(date_str: str, min_score: int = 4,
                            quotas: dict[str, int] = None) -> list[dict]:
    """Return top items collected on a specific date (YYYY-MM-DD)."""
    if not quotas:
        quotas = {"reddit": 25, "youtube": 12, "rss": 80}
    results = []
    seen = set()
    with conn() as c:
        for source, limit in quotas.items():
            rows = c.execute(
                """
                SELECT * FROM items
                WHERE relevance >= ?
                  AND DATE(fetched_at) = ?
                  AND source = ?
                ORDER BY COALESCE(final_score, relevance) DESC
                LIMIT ?
                """,
                (min_score, date_str, source, limit),
            ).fetchall()
            for r in rows:
                rid = r["id"]
                if rid not in seen:
                    seen.add(rid)
                    results.append(dict(r))
    results.sort(key=lambda x: x.get("final_score") or x.get("relevance", 0), reverse=True)
    return results


def count_for_date(date_str: str) -> tuple[int, int]:
    """Return (collected, analyzed) counts for a specific date."""
    with conn() as c:
        collected = c.execute(
            "SELECT COUNT(*) FROM items WHERE DATE(fetched_at) = ?", (date_str,)
        ).fetchone()[0]
        analyzed = c.execute(
            "SELECT COUNT(*) FROM items WHERE DATE(fetched_at) = ? AND final_score IS NOT NULL",
            (date_str,)
        ).fetchone()[0]
    return collected, analyzed


def top_stratified(min_score: int = 5, hours: int = 24,
                   quotas: dict[str, int] = None,
                   today_only: bool = False) -> list[dict]:
    """Return top items per source, ensuring source diversity.

    today_only=True: 只取今天（自然日）采集的条目，严格按日期隔离。
    quotas: {"reddit": 15, "youtube": 5, "rss": 30} etc.
    """
    if not quotas:
        quotas = {"reddit": 15, "youtube": 5, "rss": 30}
    since = "date('now')" if today_only else f"datetime('now', '-{hours} hours')"
    results = []
    seen = set()
    with conn() as c:
        for source, limit in quotas.items():
            rows = c.execute(
                f"""
                SELECT * FROM items
                WHERE relevance >= ?
                  AND fetched_at >= {since}
                  AND source = ?
                ORDER BY COALESCE(final_score, relevance) DESC
                LIMIT ?
                """,
                (min_score, source, limit),
            ).fetchall()
            for r in rows:
                rid = r["id"]
                if rid not in seen:
                    seen.add(rid)
                    results.append(dict(r))
    results.sort(key=lambda x: x.get("final_score") or x.get("relevance", 0), reverse=True)
    return results


def count_collected() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM items").fetchone()[0]


def count_analyzed() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM items WHERE processed=1").fetchone()[0]


def count_today_new() -> int:
    """今天新采集（首次入库）的条目数。"""
    today = __import__('datetime').date.today().isoformat()
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM items WHERE fetched_at >= ?",
            (today + " 00:00:00",)
        ).fetchone()[0]


def count_today_analyzed() -> int:
    """今天新完成 LLM 评分的条目数（有 final_score 的才算真正分析过）。"""
    today = __import__('datetime').date.today().isoformat()
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM items WHERE final_score IS NOT NULL AND fetched_at >= ?",
            (today + " 00:00:00",)
        ).fetchone()[0]
