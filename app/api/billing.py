"""收费功能预留接口。

现阶段不做支付，但把「套餐是什么、谁在哪个套餐、收入怎么看」的口径先定下来：
  GET /api/billing/plans      套餐档位（含占位说明）
  GET /api/billing/summary    用户套餐分布 + 收入概览（管理员）

这样后面接支付时只需要往这两条路径里填真实数据，前端面板与数据库字段
都不用再动。刻意不写「支付回调」「订单表」——那部分取决于最终选哪个
支付渠道（微信/支付宝/Stripe），提前建表多半会建错。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..config import settings
from ..db import get_auth_db
from ..models import PLAN_FREE, User

router = APIRouter(prefix="/api/billing", tags=["billing"])

# 档位定义。现在只有 free 生效，pro/team 是给后台面板展示的占位。
FALLBACK_PLANS = [
    {
        "id": PLAN_FREE,
        "name": "免费版",
        "price": 0,
        "quota_monthly": None,
        "desc": "公测期间不限量使用全部功能",
        "available": True,
    },
    {
        "id": "pro",
        "name": "专业版",
        "price": 99,
        "quota_monthly": 2000,
        "desc": "每月 2000 条线索额度，含关键词订阅推送",
        "available": False,
    },
    {
        "id": "team",
        "name": "团队版",
        "price": 399,
        "quota_monthly": None,
        "desc": "不限额度，5 个子账号，含 API 与优先支持",
        "available": False,
    },
]


@router.get("/plans")
def plans(_user=Depends(require_user)) -> dict:
    """套餐列表。configs/app.yaml 里配了 billing.plans 就用配置的。"""
    cfg = settings.billing.plans
    items = list(FALLBACK_PLANS)
    if cfg:
        # 配置文件可以覆盖价格与文案，但档位 id 保持固定，避免前端判断散落
        known = {p["id"]: p for p in items}
        for pid, patch in cfg.items():
            if pid in known:
                known[pid].update(
                    {
                        k: v
                        for k, v in patch.items()
                        if k in ("name", "price", "quota_monthly", "desc", "available")
                    }
                )
        items = list(known.values())
    return {"enabled": settings.billing.enabled, "items": items}


@router.get("/summary")
def summary(_admin=Depends(require_admin), db: Session = Depends(get_auth_db)) -> dict:
    """后台「套餐与收入」面板的数据。

    没有真实支付就没有真实收入，这里如实返回 0 并标注来源，
    不编造数字——统计面板里最忌讳的就是拿估算值凑版面。
    """
    rows = db.execute(
        select(User.plan, func.count(User.id)).group_by(User.plan)
    ).all()
    dist = [{"label": p or PLAN_FREE, "value": c} for p, c in rows]

    return {
        "enabled": settings.billing.enabled,
        "distribution": dist,
        "revenue": {
            "mrr": 0,
            "currency": "CNY",
            "note": "尚未接入支付，暂无收入数据",
        },
        "plans": FALLBACK_PLANS,
    }
