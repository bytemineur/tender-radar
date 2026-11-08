"""APScheduler 定时调度。

不依赖 Redis / Celery：单进程内置调度即可满足「每 N 分钟增量」的场景，
部署时只需要一个 uvicorn 进程，运维成本最低。
量大时再换 Celery + Redis（BidInsight 规格里的可选项）。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .crawler.pipeline import run_all
from .db import SessionLocal
from .state import crawl_lock, record_summary

logger = logging.getLogger("tender-radar.scheduler")

_scheduler: BackgroundScheduler | None = None


def _job() -> None:
    """定时任务体：全量增量采集。拿不到锁就跳过本轮（上一轮还没跑完）。"""
    if not crawl_lock.acquire(blocking=False):
        logger.info("上一轮采集仍在进行，跳过本轮")
        return
    try:
        session = SessionLocal()
        try:
            results = run_all(session)
            record_summary(results)
            new = sum(r.get("new", 0) for r in results.values() if isinstance(r, dict))
            logger.info("采集完成：新增 %s 条", new)
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("定时采集失败: %s", exc)
    finally:
        crawl_lock.release()


def start() -> BackgroundScheduler | None:
    """启动调度器。关闭时返回 None。"""
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("调度已关闭（TR_SCHEDULER_ENABLED=false）")
        return None

    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _job,
        trigger=IntervalTrigger(minutes=settings.scheduler_interval_min),
        id="crawl_all",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    _scheduler.start()
    logger.info("调度已启动：每 %s 分钟增量采集一次", settings.scheduler_interval_min)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
