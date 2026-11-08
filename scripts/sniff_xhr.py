"""用真浏览器旁听站点的 XHR，找出列表接口的真实地址与参数。

    python scripts/sniff_xhr.py https://www.ccgp-anhui.gov.cn 采购公告

流程：打开首页 → 打印所有 JSON 响应 → 找「采购公告」入口点进去 → 再打印一轮。
目的是拿到 categoryCode（政采云各省独立部署，码不通用，只能现抓）。
只读，不写库。
"""

from __future__ import annotations

import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 浏览器可执行文件：优先用环境变量，其次按平台猜几个常见位置，
# 都不存在就交给 playwright 自己下载的 Chromium（None = 用默认通道）。
# 别写死 Windows 路径——这个脚本同样要在 Linux 服务器上跑。
CHROME_CANDIDATES = (
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",
)


def _chrome_path() -> str | None:
    env = os.environ.get("TR_BROWSER_PATH") or os.environ.get("CHROME_PATH")
    if env:
        return env
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def sniff(url: str, link_text: str) -> None:
    path = _chrome_path()
    print(f"  浏览器：{path or 'playwright 默认 Chromium'}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=path, headless=True, args=["--no-sandbox"]
        )
        ctx = browser.new_context(
            user_agent=UA, locale="zh-CN", viewport={"width": 1440, "height": 900}
        )
        seen: list[tuple[str, str, str]] = []

        def on_resp(resp) -> None:
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = resp.text()[:400]
            except Exception:  # noqa: BLE001
                return
            if not body.strip().startswith(("{", "[")):
                return
            seen.append((resp.request.method, resp.url, body))

        page = ctx.new_page()
        page.on("response", on_resp)
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            print(f"  goto 失败: {type(exc).__name__}")
        page.wait_for_timeout(3500)

        print(f"\n=== {url} 首页 JSON 响应 {len(seen)} 条")
        for m, u, b in seen[:15]:
            print(f"  {m} {u[:110]}")
            print(f"      {b[:170]}")

        # 点「采购公告」入口
        n_before = len(seen)
        clicked = False
        for sel in ("a:has-text('采购公告')", "a:has-text('招标公告')"):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=8000)
                clicked = True
                print(f"  点击入口：{sel}")
                break
            except Exception:  # noqa: BLE001
                continue
        page.wait_for_timeout(4000)
        print(f"  —— 点击后新增 {len(seen) - n_before} 条（clicked={clicked}）")
        for m, u, b in seen[n_before:][:12]:
            print(f"  {m} {u[:110]}")
            print(f"      {b[:170]}")

        # 抓页面里所有 categoryCode 痕迹
        html = page.content()
        import re

        codes = sorted(set(re.findall(r"\d{2,3}-\d{5,8}", html)))
        print(f"  页面 categoryCode 候选：{codes[:15]}")

        # 请求体里带 categoryCode 的那条最值钱
        for m, u, b in seen:
            if "categoryCode" in u or "categoryCode" in b:
                print(f"  ★ {m} {u[:120]}")
                print(f"      {b[:250]}")
        time.sleep(0.5)
        browser.close()


if __name__ == "__main__":
    target = sys.argv[1]
    text = sys.argv[2] if len(sys.argv) > 2 else "采购公告"
    sniff(target, text)
