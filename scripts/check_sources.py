#!/usr/bin/env python
"""来源自检：逐个试跑所有已配置来源，报告能不能抓、抓到几条。

用法：
    python scripts/check_sources.py            # 检查全部（含已停用的）
    python scripts/check_sources.py ccgp ggzy  # 只查指定的

它**不写数据库**，只取第一页并解析，适合：
  · 新部署到服务器后，先确认哪些源在当前网络环境可用
  · 站点改版后，快速定位是哪个源的选择器失效了

输出示例：
    ✅ ccgp        中国政府采购网        browser   20 条  1.8s   样例：XX局法律服务项目…
    ❌ cebpub      中国招标投标公共服务平台  browser    0 条  3.2s   被拦截：var GH=[
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crawler.adapters import get_site, load_all_sites, parse_list  # noqa: E402
from app.crawler.fetcher import (  # noqa: E402
    BrowserSession,
    encrypt_request_body,
    fetch,
    fetch_http,
    pick_executable,
)


def check_one(rule) -> tuple[bool, str]:
    """跑一个源的第一页。返回 (是否可用, 说明)。"""
    start = (datetime.now() - timedelta(days=30)).strftime("%Y:%m:%d")
    end = datetime.now().strftime("%Y:%m:%d")
    if rule.post_data:
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")

    # build_page_url 而非 build_url：配了 first_page_url 的源，
    # 第 1 页不在 search_url 上，用 build_url 会打到错误的地址误判成「不可用」
    url = rule.build_page_url(page=1, start=start, end=end)
    t0 = time.time()

    need_browser = rule.mode == "browser"
    sess = None
    if need_browser:
        sess = BrowserSession().__enter__()
        if sess.error:
            sess.__exit__(None, None, None)
            return False, f"浏览器启动失败：{sess.error[:80]}"
        if rule.warmup_url:
            # 与 pipeline 同款：阿里云 WAF 的票要靠真开一次页面才算得出来，
            # 不预热的话这里必然拿到 17K 的挑战页，会被误判成「选择器失效」。
            werr = sess.warmup(rule.warmup_url)
            if werr:
                print(f"   ⚠ 预热失败：{werr}")
    try:
        if rule.post_data:
            post = rule.build_post_data(page=1, start=start, end=end)
            if rule.encrypt:
                post = encrypt_request_body(rule.encrypt, post)
            res = fetch_http(
                url,
                method="POST",
                data=None if rule.post_json else post,
                json_body=post if rule.post_json else None,
                legacy_tls=rule.tls_legacy,
                headers=rule.build_headers() or None,
            )
        else:
            res = fetch(
                url,
                allow_browser=True,
                prefer_browser=need_browser,
                wait_selector=rule.wait_selector or None,
                session=sess,
                headers=rule.build_headers() or None,
                raw_json=(rule.response_type == "json"),
            )
    finally:
        if sess is not None:
            sess.__exit__(None, None, None)

    cost = time.time() - t0
    if not res.ok:
        why = res.blocked_by and f"被拦截：{res.blocked_by}" or (res.error or "取页失败")
        return False, f"{why}（{cost:.1f}s）"

    rows = parse_list(rule, res.html)
    if not rows:
        return False, (
            f"取页成功但解析 0 条，选择器可能已失效（{len(res.html)} 字节，{cost:.1f}s）"
        )

    sample = (rows[0].get("title") or rows[0].get("url") or "")[:36]
    return True, f"{len(rows):>3} 条  {cost:.1f}s  样例：{sample}"


def main() -> None:
    only = set(sys.argv[1:])
    rules = [r for r in load_all_sites() if not only or r.id in only]
    if not rules:
        print("configs/sites 下没有匹配的规则")
        return

    got = pick_executable()
    print(f"浏览器内核：{got[0] if got else 'playwright 内置 Chromium'}")
    print(f"检查 {len(rules)} 个来源，只取第一页，不写库\n")

    ok_n = 0
    for r in rules:
        tag = "启用" if r.enabled else "停用"
        print(f"▸ {r.id:<10} {r.name}（{r.mode}/{tag}）", flush=True)
        try:
            ok, msg = check_one(r)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        print(f"  {'✅' if ok else '❌'} {msg}\n", flush=True)
        ok_n += 1 if ok else 0

    print(f"可用 {ok_n}/{len(rules)}")
    if ok_n == 0:
        print(
            "全部失败时先排查网络：部分政务站只对特定地域/运营商放行，\n"
            "或在服务器上没装浏览器（Docker 镜像已内置 Chromium）。"
        )


if __name__ == "__main__":
    main()
