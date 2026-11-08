"""把中国省级行政区划边界转成前端可直接用的 SVG 路径数据。

产出 web/china-map.js —— 纯静态数据，运行时不依赖任何地图服务或 CDN。

数据源：阿里 DataV.GeoAtlas（geo.datav.aliyun.com/areas_v3/bound/100000_full.json）
  该文件含 34 个省级行政区 + 九段线（adcode=100000_JD），符合标准行政区划口径。

用法：
    python scripts/build_china_map.py [--source china_full.json] [--out web/china-map.js]

为什么自己算而不用 echarts/map SDK：
  1) 合规 —— 不引入任何第三方地图服务或瓦片源，国界、台湾、九段线来自国家标准数据；
  2) 离线 —— 前端零请求，服务器部署不受外网限制；
  3) 体积 —— 原始 582KB 的边界点经过道格拉斯-普克抽稀后降到 100KB 量级。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# 省名归一：与 app/crawler/normalize.py 的 PROVINCES 白名单口径一致
_SUFFIXES = (
    "壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区", "自治区", "省", "市",
)

# 需要在地图上单独渲染的特殊要素
JD_KEY = "_ninedash"  # 九段线（南海诸岛）
# 低于这个纬度的小岛礁不进主图（由右下角南海诸岛插图表达）。
# 海南本岛最南端约 18.1°N，西沙约 16.8°N，取 17.6 刚好把远海岛礁排除在外。
LAND_LAT_MIN = 17.6


def short_name(full: str) -> str:
    """「内蒙古自治区」→「内蒙古」，「台湾省」→「台湾」。"""
    s = full.strip()
    for suf in _SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def merc_y(lat: float) -> float:
    """墨卡托纵向变换，让高纬度地区的形状不失真。

    中国纬度跨 18°N~53°N，直接用经纬度画会把东北压扁。
    这里输出的单位与经度同量级，可等比缩放。
    """
    lat = max(-85.0, min(85.0, lat))
    return math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def perimeter_area(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """返回 (包围盒面积, 有向面积)。用于取舍碎片岛屿。"""
    if len(ring) < 3:
        return 0.0, 0.0
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    box = (max(xs) - min(xs)) * (max(ys) - min(ys))
    # 鞋带公式算有向面积
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        a += x1 * y2 - x2 * y1
    return box, abs(a) / 2


def simplify(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """道格拉斯-普克折线抽稀。容忍视觉上不可辨的顶点抖动。"""
    if len(pts) < 3:
        return pts[:]

    def rdp(seq: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(seq) < 3:
            return seq[:]
        x1, y1 = seq[0]
        x2, y2 = seq[-1]
        dx, dy = x2 - x1, y2 - y1
        den = math.hypot(dx, dy)
        worst, idx = -1.0, 0
        for i in range(1, len(seq) - 1):
            px, py = seq[i]
            if den == 0:
                d = math.hypot(px - x1, py - y1)
            else:
                d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / den
            if d > worst:
                worst, idx = d, i
        if worst <= eps:
            return [seq[0], seq[-1]]
        return rdp(seq[: idx + 1])[:-1] + rdp(seq[idx:])

    out = rdp(pts)
    # 首尾必须闭合
    if out and pts and out[0] != pts[0]:
        out[0] = pts[0]
    if out and pts and out[-1] != pts[-1]:
        out[-1] = pts[-1]
    return out


def rings_of(geom: dict) -> list[list[tuple[float, float]]]:
    """GeoJSON Polygon/MultiPolygon → 环列表。"""
    t = geom.get("type")
    raw = geom.get("coordinates") or []
    if t == "Polygon":
        return [[tuple(p[:2]) for p in ring] for ring in raw if len(ring) > 2]
    if t == "MultiPolygon":
        out = []
        for poly in raw:
            for ring in poly:
                if len(ring) > 2:
                    out.append([tuple(p[:2]) for p in ring])
        return out
    return []


def build(source: Path, out: Path, width: int = 1000, eps: float = 0.42,
          min_area_ratio: float = 6e-6) -> None:
    data = json.loads(source.read_text(encoding="utf-8"))

    # ── 1) 投影 ───────────────────────────────────────────────
    provs: list[dict] = []
    jd_rings: list[list[tuple[float, float]]] = []
    for f in data["features"]:
        props = f.get("properties") or {}
        raw_name = (props.get("name") or "").strip()
        adcode = str(props.get("adcode") or "")
        rings = [[(lon, merc_y(lat)) for lon, lat in r] for r in rings_of(f["geometry"])]
        if not rings:
            continue
        c = props.get("center") or props.get("centroid")
        center = (c[0], merc_y(c[1])) if c else None
        if adcode.endswith("_JD"):
            jd_rings.extend(rings)
            continue
        provs.append({"key": short_name(raw_name), "full": raw_name,
                      "rings": rings, "center": center})

    # 南海诸岛（纬度 < LAND_LAT_MIN 的岛礁）不进主图，由右下角插图表达，
    # 这是标准中国地图的做法：既画全领土，又不会把主图纵向拉长变形。
    for p in provs:
        p["rings"] = [r for r in p["rings"] if max(lat for _, lat in r) >= LAND_LAT_MIN]

    # ── 2) 主图缩放（省界范围决定画布）─────────────────────
    # 只用每个省面积最大的那块来定画布边界：
    # 三沙这类远海岛礁会把 bbox 一直拉到低纬度，画出来下面全是空白。
    frames: list[tuple[float, float]] = []
    for p in provs:
        if not p["rings"]:
            continue
        rings = sorted(p["rings"], key=lambda r: -perimeter_area(r)[1])
        p["rings"] = rings
        frames.extend(rings[0])
    all_pts = frames
    min_x = min(p[0] for p in all_pts)
    max_x = max(p[0] for p in all_pts)
    min_y = min(p[1] for p in all_pts)
    max_y = max(p[1] for p in all_pts)
    scale = width / (max_x - min_x)
    height = round((max_y - min_y) * scale, 1)

    def to_canvas(pt: tuple[float, float]) -> tuple[float, float]:
        # SVG 的 y 轴朝下，纬度要翻转
        return (round((pt[0] - min_x) * scale, 1), round((max_y - pt[1]) * scale, 1))

    # 面积阈值按画布面积的比例给，避免把小省整个抹掉
    canvas_area = width * height
    threshold = canvas_area * min_area_ratio

    provinces = []
    total_pts = 0
    dropped = 0

    for f in provs:
        # 碎片丢弃前先按面积排序，保证每个省至少留住最大的一块
        scored = sorted(
            ((perimeter_area(r)[1], r) for r in f["rings"]), key=lambda x: -x[0]
        )
        keep_paths = []
        for i, (_area, ring) in enumerate(scored):
            pts = [to_canvas(p) for p in ring]
            _, a = perimeter_area(pts)
            if i > 0 and a < threshold:
                dropped += 1
                continue
            sp = simplify(pts, eps)
            if len(sp) < 4:
                dropped += 1
                continue
            total_pts += len(sp)
            keep_paths.append("M" + "L".join(f"{x} {y}" for x, y in sp) + "Z")

        if not keep_paths:
            continue
        ctr = to_canvas(f["center"]) if f["center"] else None
        provinces.append(
            {
                "name": f["key"],
                "full": f["full"],
                "d": "".join(keep_paths),
                "cx": round(ctr[0], 1) if ctr else None,
                "cy": round(ctr[1], 1) if ctr else None,
            }
        )

    # ── 3) 南海诸岛插图 ───────────────────────────────────────
    # 九段线是线要素，面积近似为 0，不能走面积阈值过滤，否则会被整条抹掉
    jd_pts = [p for r in jd_rings for p in r]
    inset = None
    if jd_pts:
        jx0 = min(p[0] for p in jd_pts)
        jx1 = max(p[0] for p in jd_pts)
        jy0 = min(p[1] for p in jd_pts)
        jy1 = max(p[1] for p in jd_pts)
        box_w = width * 0.105          # 插图宽约占画布 10.5%
        pad = width * 0.006            # 与画布右下角留白
        k = box_w / (jx1 - jx0)
        box_h = (jy1 - jy0) * k
        ox = width - pad - box_w       # 插图左上角在主画布中的位置
        oy = height - pad - box_h
        paths = []
        for r in jd_rings:
            pts = [(ox + (x - jx0) * k, oy + (jy1 - y) * k) for x, y in r]
            sp = simplify([(round(a, 1), round(b, 1)) for a, b in pts], eps * 0.35)
            if len(sp) >= 2:
                paths.append("M" + "L".join(f"{x} {y}" for x, y in sp))
        inset = {
            "paths": paths,
            "frame": [round(ox - 3, 1), round(oy - 3, 1),
                      round(box_w + 6, 1), round(box_h + 6, 1)],
        }

    payload = {
        "viewBox": [0, 0, width, height],
        "provinces": provinces,
        "inset": inset,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    js = (
        "/* 自动生成，勿手改。数据源：全国标准行政区划边界（含九段线）。\n"
        "   生成脚本：scripts/build_china_map.py */\n"
        "window.CHINA_MAP = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"
    )
    out.write_text(js, encoding="utf-8")

    print(f"输出        : {out}")
    print(f"画布        : {width} x {height}")
    print(f"省级行政区  : {len(provinces)} 个")
    print(f"南海诸岛段  : {len(inset['paths']) if inset else 0}")
    print(f"保留顶点    : {total_pts}，丢弃碎片 {dropped} 个环")
    print(f"文件大小    : {out.stat().st_size / 1024:.1f} KB")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(root / "data" / "_probe" / "china_full.json"))
    ap.add_argument("--out", default=str(root / "web" / "china-map.js"))
    ap.add_argument("--width", type=int, default=1000)
    ap.add_argument("--eps", type=float, default=0.42, help="抽稀容差（画布单位）")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print("缺少边界数据，先下载：")
        print("  curl -o data/_probe/china_full.json "
              "https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json")
        return 1
    build(src, Path(args.out), width=args.width, eps=args.eps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
