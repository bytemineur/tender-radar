"""进程内共享状态。

采集互斥锁用 threading.Lock 而非 asyncio.Lock：
APScheduler 在独立线程跑任务，两边要共用同一把锁，线程锁才是正确的粒度。
"""

from __future__ import annotations

import threading
from datetime import datetime

# 同一时刻只允许一轮采集，避免用户连点导致目标站被打
crawl_lock = threading.Lock()

# 最近一次全量采集的摘要，供前端展示
last_summary: dict = {"at": None, "results": {}}


def record_summary(results: dict) -> None:
    last_summary["at"] = datetime.now().isoformat(timespec="seconds")
    last_summary["results"] = results
