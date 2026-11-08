"""分析与可视化数据源。

对齐 BidInsight：
  GET /api/stats/trend            按日公告数量与预算总额
  GET /api/stats/distribution     区域 / 行业 Top-N
  GET /api/stats/overview         首页概览卡片
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import require_user
from ..db import get_db
from ..models import Source, Tender

# 统计数据同样只对登录用户开放
router = APIRouter(
    prefix="/api/stats", tags=["stats"], dependencies=[Depends(require_user)]
)


@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict:
    """首页概览：总量、未过期、7 天内到期、预算合计。"""
    now = datetime.now()
    week = now + timedelta(days=7)

    base = [Tender.is_primary.is_(True), Tender.is_result.is_(False)]

    total = db.execute(select(func.count(Tender.id)).where(*base)).scalar_one()
    # 「活跃」= 明确未过期 或 截止时间未知。
    # 真实抓取的公告常常只有发布时间，截止时间要在详情页才有；
    # 若只算 deadline >= now，刚抓到的线索会全部显示为 0。
    active_cond = or_(Tender.deadline >= now, Tender.deadline.is_(None))
    active = db.execute(
        select(func.count(Tender.id)).where(*base, active_cond)
    ).scalar_one()
    urgent = db.execute(
        select(func.count(Tender.id)).where(
            *base, Tender.deadline >= now, Tender.deadline <= week
        )
    ).scalar_one()
    budget_sum = db.execute(
        select(func.coalesce(func.sum(Tender.budget_amount), 0)).where(*base)
    ).scalar_one()
    sources = db.execute(select(func.count(Source.id))).scalar_one()

    return {
        "total": total,
        "active": active,
        "urgent": urgent,
        "budget_sum": float(budget_sum or 0),
        "sources": sources,
    }


@router.get("/trend")
def trend(days: int = Query(30, ge=7, le=180), db: Session = Depends(get_db)) -> dict:
    """时间趋势：按日公告数量与预算总额。"""
    since = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(
            func.date(Tender.publish_time).label("d"),
            func.count(Tender.id),
            func.coalesce(func.sum(Tender.budget_amount), 0),
        )
        .where(
            Tender.is_primary.is_(True),
            Tender.is_result.is_(False),
            Tender.publish_time >= since,
        )
        .group_by("d")
        .order_by("d")
    ).all()

    # 补齐没有数据的日期，避免折线断裂
    by_day = {str(r[0]): (r[1], float(r[2] or 0)) for r in rows}
    labels: list[str] = []
    counts: list[int] = []
    budgets: list[float] = []
    for i in range(days, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(d)
        c, b = by_day.get(d, (0, 0.0))
        counts.append(c)
        budgets.append(round(b / 10000, 1))  # 万元

    return {"labels": labels, "counts": counts, "budgets_wan": budgets}


@router.get("/distribution")
def distribution(
    dim: str = Query("province", pattern="^(province|industry|notice_type|source)$"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    """区域 / 行业 / 公告类型 / 来源分布 Top-N。"""
    col = {
        "province": Tender.province,
        "industry": Tender.industry,
        "notice_type": Tender.notice_type,
        "source": Tender.source_id,
    }[dim]

    rows = db.execute(
        select(col, func.count(Tender.id))
        .where(
            Tender.is_primary.is_(True),
            Tender.is_result.is_(False),
            col != "",
        )
        .group_by(col)
        .order_by(func.count(Tender.id).desc())
        .limit(limit)
    ).all()

    return {
        "dim": dim,
        "labels": [r[0] for r in rows],
        "values": [r[1] for r in rows],
    }


@router.get("/upcoming")
def upcoming(days: int = Query(7, ge=1, le=30), db: Session = Depends(get_db)) -> dict:
    """即将截止清单——招标雷达最核心的「最近要截止的高亮」。"""
    now = datetime.now()
    until = now + timedelta(days=days)
    rows = (
        db.execute(
            select(Tender)
            .where(
                Tender.is_primary.is_(True),
                Tender.is_result.is_(False),
                Tender.deadline >= now,
                Tender.deadline <= until,
            )
            .order_by(Tender.deadline.asc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": t.id,
                "title": t.title,
                "purchaser": t.purchaser,
                "deadline": t.deadline.strftime("%Y-%m-%d") if t.deadline else "",
                "days_left": (t.deadline.date() - now.date()).days if t.deadline else None,
            }
            for t in rows
        ]
    }
