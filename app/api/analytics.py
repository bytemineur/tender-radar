"""浏览统计：埋点上报 + 后台流量分析。

  POST /api/analytics/track                 埋点（公开，首页也要记）
  GET  /api/analytics/overview              概览：PV / UV / 注册用户（管理员）
  GET  /api/analytics/traffic               按天 PV/UV 趋势（管理员）
  GET  /api/analytics/referrers             浏览来源：渠道分布 + 域名 Top（管理员）
  GET  /api/analytics/pages                 页面 Top（管理员）

两个刻意的取舍：

1. **UV 用 cookie 里的匿名 visitor_id，不靠 IP。**
   同一家公司几十个人共用一个出口 IP，按 IP 去重会把 UV 严重压低；
   反过来一个人换网络又会被算成两个。cookie 不完美（清了就算新访客），
   但对「看看有多少人在用」这个颗粒度够用了。

2. **IP 只存哈希。**
   流量统计不需要知道「具体是谁」，存原文只是白白多一份隐私风险。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import client_ip, current_user_optional, require_admin
from ..db import get_auth_db
from ..models import PageView, User

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# 直接访问 / 搜索引擎 / 社交媒体 / 外部链接
SEARCH_HOSTS = (
    "baidu.com", "google.com", "bing.com", "so.com", "sogou.com",
    "yandex.com", "duckduckgo.com", "sm.cn", "so.toutiao.com",
)
SOCIAL_HOSTS = (
    "weibo.com", "weixin.qq.com", "mp.weixin.qq.com", "zhihu.com",
    "xiaohongshu.com", "xhslink.com", "douyin.com", "twitter.com",
    "x.com", "linkedin.com", "facebook.com", "juejin.cn",
)


def classify_ref(host: str) -> str:
    """把来源域名归到渠道。不做模糊匹配：认不出就归 external，宁可粗一点。"""
    if not host:
        return "direct"
    h = host.lower().lstrip("www.")
    # 自家域名内部跳转不算「外部来源」
    if any(h.endswith(x) for x in SEARCH_HOSTS):
        return "search"
    if any(h.endswith(x) for x in SOCIAL_HOSTS):
        return "social"
    return "external"


class TrackIn(BaseModel):
    visitor_id: str = Field("", max_length=64)
    path: str = Field("/", max_length=256)
    title: str = Field("", max_length=300)
    referrer: str = Field("", max_length=2000)
    utm_source: str = Field("", max_length=64)


@router.post("/track")
def track(
    body: TrackIn,
    request: Request,
    db: Session = Depends(get_auth_db),
    user: User | None = Depends(current_user_optional),
) -> dict:
    """记录一次页面访问。失败也不该影响页面，所以异常一律吞掉。"""
    host = ""
    if body.referrer:
        try:
            host = urlparse(body.referrer).netloc.lower()
        except ValueError:
            host = ""
    # 站内跳转（首页 → 控制台）不是「来源」。不排掉的话，来源榜第一名
    # 永远是自己，把真正的外部渠道挤掉。
    own = (request.headers.get("host") or "").lower()
    if host and (host == own or host.split(":")[0] == own.split(":")[0]):
        host = ""

    ip = client_ip(request)
    try:
        db.add(
            PageView(
                visitor_id=(body.visitor_id or "")[:64],
                user_id=user.id if user else None,
                path=(body.path or "/")[:256],
                title=(body.title or "")[:300],
                referrer=(body.referrer or "")[:2000],
                ref_host=host[:190],
                ref_kind=classify_ref(host),
                utm_source=(body.utm_source or "")[:64],
                ip_hash=hashlib.sha256(ip.encode("utf-8")).hexdigest() if ip else "",
                user_agent=(request.headers.get("user-agent") or "")[:300],
            )
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001  埋点不该拖垮页面
        db.rollback()
        from ..config import settings as _s  # noqa: PLC0415

        if _s.notify.enabled:
            import logging

            logging.getLogger("tender-radar.analytics").warning("埋点写入失败: %s", exc)
        return {"ok": False}
    return {"ok": True}


def _since(days: int) -> datetime:
    """days=0 表示只看今天 00:00 之后。"""
    now = datetime.now()
    if days <= 0:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=days - 1)


def _pv_uv(db: Session, since: datetime) -> tuple[int, int]:
    pv = db.execute(
        select(func.count(PageView.id)).where(PageView.created_at >= since)
    ).scalar_one()
    uv = db.execute(
        select(func.count(func.distinct(PageView.visitor_id))).where(
            PageView.created_at >= since, PageView.visitor_id != ""
        )
    ).scalar_one()
    return pv, uv


@router.get("/overview")
def overview(
    days: int = Query(30, ge=0, le=180),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_auth_db),
) -> dict:
    """后台概览卡片：今日 / 区间 PV、UV、注册用户。"""
    today_pv, today_uv = _pv_uv(db, _since(0))
    range_pv, range_uv = _pv_uv(db, _since(days))

    # 新用户按「在今天 / 在区间」分别统计，卡片直接展示，不用前端再算
    today_users = db.execute(
        select(func.count(User.id)).where(User.created_at >= _since(0))
    ).scalar_one()
    total_users = db.execute(select(func.count(User.id))).scalar_one()
    active_users = db.execute(
        select(func.count(func.distinct(PageView.user_id))).where(
            PageView.created_at >= _since(days), PageView.user_id.isnot(None)
        )
    ).scalar_one()

    return {
        "days": days,
        "today": {"pv": today_pv, "uv": today_uv, "new_users": today_users},
        "range": {
            "pv": range_pv,
            "uv": range_uv,
            # 人均浏览页数：判断访问深度，低于 1.5 基本说明「进来就走」
            "pv_per_uv": round(range_pv / range_uv, 2) if range_uv else 0,
        },
        "users": {"total": total_users, "active": active_users},
    }


@router.get("/traffic")
def traffic(
    days: int = Query(30, ge=7, le=180),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_auth_db),
) -> dict:
    """按天的 PV / UV / 新注册用户，补齐空白日期避免折线断裂。"""
    since = _since(days)
    rows = db.execute(
        select(
            func.date(PageView.created_at).label("d"),
            func.count(PageView.id),
            func.count(func.distinct(PageView.visitor_id)),
        )
        .where(PageView.created_at >= since)
        .group_by("d")
    ).all()
    pv_by = {str(r[0]): r[1] for r in rows}
    uv_by = {str(r[0]): r[2] for r in rows}

    urows = db.execute(
        select(func.date(User.created_at).label("d"), func.count(User.id))
        .where(User.created_at >= since)
        .group_by("d")
    ).all()
    u_by = {str(r[0]): r[1] for r in urows}

    labels, pvs, uvs, news = [], [], [], []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(d)
        pvs.append(pv_by.get(d, 0))
        uvs.append(uv_by.get(d, 0))
        news.append(u_by.get(d, 0))
    return {"labels": labels, "pv": pvs, "uv": uvs, "new_users": news}


@router.get("/referrers")
def referrers(
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(10, ge=1, le=50),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_auth_db),
) -> dict:
    """浏览来源：先看渠道大类，再看具体域名 / UTM。"""
    since = _since(days)
    kinds = db.execute(
        select(PageView.ref_kind, func.count(PageView.id))
        .where(PageView.created_at >= since)
        .group_by(PageView.ref_kind)
        .order_by(func.count(PageView.id).desc())
    ).all()
    hosts = db.execute(
        select(PageView.ref_host, func.count(PageView.id))
        .where(PageView.created_at >= since, PageView.ref_host != "")
        .group_by(PageView.ref_host)
        .order_by(func.count(PageView.id).desc())
        .limit(limit)
    ).all()
    utms = db.execute(
        select(PageView.utm_source, func.count(PageView.id))
        .where(PageView.created_at >= since, PageView.utm_source != "")
        .group_by(PageView.utm_source)
        .order_by(func.count(PageView.id).desc())
        .limit(limit)
    ).all()

    total = db.execute(
        select(func.count(PageView.id)).where(PageView.created_at >= since)
    ).scalar_one()

    return {
        "total": total,
        "kinds": [{"label": k, "value": c} for k, c in kinds],
        "hosts": [{"label": h or "（无）", "value": c} for h, c in hosts],
        "utm": [{"label": s or "（无）", "value": c} for s, c in utms],
    }


@router.get("/pages")
def pages(
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(10, ge=1, le=50),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_auth_db),
) -> dict:
    """被访问最多的页面。"""
    since = _since(days)
    rows = db.execute(
        select(PageView.path, func.count(PageView.id))
        .where(PageView.created_at >= since)
        .group_by(PageView.path)
        .order_by(func.count(PageView.id).desc())
        .limit(limit)
    ).all()
    return {"items": [{"label": p or "/", "value": c} for p, c in rows]}
