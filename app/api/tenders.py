"""公告检索与详情 + 导出。

对齐 BidInsight API 规格：
  GET /api/tenders  q, start_date, end_date, province, industry,
                    min_budget, max_budget, page, size
  GET /api/tenders/{id}
  POST /api/export

额外按招标雷达的交互核心补了两个参数：
  only_active=true   只看未过期（默认 true）
  sort=deadline      未过期的按截止时间升序，最近截止排最前（CaseBoard 同款交互）
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import require_user
from ..db import get_db
from ..models import Tender
from ..schemas import Paged, TenderDetail, TenderOut

# 公告数据只对登录用户开放（未登录一律 401，前端收到后引导去首页登录）
router = APIRouter(
    prefix="/api/tenders", tags=["tenders"], dependencies=[Depends(require_user)]
)


def _decorate(
    t: Tender, mirrors: int = 0, source_names: dict[str, str] | None = None
) -> dict[str, Any]:
    """补充前端直接可用的派生字段。"""
    now = datetime.now()
    days_left = None
    expired = False
    if t.deadline:
        # 按自然日差算，不用时间戳差：公告里写的「10 月 23 日截止」是当天结束时为止，
        # 若按时刻差，22 号下午看到就会显示「0 天」，读起来像已经过期了。
        days_left = (t.deadline.date() - now.date()).days
        expired = days_left < 0
    return {
        "id": t.id,
        "title": t.title,
        "source_id": t.source_id,
        "source_name": (source_names or {}).get(t.source_id, t.source_id),
        "source_url": t.source_url,
        "notice_type": t.notice_type,
        "is_result": t.is_result,
        "purchaser": t.purchaser,
        "agency": t.agency,
        "budget_amount": t.budget_amount,
        "currency": t.currency,
        "province": t.province,
        "industry": t.industry,
        "publish_time": t.publish_time,
        "deadline": t.deadline,
        "deadline_raw": t.deadline_raw,
        "deadline_confident": t.deadline_confident,
        "cluster_id": t.cluster_id,
        "is_primary": t.is_primary,
        "days_left": days_left,
        "expired": expired,
        "mirrors": mirrors,
    }


def _mirror_counts(db: Session) -> dict[int, int]:
    """统计每个 cluster 下有多少条同源镜像。"""
    rows = db.execute(
        select(Tender.cluster_id, func.count(Tender.id)).group_by(Tender.cluster_id)
    ).all()
    return {cid: cnt for cid, cnt in rows if cid is not None}


def _source_names(db: Session) -> dict[str, str]:
    """来源 id → 中文名。前端列表要显示「这条来自哪个站」。"""
    from ..models import Source  # noqa: PLC0415

    return {s.id: s.name for s in db.execute(select(Source)).scalars().all()}


@router.get("", response_model=Paged)
def list_tenders(
    q: str | None = Query(None, description="标题/采购人关键词"),
    start_date: str | None = Query(None, description="发布日期起 YYYY-MM-DD"),
    end_date: str | None = Query(None, description="发布日期止"),
    province: str | None = None,
    industry: str | None = None,
    notice_type: str | None = None,
    source_id: str | None = None,
    min_budget: float | None = None,
    max_budget: float | None = None,
    # 结果公告默认不进信息流（CaseBoard 经验）
    include_result: bool = Query(False, description="是否包含中标/结果公告"),
    only_active: bool = Query(True, description="只看未截止的"),
    sort: str = Query("deadline", description="deadline | publish | budget"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> Paged:
    conds = [Tender.is_primary.is_(True)]  # 跨源折叠后只暴露主记录
    if not include_result:
        conds.append(Tender.is_result.is_(False))
    if q:
        like = f"%{q}%"
        conds.append(or_(Tender.title.like(like), Tender.purchaser.like(like)))
    if province:
        conds.append(Tender.province == province)
    if industry:
        conds.append(Tender.industry == industry)
    if notice_type:
        conds.append(Tender.notice_type == notice_type)
    if source_id:
        conds.append(Tender.source_id == source_id)
    if min_budget is not None:
        conds.append(Tender.budget_amount >= min_budget)
    if max_budget is not None:
        conds.append(Tender.budget_amount <= max_budget)
    if start_date:
        conds.append(Tender.publish_time >= start_date)
    if end_date:
        conds.append(Tender.publish_time <= end_date)
    if only_active:
        # 「截止时间未知」不等于「已截止」。
        # 真实抓取的公告大多只给发布时间，截止时间要额外取详情才有；
        # 若把 deadline IS NULL 一律过滤掉，刚抓到的线索会整批消失。
        # 所以这里只滤「明确已过期」的。
        active_cond = or_(Tender.deadline >= datetime.now(), Tender.deadline.is_(None))
        if include_result:
            conds.append(or_(active_cond, Tender.is_result.is_(True)))
        else:
            conds.append(active_cond)

    base = select(Tender).where(*conds)

    if sort == "publish":
        base = base.order_by(Tender.publish_time.desc().nulls_last())
    elif sort == "budget":
        base = base.order_by(Tender.budget_amount.desc().nulls_last())
    else:
        # 默认：未过期的按截止时间升序，最近要截止的排最前
        base = base.order_by(Tender.deadline.asc().nulls_last())

    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = db.execute(base.offset((page - 1) * size).limit(size)).scalars().all()

    mc = _mirror_counts(db)
    names = _source_names(db)
    items = [
        TenderOut(
            **_decorate(
                t, mirrors=max(0, mc.get(t.cluster_id or 0, 1) - 1), source_names=names
            )
        )
        for t in rows
    ]
    return Paged(total=total, page=page, size=size, items=items)


@router.get("/{tender_id}", response_model=TenderDetail)
def get_tender(tender_id: int, db: Session = Depends(get_db)) -> TenderDetail:
    from fastapi import HTTPException

    t = db.get(Tender, tender_id)
    if not t:
        raise HTTPException(status_code=404, detail="公告不存在")

    mirrors_list: list[dict] = []
    if t.cluster_id:
        sibs = (
            db.execute(select(Tender).where(Tender.cluster_id == t.cluster_id))
            .scalars()
            .all()
        )
        mirrors_list = [
            {"id": s.id, "source_id": s.source_id, "url": s.source_url, "title": s.title}
            for s in sibs
            if s.id != t.id
        ]

    return TenderDetail(
        **_decorate(t, mirrors=len(mirrors_list), source_names=_source_names(db)),
        content_text=t.content_text,
        raw_path=t.raw_path,
        attachments=t.attachments,
        extract_meta=t.extract_meta,
        mirrors_list=mirrors_list,
    )


@router.post("/export")
def export_csv(
    q: str | None = None,
    province: str | None = None,
    industry: str | None = None,
    only_active: bool = True,
    include_result: bool = False,
    limit: int = Query(2000, ge=1, le=20000),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """导出 CSV（流式，避免大结果集占用内存）。"""
    conds = [Tender.is_primary.is_(True)]
    if not include_result:
        conds.append(Tender.is_result.is_(False))
    if q:
        like = f"%{q}%"
        conds.append(or_(Tender.title.like(like), Tender.purchaser.like(like)))
    if province:
        conds.append(Tender.province == province)
    if industry:
        conds.append(Tender.industry == industry)
    if only_active:
        # 同列表接口：截止时间未知的单子不能被当成已过期
        active_cond = or_(Tender.deadline >= datetime.now(), Tender.deadline.is_(None))
        if include_result:
            conds.append(or_(active_cond, Tender.is_result.is_(True)))
        else:
            conds.append(active_cond)

    rows = (
        db.execute(
            select(Tender)
            .where(*conds)
            .order_by(Tender.deadline.asc().nulls_last())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    buf = io.StringIO()
    # BOM：Excel 打开中文不乱码
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(
        ["标题", "来源", "公告类型", "采购人", "代理机构", "预算(元)", "省份", "行业",
         "发布时间", "截止时间", "链接"]
    )
    for t in rows:
        w.writerow(
            [
                t.title,
                t.source_id,
                t.notice_type,
                t.purchaser,
                t.agency,
                t.budget_amount or "",
                t.province,
                t.industry,
                t.publish_time.strftime("%Y-%m-%d") if t.publish_time else "",
                t.deadline.strftime("%Y-%m-%d") if t.deadline else "",
                t.source_url,
            ]
        )
    buf.seek(0)

    filename = f"tenders_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
