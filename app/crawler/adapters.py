"""站点适配器：规则驱动，加来源不改代码。

一个来源 = configs/sites/ 下一份 YAML。Python 侧只负责加载与执行，
符合 BidInsight「爬取/解析/ETL 解耦 + 规则版本化」的约定。

YAML 结构示例见 configs/sites/ccgp.yaml。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml
from parsel import Selector

from ..config import SITES_DIR
from .normalize import normalize_title, html_to_text


@dataclass
class FieldRule:
    """单个字段的抽取规则：多选择器冗余 + 回退。"""

    # CSS 选择器列表，按顺序尝试，第一个命中的生效
    selectors: list[str] = field(default_factory=list)
    # text | html | attr:href | attr:title ...
    extract: str = "text"
    # 可选正则，从命中结果里再抽一次（取 group(1)，无分组取 group(0)）
    regex: str | None = None
    # 抽取失败时的默认值
    default: str = ""


@dataclass
class SiteRule:
    """一份站点规则。"""

    id: str
    name: str
    base_url: str = ""
    enabled: bool = True
    # http | browser
    mode: str = "http"
    version: str = "1.0.0"
    schedule: str = ""
    # 搜索/列表入口，占位符 {page} {keyword} {start} {end}
    search_url: str = ""
    # 第 1 页专用的 URL（不给占位符，写了就整串直接用）。
    # Destoon 一类 CMS 的栏目首页和分页页不是同一个地址，而且**内容还不等价**：
    # 中国铁建 cqzdt 的 /zhaobiao/page-1.shtml 会 301 到站点首页，
    # 首页那块是「最新混合列表」，只含 20 条招标里的 16 条，
    # 剩下的 4 条既不在首页也不在第 2、3 页 —— 不配这个就会永久漏采。
    # （中国中铁 crecg 同名 URL 反而是 301 到栏目页，所以那边不用配。）
    first_page_url: str = ""
    # 渲染完成标志：等这个选择器出现再取 DOM（browser 模式用）
    wait_selector: str = ""
    # 开跑前先用真浏览器打开一次这个页面（browser 模式、且走 raw_json 时才有意义）。
    # 阿里云 WAF（acw_sc__v2）这类防护是靠页面里的 JS 挑战算 cookie 的：
    # context.request.get 走 Chromium 网络栈但**不执行 JS**，永远拿不到那张票，
    # 于是不管重试几次都被拦。先 page.goto 打开一次真实页面，让挑战脚本跑完、
    # cookie 落进同一个 context，之后本轮所有 raw_json 请求就都带得上了。
    # 大唐 tang.cdt-ec.com 就是这种情况：不预热 100% 被拦，预热后连打 5 次全通。
    warmup_url: str = ""
    # 响应形态：html（默认，走选择器）| json（走 json_fields 映射）
    response_type: str = "html"
    # json 模式下，列表数组在响应里的路径，如 "data.records"
    json_items: str = ""
    # 有些接口把真正的响应体再包一层 JSON 字符串
    # （陕西 inteligentsearch 就是 {"code":200,"content":"{\"result\":{...}}"}），
    # 指定这个键后先取出该值再二次 json.loads，json_items 才生效。
    json_unwrap: str = ""
    # 响应体**不是** JSON、但 JSON 内嵌在 HTML 的某个 JS 变量里时用这个。
    # 例如法律招标网 falvzhaobiao 把列表数据塞在
    #   window._STATIC_DATA_ = function(){ return { ... listData:'[{...}]' ... } }
    # 里，整页是 HTML，直接 json.loads 必然失败。
    # 写一个带**一个捕获组**的正则，命中的第一组会被当作 JSON 解析，
    # 之后再按 json_items 取数组。抓不到就返回空列表。
    json_extract: str = ""
    # json 模式下，字段别名映射：入库字段名 -> 响应里的键名（支持 a.b 取嵌套）
    json_fields: dict[str, str] = field(default_factory=dict)
    # json 模式下，取值后再过一道正则：入库字段名 -> 正则。
    # 用于「接口只给了长标题、采购人要从标题开头抠出来」的站点
    # （华润守正 szecp 就只有标题，没有独立采购人字段）。
    # 写法与 list_fields 的 regex 一致：有一个捕获组就取 group(1)，
    # 否则取整段匹配；匹配不上该字段留空，不留原值。
    json_field_regex: dict[str, str] = field(default_factory=dict)
    # 列表请求走 POST 时的表单体（值为字符串，同样支持占位符）
    post_data: dict[str, Any] = field(default_factory=dict)
    # true = body 用 application/json 发送（gdggzy）；false = 表单（ggzy）
    post_json: bool = False
    # 目标站还在跑老 TLS、握手阶段会重协商（中国移动 b2b.10086.cn 就是）。
    # 开了这个才连得上，代价是这一路不做前向加密。
    tls_legacy: bool = False
    # ── 请求体整体加密（备用能力，当前无站点启用）────────────────
    # 个别站点接口不吃明文 JSON，要把整个请求体用服务端**现下发**的公钥
    # 加密，再塞进 {field: 密文}（公钥可能每次请求都换）。写法：
    #   encrypt:
    #     type: rsa
    #     public_url: "https://<站点>/open/homepage/public"
    #     field: param
    # 只支持 rsa 一种；留空（默认）则请求体原样发送，既有站点行为不变。
    encrypt: dict[str, str] = field(default_factory=dict)
    # 列表请求要额外带的头。典型用途是 Referer：有些接口（zycg 就是）
    # 校验来源页，缺了 Referer 一律回 {"code":"-1"}。
    headers: dict[str, str] = field(default_factory=dict)
    # ── 请求头签名 ──────────────────────────────────────────────
    # 站点在 headers 里放一个随时间戳变化的签名，签名不对一律回
    # "sign.validation.failed"（招商局 dzzb.ciesco.com.cn 就是）。
    # 配了 sign_secret 后，headers 里可以用两个占位符：
    #   {now_ms}   当前毫秒时间戳
    #   {sign}     按 sign_secret 算出的签名，与 {now_ms} 同一次生成
    # 签名算法：md5( sort(now_ms + url_part + secret) 反序拼接 )
    # —— 这是招商局门户 axios 拦截器里的算法，别照搬到别的站点。
    sign_secret: str = ""
    # 参与签名的 URL 片段。留空则取 search_url 路径的最后一段。
    sign_url_part: str = ""
    # 列表没给详情页 URL 时，用同一行的字段拼出来。
    # 占位符取 json_fields / list_fields 的键名，如
    #   "{base}/index.html#/44/new/jygg/v3/A?noticeId={notice_id}"
    url_template: str = ""
    # 详情页链接按公告类型分派时用，key 取本行 detail_url_key 字段的值。
    # 与 detail_url_map 同一套 key，查不到才回落到 url_template。
    url_template_map: dict[str, str] = field(default_factory=dict)
    # 站点不返回的常量字段（如广东始终 province=广东），省得让抽取层去猜
    static_fields: dict[str, str] = field(default_factory=dict)
    # 只保留 url 命中该正则的列表项，滤掉侧边栏/推荐位噪声
    url_filter: str = ""
    # 命中即丢整行：字段名 -> 正则。
    # 浙江 zfcg 一个接口把采购公告/结果公告/合同公告混着返回，
    # 光靠关键词过滤挡不住「xx法律顾问服务项目合同公告」这类已成交的条目。
    drop_if: dict[str, str] = field(default_factory=dict)
    # 命中即**跳过详情请求**（行照样入库，只用列表字段）。与 drop_if 同构。
    # 国家电网的详情接口 getNoticeBid 只认招标/变更类公告，中标结果类
    # （doctype=doci-win）一律回 {"successful":false,"resultHint":"SYS001"}。
    # 不跳过就是每轮白打几十次必失败的请求——既拖慢采集，也给对方添无谓的
    # 错误日志。而这类公告本来就没有截止时间，跳了也不损失字段。
    detail_skip_if: dict[str, str] = field(default_factory=dict)
    # 列表项容器选择器（支持逗号分隔的多选择器）
    list_container: str = ""
    list_fields: dict[str, FieldRule] = field(default_factory=dict)
    # 详情字段
    detail_container: str = ""
    detail_fields: dict[str, FieldRule] = field(default_factory=dict)
    # 详情也走接口时用：POST 表单体，占位符取自本行字段（如 "{src_id}"）。
    # 有些站点的详情页只是个空壳，正文、截止时间都在另一个接口里
    # （cfcpn 的详情页还只给登录用户看全文，接口反而给得完整）。
    detail_post_data: dict[str, str] = field(default_factory=dict)
    # 详情接口收「裸标量」请求体时用（与 detail_post_data 二选一）。
    # 国家电网的 getNoticeBid 就是直接吃一个 JSON 字符串 "2026102451095019"，
    # 一旦包成 {"id": "..."} 对象，服务端一律回 {"successful":false,"resultHint":"操作异常！"}。
    # 写了这个就忽略 detail_post_data，整体作为请求体发出（需配 post_json: true，
    # 否则 httpx 会把裸字符串当表单体，服务端同样认不出来）。
    detail_post_raw: str = ""
    # 详情接口的 URL。留空则复用列表接口（cfcpn 就是同一个地址换个参数）。
    # 电信阳光采购网这类是分开的：列表 /announcementJoin/queryListNew，
    # 详情 /tenderannouncement/view，必须单独给。
    detail_url: str = ""
    # 详情接口的 HTTP 方法。**默认 POST**（历史行为，电信/国网/cfcpn 都是 POST）。
    # 政采云各省门户（安徽/湖南/山西…）的详情是 **GET**
    # /portal/detail?articleId=xxx，参数走 query string，
    # 沿用 POST 会 405 或回空，所以这里留了口子。
    # 配 GET 时 detail_post_data 会被忽略，URL 里的 {占位符} 照常替换。
    detail_method: str = "POST"
    # 详情端点按公告类型分派时用：取本行 detail_url_key 字段的值，
    # 在下面这张表里查 URL，查不到才回落到 detail_url。
    # 电信就是这种情况：比选/招标走 tenderannouncement/view，
    # 单一来源走 purchaseannounce/view，且两边的正文字段名还不一样
    # （context vs announceContext）。
    detail_url_map: dict[str, str] = field(default_factory=dict)
    # 用哪个行字段去查 detail_url_map，默认 src_type（= docTypeCode）
    detail_url_key: str = "src_type"
    # 详情接口的字段映射：入库字段名 -> 响应里的键名（支持 a.b）
    detail_json_fields: dict[str, str] = field(default_factory=dict)
    # 详情响应里记录对象的路径。留空则复用 json_items —— 但多数站点
    # 列表和详情的包法不一样（电信：列表 data.pageInfo.list，详情 data 本身），
    # 复用会什么都解析不出来且**不报错**，所以建议显式给。
    detail_json_items: str = ""
    # 每轮最多取几条详情（0 = 不限）。
    # 限流凶的站点（cfcpn 连着打十几个请求就封 IP）只能给最新的几条补详情，
    # 剩下的靠列表字段入库，总比整轮被封、一条都拿不到强。
    detail_limit: int = 0
    # 详情页二次跳转：有些聚合页（如 ggzy）列表链接指向「关联公告」页，
    # 正文在另一条 URL 上。这里给出在该页里找真实正文链接的规则。
    detail_url_from: FieldRule | None = None
    # 分页方式：page_param（改 URL 参数）| next_link（跟「下一页」）
    pagination: dict[str, Any] = field(default_factory=dict)
    # 默认搜索关键词（法律服务场景）
    default_keyword: str = ""
    # 多轮检索词：一次采集按顺序跑遍，命中结果合并去重。
    # 站点只支持单个关键词，「法律顾问」「诉讼代理」「司法辅助」的公告
    # 互不包含，只搜一个词会漏掉大量线索。
    keywords: list[str] = field(default_factory=list)
    # 站点不支持关键词检索时用：拉全量列表，本地按 keywords 过滤标题。
    # 代价是噪音请求多，收益是这类「哑列表」站点也能接入。
    local_filter: bool = False
    # 单条查询的时间跨度（天）。>0 时把大区间切成小窗口滚动请求，
    # 应对「每次查询只能翻有限页数」的站点。0 = 不切。
    window_days: int = 0
    # 首轮回灌天数。哑列表站点需要按天切片才不漏，回灌 30 天不现实。
    lookback_days: int = 0
    # 默认是否逐条取详情。列表页往往只有标题+日期，
    # 预算与截止时间要去详情页才有，缺了这两项雷达的价值就没了一半。
    fetch_detail_default: bool = False
    # 限速覆盖
    delay: dict[str, float] = field(default_factory=dict)
    path: str = ""

    def keywords_of(self, keyword: str | None = None) -> list[str | None]:
        """本次要跑的检索词列表。显式传参优先，其次 YAML 的 keywords。"""
        if keyword:
            return [keyword]
        if self.keywords:
            return list(self.keywords)
        return [self.default_keyword or None]

    def build_url(
        self,
        page: int = 1,
        keyword: str | None = None,
        start: str = "",
        end: str = "",
    ) -> str:
        """按规则拼出列表页 URL。

        注意：YAML 里用 `>-` 写长 URL 时，折行处的换行会被折叠成空格，
        直接拼进 query 会让站点返回 500。所以先剥掉 URL 里所有空白，
        再对关键词做百分号编码（中文关键词必须编码）。
        """
        from urllib.parse import quote

        kw = keyword if keyword is not None else self.default_keyword
        url = "".join(self.search_url.split())
        url = url.replace("{page}", str(page))
        url = url.replace("{keyword}", quote(kw or "", safe=""))
        url = url.replace("{start}", start or "")
        url = url.replace("{end}", end or "")
        return url

    def build_page_url(
        self,
        page: int = 1,
        keyword: str | None = None,
        start: str = "",
        end: str = "",
    ) -> str:
        """列表页分页时用的 URL —— 第 1 页可能要换一个地址。

        只给分页循环用，别拿它去拼详情 URL：详情那边的回落逻辑是
        build_url(page=1)，如果这里改了 build_url 的行为，
        那些没配 detail_url 的站点会把栏目首页当成详情页去抓。
        """
        if page == 1 and self.first_page_url:
            return "".join(self.first_page_url.split())
        return self.build_url(page=page, keyword=keyword, start=start, end=end)

    def page_size(self) -> int:
        """每页条数。pn（偏移量）占位符要靠它换算。"""
        try:
            return int(self.pagination.get("page_size") or 0) or 0
        except (TypeError, ValueError):
            return 0

    def _render(self, v: Any, page: int, kw: str, start: str, end: str) -> Any:
        """递归渲染 post_data 的一个值。

        值可能是：
          · 字符串  → 做占位符替换（站点多数参数是扁平的）
          · dict/list → 递归处理内部字符串。
            深圳 szggzy 的 fields、陕西的 condition/time 都要求**对象/数组**，
            而它们同时又含 {start}/{end} 这类占位符，所以必须支持嵌套。
        """
        if v is None:
            # YAML 里写 `statistics: null` 是为了让接口收到 JSON null，
            # 一旦 str() 成 "None" 就会被 ES 当成非法条件整个拒掉。
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        if isinstance(v, dict):
            return {k: self._render(x, page, kw, start, end) for k, x in v.items()}
        if isinstance(v, list):
            return [self._render(x, page, kw, start, end) for x in v]
        s = str(v)
        # 值为纯页码占位符时输出整数：ES 类接口对 "0" 和 0 态度不一样，
        # 传字符串容易被判成非法参数。
        if s.strip() in _INT_PLACEHOLDERS:
            size = self.page_size()
            if s.strip() == "{pn}":
                return (page - 1) * (size or 10)
            if s.strip() == "{page0}":
                return page - 1
            return page
        size = self.page_size()
        s = s.replace("{page0}", str(page - 1))                  # 0 基页码（深圳）
        s = s.replace("{pn}", str((page - 1) * (size or 10)))    # 偏移量（陕西）
        s = s.replace("{page}", str(page))
        s = s.replace("{keyword}", kw or "")
        s = s.replace("{start_compact}", _compact(start))
        s = s.replace("{end_compact}", _compact(end))
        s = s.replace("{start_dt}", f"{start} 00:00:00" if start else "")
        s = s.replace("{end_dt}", f"{end} 23:59:59" if end else "")
        s = s.replace("{start}", start or "")
        s = s.replace("{end}", end or "")
        return s

    def build_post_data(
        self, page: int = 1, keyword: str | None = None, start: str = "", end: str = ""
    ) -> dict[str, Any]:
        """按规则渲染 POST 表单体。

        日期占位符：
          {start} / {end}           YYYY-MM-DD（多数站点）
          {start_compact}            YYYYMMDD（广东 gdggzy 要拼 YYYYMMDDHHmmss）
          {start_dt} / {end_dt}      带时分秒（陕西 ES 检索要求 yyyy-MM-dd HH:mm:ss）
        页码占位符：
          {page}   1 基
          {page0}  0 基（深圳）
          {pn}     偏移量 = (page-1)*page_size（陕西）
        """
        kw = keyword if keyword is not None else self.default_keyword
        return {
            k: self._render(v, page, kw, start, end)
            for k, v in self.post_data.items()
        }

    def build_headers(self) -> dict[str, str]:
        """渲染请求头，必要时现算签名。

        没有配 sign_secret 的站点原样返回（保持既有行为）。
        配了的话，{now_ms} / {sign} 必须**同一次**算出来 —— 签名里含时间戳，
        分两次生成会错位，服务端直接判 sign.validation.failed。
        """
        if not self.sign_secret or not self.headers:
            return dict(self.headers)
        import hashlib
        import time as _time

        now_ms = int(_time.time() * 1000)
        part = self.sign_url_part or (self.search_url.rstrip("/").split("/")[-1])
        cat = f"{now_ms}{part}{self.sign_secret}"
        arr = sorted(cat)
        arr.reverse()
        sign = hashlib.md5("".join(arr).encode("utf-8")).hexdigest()
        return {
            k: v.replace("{now_ms}", str(now_ms)).replace("{sign}", sign)
            for k, v in self.headers.items()
        }

    def build_detail_post_data(self, row: dict[str, str]) -> dict[str, str] | str:
        """详情接口的 POST 体，占位符取自当前这一行。

        与 build_post_data 的区别：那边的占位符是页码 / 关键词 / 日期，
        这里是列表行里的字段（比如刚拿到的公告 id），所以直接按行渲染。

        返回类型是 dict 或 str：多数站点吃对象，少数（国家电网 getNoticeBid）
        只吃一个标量字符串，此时走 detail_post_raw 分支。
        """
        if self.detail_post_raw:
            return self.detail_post_raw.format_map(_Row(row))
        return {k: v.format_map(_Row(row)) for k, v in self.detail_post_data.items()}


# 单独成值时直接渲染成整数，不做字符串替换
_INT_PLACEHOLDERS = {"{pn}", "{page}", "{page0}"}


def _compact(date_str: str) -> str:
    """'2026-09-22' → '20260922'。给要求 YYYYMMDD 的站点用。"""
    return "".join((date_str or "").split("-"))


def _parse_field_rule(raw: Any) -> FieldRule:
    """字段规则兼容三种写法：

      title: "a"                                   # 简写，默认 text
      url: {selectors: ["a"], extract: "attr:href"}
      date: {selectors: ["span"], regex: "\\d{4}-\\d{2}-\\d{2}"}
    """
    if isinstance(raw, str):
        return FieldRule(selectors=[raw])
    if isinstance(raw, dict):
        sels = raw.get("selectors") or []
        if isinstance(sels, str):
            sels = [sels]
        return FieldRule(
            selectors=list(sels),
            extract=raw.get("extract", "text"),
            regex=raw.get("regex"),
            default=raw.get("default", ""),
        )
    return FieldRule()


def _parse_fields(raw: dict | None) -> dict[str, FieldRule]:
    out: dict[str, FieldRule] = {}
    for k, v in (raw or {}).items():
        out[k] = _parse_field_rule(v)
    return out


def load_site(path: Path) -> SiteRule | None:
    """加载单份站点 YAML。"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        return None

    if not raw.get("id"):
        raw["id"] = path.stem

    return SiteRule(
        id=str(raw.get("id")),
        name=str(raw.get("name", raw.get("id"))),
        base_url=str(raw.get("base_url", "")),
        enabled=bool(raw.get("enabled", True)),
        mode=str(raw.get("mode", "http")),
        version=str(raw.get("version", "1.0.0")),
        schedule=str(raw.get("schedule", "")),
        search_url=str(raw.get("search_url", "")),
        first_page_url=str(raw.get("first_page_url", "")),
        wait_selector=str(raw.get("wait_selector", "")),
        warmup_url=str(raw.get("warmup_url", "")),
        response_type=str(raw.get("response_type", "html")),
        json_items=str(raw.get("json_items", "")),
        json_unwrap=str(raw.get("json_unwrap", "")),
        json_extract=str(raw.get("json_extract", "")),
        json_fields={str(k): str(v) for k, v in (raw.get("json_fields") or {}).items()},
        json_field_regex={
            str(k): str(v) for k, v in (raw.get("json_field_regex") or {}).items()
        },
        post_data=dict(raw.get("post_data") or {}),
        post_json=bool(raw.get("post_json", False)),
        tls_legacy=bool(raw.get("tls_legacy", False)),
        encrypt={str(k): str(v) for k, v in (raw.get("encrypt") or {}).items()},
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        sign_secret=str(raw.get("sign_secret", "")),
        sign_url_part=str(raw.get("sign_url_part", "")),
        url_template=str(raw.get("url_template", "")),
        url_template_map={
            str(k): str(v) for k, v in (raw.get("url_template_map") or {}).items()
        },
        static_fields={str(k): str(v) for k, v in (raw.get("static_fields") or {}).items()},
        url_filter=str(raw.get("url_filter", "")),
        drop_if={str(k): str(v) for k, v in (raw.get("drop_if") or {}).items()},
        detail_skip_if={
            str(k): str(v) for k, v in (raw.get("detail_skip_if") or {}).items()
        },
        detail_limit=int(raw.get("detail_limit") or 0),
        list_container=str(raw.get("list_container", "")),
        list_fields=_parse_fields(raw.get("list_fields")),
        detail_container=str(raw.get("detail_container", "")),
        detail_fields=_parse_fields(raw.get("detail_fields")),
        detail_post_data={
            str(k): str(v) for k, v in (raw.get("detail_post_data") or {}).items()
        },
        detail_post_raw=str(raw.get("detail_post_raw", "") or ""),
        detail_json_fields={
            str(k): str(v) for k, v in (raw.get("detail_json_fields") or {}).items()
        },
        detail_url=str(raw.get("detail_url", "")),
        detail_method=str(raw.get("detail_method", "") or "POST").upper(),
        detail_url_map={str(k): str(v) for k, v in (raw.get("detail_url_map") or {}).items()},
        detail_url_key=str(raw.get("detail_url_key", "") or "src_type"),
        detail_json_items=str(raw.get("detail_json_items", "")),
        detail_url_from=(
            _parse_field_rule(raw["detail_url_from"])
            if raw.get("detail_url_from")
            else None
        ),
        pagination=dict(raw.get("pagination") or {}),
        default_keyword=str(raw.get("default_keyword", "")),
        keywords=[str(k) for k in (raw.get("keywords") or []) if str(k).strip()],
        local_filter=bool(raw.get("local_filter", False)),
        window_days=int(raw.get("window_days", 0) or 0),
        lookback_days=int(raw.get("lookback_days", 0) or 0),
        fetch_detail_default=bool(raw.get("fetch_detail", False)),
        delay=dict(raw.get("delay") or {}),
        path=str(path),
    )


