#!/usr/bin/env python
"""回填详情字段：给库里缺预算/截止时间的条目补一次详情页。

为什么需要它：
  列表页通常只给标题 + 发布日期，预算和截止时间在详情页里。
  规则改过（新增了 detail_fields）或者早先跑的是「只抓列表」模式时，
  库里会留下一批字段空白的记录，雷达上看就是一连串「未披露」。

用法：
    python scripts/backfill_detail.py                # 全部来源，最多补 100 条
    python scripts/backfill_detail.py --limit 20     # 只补 20 条
    python scripts/backfill_detail.py --source ccgp  # 只补某个来源
    python scripts/backfill_detail.py --dry-run      # 只看会补哪些，不写库

设计取向：串行、限速、失败即跳过。回填是一次性运维动作，
不该为了快而给目标站制造压力。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select  # noqa: E402

from app.crawler.adapters import (  # noqa: E402
    SiteRule,
    extract_link,
    get_site,
    load_all_sites,
    parse_detail,
)
from app.crawler.extract import extract_tender  # noqa: E402
from app.crawler.fetcher import BrowserSession, fetch  # noqa: E402
from app.crawler.pipeline import _save_raw  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Tender  # noqa: E402


def needs_backfill(t: Tender) -> bool:
    return t.budget_amount is None or t.deadline is None


def do_source(session, rule: SiteRule, limit: int, dry: bool) -> tuple[int, int]:
    """返回 (尝试数, 补全数)。"""
    rows = (
        session.execute(
            select(Tender)
            .where(
                Tender.source_id == rule.id,
                or_(Tender.budget_amount.is_(None), Tender.deadline.is_(None)),
                Tender.source_url != "",
            )
            .order_by(Tender.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0, 0

    print(f"\n=== {rule.name}（{rule.id}）待补 {len(rows)} 条 ===")
    if dry:
        for t in rows[:10]:
            print(f"  - {t.title[:48]}")
        return len(rows), 0

    # ccgp 这类站只有浏览器会话能稳定取到详情
    need_browser = rule.mode == "browser"
    sess = BrowserSession() if need_browser else None
    if sess is not None:
        sess.__enter__()
        if sess.error:
            print(f"  浏览器不可用：{sess.error}")
            sess.__exit__(None, None, None)
            return 0, 0

    tried = filled = 0
    try:
        for t in rows:
            tried += 1
            try:
                res = fetch(
                    t.source_url,
                    allow_browser=(need_browser or None),
                    prefer_browser=need_browser,
                    wait_selector=rule.detail_container or None,
                    session=sess,
                )
                if res.ok and rule.detail_url_from is not None:
                    sub = extract_link(res.html, rule.detail_url_from)
                    if sub:
                        d2 = fetch(
                            urljoin(t.source_url, sub),
                            allow_browser=(need_browser or None),
                            prefer_browser=need_browser,
                            session=sess,
                        )
                        if d2.ok:
                            res = d2
                if not res.ok:
                    print(f"  × {t.title[:34]}：{res.error or '取页失败'}")
                    continue

                detail = parse_detail(rule, res.html)
                merged = {**detail}
                data = extract_tender(t.title, detail.get("_text", ""), row=merged)

                changed = False
                if t.budget_amount is None and data["budget_amount"]:
                    t.budget_amount = data["budget_amount"]
                    changed = True
                if t.deadline is None and data["deadline"]:
                    t.deadline = data["deadline"]
                    t.deadline_confident = data["deadline_confident"]
                    changed = True
                if not t.content_text and data["content_text"]:
                    t.content_text = data["content_text"][:20000]
                    changed = True
                if changed:
                    t.raw_path = _save_raw(rule.id, res.html) or t.raw_path
                    filled += 1
                    b = f'{t.budget_amount/10000:.0f}万' if t.budget_amount else '—'
                    d = t.deadline.strftime('%Y-%m-%d') if t.deadline else '—'
                    print(f"  ✓ {t.title[:30]}  预算 {b}  截止 {d}")
                else:
                    print(f"  · {t.title[:34]}：正文里也没写预算/截止")
                session.commit()
            except Exception as exc:  # noqa: BLE001 单条失败不影响后续
                session.rollback()
                print(f"  ! {t.title[:30]}：{type(exc).__name__}: {exc}")
    finally:
        if sess is not None:
            sess.__exit__(None, None, None)
    return tried, filled


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100, help="每个来源最多处理多少条")
    ap.add_argument("--source", default="", help="只处理指定来源 id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rules = [get_site(args.source)] if args.source else load_all_sites()
    rules = [r for r in rules if r and r.enabled and r.detail_container]
    if not rules:
        print("没有可回填的来源（规则里要有 detail_container）")
        return 0

    session = SessionLocal()
    t0 = datetime.now()
    total_tried = total_filled = 0
    try:
        for r in rules:
            tried, filled = do_source(session, r, args.limit, args.dry_run)
            total_tried += tried
            total_filled += filled
    finally:
        session.close()

    cost = (datetime.now() - t0).total_seconds()
    print(f"\n完成：尝试 {total_tried} 条，补全 {total_filled} 条，用时 {cost:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
