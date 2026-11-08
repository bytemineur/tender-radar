"""采集编排：站点 → 列表 → 抽取 → 去重 → 入库。

作业最小单元 = 站点 + 时间片（幂等）。支持：
  - 增量：以 source.last_success_at（或传入 since）为界，只抓更新的
  - 断点续跑：单页失败不中断整轮，单源失败不影响全局
  - 详情按需：默认只取列表，fetch_detail=True 时才逐条取详情（慢，但字段更全）
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import RAW_DIR, settings
from ..models import CrawlRun, Source, Tender
from ..notify import notify_error, notify_new_items
from .adapters import (
    SiteRule,
    detail_skipped,
    extract_link,
    get_site,
    load_all_sites,
    parse_detail,
    parse_detail_json,
    parse_list,
    _Row,
)
from .dedupe import assign_cluster, make_hash_key
from .extract import extract_tender
from .fetcher import BrowserSession, encrypt_request_body, fetch, fetch_http
from .normalize import html_to_text


def sync_sites(session: Session) -> int:
    """把 configs/sites/*.yaml 同步进 sources 表。新增或更新规则版本。"""
    rules = load_all_sites()
    n = 0
    for r in rules:
        src = session.get(Source, r.id)
        if src is None:
            src = Source(
                id=r.id,
                name=r.name,
                base_url=r.base_url,
                enabled=r.enabled,
                mode=r.mode,
                rule_path=r.path,
                rule_version=r.version,
                schedule=r.schedule,
            )
            session.add(src)
            n += 1
        else:
            src.name = r.name
            src.base_url = r.base_url
            src.mode = r.mode
            src.rule_path = r.path
            src.rule_version = r.version
            src.schedule = r.schedule
    session.commit()
    return n


def _save_raw(source_id: str, html: str) -> str:
    """原文 HTML 落盘，保留证据链（合规要求：标注来源、不商业再分发全文）。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    d = RAW_DIR / source_id / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{uuid.uuid4().hex}.html"
    try:
        p.write_text(html, encoding="utf-8")
        return str(p.relative_to(RAW_DIR.parent))
    except Exception:  # noqa: BLE001
        return ""


def _encode_query_values(url: str) -> str:
    """把 GET 详情 URL 的 query 值逐个百分号编码。

    政采云各省门户的 articleId 是 Base64（`cty+FhbhN7fwGMByZaNcBw==`），
    原样拼进 query 会被服务端把 `+` 解成空格、`=` 当成分隔符，
    结果是 HTTP 200 但 `result.data` 为 null —— 不报错、正文却一条都没有。
    实测 percent-encode 之后（%2B / %3D）才能拿到完整正文。
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return url
    if not parts.query:
        return url
    # 不能用 parse_qsl —— 它会先把 `+` 解成空格，再编码成 %20，
    # 反而把 Base64 里的 + 永久破坏掉。这里手动按 & / = 切，
    # 拿到的是**原始未解码**的值，直接 quote 即可。
    seg: list[str] = []
    for item in parts.query.split("&"):
        if not item:
            continue
        k, _, v = item.partition("=")
        seg.append(f"{quote(k, safe='')}={quote(v, safe='')}")
    if not seg:
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(seg), parts.fragment)
    )


def _iter_windows(start: datetime, end: datetime, days: int, max_windows: int = 40):
    """把一段时间切成若干小窗口，从最近的一段开始往前滚动。

    为什么需要：gdggzy / ggzy 这类接口对单次查询能翻的页数有上限
    （gdggzy 实测只给前 3 页，约 150 条）。用「近 30 天」一个大窗口去问，
    拿到的永远只是最新那 150 条，中间的公告全漏。切成小窗后每个窗口的
    数据量都落在上限内，覆盖才完整。
    """
    if days and days > 0:
        cur_end = end
        n = 0
        while cur_end > start and n < max_windows:
            cur_start = max(start, cur_end - timedelta(days=days))
            yield cur_start, cur_end
            cur_end = cur_start
            n += 1
        return
    yield start, end


def _title_hits(title: str, rule: SiteRule) -> bool:
    """本地标题过滤：站端不支持关键词检索时，用 keywords 兜底筛。"""
    words = [w for w in rule.keywords if w]
    if not words:
        return True
    return any(w in title for w in words)


def _exists(session: Session, hash_key: str) -> bool:
    """这条公告是否已入库。用于「先判重再取详情」——重复条目不该再打目标站。"""
    return (
        session.execute(select(Tender.id).where(Tender.hash_key == hash_key)).first()
        is not None
    )


def _ingest_row(
    session: Session,
    rule: SiteRule,
    row: dict[str, str],
    *,
    stats: dict[str, int],
    new_items: list[dict],
) -> None:
    """把一行数据抽取、去重、入库。"""
    stats["found"] += 1

    title = (row.get("title") or "").strip()
    if not title:
        return

    pub = row.get("publish_time") or row.get("date") or ""
    pub_norm = pub[:10] if pub else ""
    hash_key = make_hash_key(title, pub_norm, rule.id)

    # L1 主键去重：命中则跳过
    exists = session.execute(
        select(Tender.id).where(Tender.hash_key == hash_key)
    ).first()
    if exists:
        stats["dup"] += 1
        return

    # 正文来源优先级：详情页正文 > 列表摘要 > JSON 里的 content 字段。
    # 列表摘要里偶尔就写着「预算金额 xx 万元」，白拿的信息不该浪费。
    content = row.get("_text", "") or row.get("content", "") or row.get("summary", "")
    data = extract_tender(title, content, row=row)

    t = Tender(
        hash_key=hash_key,
        source_id=rule.id,
        source_url=row.get("url", "") or "",
        title=data["title"],
        notice_type=data["notice_type"],
        is_result=data["is_result"],
        purchaser=data["purchaser"],
        agency=data["agency"],
        budget_amount=data["budget_amount"],
        currency=data["currency"],
        province=data["province"],
        industry=data["industry"],
        publish_time=data["publish_time"],
        deadline=data["deadline"],
        deadline_raw=data["deadline_raw"],
        deadline_confident=data["deadline_confident"],
        content_text=data["content_text"][:20000],
        attachments=row.get("attachments") or None,
        extract_meta=data["extract_meta"],
        raw_path=row.get("_raw_path", "") or "",
    )
    session.add(t)
    session.flush()  # 拿到 t.id 才能算 cluster

    # L3 跨源折叠
    assign_cluster(session, t)

    stats["new"] += 1
    if data["deadline"]:
        new_items.append(
            {
                "id": t.id,
                "title": t.title,
                "purchaser": t.purchaser,
                "deadline": t.deadline.strftime("%Y-%m-%d"),
                "source": rule.name,
                "url": t.source_url,
                "budget": t.budget_amount,
            }
        )


def run_source(
    session: Session,
    site_id: str,
    *,
    max_pages: int | None = None,
    since: datetime | None = None,
    keyword: str | None = None,
    fetch_detail: bool = False,
) -> dict:
    """跑单个来源。返回结果统计字典，任何异常都被收敛成 failed 状态。"""
    rule = get_site(site_id)
    src = session.get(Source, site_id)
    if rule is None or src is None:
        return {"status": "failed", "error": f"未找到站点 {site_id}"}
    if not rule.enabled:
        return {"status": "skipped", "error": "来源已禁用"}

    started = datetime.now()
    run = CrawlRun(
        source_id=site_id,
        started_at=started,
        status="running",
        since=since or src.last_success_at,
    )
    session.add(run)
    session.commit()

    stats = {"found": 0, "new": 0, "dup": 0}
    new_items: list[dict] = []
    # 页数优先级：显式传参 > 站点自己的 pagination.max_pages > 全局默认。
    # 不同源的翻页代价差别很大——深圳一条详情 3 秒，翻 3 页就是几万毫秒；
    # 而按时间倒序的接口前 10 条已经够新，再翻只是拿到更老的公告。
    # 之前漏掉站点级这一档，等于每个源都被按同一个页数对待。
    pages = (
        max_pages
        or int(rule.pagination.get("max_pages") or 0)
        or settings.crawl.max_pages
    )
    error: str | None = None
    blocked = False

    try:
        # lookback_days：首轮（没有 last_success_at 时）回灌多少天。
        # 哑列表站点用「时间精度换覆盖率」，回灌 30 天只会让首轮空跑很久。
        lookback = rule.lookback_days or 30
        start_str = (since or src.last_success_at or datetime.now() - timedelta(days=lookback))
        end_str = datetime.now()
        # 增量采集时上一轮可能就在几分钟前，窗口会窄到 0 天。
        # 按天发布的公告（发布日期只精确到天）在 0 天窗口里一条都匹配不到，
        # 于是「本轮没新增」被误报成「选择器失效」，来源健康度平白变红。
        # 兜底给足 1 天，重复的部分由去重层挡掉。
        if (end_str - start_str).days < 1:
            start_str = end_str - timedelta(days=1)
        window_span_days = max((end_str - start_str).days, 0)
        # 本地过滤的站点：列表本身与关键词无关，
        # 没必要把同一批数据按 6 个词重复拉 6 遍，一次拉完本地多词筛。
        kw_list = rule.keywords_of(keyword)
        if rule.local_filter and not keyword:
            kw_list = [None]
        # 有些接口限制单条查询能翻的页数（gdggzy 实测只给前 3 页），
        # 长时间段会漏数据，所以按天切成小窗口滚动请求。
        windows = list(_iter_windows(start_str, end_str, rule.window_days))
        warmup_err = ""
        # 详情开关：显式传参优先，其次是站点规则默认值。
        # 列表页普遍不给预算/截止时间，不取详情，雷达上的「预算/剩余天数」就是空的。
        want_detail = fetch_detail or rule.fetch_detail_default
        # browser 模式：整轮采集复用同一个浏览器与 cookie，避免每页重启
        need_browser = rule.mode == "browser"
        sess = BrowserSession() if need_browser else None
        if sess is not None:
            sess.__enter__()
            if sess.error:
                error = f"浏览器不可用：{sess.error}"
                blocked = False
                sess.__exit__(None, None, None)
                sess = None
            elif rule.warmup_url:
                # 阿里云 WAF 一类防护要靠页面 JS 算 cookie，
                # 不先真开一次页面，后面的 raw_json 请求会被全部拦掉。
                werr = sess.warmup(rule.warmup_url)
                # 预热失败不直接判死：有些站点的列表接口本身不受防护，
                # 照常往下跑，真被拦会在取页那一步报 blocked。
                if werr:
                    warmup_err = f"预热失败 {rule.warmup_url} → {werr}"
        errs: list[str] = []
        if warmup_err:
            errs.append(warmup_err)
        got_any_row = False
        # 本轮已取详情的条数，配合 rule.detail_limit 用
        detail_done = 0
        try:
            for w_start, w_end in windows:
                if blocked:
                    break
                w0 = w_start.strftime("%Y-%m-%d")
                w1 = w_end.strftime("%Y-%m-%d")
                for kw in kw_list:
                    if blocked:
                        break
                    for page in range(1, pages + 1):
                            # 用 build_page_url 而不是 build_url：第 1 页可能
                            # 不是 search_url 套 {page}=1（见 first_page_url 注释）
                            url = rule.build_page_url(
                                page=page,
                                keyword=kw,
                                start=w_start.strftime("%Y:%m:%d"),
                                end=w_end.strftime("%Y:%m:%d"),
                            )
                            if rule.post_data:
                                post = rule.build_post_data(
                                    page=page, keyword=kw, start=w0, end=w1,
                                )
                                # 请求体要整体加密的站点：先把明文 body
                                # 换成 {param: RSA(...)}。失败直接抛，
                                # 由下面的 try 记成 error，别拿明文去撞接口。
                                if rule.encrypt:
                                    post = encrypt_request_body(rule.encrypt, post)
                                # gdggzy 这类接口只收 application/json，发表单会被拒
                                res = fetch_http(
                                    url,
                                    method="POST",
                                    data=None if rule.post_json else post,
                                    json_body=post if rule.post_json else None,
                                    legacy_tls=rule.tls_legacy,
                                    # 用 build_headers 而不是裸 headers：
                                    # 带签名的站点（招商局）要现算 X-Authorization，
                                    # 缓存一份静态头会被服务端判 sign.validation.failed。
                                    headers=rule.build_headers() or None,
                                )
                            elif rule.tls_legacy:
                                res = fetch_http(url, legacy_tls=True)
                            else:
                                res = fetch(
                                    url,
                                    allow_browser=(need_browser or None),
                                    prefer_browser=need_browser,
                                    wait_selector=rule.wait_selector or None,
                                    session=sess,
                                    headers=rule.build_headers() or None,
                                    # JSON 接口走浏览器时不渲染页面，直接取响应体
                                    raw_json=(rule.response_type == "json"),
                                )
                            if not res.ok:
                                error = res.error or "取页失败"
                                # 被 WAF/验证码/限流拦截：停手标记 blocked，不重试不硬刚。
                                # 政务接口常把限流藏在 HTTP 200 的 errcode 里，
                                # 这一路通关 Linux 之外的所有站点都要留意。
                                if (
                                    res.blocked_by
                                    or "403" in (error or "")
                                    or "429" in (error or "")
                                    or "errcode=" in (error or "")
                                ):
                                    blocked = True
                                break

                            raw_path = _save_raw(site_id, res.html)
                            rows = parse_list(rule, res.html)
                            if rows:
                                got_any_row = True
                            # 首页就 0 条：多半是选择器失效，留痕便于校准。
                            # 但「时间窗内确实没有法律类公告」是正常的，
                            # 只有整个时间跨度内全都 0 条才怀疑选择器。
                            if not rows:
                                if page == 1 and kw is kw_list[0] and window_span_days >= 1:
                                    error = error or "列表解析为 0 条，可能选择器已失效"
                                break

                            kept = 0
                            for row in rows:
                                # 站点不支持关键词检索时，本地兜底过滤
                                if rule.local_filter and not _title_hits(row.get("title", ""), rule):
                                    continue
                                kept += 1
                                # 先判重再取详情：已经入库的条目没必要再打一次目标站，
                                # 否则每轮都会把整页详情重新跑一遍，慢且不礼貌。
                                title0 = (row.get("title") or "").strip()
                                hk0 = make_hash_key(
                                    title0,
                                    (row.get("publish_time") or row.get("date") or "")[:10],
                                    site_id,
                                )
                                if title0 and _exists(session, hk0):
                                    stats["found"] += 1
                                    stats["dup"] += 1
                                    continue
                                # 限流站点只给最新几条补详情，超了就只用列表字段入库
                                row_detail = want_detail and (
                                    rule.detail_limit <= 0
                                    or detail_done < rule.detail_limit
                                )
                                # 详情接口服务不了这一行（如国网的中标结果类公告）
                                # 就别白打一次请求，直接用列表字段入库。
                                if row_detail and detail_skipped(rule, row):
                                    row_detail = False
                                if row_detail and row.get("url"):
                                    detail_done += 1
                                    # detail_post_raw 也算：请求体是裸标量的站点
                                    # （国家电网 getNoticeBid）没有 detail_post_data，
                                    # 只写 detail_post_raw，条件漏掉它就退化成
                                    # 去抓 row["url"] —— 而那个 URL 是 SPA 的 hash
                                    # 路由，HTTP 拿回来的永远是同一个空壳 HTML，
                                    # 详情字段一条都补不回来且不报错。
                                    # GET 详情（政采云各省 /portal/detail?articleId=…）
                                    # 没有请求体，也得进这个分支——它同样返回 JSON，
                                    # 而不是能直接用选择器抠的 HTML 页面。
                                    if (
                                        rule.detail_post_data
                                        or rule.detail_post_raw
                                        or rule.detail_method == "GET"
                                    ):
                                        # 详情也走接口：详情页只是空壳（cfcpn 还只给
                                        # 未登录用户看截断预览），正文与截止时间在接口里。
                                        # row["url"] 仍指向详情页，那是给用户点的。
                                        dpost = rule.build_detail_post_data(row)
                                        # 列表接口要加密的站点，详情接口一般同一套
                                        # 机制，所以这里同样过一道。
                                        if rule.encrypt and isinstance(dpost, dict):
                                            dpost = encrypt_request_body(
                                                rule.encrypt, dpost
                                            )
                                        if rule.detail_method == "GET":
                                            dpost = None
                                        # 详情接口多半和列表接口不是一个地址（电信：列表
                                        # announcementJoin/queryListNew、详情
                                        # tenderannouncement/view），配了 detail_url 就用它。
                                        # 有些站点还按公告类型分端点（电信：比选走
                                        # tenderannouncement、单一来源走 purchaseannounce），
                                        # 那就用 detail_url_map 按行里的类型字段查。
                                        durl = ""
                                        if rule.detail_url_map:
                                            durl = rule.detail_url_map.get(
                                                str(row.get(rule.detail_url_key) or ""), ""
                                            )
                                        if not durl:
                                            durl = rule.detail_url
                                        durl = "".join(durl.split()) or rule.build_url(
                                            page=1, keyword=kw
                                        )
                                        # GET 详情把参数挂在 URL 上（政采云
                                        # /portal/detail?articleId={article_id}），
                                        # 占位符在这里替换，缺字段就取空串而不是整条失败。
                                        if rule.detail_method == "GET":
                                            try:
                                                durl = durl.format_map(_Row(row))
                                            except Exception:  # noqa: BLE001
                                                durl = ""
                                            durl = _encode_query_values(durl)
                                        # post_json 的站点详情接口同样只吃 JSON：
                                        # 用 data= 会发成表单编码，服务端解析不出来，
                                        # 表现为「详情一条都取不到」且没有任何报错。
                                        dres = fetch_http(
                                            durl,
                                            method=rule.detail_method,
                                            data=None if rule.post_json else dpost,
                                            json_body=dpost if rule.post_json else None,
                                            headers=rule.build_headers() or None,
                                        )
                                        if dres.ok:
                                            det = parse_detail_json(rule, dres.html)
                                            listed = (row.get("content", "")
                                                      or row.get("summary", ""))
                                            n_det = len(html_to_text(det.get("_text", "")))
                                            n_list = len(html_to_text(listed))
                                            if not listed or (
                                                n_det > n_list * 1.2 and n_det > 200
                                            ):
                                                row = {**row, **det}
                                            row["_raw_path"] = _save_raw(site_id, dres.html)
                                        row.setdefault("_raw_path", raw_path)
                                        _ingest_row(
                                            session, rule, row,
                                            stats=stats, new_items=new_items,
                                        )
                                        continue
                                    dres = fetch(
                                        row["url"],
                                        allow_browser=(need_browser or None),
                                        prefer_browser=need_browser,
                                        wait_selector=rule.detail_container or None,
                                        session=sess,
                                    )
                                    # 聚合页：列表链接指向「关联公告」页，正文在二次跳转的 URL 上
                                    if dres.ok and rule.detail_url_from is not None:
                                        sub = extract_link(dres.html, rule.detail_url_from)
                                        if sub:
                                            real = urljoin(row["url"], sub)
                                            d2 = fetch(
                                                real,
                                                allow_browser=(need_browser or None),
                                                prefer_browser=need_browser,
                                                session=sess,
                                            )
                                            if d2.ok:
                                                dres = d2
                                    if dres.ok:
                                        det = parse_detail(rule, dres.html)
                                        # 详情 ✗ 不能无条件覆盖列表正文。
                                        # 站点把登录墙/验证码/错误页藏在 HTTP 200 后面是常态
                                        # （军队采购网的详情页一律回「你好，欢迎来到…请登录」），
                                        # 这种正文一旦写进 row，列表里已经到手的正文字段就被顶掉了，
                                        # 表现出来是「预算和截止日期莫名其妙全空」而没有任何报错。
                                        #
                                        # 所以只在两种情况下采纳详情正文：
                                        #   1. 列表压根没给正文 —— 多数站点列表只有标题，详情是唯一来源
                                        #   2. 详情正文明显比列表的长 —— 说明它确实是完整版
                                        listed = row.get("content", "") or row.get("summary", "")
                                        n_det = len(html_to_text(det.get("_text", "")))
                                        n_list = len(html_to_text(listed))
                                        if not listed or (n_det > n_list * 1.2 and n_det > 200):
                                            row = {**row, **det}
                                        row["_raw_path"] = _save_raw(site_id, dres.html)
                                row.setdefault("_raw_path", raw_path)
                                _ingest_row(session, rule, row, stats=stats, new_items=new_items)

                            # 注意：本地过滤模式下「这一页没命中」不能当成「没有了」——
                            # 广东这类哑列表是按发布日期倒序的全量流，法律服务类可能隔几页才出现一条。
            # 多轮里只要有一轮拿到数据，就不算整轮失败
            if stats["found"] > 0:
                error = None
            elif len(errs) > 1:
                error = error or "；".join(errs[:3])
        finally:
            if sess is not None:
                sess.__exit__(None, None, None)

        # 更新来源状态
        src.last_run_at = datetime.now()
        if blocked:
            src.health = "blocked"
            src.fail_count += 1
            src.last_error = f"可能被目标站拦截：{error}"
            run.status = "blocked"
        elif error and stats["found"] == 0:
            src.health = "error"
            src.fail_count += 1
            src.last_error = error
            run.status = "failed"
        else:
            src.health = "idle"
            src.fail_count = 0
            src.last_success_at = datetime.now()
            src.last_error = None
            run.status = "success" if not error else "partial"

        run.items_found = stats["found"]
        run.items_new = stats["new"]
        run.items_dup = stats["dup"]
        run.error_msg = error
        session.commit()

    except Exception as exc:  # noqa: BLE001 单源异常绝不影响全局
        session.rollback()
        src = session.get(Source, site_id)
        if src:
            src.health = "error"
            src.fail_count += 1
            src.last_error = f"{type(exc).__name__}: {exc}"
            session.commit()
        run = session.get(CrawlRun, run.id)
        if run:
            run.status = "failed"
            run.error_msg = f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now()
            session.commit()
        notify_error(site_id, f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "error": str(exc)}

    run.finished_at = datetime.now()
    run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
    session.commit()

    result = {"status": run.status, **stats, "error": error}

    # 告警：连续失败达到阈值
    if run.status in {"failed", "blocked"} and src.fail_count >= settings.notify.fail_threshold:
        notify_error(site_id, src.last_error or run.status)

    # 关键词订阅命中推送
    if new_items and settings.watch_keywords:
        hits = [
            it
            for it in new_items
            if any(k in (it["title"] or "") for k in settings.watch_keywords)
        ]
        if hits:
            notify_new_items(hits)

    return result


def run_all(session: Session, *, only_enabled: bool = True, fetch_detail: bool = False) -> dict:
    """跑全部来源。单源失败不影响其他源。"""
    q = select(Source)
    if only_enabled:
        q = q.where(Source.enabled.is_(True))
    sources = session.execute(q).scalars().all()

    out: dict[str, dict] = {}
    for s in sources:
        out[s.id] = run_source(session, s.id, fetch_detail=fetch_detail)
    return out
