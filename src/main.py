"""CLI entry point — v5.0 pipeline.

  python -m src.main collect            # Fetch all enabled sources
  python -m src.main analyze            # LLM-score all unprocessed items
  python -m src.main digest             # Generate today's inspiration digest
  python -m src.main email              # Render today's digest as HTML email
  python -m src.main email 2026-05-15   # Render specific date as HTML email
  python -m src.main weekly             # Generate weekly digest (last 7 days)
  python -m src.main monthly            # Generate monthly digest (last 30 days)
  python -m src.main run                # Full v5.0 pipeline
  python -m src.main run --dry          # collect + prefilter + rank only (no LLM calls)
"""
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.console import Console

from . import db
from .analyzer import Analyzer
from .digest import write_today
from .prefilter import PreFilter
from .ranker import Ranker
from .cluster import Clusterer
from .insight_generator import InsightGenerator
from .collectors.reddit import RedditCollector
from .collectors.youtube import YouTubeCollector
from .collectors.rss import RSSCollector
from .collectors.twitter import TwitterCollector
# wechat (搜狗) collector 已废弃 —— 微信公众号现在走 rss.feeds 里的 wechat2rss.xlab.app
# 代码文件 src/collectors/wechat.py 暂保留以便未来需要时恢复

load_dotenv()
console = Console()
ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def cmd_collect() -> int:
    cfg = load_config()
    db.init()
    collectors = [
        RedditCollector(cfg.get("reddit", {})),
        YouTubeCollector(cfg.get("youtube", {})),
        RSSCollector(cfg.get("rss", {})),
        TwitterCollector(cfg.get("twitter", {})),
    ]
    total = 0
    for col in collectors:
        n = 0
        try:
            for item in col.collect():
                db.upsert_item(item.to_dict())
                n += 1
        except Exception as e:
            console.log(f"[red]{col.name} failed:[/] {e}")
            continue
        console.log(f"[green]{col.name}[/]: {n} items")
        total += n
    console.log(f"[bold]Collected {total} items[/]")
    return total


def cmd_prefilter_rank() -> tuple[int, int]:
    cfg = load_config()
    pf = PreFilter(cfg.get("prefilter", {}), cfg.get("keywords", []))
    ranker = Ranker(cfg.get("ranker", {}))

    all_unprocessed = db.get_unprocessed(limit=10_000)
    if not all_unprocessed:
        return 0, 0

    passed = []
    for it in all_unprocessed:
        if pf.should_keep(it):
            passed.append(it)
        else:
            db.mark_skipped(it["id"])
    dropped = len(all_unprocessed) - len(passed)
    console.log(f"Prefilter: {len(passed)} kept / {len(all_unprocessed)} total ({dropped} dropped)")

    if not passed:
        return 0, len(all_unprocessed)

    candidates = ranker.select(passed)
    candidate_ids = {it["id"] for it in candidates}
    for it in passed:
        if it["id"] not in candidate_ids:
            db.mark_skipped(it["id"])
    console.log(f"Ranker: {len(candidates)} candidates selected, {len(passed) - len(candidates)} marked skipped")

    for it in candidates:
        db.update_pre_score(it["id"], it.get("pre_score", 0))

    return len(candidates), len(all_unprocessed)


def cmd_analyze() -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cfg = load_config()
    a_cfg = cfg.get("analyzer", {})
    workers = a_cfg.get("workers", 10)
    batch_size = a_cfg.get("batch", 100)
    total = 0

    def _score_one(it: dict) -> tuple[dict, dict | None, str | None]:
        """Return (item, result, error_msg)."""
        try:
            a = Analyzer(model=a_cfg.get("model", "qwen3.7-max"))
            res = a.score(it)
            return it, res, None
        except Exception as e:
            return it, None, str(e)

    while True:
        items = db.get_unprocessed(limit=batch_size)
        if not items:
            break
        console.log(f"Analyzing {len(items)} items (total done: {total}, workers={workers})…")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_score_one, it): it for it in items}
            for fut in as_completed(futures):
                it, res, err = fut.result()
                if err:
                    console.log(f"  [red]fail[/] {it['id']}: {err}")
                    continue
                db.update_analysis_v2(
                    item_id=it["id"],
                    relevance=res["relevance"],
                    summary=res["summary"],
                    ideas=res["ideas"],
                    novelty=res["novelty"],
                    impact=res["impact"],
                    signal_score=res["signal_score"],
                    final_score=res["final_score"],
                    title_cn=res.get("title_cn", ""),
                    contribution=res.get("contribution", ""),
                    why_matters=res.get("why_matters", ""),
                    key_points=res.get("key_points", "[]"),
                )
                console.log(
                    f"  [N{res['novelty']} I{res['impact']} S{res['signal_score']}"
                    f" → {res['final_score']:.1f}] {res.get('title_cn', it['title'][:40])}"
                )
                total += 1
    console.log(f"[bold]Analyzed {total} items total[/]")
    return total


