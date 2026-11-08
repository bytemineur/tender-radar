"""来源管理、手动抓取、健康检查。

手动抓取放在线程池执行，避免阻塞事件循环；生产量大建议换成 Celery。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..config import settings
from ..crawler.pipeline import run_all, run_source, sync_sites
from ..db import get_db
from ..models import Source, Tender
from ..schemas import CrawlRequest, CrawlResult, HealthOut, SourceOut, SourcePatch
from ..state import crawl_lock, record_summary

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    """健康检查对外公开：首页要显示「已收录 N 条」，不能要求先登录。"""
    total = db.execute(select(func.count(Tender.id))).scalar_one()
    n_src = db.execute(select(func.count(Source.id))).scalar_one()
    last = db.execute(select(func.max(Source.last_run_at))).scalar_one()
    # 这个接口是公开的（首页要用），所以不回库文件的绝对路径——
    # 那等于把服务器的目录结构挂在公网上。只回驱动类型。
    backend = settings.database_url.split(":")[0] if settings.database_url else ""
    return HealthOut(
        status="ok",
        db=backend,
        total=total,
        sources=n_src,
        scheduler=settings.scheduler_enabled,
        last_run_at=last,
    )


# 以下来源管理与采集接口需要登录；其中「改来源 / 触发采集」属于运维动作，
# 收敛到管理员，避免普通用户随手把整个采集任务点起来。
@router.get("/sources", response_model=list[SourceOut])
def list_sources(
    db: Session = Depends(get_db), _u=Depends(require_user)
) -> list[SourceOut]:
    rows = db.execute(select(Source).order_by(Source.id)).scalars().all()
    return [SourceOut.model_validate(r) for r in rows]


@router.post("/sources/reload")
def reload_sources(
    db: Session = Depends(get_db), _u=Depends(require_admin)
) -> dict:
    """重新扫描 configs/sites/*.yaml 并同步到库——加来源不用改代码。"""
    added = sync_sites(db)
    total = db.execute(select(func.count(Source.id))).scalar_one()
    return {"added": added, "total": total}


@router.patch("/sources/{site_id}", response_model=SourceOut)
def patch_source(
    site_id: str,
    body: SourcePatch,
    db: Session = Depends(get_db),
    _u=Depends(require_admin),
) -> SourceOut:
    src = db.get(Source, site_id)
    if not src:
        raise HTTPException(404, "来源不存在")
    if body.enabled is not None:
        src.enabled = body.enabled
    if body.name is not None:
        src.name = body.name
    db.commit()
    db.refresh(src)
    return SourceOut.model_validate(src)


@router.post("/crawl", response_model=CrawlResult)
async def trigger_crawl(
    body: CrawlRequest, db: Session = Depends(get_db), _u=Depends(require_admin)
) -> CrawlResult:
    """手动触发采集。单源或全量。并发保护：同一时刻只允许一轮。

    只开放给管理员：一次全量采集要打十几个政务站点，被随手点几下很容易
    触发对方限流，把整站采集搞挂。
    """
    if not crawl_lock.acquire(blocking=False):
        raise HTTPException(409, "已有采集任务在运行，请稍后再试")

    def _work() -> dict:
        from ..db import SessionLocal

        s = SessionLocal()
        try:
            if body.site_id:
                return {
                    body.site_id: run_source(
                        s,
                        body.site_id,
                        max_pages=body.max_pages,
                        keyword=body.keyword,
                        fetch_detail=body.fetch_detail,
                    )
                }
            return run_all(s, fetch_detail=body.fetch_detail)
        finally:
            s.close()

    try:
        results = await asyncio.to_thread(_work)
    finally:
        crawl_lock.release()

    record_summary(results)
    return CrawlResult(ok=True, results=results)
