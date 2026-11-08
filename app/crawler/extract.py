"""字段抽取：规则优先，LLM 兜底。

策略来自 CaseBoard 的实战结论——
「采购人 / 预算 / 截止时间」这类字段先用正则 + 关键词词典扫（零成本、稳定、可解释），
扫不到再上 LLM。特别是自由排版的央企/国企公告必须靠 LLM，
但绝不让 LLM 承担全部：成本和 Token 都扛不住量。

每条记录都会留下 extract_meta，记录字段是 rule 命中还是 llm 补齐，便于审计与回滚。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import httpx

from ..config import settings
from .normalize import (
    clean_party,
    guess_industry,
    guess_province,
    normalize_province,
    parse_date,
    parse_money,
    to_halfwidth,
    validate_deadline,
)

# 政府采购预算的下限。低于这个数基本是误抽（工作量天数、包序号、文件工本费），
# 顶到卡片上比留空更糟。真实的法律服务采购预算没有几百块的。
MIN_BUDGET = 1000.0

# ── 规则层 ──────────────────────────────────────────────────────────────
# 采购人 / 代理机构：两种常见写法
#  A. 内联式：「采购人：成都市青羊区司法局」
#  B. 分块式：「1.采购人信息  名  称： 成都市青羊区司法局」——政务站最常见的表格扁平化结果
#
# 坑：B 的「名    称」中间会被塞一堆空格；A 的 label 不能直接写「采购方」，
# 否则「采购方式：竞争性磋商」会被当成采购人抓出「式：竞争性磋商」。
# 所以 label 后面统一做否定前瞻，并对抽到的值做「像个单位名」校验。
# 分隔符除了冒号还认「为」：运营商/国企的公告普遍写成
# 「采购人为中国电信股份有限公司商洛分公司，采购代理机构为华信咨询设计研究院有限公司」
# （电信阳光采购网整站都是这个写法，只认冒号的话采购人一条都抽不出来）。
# 标签部分必须是**非捕获组**：rule_purchaser 取的是 m.group(1)，
# 写成捕获组的话 group(1) 拿到的是「采购人」三个字而不是单位名，
# 校验必然失败、采购人永远抽不出来（这个 bug 潜伏了很久，
# 因为另一条 _PURCHASER_NAME 只有一组，看起来是能用的）。
_PURCHASER = re.compile(
    r"(?:采购人(?!信息|单位名称)|采购单位|招标人|招标单位|委托方|业主单位|项目业主|建设单位)"
    r"\s*(?:[:：]|为)\s*([^\s,，;；。\n（(]{2,60})"
)
_PURCHASER_NAME = re.compile(
    r"采购人[^。\n]{0,24}?名\s*称\s*[:：]\s*([^\s,，;；。\n]{2,60})"
)
# 同上，「采购代理机构为华信咨询设计研究院有限公司」也要能吃
_AGENCY = re.compile(
    r"(?:代理机构|招标代理|采购代理|代理公司)\s*(?:[:：]|为)\s*"
    r"([^\s,，;；。\n（(]{2,60})"
)
_AGENCY_NAME = re.compile(
    r"(?:代理|代理?机构)[^。\n]{0,24}?名\s*称\s*[:：]\s*([^\s,，;；。\n]{2,60})"
)

# 「像个单位名」：先做形态归一，再要求命中机构后缀或专有名词
_ORG_TAIL = (
    "人民政府", "人民法院", "人民检察院", "人民政府办公室", "管委会", "管理局", "事务所", "合作社",
    "局", "部", "委", "办", "院", "所", "公司", "中心", "大学", "学院", "医院", "集团", "厂",
    "站", "队", "银行", "协会", "学会", "社区", "街道", "处", "厅", "中学", "小学", "党校", "工会",
    # 政务主体常见的收尾词——漏了它们会把大量合法采购人误判成噪声
    "机关", "办公室", "委员会", "办公厅", "党支部", "研究院", "研究院所", "图书馆", "博物馆",
)
# 归一化时裁掉的后缀 / 括号尾巴：「司法局（机关）」「司法局本级」都应还原成「司法局」
_ORG_TRIM = re.compile(r"[（(][^）)]*[）)]\s*$")
_ORG_TRIM2 = re.compile(r"(本级|机关本级)\s*$")
# 句首虚词：「采购人：为鼎和财产保险股份有限公司」里的「为」会被一起捕获
_ORG_HEAD = re.compile(r"^(为|由|系|即|名称|名|采购人|招标人|采购单位|委托)\s*")
# 命中即判废：这些是正文里被误当成单位名的句子片段
_ORG_NOISE = (
    "管理办法", "暂行办法", "工作要求", "名单", "为准", "信息名称", "竞争性", "磋商", "邀请",
    "联系电话", "联系方式", "联系地址", "地址", "电话", "邮编", "邮箱", "开户", "账号",
    "统一社会信用", "项目联系人", "详见", "公告", "答疑", "踏勘", "单位名称",
)


def _plausible_org(val: str) -> bool:
    """抽出来的采购人是否像个单位名。宁可漏抽，也不要把噪声顶到卡片上。"""
    s = (val or "").strip()
    if not s:
        return False
    # 形态归一：去掉尾随括号、（本级），这些写法在预算公开表里很常见
    for _ in range(3):
        s2 = _ORG_HEAD.sub("", s).strip()
        s2 = _ORG_TRIM.sub("", s2).strip()
        s2 = _ORG_TRIM2.sub("", s2).strip()
        if s2 == s:
            break
        s = s2
    if not (3 <= len(s) <= 60):
        return False
    if any(w in s for w in _ORG_NOISE):
        return False
    return s.endswith(_ORG_TAIL)
# 中文日期的通用写法：数字与「年/月/日」之间可能夹空格。
# 政府公告多半是 Word 转 HTML，排版空格会原样带进正文，
# 写成「2026年 11 月 16 日 10 时 00 分」——不留 \s* 就整个匹配不上
# （cfcpn 全站如此，正文里有明确截止时间却一条都抽不出来）。
_CN_DATE = r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
# 时刻：既认「09:30」，也认「9 时 30 分」
_CN_TIME = r"(?:\s*\d{1,2}\s*[:：时]\s*\d{1,2}(?:\s*[:：分]\s*\d{1,2})?\s*分?)?"

# 截止时间：优先找「响应/投标/报名截止时间」而非泛泛的日期
_DEADLINE_PAT = re.compile(
    r"(?:响应文件递交的?截止时间|响应截止时间|投标文件递交的?截止时间|投标截止时间|"
    r"报名截止时间|报名截止|响应截止|递交截止时间|报价截止时间|开标时间|截止时间)"
    r"[^0-9]{0,20}?(" + _CN_DATE + _CN_TIME + r")"
)
# 兜底：任意日期 + 附近出现「截止」字样
_DEADLINE_LOOSE = re.compile(r"(" + _CN_DATE + r")")

# 发布时间的显式标记。只有带这类标记才敢从正文里取日期
_PUBLISH_MARK = re.compile(
    r"(发布时间|公告发布时间|发布日期|公告发布日期|信息时间|公告日期|刊登日期|公示时间)"
    r"\s*[:：]?\s*(" + _CN_DATE + r")"
)

# 公告类型判定
_NOTICE_TYPES = [
    ("中标公告", ["中标公告", "中标结果", "中标候选人", "成交公告", "成交结果",
                  "中标公示", "入围结果", "中选结果", "成交公示"]),
    ("更正公告", ["更正公告", "变更公告", "澄清公告", "补充公告", "修改公告", "更正事项"]),
    ("废标公告", ["废标", "流标", "终止公告", "中止公告"]),
    ("资格预审", ["资格预审", "预审公告"]),
    ("竞争性磋商", ["竞争性磋商", "磋商公告"]),
    ("竞争性谈判", ["竞争性谈判", "谈判公告"]),
    ("询价公告", ["询价公告", "询价采购"]),
    ("单一来源", ["单一来源", "单一来源采购"]),
    ("公开招标", ["公开招标", "招标公告", "采购公告", "采购/资审公告", "征集公告"]),
    ("邀请招标", ["邀请招标", "邀请公告"]),
]
_RESULT_WORDS = ("中标", "成交", "结果公示", "结果公告", "废标", "流标", "入围结果")


def rule_notice_type(title: str, text: str = "") -> str:
    """判定公告类型。

    政务站标题常写成「中标（成交）结果公告」「竞争性磋商(二次)公告」，
    括号里的补充说明会把关键词切断（"中标结果" 被拆成 "中标（成交）结果"），
    所以匹配前先剥掉括号内容再试一遍。标题优先，正文仅兜底。
    """
    def _match(blob: str) -> str:
        if not blob:
            return ""
        tight = re.sub(r"[（(][^）)]{0,10}[）)]", "", blob)
        for name, kws in _NOTICE_TYPES:
            for kw in kws:
                if kw in tight or kw in blob:
                    return name
        return ""

    # 「XX项目采购合同」这类标题不含任何公告类型词，只能看结尾
    if re.search(r"(合同|合同公告|合同公示)$", (title or "").strip()):
        return "采购合同"

    return _match(title) or _match(text[:400]) or "其他公告"


def rule_is_result(notice_type: str, title: str) -> bool:
    """结果公告单独分流——不进主信息流（CaseBoard 明确做过这个处理）。"""
    if notice_type in {"中标公告", "废标公告"}:
        return True
    return any(w in title for w in _RESULT_WORDS)


def rule_purchaser(text: str) -> str:
    """采购人。先试「采购人信息 名称：」再试「采购人：」，命中后仍要过 plausibility 校验。"""
    if not text:
        return ""
    for pat in (_PURCHASER_NAME, _PURCHASER):
        for m in pat.finditer(text):
            val = re.sub(r"(联系|电话|地址|邮编|邮箱).*$", "", m.group(1)).strip("、,，;； ")
            if _plausible_org(val):
                return val
    return ""


def rule_agency(text: str) -> str:
    if not text:
        return ""
    for pat in (_AGENCY_NAME, _AGENCY):
        for m in pat.finditer(text):
            val = re.sub(r"(联系|电话|地址|邮编|邮箱).*$", "", m.group(1)).strip("、,，;； ")
            if _plausible_org(val):
                return val
    return ""


def rule_deadline(text: str) -> tuple[str, str]:
    """返回 (原文, 说明)。原文交给 validate_deadline 判定可信度。"""
    if not text:
        return "", ""
    m = _DEADLINE_PAT.search(text)
    if m:
        return m.group(1).strip(), "精确匹配"
    # 兜底：找带「截止」二字的句子里的日期
    for sent in re.split(r"[。；;\n]", text):
        if "截止" in sent:
            m2 = _DEADLINE_LOOSE.search(sent)
            if m2:
                return m2.group(1).strip(), "句子级兜底"
    return "", ""


def rule_budget(text: str) -> tuple[float | None, bool]:
    """优先在明确带「预算/控制价」的句子里找金额。

    坑：正文里「项目编号：2026年第9486号」这类内容与预算同处一句时，
    直接在整句里找第一个数字会把年份当成预算。所以从「预算」二字之后
    才开始找，并且优先取带明确单位的那个。
    """
    if not text:
        return None, False

    fallback: float | None = None
    for sent in re.split(r"[。；;\n]", text):
        m = re.search(r"(预算|控制价|最高限价|限价)", sent)
        if not m:
            continue
        tail = sent[m.start(): m.start() + 80]  # 只看「预算」之后的一小段
        val, has_unit = parse_money(tail)
        if val is None or val < MIN_BUDGET:
            continue
        if has_unit:
            return val, True
        if fallback is None:
            fallback = val

    if fallback is not None:
        return fallback, False

    # 全文兜底：只认带「元/万元」明确单位的
    val, has_unit = parse_money(text)
    if val is None or not has_unit or val < MIN_BUDGET:
        return None, False
    return val, has_unit


# ── LLM 兜底 ────────────────────────────────────────────────────────────
_LLM_PROMPT = """你是招投标信息抽取助手。从下面的公告内容中抽取字段，只输出 JSON，不要解释。

