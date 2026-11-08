"""数据库会话 —— **业务库与账号库分开**。

为什么分库（这是踩过坑之后的决定，不是过度设计）：

采集一轮要写几百条数据，事务能开好几分钟；SQLite 的写锁是**整库一把**，
这期间 Web 侧任何一次写入都会撞上它。而登录/鉴权每个请求都要写
auth_sessions.last_seen_at —— 于是「正在采集」和「正在登录」互斥，
谁后到谁报错 `database is locked`，用户看到的就是登录接口 500。
WAL 只解决「读不被写挡」，写与写之间依然互斥，靠 busy_timeout 也只是
让后到的排队等，采集几分钟就干等几分钟。

真正的解法是把**账号数据彻底搬出业务库**：
    业务库 data/tender.db → sources / tenders / crawl_runs（采集独占）
    账号库 data/auth.db   → users / login_codes / auth_sessions / page_views
两个库各有一把锁，采集写业务库时登录写账号库，互不相干。附带好处是
备份/迁移用户数据时不用动几十兆的招标正文。

切 PostgreSQL 用 TR_DATABASE_URL 即可；PG 是行级锁不存在这个问题，
所以那时 auth_database_url 默认沿用主库，不会白拆一份。

环境变量：
    TR_DATABASE_URL        业务库（默认 sqlite:///data/tender.db）
    TR_AUTH_DATABASE_URL   账号库（默认 sqlite:///data/auth.db）
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger("tender-radar.db")


class Base(DeclarativeBase):
    """业务库 ORM 基类：招标数据与采集状态。"""


class AuthBase(DeclarativeBase):
    """账号库 ORM 基类：用户、会话、验证码、埋点。

    两张表分属不同 metadata，`create_all` 要分别指定 bind，别指望一次建完。
    """


def _sqlite_pragma(engine) -> None:  # noqa: ANN001
    """SQLite 的三条必备 PRAGMA。

    busy_timeout 尤其别省：即使分了库，账号库内部（埋点 / 会话更新）仍有
    并发写，没有它照样 "database is locked"。
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):  # noqa: ANN001, ANN202
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


def _build_engine(url: str):
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
        _sqlite_pragma(engine)
        return engine
    return create_engine(url, pool_pre_ping=True, future=True)


engine = _build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

auth_engine = _build_engine(settings.auth_database_url)
AuthSessionLocal = sessionmaker(
    bind=auth_engine, autoflush=False, autocommit=False, future=True
)


# ══════════════════════════════════════════════════════════════════════
# 一次性迁移：把老版本混在业务库里的账号表搬到账号库
#
# 老库已经跑了一段时间，users / auth_sessions 里有真实用户，直接 create_all
# 建个空库等于把人踢下线。所以这里复制数据，**确认搬完才删源表**，
# 且整个过程幂等（搬完源表就没了，第二次进来直接跳过）。
# ══════════════════════════════════════════════════════════════════════

AUTH_TABLES = ("users", "login_codes", "auth_sessions", "page_views")


def _sqlite_file(url: str) -> Path | None:
    """取 SQLite 文件的绝对路径；非 SQLite 或内存库返回 None。"""
    if not url.startswith("sqlite"):
        return None
    try:
        db = make_url(url).database
    except Exception:  # noqa: BLE001  解析失败就当非文件库处理
        return None
    if not db or db == ":memory:":
        return None
    return Path(db)


