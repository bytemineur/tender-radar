"""配置中心。

设计原则（沿用 BidInsight 约定）：配置中心化，不在代码里硬编码站点规则。
站点规则全部落在 configs/sites/*.yaml，由 crawler.adapters 动态加载。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ── 路径 ────────────────────────────────────────────────────────────────
ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = ROOT / "configs"
SITES_DIR: Path = CONFIG_DIR / "sites"
WEB_DIR: Path = ROOT / "web"
DATA_DIR: Path = ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _env_bool(name: str, default: bool) -> bool:
    """读布尔型环境变量。

    不能写成 `raw.lower() in {"1","true",...}` 就完事：那样凡是认不出来的值
    都会被当成 False。像 TR_REQUIRE_LOGIN 这种安全开关，一旦 .env 里的值被
    污染（典型是行尾注释没剥干净，变成 "true  # 说明"），门禁就会被静默关掉
    ——服务照样起来，日志一句不提，数据在公网裸奔。所以认不出来时回退默认值
    并告警，而不是默认 False。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    logger.warning(
        "环境变量 %s 的值 %r 无法识别为布尔值，已回退为默认值 %s。"
        "若这是安全相关开关（如 TR_REQUIRE_LOGIN），请检查 .env 里的写法。",
        name,
        raw,
        default,
    )
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class CrawlConfig:
    """采集层全局参数。"""

    # 请求限速（秒）。招标站点普遍脆弱，默认 2–5 秒随机间隔。
    min_delay: float = _env_float("TR_MIN_DELAY", 2.0)
    max_delay: float = _env_float("TR_MAX_DELAY", 5.0)
    # 单次请求超时
    timeout: float = _env_float("TR_TIMEOUT", 25.0)
    # 指数退避：1s / 4s / 16s
    max_retries: int = _env_int("TR_MAX_RETRIES", 3)
    backoff_base: float = _env_float("TR_BACKOFF_BASE", 1.0)
    # 单源单次最多翻多少页，防止失控
    max_pages: int = _env_int("TR_MAX_PAGES", 5)
    # 是否允许降级到 Playwright 无头浏览器
    browser_fallback: bool = _env_bool("TR_BROWSER_FALLBACK", True)
    # 是否开启 LLM 兜底抽取（关闭则纯规则，零成本运行）
    llm_fallback: bool = _env_bool("TR_LLM_FALLBACK", False)


@dataclass
class LLMConfig:
    """LLM 兜底抽取（可选）。任意 OpenAI 兼容接口。"""

    base_url: str = os.getenv("TR_LLM_BASE_URL", "https://api.deepseek.com")
    api_key: str = os.getenv("TR_LLM_API_KEY", "")
    model: str = os.getenv("TR_LLM_MODEL", "deepseek-chat")
    timeout: float = _env_float("TR_LLM_TIMEOUT", 60.0)


@dataclass
class AuthConfig:
    """账号与登录。

    邮箱 + 验证码，不设密码。没配 SMTP 时自动进入「开发模式」：
    验证码只打到后端日志，并随接口返回给前端提示——这样本地零配置也能
    跑通注册登录全流程，配好 SMTP 后自动切回真实发信。
    """

    # 管理员邮箱白名单（逗号分隔）。这些邮箱登录即管理员，优先级最高。
    admin_emails: list[str] = field(default_factory=list)
    # 登录态有效期（天）
    session_days: int = 7
    # 验证码有效期（秒）
    code_ttl: int = 600
    # 验证码长度
    code_len: int = 6
    # 单邮箱发码频率限制（秒）
    code_interval: int = 60
    # 是否强制要求登录才能访问数据接口。关掉后退化成旧版全开放（仅调试用）
    require_login: bool = True

    # ── SMTP ──────────────────────────────────────────────────
    # 六项全填才会走真实发信；缺一项就降级到开发模式。
    smtp_host: str = ""
    smtp_port: int = 465
    # True = SSL 直连（465）；False = STARTTLS（587）
    smtp_ssl: bool = True
    smtp_user: str = ""
    smtp_pass: str = ""
    # 发件人显示名与地址，留空则用 smtp_user
    mail_from: str = ""

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    @property
    def dev_mode(self) -> bool:
        """未配 SMTP → 开发模式（验证码回显到前端）。"""
        return not self.smtp_ready


