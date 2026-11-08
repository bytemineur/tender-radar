"""勘察一个站点的列表结构：找接口、找容器。

    python scripts/inspect_site.py https://example.com/list

输出三块：
  1. 页面里出现的疑似 JSON 接口地址（.do / /api/ / query|list|search|notice）
  2. 表单（action + 字段）
  3. 重复结构最多的容器（ul/div/table 下同构子节点 >= 8 个的）

只读，不写库，不改配置。用来替代人肉 F12。
"""

from __future__ import annotations

import re
import sys
from collections import Counter

import httpx
from parsel import Selector

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

API_RE = re.compile(
    r"[\"'\(`\s]((?:https?://[^/\"'\s]+)?/[A-Za-z0-9_./\-]*"
    r"(?:\.do|/api/|/rest/|/open/|query|Query|list|List|search|Search|notice|Notice|"
    r"bulletin|Bulletin|announcement|page|Page)[A-Za-z0-9_./?&\-]*)"
)


def show(url: str) -> None:
    try:
        with httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=25,
            follow_redirects=True,
            verify=False,
            trust_env=False,
        ) as cli:
            r = cli.get(url)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] {url} -> {type(exc).__name__}: {exc}")
        return

    print(f"\n=== {url}\n    {r.status_code}  {len(r.text)}B  final={r.url}")
    if r.status_code != 200 or len(r.text) < 500:
        print("    （非 200 或内容过短，先看这一行就够）")
        print("    " + re.sub(r"\s+", " ", r.text[:300]))
        return

    sel = Selector(r.text)
    title = sel.css("title::text").get(default="").strip()
    print(f"    title: {title[:60]}")

    apis = Counter()
    for m in API_RE.finditer(r.text):
        u = m.group(1)
        if len(u) < 8 or u.startswith("//"):
            continue
        if any(u.lower().endswith(x) for x in (".js", ".css", ".png", ".jpg", ".svg", ".ico")):
            continue
        apis[u] += 1
    if apis:
        print("    ── 疑似接口 ──")
        for u, n in apis.most_common(14):
            print(f"      {n:>3}x  {u[:110]}")

    forms = sel.css("form")
    if forms:
        print("    ── 表单 ──")
        for f in forms[:5]:
            action = f.attrib.get("action", "")
            names = f.css("input::attr(name)").getall()
            print(f"      action={action[:80]}  fields={[n for n in names if n][:12]}")

    print("    ── 重复容器 ──")
    cand = []
    for node in sel.css("ul, ol, div, table"):
        kids = node.xpath("./li | ./tr | ./div | ./a")
        if len(kids) >= 8:
            cls = node.attrib.get("class", "")[:40]
            ident = node.attrib.get("id", "")[:20]
            tag = node.root.tag
            cand.append(
                (
                    len(kids),
                    f"{tag}" + (f".{cls.split()[0]}" if cls else "")
                    + (f"#{ident}" if ident else ""),
                )
            )
    cand.sort(reverse=True)
    seen = set()
    for n, path in cand[:12]:
        if path in seen:
            continue
        seen.add(path)
        print(f"      {n:>3} 子项   {path}")
    if not cand:
        print("      （没找到 >=8 个同构子节点的容器，可能是 JS 渲染）")


def main() -> None:
    for u in sys.argv[1:]:
        show(u)


if __name__ == "__main__":
    main()
