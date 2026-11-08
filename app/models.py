"""数据模型。

对齐 BidInsight 数据模型规格，并按 CaseBoard 的实战经验补充了
is_result（结果公告分流）、deadline_raw / 抽取溯源、cluster_id（跨源折叠）
三处——这三点是「跨来源重复折叠」「结果公告不进信息流」「2099 年占位截止日」
三个真实坑的结构化落点。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import AuthBase, Base


def _now() -> datetime:
    return datetime.now()


# 角色：普通用户 / 管理员
ROLE_USER = "user"
ROLE_ADMIN = "admin"

# 套餐（收费功能预留，当前一律 free，字段先落地避免以后改表）
PLAN_FREE = "free"


class Source(Base):
    """采集来源（一个站点一份 YAML 规则）。"""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # http | browser
    mode: Mapped[str] = mapped_column(String(16), default="http")
    rule_path: Mapped[str] = mapped_column(String(256), default="")
    # 规则版本号，便于回滚
    rule_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    schedule: Mapped[str] = mapped_column(String(64), default="")
    # 运行状态：idle / running / blocked / error
    health: Mapped[str] = mapped_column(String(16), default="idle")
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    runs: Mapped[list["CrawlRun"]] = relationship(back_populates="source")


class Tender(Base):
    """招标公告。"""

    __tablename__ = "tenders"
    __table_args__ = (
        UniqueConstraint("hash_key", name="uq_tenders_hash_key"),
        Index("ix_tenders_publish_time", "publish_time"),
        Index("ix_tenders_deadline", "deadline"),
        Index("ix_tenders_source", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 去重键：hash(normalize(title) + publish_date + purchaser)
    hash_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sources.id"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, default="")

    title: Mapped[str] = mapped_column(Text, nullable=False)
    # 公告类型：公开招标 / 竞争性磋商 / 中标公告 / 更正公告 ...
    notice_type: Mapped[str] = mapped_column(String(64), default="")
    # 结果公告单独标记：不进主信息流（CaseBoard 经验）
    is_result: Mapped[bool] = mapped_column(Boolean, default=False)

    purchaser: Mapped[str] = mapped_column(String(256), default="")
    agency: Mapped[str] = mapped_column(String(256), default="")

    budget_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")

    province: Mapped[str] = mapped_column(String(64), default="")
    city: Mapped[str] = mapped_column(String(64), default="")
    industry: Mapped[str] = mapped_column(String(64), default="")

    publish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 原始截止时间文本：抽取不确定时保留原文，供人工核对
    deadline_raw: Mapped[str] = mapped_column(String(128), default="")
    # 截止时间是否可信（合理性校验未通过则 False）
    deadline_confident: Mapped[bool] = mapped_column(Boolean, default=False)

    content_text: Mapped[str] = mapped_column(Text, default="")
    # 原文 HTML 落盘路径，保留证据链
    raw_path: Mapped[str] = mapped_column(String(512), default="")
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 抽取溯源：记录哪些字段由规则命中、哪些走了 LLM
    extract_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 跨源折叠：同一标投在多个平台时共享一个 cluster_id
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 折叠时保留最早记录为「主记录」
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CrawlRun(Base):
    """抓取批次——作业最小单元 = 站点 + 时间片（幂等）。"""

    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sources.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # running / success / partial / failed / blocked
    status: Mapped[str] = mapped_column(String(16), default="running")
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_dup: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本次跑到的时间片边界，用于增量与断点续跑
    since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source: Mapped["Source"] = relationship(back_populates="runs")


# ══════════════════════════════════════════════════════════════════════
# 账号体系
#
# 邮箱 + 验证码登录，不设密码：省掉密码存储、找回、弱口令这些麻烦，
# 也符合「目前不限制注册」的阶段。验证码走 LoginCode，会话走 AuthSession
# （令牌存库而不是 JWT 无状态，是为了后台能直接踢人、能看到在线设备）。
#
# **这里全部继承 AuthBase 而不是 Base**：账号表独立成库（data/auth.db），
# 与采集的业务库分开。混在一个库时，采集的长事务抱着 SQLite 的写锁，
# 登录要写 auth_sessions 就撞 "database is locked" → 登录接口 500。
# 新增账号相关的表时记得也继承 AuthBase，否则会建进业务库、查不到。
# ══════════════════════════════════════════════════════════════════════


class User(AuthBase):
    """注册用户。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(190), nullable=False, unique=True)
    # 昵称：注册时没让填，先拿邮箱前缀兜底，后台可改
    nickname: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default=ROLE_USER)
    # 账号停用：只拦登录，不删数据（保留统计口径）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── 收费预留 ──────────────────────────────────────────────
    # 现阶段全部是 free，套餐/额度/到期时间三个字段先把位置占住，
    # 接支付时只需要写业务逻辑，不用再动表结构。
    plan: Mapped[str] = mapped_column(String(32), default=PLAN_FREE)
    # 每月可查看的线索配额，null 表示不限
    quota_monthly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    login_count: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now
    )


class LoginCode(AuthBase):
    """邮箱登录验证码。

    只存哈希不存明文——验证码等同于一次性密码，落库明文属于低级错误。
    """

    __tablename__ = "login_codes"
    __table_args__ = (
        Index("ix_login_codes_email", "email"),
        Index("ix_login_codes_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(190), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 发信渠道：smtp / dev（开发模式只打日志）。留痕便于排查「收不到邮件」
    channel: Mapped[str] = mapped_column(String(16), default="dev")
    request_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuthSession(AuthBase):
    """登录会话。

    令牌本身是随机串，库里只存哈希；这样即使数据库被读也拿不到有效令牌。
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_token", "token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship()


class PageView(AuthBase):
    """浏览埋点。

    记录每次页面访问，用于后台的「每天浏览量 / 浏览来源」统计。
    ip 只存哈希（不存原文）——流量统计不需要知道具体是谁，少存一份隐私风险。
    """

    __tablename__ = "page_views"
    __table_args__ = (
        Index("ix_page_views_created", "created_at"),
        Index("ix_page_views_path", "path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 匿名访客标识（cookie），用于算 UV；登录后会带上 user_id
    visitor_id: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str] = mapped_column(String(256), default="/")
    title: Mapped[str] = mapped_column(String(300), default="")
    # 来源：直接访问则为空
    referrer: Mapped[str] = mapped_column(Text, default="")
    ref_host: Mapped[str] = mapped_column(String(190), default="")
    # 归类后的渠道：direct / search / social / external
    ref_kind: Mapped[str] = mapped_column(String(16), default="direct")
    utm_source: Mapped[str] = mapped_column(String(64), default="")
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
