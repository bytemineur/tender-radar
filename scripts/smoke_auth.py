"""首页 / 登录 / 控制台 / 管理后台 的浏览器冒烟测试。

用真实浏览器跑一遍主流程，截图落在 scripts/shots/ 下。
浏览器复用本机 Chrome（与项目抓取层同一套路），不额外下载内核。

用法：
    .venv/Scripts/python.exe scripts/smoke_auth.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# 抓取层要清掉代理才能连本地调试端口，这里同样处理
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "scripts" / "shots"
BASE = os.environ.get("TR_BASE", "http://127.0.0.1:8123")

# 与 configs/app.yaml 的 auth.admin_emails 保持一致
# 默认跑真实管理员邮箱（configs/app.yaml → auth.admin_emails）
ADMIN_EMAIL = os.environ.get("TR_SMOKE_ADMIN", "zhanghuihua868@outlook.com")
# 非白名单邮箱，用来验证「其他人一律普通用户」
USER_EMAIL = os.environ.get("TR_SMOKE_USER", "someone@example.com")


def find_chrome() -> str | None:
    import shutil

    for p in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).exists():
            return p
    return shutil.which("chrome") or shutil.which("msedge")


def login(page, email: str) -> None:
    """邮箱直登：填邮箱 → 点登录。不校验验证码。"""
    page.fill("#authEmail", email)
    page.click("#authSubmit")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    exe = find_chrome()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=exe,
            args=["--headless=new", "--disable-gpu", "--no-sandbox"],
        )
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()

        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        # ── 1. 首页 ──────────────────────────────────
        page.goto(f"{BASE}/", wait_until="networkidle")
        print("首页标题:", page.title())
        stats = page.inner_text("#heroStats").replace("\n", " ")
        print("Hero 数字:", stats)
        page.screenshot(path=str(SHOTS / "01-home-top.png"))
        # 整页截图，检查各区块排版
        page.screenshot(path=str(SHOTS / "02-home-full.png"), full_page=True)

        # ── 2. 未登录进控制台应被门禁挡住 ───────────────
        page.goto(f"{BASE}/app.html", wait_until="networkidle")
        page.wait_for_selector("#gate.show", timeout=8000)
        print("门禁已出现（未登录）")
        page.screenshot(path=str(SHOTS / "03-gate.png"))

        # ── 3. 登录（管理员）──────────────────────────
        page.click("#gateBtn")
        page.wait_for_selector(".auth-mask.show", timeout=5000)
        login(page, ADMIN_EMAIL)
        page.wait_for_url(re.compile(r"app\.html"), timeout=15000)
        page.wait_for_selector("#view-board.active", timeout=15000)
        page.wait_for_timeout(1200)
        print("登录后控制台已加载，看板总数:", page.inner_text("#mTotal"))
        page.screenshot(path=str(SHOTS / "04-app-board.png"))

        # 顶栏应出现账号与「管理员」徽标
        acct = page.inner_text("#accountSlot")
        print("顶栏账号区:", acct.replace("\n", " ").strip())

        # ── 4a. 已登录回首页：应显示「进入控制台」，不再是「登录 / 注册」 ──
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(800)
        home_slot = page.inner_text("#accountSlot")
        print("首页账号区（已登录）:", home_slot.replace("\n", " ").strip())
        if "进入控制台" not in home_slot:
            errors.append("登录后首页没有「进入控制台」入口")
        page.click('#accountSlot a[href="app.html"]')
        page.wait_for_selector("#view-board.active", timeout=15000)
        page.wait_for_timeout(800)
        print("从首页「进入控制台」跳转成功")

        # ── 4. 雷达 ──────────────────────────────────
        page.click('#tabs button[data-view="radar"]')
        page.wait_for_timeout(1500)
        n = page.eval_on_selector_all("#list .card", "els => els.length")
        print("雷达卡片数:", n)
        page.screenshot(path=str(SHOTS / "05-app-radar.png"))

        # ── 5. 来源 ──────────────────────────────────
        page.click('#tabs button[data-view="sources"]')
        page.wait_for_timeout(1200)
        rows = page.eval_on_selector_all("#srcBody tr", "els => els.length")
        print("来源行数:", rows)
        page.screenshot(path=str(SHOTS / "06-app-sources.png"))

        # ── 6. 管理后台（管理员才看得到入口）───────────
        admin_tab = page.query_selector("#tabAdmin")
        if not admin_tab or admin_tab.is_hidden():
            errors.append("管理员账号看不到「管理」入口")
        else:
            page.click('#tabs button[data-view="admin"]')
            page.wait_for_selector("#view-admin.active", timeout=8000)
            page.wait_for_timeout(2000)
            print("后台今日 PV:", page.inner_text("#aTodayPv"),
                  "/ UV:", page.inner_text("#aTodayUv"),
                  "/ 用户:", page.inner_text("#aUsers"))
            print("后台提示:", page.inner_text("#admHint"))
            print("套餐提示:", page.inner_text("#admBilling"))
            users = page.eval_on_selector_all("#admUserBody tr", "els => els.length")
            print("后台用户行数:", users)
            page.screenshot(path=str(SHOTS / "07-admin.png"), full_page=True)

        # ── 7. 横向溢出检查（三档宽度）────────────────
        for w, name in ((1440, "wide"), (1024, "mid"), (390, "mobile")):
            page.set_viewport_size({"width": w, "height": 900})
            page.wait_for_timeout(500)
            over = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            print(f"  宽度 {w}: 横向溢出 {over}px")
            if over > 1:
                errors.append(f"{name}({w}px) 横向溢出 {over}px")
            page.screenshot(path=str(SHOTS / f"08-admin-{name}.png"))

        # 首页同样检查
        page.set_viewport_size({"width": 390, "height": 900})
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(600)
        over = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        print(f"  首页 390px: 横向溢出 {over}px")
        if over > 1:
            errors.append(f"首页移动端横向溢出 {over}px")
        page.screenshot(path=str(SHOTS / "09-home-mobile.png"), full_page=True)

        # ── 8. 登出 ──────────────────────────────────
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(f"{BASE}/app.html", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.click(".acct-btn")
        page.wait_for_timeout(300)
        page.click('[data-act="logout"]')
        page.wait_for_timeout(1500)
        print("登出后 URL:", page.url)
        if "app.html" in page.url:
            errors.append("登出后仍停留在控制台")
        page.screenshot(path=str(SHOTS / "10-after-logout.png"))

        # ── 8b. 非白名单邮箱登录后只能是普通用户 ─────────
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(500)
        page.click('.actions [data-act="login"]')
        page.wait_for_selector(".auth-mask.show", timeout=5000)
        login(page, USER_EMAIL)
        page.wait_for_url(re.compile(r"app\.html"), timeout=15000)
        page.wait_for_selector("#view-board.active", timeout=15000)
        page.wait_for_timeout(1200)
        # 必须查「真实可见性」而不是 hidden 属性：CSS 里 .tab-seg button 自带
        # display，会盖掉浏览器默认的 [hidden]{display:none}，属性在、按钮照样显示。
        if page.is_visible("#tabAdmin"):
            errors.append("普通用户也能看到「管理」标签")
        if "管理员" in page.inner_text("#accountSlot"):
            errors.append("普通用户被标成了管理员")
        print("普通用户登录:", page.inner_text("#accountSlot").replace("\n", " ").strip(),
              "| 管理标签可见:", page.is_visible("#tabAdmin"))
        page.screenshot(path=str(SHOTS / "10b-user-login.png"))
        page.click(".acct-btn")
        page.wait_for_timeout(300)
        page.click('[data-act="logout"]')
        page.wait_for_timeout(1200)

        # ── 9. 首页未登录态 + 登录弹窗 ──────────────────
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(600)
        if "登录" not in page.inner_text("#accountSlot"):
            errors.append("登出后首页没有恢复成「登录 / 注册」")
        page.click('.actions [data-act="login"]')
        page.wait_for_selector(".auth-mask.show", timeout=5000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "11-home-login-modal.png"))

        # 免验证提示应当直接写在弹窗里，让用户知道「填邮箱就能进」
        page.fill("#authEmail", ADMIN_EMAIL)
        dev_txt = page.inner_text("#authDev")
        print("登录提示:", dev_txt)
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "11b-login-devcode.png"))

        page.click(".auth-x")
        page.wait_for_timeout(300)
        if page.query_selector(".auth-mask.show"):
            errors.append("登录弹窗点 × 关不掉")

        # ── 10. 浅色主题 ─────────────────────────────
        page.click('#themeSwitch button[data-theme="light"]')
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "12-home-light.png"), full_page=True)
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
        page.screenshot(path=str(SHOTS / "13-home-light-top.png"))

        real = [e for e in console_errors if "status of 429" not in e]
        if real:
            errors.append("控制台报错：" + " | ".join(real[:5]))

        browser.close()

    print("\n" + "=" * 50)
    if errors:
        print("发现问题：")
        for e in errors:
            print("  ✗", e)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