def load_all_sites() -> list[SiteRule]:
    """加载 configs/sites/ 下全部规则。"""
    SITES_DIR.mkdir(parents=True, exist_ok=True)
    rules: list[SiteRule] = []
    for p in sorted(SITES_DIR.glob("*.yaml")):
        r = load_site(p)
        if r:
            rules.append(r)
    return rules


def get_site(site_id: str) -> SiteRule | None:
    p = SITES_DIR / f"{site_id}.yaml"
    if not p.exists():
        return None
    return load_site(p)


# ── 执行：按规则抽取 ─────────────────────────────────────────────────────
def _smart_join(parts: list[str]) -> str:
    """拼接文本节点。中文之间不插空格，否则「行政诉讼代理 法律服务」会断词。"""
    out = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if out and not (_is_cjk(out[-1]) and _is_cjk(p[0])):
            out += " "
        out += p
    return out


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or ch in "，。、；：（）《》【】“”‘’"


def _apply(sel: Selector, rule: FieldRule) -> str:
    """按字段规则在给定节点内抽取。多选择器冗余回退。"""
    import re as _re

    for css in rule.selectors:
        try:
            if rule.extract.startswith("attr:"):
                attr = rule.extract.split(":", 1)[1]
                val = sel.css(css).attrib.get(attr, "")
            elif rule.extract == "html":
                val = sel.css(css).get() or ""
            else:
                node = sel.css(css)
                if not node:
                    continue
                val = _smart_join(node.css("::text").getall())
            val = (val or "").strip()
        except Exception:  # noqa: BLE001
            val = ""

        if not val:
            continue
        if rule.regex:
            m = _re.search(rule.regex, val)
            val = (m.group(1) if m and m.groups() else m.group(0) if m else "")
        if val:
            return val.strip()
    return rule.default