def _table_exists(conn: sqlite3.Connection, name: str, schema: str = "main") -> bool:
    row = conn.execute(
        "SELECT 1 FROM %s.sqlite_master WHERE type='table' AND name=?" % schema,
        (name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, name: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({name})").fetchall()]


def migrate_auth_tables_from_main() -> int:
    """把业务库里的旧账号表数据搬到账号库。返回搬运的行数总和。

    只在「两库都是文件型 SQLite 且不是同一个文件」时才做，其他情况（PG、
    内存库、本来就是两个库）直接返回 0。搬完会 DROP 主库旧表，验证不通过
    则保留源表并告警——宁可多留一份，绝不先删后查。
    """
    main_file = _sqlite_file(settings.database_url)
    auth_file = _sqlite_file(settings.auth_database_url)
    if not main_file or not auth_file:
        return 0
    if main_file.resolve() == auth_file.resolve():
        return 0  # 指向同一个库，没什么好搬的
    if not main_file.exists():
        return 0  # 全新部署，主库还没建
    auth_file.parent.mkdir(parents=True, exist_ok=True)

    moved = 0
    conn = sqlite3.connect(str(main_file), timeout=30)
    try:
        conn.execute("ATTACH DATABASE ? AS authdb", (str(auth_file),))
        todo = [t for t in AUTH_TABLES if _table_exists(conn, t, "main")]
        if not todo:
            return 0
        if not all(_table_exists(conn, t, "authdb") for t in todo):
            logger.warning("账号库表结构缺失，跳过旧数据迁移")
            return 0

        logger.info("检测到旧版账号表仍在业务库，开始迁移到 %s", auth_file.name)
        for t in todo:
            cols_main = _columns(conn, t, "main")
            cols_auth = _columns(conn, t, "authdb")
            cols = [c for c in cols_main if c in cols_auth]
            if not cols:
                logger.warning("表 %s 列不匹配，跳过（业务库 %s / 账号库 %s）",
                               t, cols_main, cols_auth)
                continue
            collist = ", ".join(f'"{c}"' for c in cols)
            # INSERT OR IGNORE：重复执行时不会因主键冲突炸掉
            cur = conn.execute(
                f"INSERT OR IGNORE INTO authdb.{t} ({collist}) SELECT {collist} FROM main.{t}"
            )
            moved += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        conn.commit()

        # 搬完先核对，行数对不上就不删源表——数据比整洁重要
        ok = True
        for t in todo:
            n_main = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
            n_auth = conn.execute(f"SELECT COUNT(*) FROM authdb.{t}").fetchone()[0]
            if n_auth < n_main:
                ok = False
                logger.warning(
                    "表 %s 迁移后行数少于源表（%s < %s），保留业务库旧表不动",
                    t, n_auth, n_main,
                )
        if ok:
            for t in todo:
                conn.execute(f"DROP TABLE main.{t}")
            conn.commit()
            logger.info("旧账号表已迁出业务库：%s", ", ".join(todo))
    except Exception as exc:  # noqa: BLE001  迁移失败不能拖垮启动
        conn.rollback()
        logger.warning("账号表迁移失败（不影响启动）：%s", exc)
        return 0
    finally:
        try:
            conn.execute("DETACH DATABASE authdb")
        except Exception:  # noqa: BLE001
            pass
        conn.close()
    return moved


def init_db() -> None:
    """建表（首次运行）。生产建议改用 Alembic 迁移。"""
    from . import models  # noqa: F401  确保两个 Base 的模型都已注册

    Base.metadata.create_all(bind=engine)
    AuthBase.metadata.create_all(bind=auth_engine)

    # 老库升级：把混在业务库里的账号数据搬出来再删源表（幂等）
    moved = migrate_auth_tables_from_main()
    if moved:
        logger.info("账号数据迁移完成，共 %s 行", moved)


# ══════════════════════════════════════════════════════════════════════
# 会话
# ══════════════════════════════════════════════════════════════════════


@contextmanager
def session_scope() -> Iterator[Session]:
    """业务库：带事务的会话上下文。"""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def auth_session_scope() -> Iterator[Session]:
    """账号库：带事务的会话上下文。"""
    s = AuthSessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：业务库会话。"""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def get_auth_db() -> Iterator[Session]:
    """FastAPI 依赖：账号库会话。

    凡是碰 User / AuthSession / LoginCode / PageView 的接口都用这个，
    别手滑拿成 get_db —— 那会把登录重新拽回采集的锁上。
    """
    s = AuthSessionLocal()
    try:
        yield s
    finally:
        s.close()