@dataclass
class BillingConfig:
    """收费功能预留。

    现阶段只是把开关和文案放在配置里，后台面板据此显示占位页。
    真正接支付时，这里会补上价格档位与支付渠道配置。
    """

    enabled: bool = False
    # 各套餐每月可查看线索数（null 表示不限）
    plans: dict = field(default_factory=dict)


@dataclass
class NotifyConfig:
    """失败告警推送。"""

    enabled: bool = _env_bool("TR_NOTIFY_ENABLED", False)
    # webhook 兼容企业微信机器人 / 飞书 / Slack 的通用 JSON 推送
    webhook: str = os.getenv("TR_NOTIFY_WEBHOOK", "")
    # 单源连续失败多少次才告警，避免抖动误报
    fail_threshold: int = _env_int("TR_NOTIFY_FAIL_THRESHOLD", 3)


def _default_auth_url(main_url: str) -> str:
    """账号库的默认连接串。

    SQLite 必须拆出来：采集一轮要写几百条、事务能开好几分钟，这期间它
    抱着主库的写锁不放，而登录/鉴权每个请求都要写 auth_sessions——两边
    撞在同一把锁上就是 "database is locked" → 登录接口 500。账号数据（用户、
    会话、验证码、埋点）独立成库后，采集再忙也碰不到登录。
    PostgreSQL 没有这个问题（MVCC 行级锁），所以沿用主库，不白拆。
    """
    if main_url.startswith("sqlite"):
        return f"sqlite:///{(DATA_DIR / 'auth.db').as_posix()}"
    return main_url


@dataclass
class Settings:
    """全局设置。"""

    app_name: str = "招标雷达"
    # 数据库连接串。默认 SQLite（零运维），生产可切 PostgreSQL。
    # 这个库只放业务数据：sources / tenders / crawl_runs
    database_url: str = os.getenv(
        "TR_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'tender.db').as_posix()}"
    )
    # 账号库：users / login_codes / auth_sessions / page_views
    # 留空则按 _default_auth_url 推导（SQLite 拆成 data/auth.db，PG 沿用主库）
    auth_database_url: str = field(
        default_factory=lambda: os.getenv("TR_AUTH_DATABASE_URL")
        or _default_auth_url(
            os.getenv("TR_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'tender.db').as_posix()}")
        )
    )
    # 调度开关：关闭后只提供 API，不自动抓取
    scheduler_enabled: bool = _env_bool("TR_SCHEDULER_ENABLED", True)
    # 调度周期（分钟）：增量抓取间隔
    scheduler_interval_min: int = _env_int("TR_SCHEDULER_INTERVAL_MIN", 120)
    # 启动时是否立即跑一轮
    crawl_on_startup: bool = _env_bool("TR_CRAWL_ON_STARTUP", False)
    # 关键词订阅（命中后推送），逗号分隔；留空表示不启用
    watch_keywords: list[str] = field(
        default_factory=lambda: [
            k.strip()
            for k in os.getenv("TR_WATCH_KEYWORDS", "").split(",")
            if k.strip()
        ]
    )
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    billing: BillingConfig = field(default_factory=BillingConfig)

    @classmethod
    def load(cls) -> "Settings":
        path = CONFIG_DIR / "app.yaml"
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        else:
            raw = {}
        # 环境变量优先级高于文件
        auth_raw = raw.get("auth") or {}
        billing_raw = raw.get("billing") or {}
        return cls(
            app_name=raw.get("app_name", "招标雷达"),
            database_url=os.getenv("TR_DATABASE_URL")
            or raw.get("database_url")
            or cls.database_url,
            auth_database_url=os.getenv("TR_AUTH_DATABASE_URL")
            or raw.get("auth_database_url")
            or _default_auth_url(
                os.getenv("TR_DATABASE_URL")
                or raw.get("database_url")
                or cls.database_url
            ),
            scheduler_enabled=_env_bool(
                "TR_SCHEDULER_ENABLED", bool(raw.get("scheduler_enabled", True))
            ),
            scheduler_interval_min=_env_int(
                "TR_SCHEDULER_INTERVAL_MIN", int(raw.get("scheduler_interval_min", 120))
            ),
            crawl_on_startup=_env_bool(
                "TR_CRAWL_ON_STARTUP", bool(raw.get("crawl_on_startup", False))
            ),
            watch_keywords=[
                k.strip() for k in (raw.get("watch_keywords") or []) if k.strip()
            ],
            crawl=CrawlConfig(
                min_delay=_env_float("TR_MIN_DELAY", raw.get("crawl", {}).get("min_delay", 2.0)),
                max_delay=_env_float("TR_MAX_DELAY", raw.get("crawl", {}).get("max_delay", 5.0)),
                timeout=_env_float("TR_TIMEOUT", raw.get("crawl", {}).get("timeout", 25.0)),
                max_retries=_env_int("TR_MAX_RETRIES", raw.get("crawl", {}).get("max_retries", 3)),
                max_pages=_env_int("TR_MAX_PAGES", raw.get("crawl", {}).get("max_pages", 5)),
                browser_fallback=_env_bool(
                    "TR_BROWSER_FALLBACK", raw.get("crawl", {}).get("browser_fallback", True)
                ),
                llm_fallback=_env_bool(
                    "TR_LLM_FALLBACK", raw.get("crawl", {}).get("llm_fallback", False)
                ),
            ),
            auth=AuthConfig(
                admin_emails=[
                    e.strip().lower()
                    for e in os.getenv(
                        "TR_ADMIN_EMAILS",
                        ",".join(auth_raw.get("admin_emails") or []),
                    ).split(",")
                    if e.strip()
                ],
                session_days=_env_int(
                    "TR_SESSION_DAYS", auth_raw.get("session_days", 7)
                ),
                code_ttl=_env_int("TR_CODE_TTL", auth_raw.get("code_ttl", 600)),
                code_len=_env_int("TR_CODE_LEN", auth_raw.get("code_len", 6)),
                code_interval=_env_int(
                    "TR_CODE_INTERVAL", auth_raw.get("code_interval", 60)
                ),
                require_login=_env_bool(
                    "TR_REQUIRE_LOGIN", bool(auth_raw.get("require_login", True))
                ),
                smtp_host=os.getenv("TR_SMTP_HOST", auth_raw.get("smtp_host", "")),
                smtp_port=_env_int("TR_SMTP_PORT", auth_raw.get("smtp_port", 465)),
                smtp_ssl=_env_bool("TR_SMTP_SSL", bool(auth_raw.get("smtp_ssl", True))),
                smtp_user=os.getenv("TR_SMTP_USER", auth_raw.get("smtp_user", "")),
                smtp_pass=os.getenv("TR_SMTP_PASS", auth_raw.get("smtp_pass", "")),
                mail_from=os.getenv("TR_MAIL_FROM", auth_raw.get("mail_from", "")),
            ),
            billing=BillingConfig(
                enabled=_env_bool(
                    "TR_BILLING_ENABLED", bool(billing_raw.get("enabled", False))
                ),
                plans=billing_raw.get("plans") or {},
            ),
        )


