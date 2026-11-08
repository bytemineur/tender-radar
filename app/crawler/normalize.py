"""标准化清洗。

BidInsight 规格：标题/正文/预算/币种/采购人/代理/截止/区域/行业
→ 噪声去除、时间归一化、金额抽取（默认 CNY 映射）。

另按 CaseBoard 实战补了两点：
  1) 全角→半角 + 去空白 + 剥离【】符号前缀，否则「（2026）苏 0213 民初…」
     与「(2026)苏0213民初…」会当成两条（CaseBoard 案号归一化踩过同样的坑）
  2) deadline 合理性校验：不合法的「2099 年占位截止日」一律降级为 unknown
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

# ── 字符归一化 ──────────────────────────────────────────────────────────
# 全角字母数字 → 半角
_FULL2HALF = {chr(0xFF01 + i): chr(0x21 + i) for i in range(94)}
_FULL2HALF[0x3000] = " "  # 全角空格

# 常见的公告标题装饰前缀，剥离后再做相似度比较
_PREFIX_NOISE = re.compile(
    r"^\s*([【\[（(][^】\]）)]{1,12}[】\]）)]\s*)+"
)


def to_halfwidth(s: str) -> str:
    """全角转半角。"""
    return s.translate(_FULL2HALF) if s else ""


def normalize_text(s: str | None) -> str:
    """通用文本清洗：全角化、压缩空白。"""
    if not s:
        return ""
    s = to_halfwidth(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_title(title: str | None) -> str:
    """标题归一化——去重与跨源折叠的比对基准。

    步骤：全角转半角 → 压缩空白 → 剥离【xx】装饰前缀 → 统一标点。
    """
    if not title:
        return ""
    s = to_halfwidth(title)
    s = re.sub(r"\s+", "", s)  # 标题比对时彻底去空白
    s = _PREFIX_NOISE.sub("", s)
    # 中英文标点统一，避免「：」与「:」造成误判
    s = s.replace("：", ":").replace("，", ",").replace("；", ";")
    return s.strip().lower()


# ── 时间归一化 ──────────────────────────────────────────────────────────
_TIME_PATTERNS = [
    (re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"), "ymd"),
    (re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "ymd"),
    (re.compile(r"(\d{4})(\d{2})(\d{2})"), "ymd"),
]

# 截止时间常见表述：注意只取日期部分，时刻另行解析
_DEADLINE_HINT = re.compile(
    r"(截止(时间|日期)?|报名(截止|至|时间)|投标(截止|至|时间)|响应(截止|时间)|"
    r"递交(截止|时间)|开标(时间|日期)|报价(截止|时间))"
)


def parse_date(raw: str | None) -> datetime | None:
    """把各种写法归一到 datetime。失败返回 None，绝不抛异常。"""
    if not raw:
        return None
    s = to_halfwidth(raw).strip()
    for pat, kind in _TIME_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            return datetime(y, mo, d)
        except (ValueError, TypeError):
            continue
    return None


def validate_deadline(
    deadline: datetime | None, publish: datetime | None
) -> tuple[datetime | None, bool]:
    """截止时间合理性校验。

    CaseBoard 曾出现「2099 年」占位截止日污染排序，因此这里硬性拦截：
      - 必须晚于发布时间
      - 必须早于发布时间 + 365 天
    不通过则丢弃日期但保留原文（deadline_raw），标记不可信。
    """
    if deadline is None:
        return None, False
    if publish is not None:
        if deadline <= publish:
            return None, False
        if deadline > publish + timedelta(days=365):
            return None, False
    else:
        # 没有发布时间兜底时，至少不能是遥远的未来
        if deadline > datetime.now() + timedelta(days=365 * 2):
            return None, False
    return deadline, True


# ── 金额抽取 ────────────────────────────────────────────────────────────
_NUM = r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?"
# 数字与单位之间可能夹一个左括号：湖北平台就写「预算金额：66.000000(万元)」。
# 之前严格连写，单位组匹配不到，66 万会被当成 66 元 —— 直接被 MIN_BUDGET 砍掉。
_MONEY = re.compile(
    r"[¥￥]?\s*(" + _NUM + r")\s*[（(]?\s*(万元整|万元|亿元|亿|元整|元)?"
)
# 单位到「元」的换算
_UNIT_SCALE = {
    "": 1.0,
    "元": 1.0,
    "元整": 1.0,
    "万": 10_000.0,
    "万元": 10_000.0,
    "万元整": 10_000.0,
    "亿": 100_000_000.0,
    "亿元": 100_000_000.0,
}

# 预算字段的上下文提示词，命中才认，避免把编号、电话当成金额
_BUDGET_CONTEXT = re.compile(r"(预算|控制价|限价|金额|报价|标的)")


def parse_money(raw: str | None) -> tuple[float | None, bool]:
    """抽取金额并归一到『元』。返回 (金额, 是否明确带单位)。

    未带单位时不猜（CaseBoard 教训：预算不能靠猜单位），由调用方决定。

    注意：千分位必须在「匹配之后」再去掉。早先的实现先 replace(",", "")，
    导致 790,000.00 被 [0-9]{1,4} 截成 7900 —— 预算整整少两个数量级。
    """
    if not raw:
        return None, False
    s = to_halfwidth(raw).replace("，", ",")
    m = _MONEY.search(s)
    if not m:
        return None, False
    try:
        # 带千分位的写法要先把逗号摘掉才能转 float
        value = float(m.group(1).replace(",", ""))
    except (ValueError, TypeError):
        return None, False
    unit = (m.group(2) or "").strip()
    scale = _UNIT_SCALE.get(unit)
    if scale is None:
        return None, False
    return value * scale, bool(unit)


# ── 区域与行业 ──────────────────────────────────────────────────────────
PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
]

INDUSTRIES = {
    "法律服务": ["法律", "律师", "顾问", "法务", "诉讼", "合规", "仲裁"],
    "信息技术": ["软件", "信息化", "系统", "网络", "数据", "云", "平台", "运维"],
    "建设工程": ["施工", "建筑", "工程", "装修", "市政", "监理", "造价"],
    "医疗设备": ["医疗", "药品", "器械", "医院", "卫生"],
    "教育培训": ["教育", "培训", "课程", "学校"],
    "物业服务": ["物业", "保洁", "保安", "绿化", "餐饮"],
    "交通物流": ["运输", "物流", "车辆", "道路", "交通"],
    "金融保险": ["金融", "银行", "保险", "证券", "审计"],
    "能源环保": ["能源", "电力", "环保", "节能", "新能源"],
    "咨询服务": ["咨询", "评估", "设计", "规划", "研究"],
}


# ── 省份推断 ────────────────────────────────────────────────────────────
# 「09点00分（北京时间）」是截止时间的标准写法，
# 直接子串匹配会把湖北的公告判成北京 —— 必须先把这个词挖掉
_PROV_TRAP = re.compile(r"北京时间")

# 中标/成交公告里，中标律所所在地不是项目所在地：
# 「北京市中伦（广州）律师事务所」≠ 北京的采购项目
_PROV_NOISE = (
    "律师事务所", "成交供应商", "中标供应商", "中标人", "成交人", "中标单位",
    "候选人", "成交金额", "代理服务费", "应答人", "响应供应商", "招标人代理",
)

# 这些前缀后面的地名，几乎一定是项目所在地
_PROV_CUE = (
    "采购人", "招标人", "采购单位", "招标单位", "发布单位", "业主单位", "采购机构",
    "项目所在地", "所属地区", "所在地区", "实施地点", "交货地点", "服务地点",
    "地址", "联系地址", "采购人地址",
)

# 很多采购人只写城市不写省（「深圳供电局有限公司」「梅州供电局」），
# 补一层地级市 → 省份映射可以把这些线索落到地图上。
# 只收无歧义的地级市名：像「中山」「朝阳」这种同时是市辖区的名字没放进来。
CITY_TO_PROVINCE = {
    # 华北 / 东北
    "石家庄": "河北", "唐山": "河北", "保定": "河北", "邯郸": "河北", "廊坊": "河北",
    "秦皇岛": "河北", "沧州": "河北", "张家口": "河北", "承德": "河北", "衡水": "河北",
    "太原": "山西", "大同": "山西", "临汾": "山西", "运城": "山西", "长治": "山西",
    "晋中": "山西", "忻州": "山西", "吕梁": "山西", "阳泉": "山西", "晋城": "山西",
    "呼和浩特": "内蒙古", "包头": "内蒙古", "鄂尔多斯": "内蒙古", "赤峰": "内蒙古",
    "通辽": "内蒙古", "呼伦贝尔": "内蒙古", "乌兰察布": "内蒙古",
    "沈阳": "辽宁", "大连": "辽宁", "鞍山": "辽宁", "抚顺": "辽宁", "锦州": "辽宁",
    "营口": "辽宁", "盘锦": "辽宁", "丹东": "辽宁", "葫芦岛": "辽宁", "辽阳": "辽宁",
    "长春": "吉林", "四平": "吉林", "通化": "吉林", "松原": "吉林", "辽源": "吉林",
    "哈尔滨": "黑龙江", "大庆": "黑龙江", "齐齐哈尔": "黑龙江", "牡丹江": "黑龙江",
    # 华东
    "南京": "江苏", "苏州": "江苏", "无锡": "江苏", "常州": "江苏", "南通": "江苏",
    "徐州": "江苏", "扬州": "江苏", "盐城": "江苏", "泰州": "江苏", "镇江": "江苏",
    "淮安": "江苏", "连云港": "江苏", "宿迁": "江苏",
    "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "嘉兴": "浙江", "金华": "浙江",
    "绍兴": "浙江", "台州": "浙江", "湖州": "浙江", "丽水": "浙江", "衢州": "浙江",
    "舟山": "浙江",
    "合肥": "安徽", "芜湖": "安徽", "蚌埠": "安徽", "阜阳": "安徽", "安庆": "安徽",
    "马鞍山": "安徽", "铜陵": "安徽", "淮南": "安徽", "滁州": "安徽", "六安": "安徽",
    "福州": "福建", "厦门": "福建", "泉州": "福建", "漳州": "福建", "莆田": "福建",
    "龙岩": "福建", "三明": "福建", "宁德": "福建", "南平": "福建",
    "南昌": "江西", "赣州": "江西", "九江": "江西", "上饶": "江西", "宜春": "江西",
    "吉安": "江西", "抚州": "江西", "景德镇": "江西", "萍乡": "江西",
    "济南": "山东", "青岛": "山东", "烟台": "山东", "潍坊": "山东", "淄博": "山东",
    "临沂": "山东", "济宁": "山东", "威海": "山东", "泰安": "山东", "东营": "山东",
    "日照": "山东", "枣庄": "山东", "聊城": "山东", "德州": "山东", "滨州": "山东",
    # 华中 / 华南
    "郑州": "河南", "洛阳": "河南", "开封": "河南", "新乡": "河南", "许昌": "河南",
    "焦作": "河南", "安阳": "河南", "南阳": "河南", "商丘": "河南", "信阳": "河南",
    "武汉": "湖北", "黄石": "湖北", "襄阳": "湖北", "宜昌": "湖北", "荆州": "湖北",
    "十堰": "湖北", "孝感": "湖北", "荆门": "湖北", "鄂州": "湖北", "黄冈": "湖北",
    "咸宁": "湖北", "随州": "湖北",
    "长沙": "湖南", "株洲": "湖南", "湘潭": "湖南", "衡阳": "湖南", "岳阳": "湖南",
    "常德": "湖南", "郴州": "湖南", "永州": "湖南", "邵阳": "湖南", "益阳": "湖南",
    "广州": "广东", "深圳": "广东", "珠海": "广东", "东莞": "广东", "佛山": "广东",
    "惠州": "广东", "汕头": "广东", "湛江": "广东", "江门": "广东", "茂名": "广东",
    "肇庆": "广东", "梅州": "广东", "清远": "广东", "韶关": "广东", "揭阳": "广东",
    "潮州": "广东", "河源": "广东", "云浮": "广东", "汕尾": "广东", "阳江": "广东",
    "南宁": "广西", "桂林": "广西", "柳州": "广西", "北海": "广西", "玉林": "广西",
    "钦州": "广西", "贵港": "广西", "梧州": "广西", "百色": "广西",
    "海口": "海南", "三亚": "海南", "儋州": "海南",
    # 西南 / 西北
    "成都": "四川", "绵阳": "四川", "德阳": "四川", "宜宾": "四川", "南充": "四川",
    "泸州": "四川", "达州": "四川", "乐山": "四川", "自贡": "四川", "攀枝花": "四川",
    "贵阳": "贵州", "遵义": "贵州", "安顺": "贵州", "六盘水": "贵州",
    "昆明": "云南", "曲靖": "云南", "大理": "云南", "丽江": "云南", "玉溪": "云南",
    "拉萨": "西藏", "日喀则": "西藏",
    "西安": "陕西", "咸阳": "陕西", "宝鸡": "陕西", "渭南": "陕西", "榆林": "陕西",
    "汉中": "陕西", "延安": "陕西",
    "兰州": "甘肃", "酒泉": "甘肃", "天水": "甘肃", "张掖": "甘肃",
    "西宁": "青海", "银川": "宁夏", "乌鲁木齐": "新疆", "喀什": "新疆", "伊犁": "新疆",
}

# 把「省名 → 自身」和「城市名 → 省份」合成一张表，一次扫描搞定
_PROV_TOKENS: dict[str, str] = {p: p for p in PROVINCES}
_PROV_TOKENS.update(CITY_TO_PROVINCE)


def _prov_scores(text: str) -> dict[str, int]:
    """给一段文本里出现的每个候选省份打分。"""
    if not text:
        return {}
    s = _PROV_TRAP.sub("\u3000\u3000\u3000", str(text))
    scores: dict[str, int] = {}
    for token, prov in _PROV_TOKENS.items():
        for m in re.finditer(re.escape(token), s):
            before = s[max(0, m.start() - 24): m.start()]
            after = s[m.end(): m.end() + 40]
            # 中标人/律所上下文里的地名与项目所在地无关，直接作废
            if any(w in after or w in before[-8:] for w in _PROV_NOISE):
                continue
            score = 1
            if any(cue in before for cue in _PROV_CUE):
                score = 5
            # 紧接着「省/市/自治区」后缀，基本就是行政区名本身
            if after.startswith(("省", "市", "自治区", "壮族自治区")):
                score = max(score, 3)
            scores[prov] = scores.get(prov, 0) + score
    return scores


def guess_province(*chunks: str | None) -> str:
    """从多段文本里猜省份，chunks **按优先级传**（如 采购人 > 标题 > 正文）。

    早先的实现是「按省份列表顺序找第一个子串」，北京排在列表第一位，
    于是所有带「（北京时间）」的公告都被归到北京；中标公告里中标律所的名字
    也会顶替项目所在地。现在改成：先看高优先级片段，片段内按加权票数决出。
    猜不到返回空串（宁缺勿错）。
    """
    for c in chunks:
        scores = _prov_scores(c)
        if scores:
            # 同票时按含2/3字省份列表的顺序取，保证结果稳定可复现
            return max(scores.items(), key=lambda kv: (kv[1], -PROVINCES.index(kv[0])))[0]
    return ""


def is_valid_province(val: str | None) -> bool:
    """校验一个值是不是真省份。

    站点列表页常把「服务」「其他」之类栏目名塞在同一个字段里，
    不做校验就会把「服务」当成省份入库。只有白名单内的才认。
    """
    if not val:
        return False
    v = val.strip()
    return any(v == p or v.startswith(p) for p in PROVINCES)


# 省级后缀，长的放前面（「广西壮族自治区」要先于「自治区」匹配上）
_PROV_SUFFIXES = (
    "壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "特别行政区", "省", "市",
)


def normalize_province(val: str | None) -> str:
    """把「辽宁省」「广西壮族自治区」之类归一到白名单里的简称。

    不同来源写法不一致（ccgp 给「辽宁」，ggzy 给「辽宁省」），
    不归一会导致同一个省在统计里被拆成两条。
    """
    if not val:
        return ""
    s = val.strip()
    for suf in _PROV_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    for p in PROVINCES:
        if s == p:
            return p
    for p in PROVINCES:
        if s.startswith(p):
            return p
    return ""


# 列表页字段尾部常粘连的公告类型词（ccgp 的 span 用空格分隔，中文间不加空格）
_NOTICE_TAIL = (
    "中标（成交）结果公告", "中标(成交)结果公告", "中标结果公告", "成交结果公告",
    "竞争性磋商公告", "竞争性谈判公告", "公开招标公告", "邀请招标公告",
    "询价公告", "单一来源公告", "资格预审公告", "征集公告", "采购公告",
    "更正公告", "变更公告", "废标公告", "流标公告", "终止公告",
    "结果公告", "中标公告", "成交公告", "其他公告", "招标公告",
)


def clean_party(name: str | None) -> str:
    """清洗采购人/代理机构名，剥掉尾部粘连的公告类型词。

    例：'安徽公共资源交易集团项目管理有限公司成交公告' -> '安徽公共资源交易集团项目管理有限公司'
    """
    if not name:
        return ""
    s = name.strip().strip("|").strip()
    changed = True
    while changed:
        changed = False
        for tail in _NOTICE_TAIL:
            if s.endswith(tail) and len(s) > len(tail):
                s = s[: -len(tail)].strip()
                changed = True
    return s


def guess_industry(*chunks: str | None) -> str:
    """按关键词词典猜行业。命中多个时取第一个（词典顺序即优先级）。"""
    blob = " ".join(c for c in chunks if c)
    for name, kws in INDUSTRIES.items():
        for kw in kws:
            if kw in blob:
                return name
    return ""


# ── 正文清洗 ────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def html_to_text(html: str | None, limit: int = 20_000) -> str:
    """HTML → 纯文本（噪声去除）。"""
    if not html:
        return ""
    s = _SCRIPT.sub(" ", html)
    s = _TAG.sub(" ", s)
    s = re.sub(r"&nbsp;?", " ", s)
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"&lt;", "<", s).replace("&gt;", ">")
    s = to_halfwidth(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()[:limit]
