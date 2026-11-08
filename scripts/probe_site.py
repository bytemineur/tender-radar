#!/usr/bin/env python
"""站点探测工具：找出一个招标站点的真实数据接口与 DOM 结构。

用法：
    python scripts/probe_site.py <url> [关键词]

它做三件事：
  1. 用真浏览器打开页面，等渲染完成，截图 + 存 DOM
  2. 旁听所有 XHR / fetch 请求，把返回 JSON 的接口挑出来
  3. 打印候选列表项选择器（哪个容器下有最多长得像公告的链接）

为什么需要它：政务站大量是 JS 渲染 + 瑞数动态防护，光看 HTML 源码
什么也看不到。校准新来源时先用它跑一遍，比盲猜选择器快得多。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.crawler.fetcher import pick_executable  # noqa: E402

OUT = Path("data/_probe")


def looks_like_list(payload: str) -> bool:
    """粗判：返回体里是否有公告列表特征。"""
    hits = 0
    for kw in ("title", "noticeTitle", "bulletinName", "tenderName", "publishTime", "publishDate"):
        if kw in payload:
            hits += 1
    return hits >= 2 and len(payload) > 500


def main() -> None:
    url = sys.argv[1]
    keyword = sys.argv[2] if len(sys.argv) > 2 else "法律服务"
    OUT.mkdir(parents=True, exist_ok=True)

    xhrs: list[tuple[str, str, str]] = []  # (method, url, body)

    got = pick_executable()
    with sync_playwright() as p:
        kw = {"headless": True, "args": ["--headless=new", "--no-sandbox",
                                         "--disable-blink-features=AutomationControlled"]}
        if got:
            kw["executable_path"] = got[1]
        browser = p.chromium.launch(**kw)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        def on_resp(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" not in ct:
                    return
                body = resp.text()
                if looks_like_list(body):
                    xhrs.append((resp.request.method, resp.url, body[:4000]))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_resp)
        print(f"[1/3] 打开 {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            print("  goto 警告:", str(exc)[:120])
        page.wait_for_timeout(6000)
        # 尝试在站内搜索框输入关键词，触发一次列表请求
        try:
            box = page.query_selector(
                "input[type=text][placeholder*=搜索], input[placeholder*=关键词], "
                "input[name=searchWord], input#searchWord"
            )
            if box:
                box.fill(keyword)
                box.press("Enter")
                print(f"  已用关键词「{keyword}」触发搜索")
                page.wait_for_timeout(5000)
        except Exception:  # noqa: BLE001
            pass

        print(f"[2/3] 页面标题: {page.title()}")
        html = page.content()
        (OUT / "probe_dom.html").write_text(html, encoding="utf-8")
        try:
            page.screenshot(path=str(OUT / "probe.png"), full_page=False)
        except Exception:  # noqa: BLE001
            pass
        print(f"  DOM 已存 {OUT / 'probe_dom.html'}（{len(html)} 字节）")

        # 候选容器：统计哪些 class 下有 >=5 个同构子元素且含链接
        print("[3/3] 候选列表容器（按条目数排序）:")
        cands = page.evaluate("""() => {
            const out = [];
            const seen = new Set();
            document.querySelectorAll('ul, ol, div, tbody').forEach(el => {
                const kids = Array.from(el.children);
                if (kids.length < 5) return;
                const withLink = kids.filter(k => k.querySelector('a')).length;
                if (withLink < kids.length * 0.6) return;
                let cls = el.tagName.toLowerCase();
                if (el.id) cls += '#' + el.id;
                if (el.className && typeof el.className === 'string') {
                    cls += '.' + el.className.trim().split(/\\s+/).join('.');
                }
                if (seen.has(cls)) return;
                seen.add(cls);
                const sample = (kids[0].innerText || '').replace(/\\s+/g,' ').slice(0,60);
                out.push({sel: cls, n: kids.length, sample});
            });
            out.sort((a,b) => b.n - a.n);
            return out.slice(0, 12);
        }""")
        for c in cands:
            print(f"  {c['n']:>3} 条  {c['sel'][:80]}")
            print(f"        样例: {c['sample']}")

        if xhrs:
            print(f"\n抓到 {len(xhrs)} 个疑似列表 JSON 接口:")
            for i, (m, u, body) in enumerate(xhrs[:8], 1):
                print(f"  [{i}] {m} {u[:150]}")
                print(f"      响应片段: {body[:220].replace(chr(10), ' ')}")
            (OUT / "probe_xhr.json").write_text(
                json.dumps([{"method": m, "url": u, "body": b} for m, u, b in xhrs],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  完整响应已存 {OUT / 'probe_xhr.json'}")
        else:
            print("\n没抓到 JSON 列表接口——说明是服务端渲染，直接用 list_container 选择器即可。")

        browser.close()


if __name__ == "__main__":
    main()
