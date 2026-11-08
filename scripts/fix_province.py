#!/usr/bin/env python
"""重算省份归属（离线，不打目标站）。

背景：guess_province 早先是「按省份列表顺序找第一个子串」，
北京排第一，于是所有出现「（北京时间）」的公告都被判成北京；
中标公告里中标律所的名字（「北京市中伦（广州）律师事务所」）也会顶替项目所在地。
规则修好之后，库里已经写进去的旧值需要一次迁移。

用法：
    python scripts/fix_province.py --dry-run   # 只看将要怎么改
    python scripts/fix_province.py             # 写库

取值优先级（由高到低）：
  1. 站点列表页自带的地区字段（ccgp / ggzy）—— 这是站点给的，最可信，**不动**
  2. 站点规则里写死的 static_fields.province（省级平台如 gdggzy / hbggzy）
  3. guess_province(采购人, 标题, 正文开头)

只有「站点压根不给地区、全靠猜」的源（目前是 csg）才需要重算；
1、2 两类源的历史值一律保留，避免把站点权威数据洗掉。
重算后仍拿不到就留空——宁缺勿错。空省份在看板上是「暂无线索」，不算骗人。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.crawler.adapters import load_all_sites  # noqa: E402
from app.crawler.normalize import guess_province  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Tender  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        # 站点级写死的省份（省级平台）优先于任何文本推断
        static_prov: dict[str, str] = {}
        has_field: set[str] = set()
        for rule in load_all_sites():
            pv = (rule.static_fields or {}).get("province", "")
            if pv:
                static_prov[rule.id] = pv
            for fields in (rule.json_fields, rule.list_fields):
                if fields and "province" in fields:
                    has_field.add(rule.id)

        known = set(static_prov) | has_field
        print("站点自带/写死省份的源:", sorted(known) or "无")
        print("需要重算的源        :",
              sorted({r.id for r in load_all_sites()} - known) or "无")

        rows = session.execute(select(Tender).order_by(Tender.id)).scalars().all()
        print(f"\n待处理 {len(rows)} 条{'（dry-run，不写库）' if args.dry_run else ''}\n")

        n_fix = n_set = n_clear = n_keep = 0
        for t in rows:
            src = t.source_id or ""
            pinned = static_prov.get(src, "")
            old = t.province or ""
            if pinned:
                # 站点级写死的省份最权威，旧值哪怕是别处猜的也要服从它
                if old != pinned:
                    print(
                        f"  [定] id={t.id:<4} {src:<8} "
                        f"{old or '空':<5} → {pinned:<5} {t.title[:40]}"
                    )
                    n_fix += 1
                    if not args.dry_run:
                        t.province = pinned
                else:
                    n_keep += 1
                continue
            if src in has_field:
                # 站点列表页自带地区字段：历史值能动信，只补全新入库的空记录
                continue

            new = guess_province(
                t.purchaser or "", t.title or "", (t.content_text or "")[:300]
            )
            if new == old:
                n_keep += 1
                continue
            tag = "改" if new and old else ("补" if new else "清")
            print(
                f"  [{tag}] id={t.id:<4} {src:<8} "
                f"{old or '空':<5} → {new or '空':<5} {t.title[:40]}"
            )
            if new and old:
                n_fix += 1
            elif new:
                n_set += 1
            else:
                n_clear += 1
            if not args.dry_run:
                t.province = new

        if not args.dry_run:
            session.commit()
        print(
            f"\n完成：修正 {n_fix} 条，补齐 {n_set} 条，清空不确定值 {n_clear} 条，"
            f"未变动 {n_keep} 条（共 {len(rows)} 条）"
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
