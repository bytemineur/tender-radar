#!/usr/bin/env python
"""修去重键：按新规则重算 hash_key，并合并历史遗留的重复行。

背景：
  L1 键原先参与计算的 purchaser 不可靠（同一条公告两次抓取会抽出不同的垃圾值），
  导致同一条招标算出两个 hash，在雷达里重复出现。改为 source_id 之后，
  必须把库里旧键翻新，否则新录的一条仍能和旧的那条共存。

做什么：
  1. 按 (source_id, 归一化标题, 发布日期) 重新计算 hash_key
  2. 同一新键下有多条时，保留信息最全（优先有预算/截止/正文）且 id 最小的一条
  3. 删除被合并掉的重复行，修正 cluster 指针

用法：
    python scripts/dedupe_repair.py            # 实际执行
    python scripts/dedupe_repair.py --dry-run  # 只看将要合并什么
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.crawler.dedupe import make_hash_key  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Tender  # noqa: E402


def _score(t: Tender) -> tuple:
    """信息完整度打分，分高者留下来。"""
    return (
        bool(t.budget_amount),
        bool(t.deadline),
        len(t.content_text or ""),
        bool(t.purchaser),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        rows = session.execute(select(Tender).order_by(Tender.id)).scalars().all()
        groups: dict[str, list[Tender]] = {}
        for t in rows:
            pub = t.publish_time.strftime("%Y-%m-%d") if t.publish_time else ""
            key = make_hash_key(t.title, pub, t.source_id)
            groups.setdefault(key, []).append(t)

        dup_groups = [v for v in groups.values() if len(v) > 1]
        print(f"共 {len(rows)} 条 → {len(groups)} 个去重键，发现 {len(dup_groups)} 组重复")

        removed = 0
        for members in dup_groups:
            # 信息最全者优先；同分时取 id 最小（最早入库）的一条
            keep = sorted(members, key=lambda t: (_score(t), -t.id))[-1]
            drop = [t for t in members if t is not keep]
            print(f"\n  保留 id={keep.id} 预算={keep.budget_amount} 截止={keep.deadline}")
            print(f"    {keep.title[:50]}")
            for d in drop:
                print(f"    删除 id={d.id} 预算={d.budget_amount} 截止={d.deadline}")
                removed += 1

            if args.dry_run:
                continue

            # 先把被删行的 cluster 指针挪到保留行，避免悬空引用
            drop_ids = {d.id for d in drop}
            for t in session.execute(select(Tender)).scalars().all():
                if t.cluster_id in drop_ids:
                    t.cluster_id = keep.cluster_id
            for d in drop:
                session.delete(d)
            session.flush()

            # 覆写保留行的主键，后续复抓按新键命中
            pub = keep.publish_time.strftime("%Y-%m-%d") if keep.publish_time else ""
            keep.hash_key = make_hash_key(keep.title, pub, keep.source_id)

        # 单条记录也要翻新主键
        for key, members in groups.items():
            if len(members) == 1:
                members[0].hash_key = key

        if args.dry_run:
            print(f"\n[dry-run] 将删除 {removed} 条重复行")
        else:
            session.commit()
            left = session.execute(select(Tender)).scalars().all()
            print(f"\n完成：删除 {removed} 条，剩余 {len(left)} 条")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