def cmd_cluster(analyzed_count: int) -> int:
    cfg = load_config()
    cl_cfg = cfg.get("cluster", {})
    if not cl_cfg.get("enabled", True):
        console.log("[dim]Clustering disabled[/]")
        return 0

    clusterer = Clusterer(cl_cfg)
    items = db.top_recent(min_score=0, hours=48, limit=min(analyzed_count, 200))
    if len(items) < 3:
        console.log(f"[dim]Too few items for clustering ({len(items)})[/]")
        return 0

    console.log(f"Clustering {len(items)} items into {cl_cfg.get('num_topics', 5)} topics…")
    theme_map = clusterer.cluster(items)

    for theme, item_ids in theme_map.items():
        for iid in item_ids:
            db.set_cluster(iid, theme)
        console.log(f"  [{len(item_ids)}] {theme}")

    console.log(f"[bold]{len(theme_map)} clusters written to DB[/]")
    return len(theme_map)


def cmd_insight() -> dict:
    """Generate core trend insights from top items. Returns {headline, trends}."""
    cfg = load_config()
    ins_cfg = cfg.get("insight", {})
    if not ins_cfg.get("enabled", True):
        console.log("[dim]Insight generation disabled[/]")
        return {"headline": "", "trends": []}

    gen = InsightGenerator(ins_cfg)
    items = db.top_recent(min_score=5, hours=48, limit=50)
    console.log(f"Generating insights from top {min(len(items), gen.top_n)} items…")
    result = gen.generate(items)

    if result.get("headline"):
        console.log(f"  Headline: {result['headline']}")
    for t in result.get("trends", []):
        console.log(f"  Trend: {t.get('title', '')}")
    return result


def cmd_digest(collected: int = 0, analyzed: int = 0,
               headline: str = "", core_trends: list[dict] = None):
    cfg = load_config()
    a_cfg = cfg.get("analyzer", {})
    path = write_today(
        min_score=a_cfg.get("min_score", 5),
        limit=50,
        total_collected=collected,
        total_analyzed=analyzed,
        headline=headline,
        core_trends=core_trends or [],
    )
    console.log(f"[green]Digest written:[/] {path}")
    # Auto-generate HTML email version
    try:
        from .email_renderer import render_digest_as_email
        html_path = render_digest_as_email()
        console.log(f"[green]Email HTML written:[/] {html_path}")
    except Exception as e:
        console.log(f"[yellow]Email HTML skipped:[/] {e}")
    # Auto-generate WeChat article
    try:
        from .wechat_publisher import generate_wechat_article
        wx_html, wx_md = generate_wechat_article()
        console.log(f"[green]WeChat article:[/] {wx_html}")
    except Exception as e:
        console.log(f"[yellow]WeChat article skipped:[/] {e}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    dry_run = "--dry" in sys.argv

    if cmd == "collect":
        cmd_collect()
    elif cmd == "prefilter":
        cmd_prefilter_rank()
    elif cmd == "analyze":
        cmd_analyze()
    elif cmd == "cluster":
        cmd_cluster(db.count_analyzed())
    elif cmd == "insight":
        cmd_insight()
    elif cmd == "digest":
        cmd_digest(collected=db.count_today_new(), analyzed=db.count_today_analyzed())
    elif cmd == "email":
        date_arg = sys.argv[2] if len(sys.argv) > 2 else ""
        from .email_renderer import render_digest_as_email
        try:
            html_path = render_digest_as_email(date_arg)
            console.log(f"[green]Email HTML written:[/] {html_path}")
        except FileNotFoundError as e:
            console.log(f"[red]Error:[/] {e}")
            sys.exit(1)
    elif cmd == "wechat":
        from .wechat_publisher import generate_wechat_article
        try:
            wx_html, wx_md = generate_wechat_article()
            console.log(f"[green]WeChat article written:[/] {wx_html}")
        except Exception as e:
            console.log(f"[red]WeChat article failed:[/] {e}")
            sys.exit(1)
    elif cmd == "date":
        date_arg = sys.argv[2] if len(sys.argv) > 2 else ""
        if not date_arg:
            console.log("[red]用法：python -m src.main date YYYY-MM-DD[/]")
            sys.exit(1)
        from .digest import write_for_date
        path = write_for_date(date_arg)
        console.log(f"[green]Digest written:[/] {path}")
    elif cmd == "weekly":
        days_arg = next((int(a) for a in sys.argv[2:] if a.isdigit()), 7)
        from .weekly_digest import write_weekly
        path = write_weekly(days=days_arg)
        if path:
            console.log(f"[green]Weekly digest:[/] {path}")
    elif cmd == "monthly":
        from .weekly_digest import write_monthly
        path = write_monthly()
        if path:
            console.log(f"[green]Monthly digest:[/] {path}")
    elif cmd == "run":
        collected = cmd_collect()
        candidates, total = cmd_prefilter_rank()
        if dry_run:
            console.log(f"[bold yellow]Dry run:[/] {candidates} candidates from {total} unprocessed")
            console.log("Run without --dry to proceed with LLM analysis.")
            return
        if candidates == 0:
            console.log("[yellow]No candidates after prefilter+rank, stopping.[/]")
            return
        analyzed = cmd_analyze()
        if analyzed >= 3:
            cmd_cluster(analyzed)
        insight_result = cmd_insight()
        cmd_digest(
            collected=collected,
            analyzed=analyzed,
            headline=insight_result.get("headline", ""),
            core_trends=insight_result.get("trends", []),
        )
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
