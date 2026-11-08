"""候选来源批量探活。

用途：接入新来源前，先确认「这个域名活着、列表页能拿到东西」，
避免把一份写得很漂亮但根本取不到数的规则提交进 configs/sites/。

    python scripts/probe_candidates.py           # 跑内置清单
    python scripts/probe_candidates.py 央企       # 只跑名字含「央企」的分组

只发 GET，不写库，不改任何配置。
"""

from __future__ import annotations

import sys

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 只放权威来源：政府（财政/公共资源交易）与国央企电子商务平台。
# 第三方聚合站（必联、采招网、千里马、法律招标网一类）一律不收。
CANDIDATES: dict[str, list[tuple[str, str]]] = {
    "央企·电力能源": [
        ("华能集团电子商务平台", "https://ec.chng.com.cn/"),
        ("华电电子商务平台", "https://ecm.chd.com.cn/"),
        ("华电电子商务平台(旧域)", "https://ec.chd.com.cn/"),
        ("国家电投电子商务平台", "https://ebidding.spic.com.cn/"),
        ("三峡集团电子采购平台", "https://eps.ctg.com.cn/"),
        ("中核集团电子商务平台", "http://ec.cnnc.com.cn/"),
        ("中广核电子商务平台", "https://epr.cgnpc.com.cn/"),
        ("中石化电子招标投标交易平台", "https://ebidding.sinopec.com/"),
        ("中石油电子招标平台", "http://ebid.cnpc.com.cn/"),
    ],
    "央企·其他": [
        ("中国交建供应链管理平台", "https://ec.ccccltd.cn/"),
        ("中国中车采购电子商务平台", "https://pur.crrc.cn/"),
        ("中国宝武招标平台", "https://bid.baowugroup.com/"),
        ("中国中化电子采购平台", "https://ebuy.sinochem.com/"),
        ("国机集团电子招标平台", "https://ebid.sinomach.com.cn/"),
        ("中国铁塔电子采购平台", "https://ebid.chinatowercom.cn/"),
        ("中国有色集团采购平台", "https://ec.cnmc.com.cn/"),
        ("中国五矿采购电子商务平台", "https://ec.minmetals.com.cn/"),
        ("中国建材集团招标采购平台", "https://www.cnbmec.com/"),
        ("中国船舶集团电子招标平台", "https://zb.cssc.net.cn/"),
        ("国铁采购平台", "https://cg.95306.cn/"),
    ],
    "政府·省级财政": [
        ("北京市政府采购网", "https://zfcg.bjcz.gov.cn/"),
        ("河北省政府采购网", "http://www.ccgp-hebei.gov.cn/"),
        ("山西省政府采购网", "http://www.ccgp-shanxi.gov.cn/"),
        ("辽宁省政府采购网", "http://www.ccgp-liaoning.gov.cn/"),
        ("吉林省政府采购网", "http://www.ccgp-jilin.gov.cn/"),
        ("江苏省政府采购网", "http://www.ccgp-jiangsu.gov.cn/"),
        ("安徽省政府采购网", "http://www.ccgp-anhui.gov.cn/"),
        ("福建省政府采购网", "http://zfcg.czt.fujian.gov.cn/"),
        ("江西省政府采购网", "http://www.ccgp-jiangxi.gov.cn/"),
        ("山东省政府采购网", "http://www.ccgp-shandong.gov.cn/"),
        ("河南省政府采购网", "http://www.hngp.gov.cn/"),
        ("湖南省政府采购网", "http://www.ccgp-hunan.gov.cn/"),
        ("黑龙江省政府采购网", "http://www.hljcg.gov.cn/"),
    ],
    "政府·公共资源交易": [
        ("北京市公共资源交易服务平台", "https://ggzy.beijing.gov.cn/"),
        ("重庆市公共资源交易网", "https://www.cqggzy.com/"),
        ("贵州省公共资源交易云", "https://ggzy.guizhou.gov.cn/"),
        ("云南省公共资源交易信息网", "https://ggzy.yn.gov.cn/"),
        ("广西公共资源交易平台", "http://gxggzy.gxzf.gov.cn/"),
        ("内蒙古公共资源交易网", "https://ggzyjy.nmg.gov.cn/"),
        ("甘肃省公共资源交易网", "https://ggzyjy.gansu.gov.cn/"),
        ("宁夏公共资源交易网", "https://www.nxggzyjy.org/"),
        ("青海省公共资源交易网", "https://www.qhggzyjy.ggzy.gov.cn/"),
        ("江苏省公共资源交易平台", "https://jsggzy.jszwfw.gov.cn/"),
        ("黑龙江省公共资源交易网", "https://ggzyjyw.hlj.gov.cn/"),
    ],
}

_HIT = ("招标", "采购", "公告", "中标", "谈判", "磋商")


def probe(name: str, url: str) -> None:
    try:
        with httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=20,
            follow_redirects=True,
            verify=False,
            # 不走 http_proxy：本机那个代理会对部分政务域名直接回 502，
            # 那不是站点不通，是代理不放行。探活要测的是真实可达性。
            trust_env=False,
        ) as cli:
            r = cli.get(url)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR ] {name:<24} {url}  ->  {type(exc).__name__}: {exc}")
        return

    text = r.text
    title = ""
    if "<title" in text[:4000].lower():
        seg = text[text.lower().find("<title") :]
        seg = seg[seg.find(">") + 1 :]
        title = seg[: seg.find("<")].strip().replace("\n", " ")[:40]
    hits = [w for w in _HIT if w in text]
    flag = "OK " if r.status_code == 200 and len(text) > 2000 else "?? "
    print(
        f"[{flag}] {name:<24} {r.status_code} {len(text):>8}B "
        f"final={str(r.url)[:60]:<60} title={title[:30]:<30} 命中={','.join(hits) or '无'}"
    )


def main() -> None:
    filt = sys.argv[1] if len(sys.argv) > 1 else ""
    for group, items in CANDIDATES.items():
        if filt and filt not in group:
            continue
        print(f"\n── {group} ──")
        for name, url in items:
            probe(name, url)


if __name__ == "__main__":
    main()