def parse_list(rule: SiteRule, html: str) -> list[dict[str, str]]:
    """按规则解析列表页，返回行字典列表。"""
    if rule.response_type == "json":
        return parse_list_json(rule, html)

    import re as _re

    sel = Selector(text=html)
    container = rule.list_container or "body"
    nodes = sel.css(container)
    url_re = _re.compile(rule.url_filter) if rule.url_filter else None
    rows: list[dict[str, str]] = []
    for node in nodes:
        row: dict[str, str] = {}
        for fname, frule in rule.list_fields.items():
            row[fname] = _apply(node, frule)
        _finalize_row(rule, row)
        # url_filter：滤掉侧栏/推荐位（它们的链接形态与正文条目不同）
        if url_re is not None and row.get("url") and not url_re.search(row["url"]):
            continue
        # 至少需要标题，否则视为噪声节点
        if row.get("title", "").strip() or row.get("url", "").strip():
            rows.append(row)
    return rows


def _dig(obj: Any, path: str) -> Any:
    """按 a.b.c 取嵌套值，取不到返回 None。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _strip_fragment(s: str) -> str:
    """把 JSON 里夹带的 HTML 片段转纯文本。

    不少政务接口（湖北 hbggzyfwpt 的 bulletinContent 就是）直接把富文本正文
    塞进 JSON 字段。原样交给抽取层的话，<span> 会把「预算金额：790,000 元」
    拆成两截，正则匹配失败。这里统一去标签，抽取层只看到连续文本。
    """
    if "<" not in s or ">" not in s:
        return s
    if not _TAG_RE.search(s):
        return s
    try:
        txt = Selector(text=s).xpath("string(.)").get() or ""
    except Exception:  # noqa: BLE001 畸形片段退化为原文
        return s
    return txt.strip()


_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# ES 全文检索接口会把命中词包成 <em style='color:red'>法律</em>。
# 不剥掉的话「<em>法律</em>顾问服务」既做不了标题去重也做不了关键词匹配。
_HL_RE = re.compile(r"</?em\b[^>]*>", re.I)


def _clean_highlight(s: str) -> str:
    return _HL_RE.sub("", s)


def _loads_any(body: str) -> Any:
    """尽量把响应体解析成对象，兼容「浏览器打开 JSON 接口」的情形。

    用 Playwright 直接 goto 一个返回 application/json 的 URL 时，Chrome 会用
    内置 viewer 把内容包成 <pre>{...}</pre>，page.content() 拿到的是这段 HTML
    而不是原始 JSON。所以先直接 loads，失败再剥一层 <pre>。
    """
    import html as _html
    import json as _json
    import re as _re

    if not body:
        return None
    try:
        return _json.loads(body)
    except Exception:  # noqa: BLE001
        pass
    m = _re.search(r"<pre[^>]*>(.*?)</pre>", body, _re.S | _re.I)
    if not m:
        return None
    try:
        return _json.loads(_html.unescape(m.group(1)).strip())
    except Exception:  # noqa: BLE001
        return None


def parse_list_json(rule: SiteRule, body: str) -> list[dict[str, str]]:
    """解析 JSON 接口返回。适合已找到干净 API 的站点（比抠 DOM 稳得多）。

    有些站点会直接返回「政府采购」这种编码值，也有的把正文一起给了，
    这里统一转成字符串，交给 extract 层做归一。
    """
    import html as _html  # 抠出来的片段可能含 &quot; 之类的实体，先还原
    import json as _json  # json_unwrap 的二次解析还要用
    import re as _re_ex

    if rule.json_extract:
        # 整页是 HTML、JSON 藏在 JS 变量里（falvzhaobiao 的 listData）。
        # 先用正则把那一坨抠出来，再当 JSON 解析。
        m = _re_ex.search(rule.json_extract, body, _re_ex.S)
        if not m:
            return []
        data = _loads_any(_html.unescape(m.group(1)))
    else:
        data = _loads_any(body)
    if data is None:
        return []

    # 陕西这类接口把真正的响应体塞在一个字符串字段里，先剥一层
    if rule.json_unwrap:
        inner = _dig(data, rule.json_unwrap)
        if isinstance(inner, str):
            try:
                data = _json.loads(inner)
            except Exception:  # noqa: BLE001
                return []
        elif inner is not None:
            data = inner

    items = _dig(data, rule.json_items) if rule.json_items else data
    if not isinstance(items, list):
        return []

    rows: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row: dict[str, str] = {}
        for field_name, src_key in rule.json_fields.items():
            # "url||linkTo"：前一个字段为空时依次回退。
            # 深圳 szggzy 的老数据只有 linkTo，新数据才有 url，
            # 少了这条回退，一半公告会因为没链接而拿不到详情。
            val = None
            for key in src_key.split("||"):
                val = _dig(it, key.strip())
                if val not in (None, ""):
                    break
            if val is None:
                row[field_name] = ""
            elif isinstance(val, (dict, list)):
                row[field_name] = ""
            else:
                row[field_name] = _clean_highlight(_strip_fragment(str(val).strip()))
        # 取值后再过一道正则（采购人从标题开头抠出来这类场景）
        for field_name, pattern in rule.json_field_regex.items():
            src = row.get(field_name) or ""
            if not src:
                continue
            try:
                m = _re_ex.search(pattern, src)
            except _re_ex.error:  # noqa: PERF203 规则写错不该让整轮采集崩
                continue
            if not m:
                row[field_name] = ""
            elif m.groups():
                row[field_name] = m.group(1).strip()
            else:
                row[field_name] = m.group(0).strip()
        _normalize_ms_timestamp(row)
        _finalize_row(rule, row)
        if row.get("title") and not _drop_row(rule, row):
            rows.append(row)
    return rows


class _Row(dict):
    """占位符缺失时取空串，避免 url_template 因少一个字段整体失败。"""

    def __missing__(self, key: str) -> str:
        return ""


def _row_hits(patterns: dict[str, str], row: dict[str, str]) -> bool:
    """行级黑名单匹配：字段名 -> 正则，命中任一即 True。"""
    for field_name, pattern in patterns.items():
        val = row.get(field_name) or ""
        try:
            if re.search(pattern, val):
                return True
        except re.error:  # noqa: PERF203 规则写错不该让整轮采集崩
            continue
    return False


def _drop_row(rule: SiteRule, row: dict[str, str]) -> bool:
    """行级黑名单：命中任一规则就丢掉。

    用来挡「同一接口里混着返回、但根本不该入库」的条目，
    比如浙江 zfcg 把采购公告、结果公告、合同公告塞在一个列表里。
    """
    return _row_hits(rule.drop_if, row)


def detail_skipped(rule: SiteRule, row: dict[str, str]) -> bool:
    """这一行要不要跳过详情请求（行本身仍然入库）。

    给「详情接口只服务某一类公告」的站点用，详见 SiteRule.detail_skip_if。
    """
    return _row_hits(rule.detail_skip_if, row)


_MS_TS_FIELDS = ("publish_time", "deadline")
_MS_TS_RE = re.compile(r"^\d{13}$")


def _normalize_ms_timestamp(row: dict[str, str]) -> None:
    """把毫秒时间戳字段转成本地时间字符串。

    政采云（浙江/四川等多省都用它那套门户）的时间字段是 13 位毫秒，
    直接入库的话 parse_date 认不出来，发布日期会整片丢失。
    """
    from datetime import datetime

    for f in _MS_TS_FIELDS:
        v = (row.get(f) or "").strip()
        if _MS_TS_RE.match(v):
            try:
                row[f] = datetime.fromtimestamp(int(v) / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (ValueError, OSError, OverflowError):
                pass


def _finalize_row(rule: SiteRule, row: dict[str, str]) -> None:
    """行后处理：补常量字段、拼相对 URL、按模板造详情链接。"""
    from urllib.parse import urljoin

    for k, v in rule.static_fields.items():
        if not row.get(k):
            row[k] = v

    if row.get("url") and rule.base_url and not row["url"].startswith("http"):
        row["url"] = urljoin(rule.base_url, row["url"])

    # 有些站点列表不给详情链接（广东 gdggzy 的 linkUrl 恒为 null），
    # 但字段里带着能拼出详情页的参数，交给规则自己拼。
    if rule.url_template or rule.url_template_map:
        # 详情页链接的拼法也可能**按公告类型不同**。
        # 电信的 DeclareDetails 就靠 URL 里的 type 参数选视图：
        # 招标公告 type=1、单一来源 type=2、资格预审 type=6，
        # 写死 1 的话单一来源点进去是「未获取到公告信息」。
        tpl = rule.url_template_map.get(
            str(row.get(rule.detail_url_key) or ""), ""
        ) or rule.url_template
        if tpl and not row.get("url"):
            try:
                row["url"] = tpl.format_map(_Row(row))
            except Exception:  # noqa: BLE001
                row["url"] = ""


def parse_detail(rule: SiteRule, html: str) -> dict[str, str]:
    """按规则解析详情页。

    注意：不少站点的「详情 URL」其实是个接口（政采云的 /portal/detail 直接
    吐 JSON），parsel 识别成 json 类型后调 .css 会抛
    "Cannot use css on a Selector of type 'json'"，整轮采集就崩了。
    所以这里先判断类型：不是 HTML 就按原始文本处理，选择器全部落空也没关系，
    列表字段本来就已经够用。
    """
    out: dict[str, str] = {}
    if not _looks_like_html(html):
        for fname in rule.detail_fields:
            out[fname] = ""
        out["_text"] = html_to_text(html)
        return out

    sel = Selector(text=html)
    container = rule.detail_container or "body"
    try:
        node = sel.css(container)
    except Exception:  # noqa: BLE001
        node = []
    if not node:
        node = sel.css("body")
    for fname, frule in rule.detail_fields.items():
        out[fname] = _apply(node[0] if len(node) else sel, frule)
    out["_text"] = html_to_text(node[0].get() if len(node) else html)
    return out


def parse_detail_json(rule: SiteRule, body: str) -> dict[str, str]:
    """解析「详情接口」的返回。

    与 parse_detail 的区别：那边抠 HTML，这里直接吃 JSON。
    给那些详情页只是空壳、正文和截止时间都藏在接口里的站点用
    （cfcpn 的详情页甚至只给未登录用户看截断预览，接口反而完整）。

    只取响应里第一条记录。返回结构与 parse_detail 一致，
    正文统一放在 _text 里，好让 pipeline 那套「详情正文不能覆盖列表正文」
    的判断照样生效。
    """
    import html as _html
    import re as _re

    data = _loads_any(body)
    if data is None:
        return {}
    items = (
        _dig(data, rule.detail_json_items)
        if rule.detail_json_items
        else (_dig(data, rule.json_items) if rule.json_items else data)
    )
    # 列表接口返回数组，详情接口通常直接返回**单个对象**
    # （电信就是 {"code":200,"data":{...}}，data 是 dict 不是 list）。
    # 这里兼容两种，省得为每个站点单独写解析。
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return {}
    it = items[0]
    if not isinstance(it, dict):
        return {}

    out: dict[str, str] = {}
    for fname, src_key in rule.detail_json_fields.items():
        # 候选键：同一个语义在不同公告类型里字段名可能不一样
        # （电信：招标公告叫 context，单一来源叫 announceContext），
        # 用 | 分隔，取第一个非空值。
        v = None
        for key in src_key.split("|"):
            v = _dig(it, key.strip())
            if v not in (None, ""):
                break
        if v is None:
            continue
        s = _html.unescape(str(v).strip())
        # 正文通常是转义过的 HTML（&lt;p&gt;…），还原成纯文本再入库
        if "<" in s:
            s = _re.sub(r"<script.*?</script>", " ", s, flags=_re.S | _re.I)
            s = _re.sub(r"<style.*?</style>", " ", s, flags=_re.S | _re.I)
            s = _re.sub(r"<[^>]+>", " ", s)
        s = _re.sub(r"\s+", " ", s).strip()
        if s:
            out[fname] = s
    out["_text"] = out.get("content", "")
    return out


def _looks_like_html(text: str) -> bool:
    """页面是 HTML 还是别的（JSON / 纯文本）。"""
    head = (text or "").lstrip()[:200]
    if head.startswith("{"):
        return False
    return "<" in head


def extract_link(html: str, rule: FieldRule) -> str:
    """在一页 HTML 里按规则找一个 URL（用于详情页二次跳转）。"""
    if not html:
        return ""
    sel = Selector(text=html)
    return _apply(sel, rule)


def title_of(row: dict[str, str]) -> str:
    """取行里的标题（兼容 site 自定义字段名）。"""
    return normalize_title(row.get("title", ""))
