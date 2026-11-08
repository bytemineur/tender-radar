#!/usr/bin/env python
"""离线重抽取：用本地已存的详情页正文，重新跑一遍字段抽取。

为什么要它：
  抽取规则一直在演进（千分位金额、采购人噪声过滤、发布时间猜测……
  这几个 bug 修完之前入库的记录全是错的）。详情页正文一旦存下来就不再变化，
  所以「重新抽一遍」不需要再访问目标站——零请求、零封禁风险、秒级完成。

  在线补正文交给 backfill_detail.py，本脚本只做离线重算，两者分工明确：
      backfill_detail.py  缺正文 → 去站上取详情
      reextract.py        有正文 → 重算字段（含抽取规则升级后的修复）

用法：
    python scripts/reextract.py               # 只补空字段（安全）
    python scripts/reextract.py --force       # 用新结果覆盖已有值
    python scripts/reextract.py --dry-run     # 只看差异，不写库

设计取向：默认只填空不覆盖，避免把人工校验过的值改坏。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select  # noqa: E402

from app.crawler.extract import _plausible_org, extract_tender  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Tender  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已有值（默认只补空）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条，0 表示不限")
    args = ap.parse_args()

    session = SessionLocal()
    t0 = datetime.now()
    try:
        q = select(Tender).where(Tender.content_text.isnot(None))
        if not args.force:
            q = q.where(
                or_(
                    Tender.budget_amount.is_(None),
                    Tender.deadline.is_(None),
                    Tender.purchaser.is_(None),
                    Tender.purchaser == "",
                )
            )
        rows = session.execute(q.order_by(Tender.id)).scalars().all()
        if args.limit:
            rows = rows[: args.limit]

        print(f"待处理 {len(rows)} 条{'（dry-run，不写库）' if args.dry_run else ''}")
        n_b = n_d = n_p = n_bad = 0

        for t in rows:
            body = t.content_text or ""
            if len(body) < 60:
                continue
            row = {
                # 发布时间以库里已有的为准，避免规则再从正文乱猜
                "publish_time": t.publish_time.strftime("%Y-%m-%d") if t.publish_time else "",
                "province": t.province or "",
            }
            try:
                d = extract_tender(t.title, body, row=row)
            except Exception as exc:  # noqa: BLE001 单条失败不影响整批
                n_bad += 1
                print(f"  ! id={t.id} 抽取异常 {type(exc).__name__}: {exc}")
                continue

            changed = False
            # 预算
            nb = d["budget_amount"]
            if nb and (t.budget_amount is None or args.force):
                if t.budget_amount is None:
                    n_b += 1
                t.budget_amount = nb
                t.currency = "CNY"
                changed = True
            # 截止时间
            nd = d["deadline"]
            if nd and (t.deadline is None or args.force):
                if t.deadline is None:
                    n_d += 1
                t.deadline = nd
                t.deadline_raw = d["deadline_raw"] or t.deadline_raw
                t.deadline_confident = d["deadline_confident"]
                changed = True
            # 采购人：空了要补；存着的若判废（旧规则的垃圾值如「式：竞争性磋商」）也要替换
            np_ = d["purchaser"]
            want_purge = bool(t.purchaser) and not _plausible_org(t.purchaser)
            if np_ and (not t.purchaser or want_purge or args.force):
                if not t.purchaser or want_purge:
                    n_p += 1
                t.purchaser = np_
                changed = True
            elif want_purge and not np_:
                # 抽不出更好的值，至少把噪声清掉，别让它显示在卡片上
                t.purchaser = ""
                changed = True
            if d["agency"] and not t.agency:
                t.agency = d["agency"]
                changed = True
            if d["province"] and not t.province:
                t.province = d["province"]
                changed = True

            if changed and not args.dry_run:
                session.commit()
            elif args.dry_run and (nb or nd or np_):
                b = f"{nb / 10000:.1f}万" if nb else "—"
                dl = nd.strftime("%Y-%m-%d") if nd else "—"
                print(f"  · id={t.id:<4} 预算 {b:<10} 截止 {dl:<12} {t.title[:32]}")

        print(
            f"\n完成：预算 +{n_b}，截止 +{n_d}，采购人 +{n_p}，异常 {n_bad}，"
            f"用时 {(datetime.now() - t0).total_seconds():.1f}s"
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