字段说明：
- purchaser: 采购人/招标人单位全称，没有则空字符串
- agency: 代理机构名称，没有则空字符串
- budget: 预算金额，纯数字（单位统一为元），没有则 null
- deadline: 投标/响应截止时间，格式 YYYY-MM-DD，没有则 null
- province: 省份，没有则空字符串
- industry: 行业（法律服务/信息技术/建设工程/医疗设备/教育培训/物业服务/交通物流/金融保险/能源环保/咨询服务），没有则空字符串

公告标题：{title}

公告正文（可能已截断）：
{content}

只输出 JSON，例如：{{"purchaser":"","agency":"","budget":null,"deadline":null,"province":"","industry":""}}
"""


def llm_extract(title: str, content: str) -> dict | None:
    """LLM 兜底抽取。未配置 API key 或关闭开关时返回 None（不影响主流程）。"""
    if not settings.crawl.llm_fallback or not settings.llm.api_key:
        return None

    prompt = _LLM_PROMPT.format(title=title[:200], content=content[:3000])
    try:
        with httpx.Client(timeout=settings.llm.timeout) as client:
            resp = client.post(
                f"{settings.llm.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm.api_key}"},
                json={
                    "model": settings.llm.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        raw = data["choices"][0]["message"]["content"]
        # 模型偶尔会把 JSON 包在代码块里
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


# ── 编排 ────────────────────────────────────────────────────────────────
def extract_tender(
    title_raw: str,
    content: str = "",
    *,
    row: dict[str, str] | None = None,
    use_llm: bool | None = None,
) -> dict:
    """完整抽取一条公告。返回可直接入库的字段字典。

    meta 记录每个字段的来源是 rule 还是 llm，便于后续审计。
    """
    row = row or {}
    use_llm = settings.crawl.llm_fallback if use_llm is None else use_llm

    title = to_halfwidth(title_raw or row.get("title", "")).strip()
    body = content or row.get("_text", "") or ""

    meta: dict[str, str] = {}

    # 1) 公告类型
    notice_type = rule_notice_type(title, body)
    meta["notice_type"] = "rule"

    # 2) 采购人 / 代理
    #  列表页给的 purchaser 先验一遍再收：站点这一栏有时是栏目名或整段联系人信息，
    #  不如直接自己从正文抽（例如 ccgp 某条曾存成「或招标代理机构查询为准）」）
    row_pur = clean_party((row.get("purchaser") or "").strip())
    purchaser = row_pur if _plausible_org(row_pur) else ""
    if not purchaser:
        purchaser = clean_party(rule_purchaser(body))
    if not purchaser:
        purchaser = clean_party(rule_purchaser(title))
    meta["purchaser"] = "rule" if purchaser else ""

    row_ag = clean_party((row.get("agency") or "").strip())
    agency = row_ag if _plausible_org(row_ag) else clean_party(rule_agency(body))
    meta["agency"] = "rule" if agency else ""

    # 3) 发布时间
    # 注意：绝对不要用「正文前 N 个字里随便一个日期」当发布时间。
    # 公告导语几乎必写「并于 XXXX 年 X 月 X 日前提交响应文件」，
    # 那个日期正是截止时间，拿它当 publish 会让 validate_deadline 判定
    # deadline <= publish 从而把正确的截止时间丢掉（历史遗留 bug）。
    publish_raw = (row.get("publish_time") or row.get("date") or "").strip()
    publish = parse_date(publish_raw)
    if publish is None:
        m = _PUBLISH_MARK.search(body)
        if m:
            publish = parse_date(m.group(2))
    meta["publish_time"] = "rule" if publish else ""

    # 4) 截止时间（含合理性校验）
    dl_raw, _how = rule_deadline(body)
    if not dl_raw:
        dl_raw = (row.get("deadline") or "").strip()
    deadline = parse_date(dl_raw)
    deadline, confident = validate_deadline(deadline, publish)
    meta["deadline"] = "rule" if deadline else ""

    # 5) 预算
    # 优先用站点规则在详情页精确定位到的「预算金额…万元」，
    # 它比全文扫一遍可靠；只有在没配 detail 规则时才退回正文启发式。
    b_raw = (row.get("budget") or "").strip()
    budget: float | None = None
    has_unit = False
    if b_raw:
        budget, has_unit = parse_money(b_raw)
        if not has_unit or budget is None or budget < MIN_BUDGET:
            budget = None
    # 站点给的值不可用（缺单位 / 数量级明显不对）时，仍要退回正文启发式，
    # 不能就此认定「该标段没预算」
    if budget is None:
        budget, has_unit = rule_budget(body)
        if budget is not None and budget < MIN_BUDGET:
            budget = None
    meta["budget"] = "rule" if budget is not None else ""

    # 6) 区域与行业
    raw_prov = (row.get("province") or "").strip()
    # 列表页这一栏常混进栏目名（如「服务」「其他」），不是省份就丢掉重猜；
    # 「辽宁省」「广西壮族自治区」这类带后缀的归一到简称
    province = normalize_province(raw_prov) or guess_province(
        # 采购人 > 标题 > 正文开头：中标公告里正文会大量出现中标律所所在地，
        # 那不是项目所在地，所以正文优先级最低
        raw_prov,
        purchaser,
        title,
        body[:300],
    )
    industry = (row.get("industry") or "").strip() or guess_industry(title, body[:500])
    meta["province"] = "rule" if province else ""
    meta["industry"] = "rule" if industry else ""

    # 7) LLM 兜底：仅对规则没命中的字段补齐
    if use_llm:
        missing = (
            not purchaser
            or budget is None
            or deadline is None
            or not province
            or not industry
        )
        if missing:
            got = llm_extract(title, body) or {}
            if not purchaser and got.get("purchaser"):
                purchaser = str(got["purchaser"]).strip()
                meta["purchaser"] = "llm"
            if not agency and got.get("agency"):
                agency = str(got["agency"]).strip()
                meta["agency"] = "llm"
            if budget is None and isinstance(got.get("budget"), (int, float)):
                budget = float(got["budget"])
                meta["budget"] = "llm"
            if deadline is None and got.get("deadline"):
                d = parse_date(str(got["deadline"]))
                d2, conf2 = validate_deadline(d, publish)
                if d2:
                    deadline, confident = d2, conf2
                    meta["deadline"] = "llm"
            if not province and got.get("province"):
                province = str(got["province"]).strip()
                meta["province"] = "llm"
            if not industry and got.get("industry"):
                industry = str(got["industry"]).strip()
                meta["industry"] = "llm"

    return {
        "title": title,
        "notice_type": notice_type,
        "is_result": rule_is_result(notice_type, title),
        "purchaser": purchaser,
        "agency": agency,
        "budget_amount": budget,
        "currency": "CNY" if budget is not None else "",
        "province": province,
        "industry": industry,
        "publish_time": publish,
        "deadline": deadline,
        "deadline_raw": dl_raw,
        "deadline_confident": confident,
        "content_text": body,
        "extract_meta": meta,
        # 供调试：标注抽取时间
        "_extracted_at": datetime.now().isoformat(timespec="seconds"),
    }