def ensure_dirs() -> None:
    """确保运行时目录存在。"""
    for d in (DATA_DIR, RAW_DIR, SITES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_dotenv(path: Path | None = None) -> int:
    """把 .env 读进 os.environ。零依赖，约 20 行，够用就好。

    为什么不用 python-dotenv：为了这一个功能拉一个依赖不值当，而且它
    的变量展开、多行值这些特性这里都用不上。

    优先级：真实环境变量 > .env。也就是说 .env 只用来兜底，不会覆盖
    systemd / docker / shell 里已经设好的值——否则在容器里改环境变量
    会被镜像里的 .env 悄悄顶掉，极难排查。

    支持 `KEY=value  # 行尾注释` 的写法（注释会被剥掉）。值里确实要带 `#`
    或空格的话，用引号包起来：`KEY="a # b"`。

    返回实际写入的条数，方便启动时打日志。
    """
    target = path or (ROOT / ".env")
    if not target.exists():
        return 0

    written = 0
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return 0

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # 兼容 `export KEY=value` 写法
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # 剥离行尾注释：`KEY=value  # 说明` 里的 `#` 之后不算值。
        # 只对未加引号的值生效——加了引号就按字面量处理，引号内的 # 是值的一部分。
        quoted = len(val) >= 2 and val[0] in ("'", '"')
        if not quoted:
            cut = len(val)
            for sep in (" #", "\t#"):
                pos = val.find(sep)
                if pos != -1 and pos < cut:
                    cut = pos
            val = val[:cut].rstrip()
        # 去掉成对引号，但保留值内部的转义
        if quoted and val[0] == val[-1]:
            val = val[1:-1]
        if key in os.environ:  # 真实环境变量优先
            continue
        os.environ[key] = val
        written += 1
    return written


_loaded = _load_dotenv()
settings = Settings.load()
