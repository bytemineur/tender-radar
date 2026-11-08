# 招标雷达 · Tender Radar

多源招标/采购信息采集、标准化、去重与检索平台。Web 界面，单进程部署。
面向法律服务场景：帮你第一时间发现「哪个单位在采购法律服务、预算多少、什么时候截止」。

---

## 先说清楚一件事

`yipkingcn/BidInsight` 这个仓库**只有 `LICENSE` 和 `README.md`，没有任何代码**——它是一份架构规格说明书，不是一个能跑的框架。

所以本项目不是「fork 了 BidInsight」，而是：

> **严格按 BidInsight README 里的架构规格与数据模型实现，并把 CaseBoard 在真实生产里踩过的坑逐条落成代码。**

| BidInsight 规格 | 本项目落地 |
|:---|:---|
| 目录结构 `app/api`、`crawlers`、`configs`、`db`、`scripts` | ✅ 同构（略作扁平化） |
| 数据模型 tenders / purchasers / crawl_logs / tasks | ✅ `tenders` / `sources` / `crawl_runs` |
| `hash_key = hash(normalize(title) + publish_date + purchaser)` | ✅ `dedupe.make_hash_key()` 完全一致 |
| 规则优先，`configs/sites/*.yaml` 定义选择器 | ✅ `crawler/adapters.py` |
| 多选择器冗余回退 | ✅ `FieldRule.selectors` 列表按序尝试 |
| 限流 + 抖动 + UA 池 + 指数退避 | ✅ `crawler/fetcher.py`（1s/4s/16s） |
| 增量更新、断点续跑 | ✅ `source.last_success_at` + 单页失败不中断 |
| 标准化：时间归一、金额抽取、CNY 映射 | ✅ `crawler/normalize.py` |
| 基于哈希的软去重 + 跨批次关联 | ✅ 三层去重（见下） |
| React + FastAPI + PostgreSQL + Redis | ⚠️ 改为 **FastAPI + 原生前端 + SQLite**，理由见「技术选型」 |
| JWT 鉴权 | ⏳ 未实现（内网部署可先靠反向代理 Basic Auth） |

### 从 CaseBoard 借来的四条实战经验

| CaseBoard 的坑 | 本项目对应实现 |
|:---|:---|
| 「2099 年」占位截止日污染排序 | `normalize.validate_deadline()`：必须晚于发布日、且不超过发布日 +365 天，否则丢弃日期但保留 `deadline_raw`，标 `deadline_confident=false` |
| 跨来源重复折叠 | `dedupe.py` 的 SimHash + Jaccard 二次确认，同簇只暴露最早一条（`is_primary`） |
| 结果公告不进主信息流 | `tenders.is_result` 独立字段，列表默认过滤 |
| 验证码不批量对抗 | 批量只取列表（`fetch_detail=false`），详情按需单条拉取；适配器里明确注释不要写进定时任务 |

---

## 真实抓取：现在能抓到什么、抓不到什么

> 这一节是实测结论（2026-10-22），不是推测。跑一遍 `python scripts/check_sources.py` 可以在你自己的网络环境复现。

### 三十四个来源的实测状态（33 个在采 + 1 个暂不接，演示源已下线）

> ➕ **2026-10-31 新增七个权威源**：
> `jsggzy`（江苏公共资源交易）、`cqggzy`（重庆公共资源交易）、
> `ctg` / `ctgcg`（三峡集团电子采购平台·招标 / 采购）、
> `ahzfcg` / `hnzfcg` / `sxzfcg`（安徽 / 湖南 / 山西 政采云）。
> 选源标准与前二十六条一致：**只收政府与国央企官方平台，第三方聚合站一律不收**。

> ⚠️ **法律招标网（flzb）已移除**：按需求删除 `configs/sites/flzb.yaml`（本表原第 2 行）。
> 该源是 `json_extract` 能力的首个用例，下面「数据藏在 JS 变量里怎么办」一节仍以它为例，
> 保留作技术备忘；若要重新接入，照那一节的写法新建同名配置即可。

| 来源 | 类别 | 模式 | 状态 | 说明 |
|:---|:---|:---|:---|:---|
| **中国电信阳光采购网** ctsc | **央企（运营商）·全国** | `http` | ✅ 可用 | 全国 31 省 + 总部 + 子公司（天翼云、数字生活…）的采购都在一个门户。**整站是 SPA，但接口可以裸 HTTP 直调**，不需要浏览器（首页那堆防护脚本对这个接口不生效）。三个坑：关键词字段叫 **`title`**（传错不报错、只是不过滤）、**`type` 一次只能传一个公告类型**（所以干脆不传，用 `drop_if` 挡结果类）、正文在详情的 **`context`** 而单一来源叫 **`announceContext`**。详情端点**按类型分**（`tenderannouncement` / `purchaseannounce` / `prequalfication`），用 `detail_url_map` 分派。**列表直接给截止时间**，所以不取详情的条目也有截止 |
| **江苏公共资源交易** jsggzy | 公共资源·**省级** | `http` | ✅ 可用 | 与陕西同一套 ES 中间件，但**路径不带 `_new`、响应没有 content 包裹**（所以不能配 `json_unwrap`）。分类号与陕西完全不同：`003004002`=采购公告、`003004003`=更正、`003004006`=成交，陕西那套 `001001004001` 在这里一条都搜不到。字段两个坑：**`webdate` 恒为空**，时间要取 `infodateformat`；**`title` 带 `<em>` 高亮标签**，干净原文在 `titlenew`。命中质量是这批新源里最高的——村（社区）法律顾问、司法局 12348 公共法律服务、法律顾问助理服务单位采购，镇街一级的法律顾问采购是江苏特色品类 |
| **重庆公共资源交易** cqggzy | 公共资源·**直辖市** | `http` | ✅ 可用 | ES 中间件**带 content 包裹**（与陕西同、与江苏反），必须配 `json_unwrap`。两个特别之处：① 分类维度**没有 12 位 categorynum**，只有 `categorytype`（中介超市/国企采购）和 `categorytype2`（中选/成交/交易公告），不能用 condition 钉，只能靠 `drop_if` 挡「中选\|成交\|结果」；② **`companyname` 白送采购人全称**，是所有 ES 系源里独一份。cnum 只有 001 和 003 有数据（002/004/005/006 全为 0）。「中介超市」是重庆特有的政府中介服务选聘平台，机关和国企在这里买法律服务，是有效商机不是噪音 |
| **三峡集团电子采购平台·采购公告** ctgcg | **央企·能源（官方）** | `http` | ✅ 可用 | 全站 SSR 静态 CMS，**纯 GET、无登录、无 cookie、无反爬**，这批新源里最好接的央企平台。栏目串是随机字符：`2ywgg1`=采购公告、`1ywgg1`=招标公告、`1ywgg3/4`=中标类（不采）。翻页是 `index.htm?pageNo={n}`（**`index_1.htm` 不存在**，返回的是首页内容，看起来像成功其实没翻页；页面上的「1 2 3」按钮是 `javascript:;`，别去找 next_link）。列表项 `ul#list1 > li`：标题取 **`a` 的 `title` 属性**（text 里混着图标标签和空的采购方式占位），日期用正则从整行抠。**没有站内检索**（`search.jspx` 一律 404），走 `local_filter`。实测 20 页 200 条里 4 条法律类（常年法律顾问 / 法律案件风险代理 / 境外仲裁案专项法律服务），命中率约 2% |
| **三峡集团电子采购平台·招标公告** ctg | **央企·能源（官方）** | `http` | ✅ 可用（命中率极低） | 与 ctgcg 同源同构，只有栏目位不同。实测翻到第 20 页（200 条）**一条法律类都没有**——三峡的法律服务采购几乎全走采购频道，招标频道是工程/设备为主。所以只翻 10 页当保险，不深翻。保留它是因为「央企官方源 + 零成本」，命中率低不等于没有 |
| ~~**法律招标网** flzb~~ | ~~垂直·法律~~ | `http` | ❌ **已移除** | 已按需求删除 `configs/sites/flzb.yaml`。（原为「法律」方向上纯度最高的源——整站只做法律类招标，实测一页 30 条里招标公告 21 / 招标预告 7 / 招标变更 2，**零噪音**，所以不需要 `drop_if`。页面是 SSR 但列表数据**不在 DOM 里**，内嵌在 `window._STATIC_DATA_` 的 `listData:'[...]'`，用 `json_extract` 正则抠出来解析（本站第一个用这个能力的源）。URL `/flgg-2703/010/{page}/`，`flgg`=招标公告（**`flzb` 是中标公告，别选错**）。列表只给标题/地区/日期，**预算和截止时间在详情页**，`detail_container` 要选 `div.detail-cont`（采购人 `div.wyCompanyBlock` 在正文容器外，选 `div.detail-text` 会一条都取不到）。正文有付费墙打码（`****`），但预算和截止通常是明文 |
| **中国金融集中采购网** cfcpn | **金融（银行/保险/证券）** | `http` | ✅ 可用（有配额） | 法律类密度最高的源之一，全是银行、保险的常年法律顾问与诉讼服务项目。**只有 HTTP，没有 HTTPS**；接口校验 `Referer`，缺了直接 403。**限流是配额制**：一轮 21 次请求会连吃 40 多个 403、7 分钟不恢复，加大 `delay` 无效，只能减少请求数——所以关键词只留 2 个、`detail_limit: 3`（共 5 次请求/轮）、采集频次降到每天 3 次。接口**不返回截止时间**，但正文里写着，由抽取层提出，因此只有取到详情的最新几条有截止时间 |
| **中央国家机关政府采购中心** zycg | 财政·**中央部委** | `browser` | ✅ 可用 | 内容全在 `/freecms/rest/v1/notice/selectInfoMore.do` 接口里，列表页是空壳且**自身 JS 有 bug**（jQuery 版本不对，`#noticeShow` 渲染不出来），所以只能直接吃接口。接口要求 `implementWay` 与 `noticeType` 同时给值，缺任一个就 `code=-1`；`title` 按标题子串匹配。详见下面「接口类来源的三个坑」 |
| **中国政府采购网** ccgp | 财政·中央 | `browser` | ✅ 可用 | 搜索页有 WAF，**纯 HTTP 一律返回「您的访问过于频繁」**；用真浏览器打开则完全正常，稳定取回 20 条/页 |
| **全国公共资源交易平台** ggzy | 公共资源·全国 | `http` | ✅ 可用 | 页面是 Vue 渲染，但背后有干净的 JSON 接口，POST 表单直连即可，**比网页更稳**；响应自带正文，不必再取详情 |
| **深圳公共资源交易** szggzy | 公共资源·**城市（本地）** | `http` | ✅ 可用 | 首页只是 398 字节跳转壳，列表走 `POST /cms/api/v1/trade/content/page`；必须同时给 `fields=采购公告` 才不会把中标公告混进来。**详情页是静态 HTML**，正文直接写着「项目预算：¥1,256,100.00」和递交截止，是所有源里字段最完整的一个 |
| **上海政府采购网** shzfcg | 财政·**直辖市**（政采云） | `http` | ✅ 可用 | 与浙江同一套政采云门户，`POST /portal/category`，**只有 `categoryCode` 不同**（上海 `77-938917`，浙江 `110-606633`；上海首页另有 `shCategory12` 通知类、`77-489585` 非政府采购类）。噪音比例高：实测 75 条里只有 15 条还能投，全靠 `drop_if` 挡合同/结果/终止三类。**预算字段质量最好的源之一**，可投公告基本都带 `budgetPrice`（格式 `"540000元"`），法律顾问项目客单价 54 万～220 万 |
| **浙江政府采购网** zjzfcg | 财政·省级（政采云） | `http` | ✅ 可用 | `POST /portal/category`，关键词字段叫 **`keyword`**（title / searchTitle 传了不生效）；时间是 13 位毫秒戳。一个接口混发采购/结果/合同三类公告，靠 `drop_if` 挡掉后两类 |
| **安徽政府采购网** ahzfcg | 财政·省级（政采云） | `http` | ✅ 可用 | 政采云新版**字符串栏目码** `ZcyAnnouncement2`（=采购公告，全库 32 万条、法律类 279 条）。**三省编号互不相通**：安徽采购公告是 `2`、山西是 `1`，而山西的 `2` 是中标结果——别跨省照抄。栏目本身**零噪音**（只有公开招标/竞争性磋商/询价/竞争性谈判四类，全是能投的）。**采购人字段是 `author`，`purchaseName` 实测 15/15 全空**，所以配 `purchaseName\|\|author`。列表不给预算，只能从正文补；详情是 **GET** `/portal/detail?articleId=`，返回 JSON 正文 1500～2700 字 |
| **湖南政府采购网** hnzfcg | 财政·省级（政采云） | `http` | ✅ 可用 | 政采云**老式数字码** `160-945988`（=采购公告，法律类 408 条）。首页抠出 42 个候选码逐个实测，其余多是合同/中标/废标类噪音栏目。**列表既不给 `bidOpeningTime`（0/15）也不给预算（1/15），正文是唯一来源**，所以 `fetch_detail` 必开、正文 1.0～1.6 万字。采购人恒在 `purchaseName`（15/15），`author` 恒空——与安徽正好相反 |
| **山西政府采购网** sxzfcg | 财政·省级（政采云） | `http` | ✅ 可用 | 栏目码 `ZcyAnnouncement1`（=采购公告，法律类 347 条）。**三省里字段最全的一家**：`purchaseName`/`budgetPrice`/`bidOpeningTime`/`districtName`/`gpCatalogName`/`projectName` 全是 15/15 填充，不开详情也有完整商机信息（法律顾问类预算 45 万级）。注意 **`author` 在这站是代理机构**，采购人要看 `purchaseName`。正文 1.7～2.5 万字 |
| **陕西公共资源交易** sxggzy | 公共资源·省级 | `http` | ✅ 可用 | 藏在「全文检索」后的 ES 接口。**关键词 `wd` 不能 URL 编码**，`cnum` 只有 3 位，还得用 `condition` 钉住 `categorynum=001001004001` 才只拿到采购公告 |
| **四川政府采购** sczfcg | 财政·省级 | `http` | ✅ 可用 | 与陕西同一套 ES 中间件，但**路径少一个 `_new`** 且响应**没有外层包装**。标题取 `titlenew`（干净原文），栏目 `002002001` |
| **四川国企采购** scgqcg | 国企·省级 | `http` | ✅ 可用（量少） | 同接口不同栏目（002008001）。全库仅 12 条法律类公告，所以**刻意不做时间过滤**，每轮全量扫，靠去重兜底 |
| **湖北公共资源交易** hbggzy | 公共资源·省级 | `http` | ✅ 可用 | `POST /jyxxAjax/zfcg/cggg`，关键词参数叫 **`bulletinTitle`**（不是 title / keyword，那些传了不生效），按标题子串精确过滤；响应带 `bulletinContent` 全文，一次请求拿全。详情 URL 由前端 JS 规则拼：`/jyxx/zfcg/cgggDetail?guid={guid}` |
| **广东公共资源交易** gdggzy | 公共资源·省级 | `http` | ✅ 可用（有上限） | JSON 接口干净，但**不支持关键词检索**，且单次查询只能翻到前 3 页（约 150 条）。靠 `local_filter` 本地筛 + `window_days` 按天切片兜住 |
| **南方电网采购平台** csg | **央企** | `http` | ✅ 可用 | `GET /dbsearch.jspx?q={keyword}&types=服务&pageNo={page}`，纯 GET 无 cookie 要求；覆盖南方电网及各省电网公司、鼎和财险等二级单位 |
| **国家电网电子商务平台** sgcc | **央企·电网** | `http` | ✅ 可用 | 门户首页挂了**瑞数动态防护**（`$_ts` + `meta content="k_mvd7.."`），HTML 必须浏览器才看得见；但 `/ecpwcmcore/**` 这路 JSON 接口不在防护范围内，**裸 httpx POST 直接打得通，不用浏览器也不用签名**。列表 `POST /ecpwcmcore/index/noteList`（`key` 关键词、`index` 1 基页码、`firstPageMenuId: 20180502001` = 全部公告），**结果按发布时间倒序**，增量靠「重拉前两页 + 去重」。列表自带 `openbidTime`（毫秒戳开标时间，已出结果的公告是 null）。详情要走 `POST getNoticeBid`，**请求体是一个裸 JSON 字符串**（公告 id），包成对象一律回 `resultHint: SYS001` → 用 `detail_post_raw`。该接口只服务 `doci-bid` / `doci-change`，中标结果类 `doci-win` 必失败 → 用 `detail_skip_if` 跳过，省掉每轮几十次白打的请求。**国网公告不披露预算**，所以 `budget` 恒为空，这不是抽取失败 |
| **军队采购网** plap | **军队** | `http` | ✅ 可用 | **法律类线索密度最高的一个源**。域名是 `plap.mil.cn`（`.cn` 已废弃），FreeCMS 的 `selectInfoMoreChannel.do` 接口，`title` 按标题子串匹配。返回自带 `content` 全文、`budget`、`openTenderTime`、`regionName`，且**默认按发布时间倒序**——每轮拉第一页就是最新。**注意详情页需要登录**，务必保持 `fetch_detail: false` |
| **中国移动采购与招标网** cmcc | **央企·通信** | `http` | ✅ 可用 | `POST .../publish/queryList`，关键词字段叫 **`name`**（title / keyword 传了不生效）。列表就带 `tenderSaleDeadline`，不取详情也有截止时间。站点 TLS 老旧、握手会重协商，需开 `tls_legacy` |
| **中国中铁采购电子商务平台** crecg | **央企·基建（聚合）** | `http` | ✅ 可用（命中率低） | **技术上是最好接的源**：Destoon 静态页，纯 GET，无登录无 cookie 无反爬。但⚠️**它不是中铁官网**——页面底部自述是「电力招标网按招标信息关键词归类汇总的结果」，属第三方聚合；数据反映中铁各工程局的招标动态，别当官方口径用。**没有站内搜索**（搜索框 form 指向总站，本站 `/search*` 全 404/403），所以走 `local_filter` 拉全量本地筛，与 gdggzy 同路子。分页 `/zhaobiao/page-{n}.shtml`（`page-1` 会 301 回栏目页，`follow_redirects` 已开，无需特判）。列表**只有标题+日期+链接**，无采购人/预算/截止。**详情页抓不到**：链接指向 `www.dlzb.com`，阿里云 WAF + 滑块验证码，浏览器也过不去，所以 `fetch_detail: false`。第 1 页混着「结果公示」块，用 `url_filter: "d-zb-\d+\.html"` 挡掉（注意 `drop_if` **只在 JSON 解析路径生效**，HTML 路径要用 `url_filter`）。发布量约 200 条/天但法律类仅 ~0.25%，实测 40 页才筛出 2 条（其中一条是「诉讼代理服务」竞争性谈判），稳态 `max_pages: 10` 配每 3 小时一轮即可覆盖 |
| **中国铁建物资采购网** crcc | **央企·基建（聚合）** | `http` | ✅ 可用（命中率低） | crecg 的**同源兄弟站**（都是电力招标网 dlzb.com 的 Destoon 聚合页，同样⚠️**不是中国铁建官网**），选择器与 `url_filter` 完全通用。三个差异点：① **只能用 http**——该域 TLS 握手直接失败，且不能加 `www`（403）；② 发布量约 67 条/天，只有中铁的三分之一；③ **第 1 页必须用 `first_page_url` 指向 `/zhaobiao/`**——本站的 `/zhaobiao/page-1.shtml` 会 301 跳到**站点首页**（crecg 那边同名 URL 跳的是栏目页，两边行为不同），首页是混合列表只有 20 条招标里的 16 条，少的 4 条既不在首页也不在第 2、3 页，不配就会永久漏采。详情页同样在 `www.dlzb.com` 被阿里云 WAF 拦，`fetch_detail: false`。法律类命中率 ~0.25%（500 条里 1 条「工程造价**纠纷**调解咨询服务」） |
| **国能e招（国家能源集团）** chnenergy | **央企·能源** | `http` | ✅ 可用（命中率低） | **官方平台**（不是聚合站，与 crecg/crcc 不同），且是全站静态 HTML、纯 GET、无反爬。后端是 Java CMS（bidcms/PrimeFaces）但列表页与详情页**都是服务端渲染好的静态页，没有 JSON 接口**，别去找 XHR。列表 `/bidweb/001/001002/{page}.html`（15 条/页倒序，共 9632 页），**page 1 直接用 `1.html`**，不需要 `first_page_url`。无站内搜索 → `local_filter`。✅ **详情页能抓且值得抓**（`div.con` 正文，含招标人/代理机构/截止时间），这是本源相对 crecg/crcc 的最大优势 —— 那两家这两项永远是空的。⚠️ 已知取舍：正文里「招标文件购买截止」出现在「投标截止」之前，`_DEADLINE_PAT` 取最左匹配，所以抽到的是**买标书截止日**（比真实投标截止早约 2 周），偏保守但不算错。发布量约 28 条/天，法律类命中 1/750（「龙源工程2026-2027年度**法律纠纷案件服务**公开招标」） |
| **大唐集团电子商务平台** datang | **央企·能源** | `browser` | ✅ 可用（命中率高） | **官方平台**（大唐电商技术有限公司运营），全站挂**阿里云 WAF**（`acw_tc` + `acw_sc__v2` JS 挑战）。⚠️ 两个关键坑：① 裸 httpx 无论 GET/POST 一律拿回 17136 字节的挑战页，**接口 `/notice/moreController/getList` 也一样被挡**（这点与 sgcc 不同，国网的 JSON 接口是不设防的）；② 光走 Chromium 网络栈（`raw_json`）**也不够**——`context.request.get` 不执行 JS，永远算不出 `acw_sc__v2`，重试几次都没用。必须先 `page.goto` 真打开一次页面让挑战脚本跑完 → 这就是新增的 **`warmup_url`** 字段的由来，实测预热后连打 5 次全通。✅ 两个优势：**接口支持服务端关键词过滤**（`message_title` 标题子串），不用 `local_filter` 拉全量；**字段非常全**（`message_title` / `message_no` / `bid_tenderer` 采购人 / `publish_time` / `deadline`），所以 `fetch_detail: false` 也不缺字段。❌ 详情页是 flash 插件渲染的 PDF，HTML 里没有正文，抓了也没用。⚠️ `startDate` & `endDate` **必须出现在 query string 里**（可空），缺任一个后端直接 500。法律类命中：法律 23 / 法律顾问 5 / 纠纷 2 / 仲裁 2 / 诉讼 1，是**法律类命中率最高的央企源之一**（对比国能 1/750） |
| **中国邮政电子采购与物资供应平台** chinapost | **央企·邮政** | `http` | ✅ 可用（命中率高） | **官方平台**，且是本项目里**最好接的央企源之一**：**零反爬**，裸 httpx GET 直连 200，无瑞数、无 WAF、无 cookie、不需要浏览器。栏目按 `categoryId` 分（88 采购公告 / 89 变更 / 90 候选人公示 / 91 成交结果），**只采 88**——只有它有「报名截止时间」。✅ **服务端关键词搜索可用**，但参数叫 **`goSearch`**（试过 `title` / `key` / `search` 都无效、返回与无关键词完全相同的页面，排查时容易误判成"不支持搜索"）。命中：法律 37 页 / 诉讼 16 / 律师 13 / 法律顾问 12 / 纠纷 9 / 仲裁 4（10 条/页）。⚠️ 搜索是**全文匹配**不是标题匹配，「纠纷」会命中正文里出现该词的「货运险」「劳务派遣」公告，**刻意不收「合规」**（20 页全是「旺季氛围营造」「月台移动踏板」这类噪声）。✅ 列表页字段意外地全：项目编号、**采购方式**、**报名截止时间**都在 `ul.newsinfo` 里按 `nth-child` 取，所以 `fetch_detail: false` 也不缺字段 —— 详情页正文是 **pdfjs iframe 加载的 PDF**，HTML 里一个字没有（与 datang 同结论）。发布日期藏在详情页 URL 目录名里（`/biddingBulletin/2026-10-23/141107.html`），从 href 正则抠比解析 `div.newsDate`（「2026」+「10/23」两截文本）干净。⚠️ 容器**必须写 `ul.newslist > li`**：写 `ul.newslist li` 会把 `newsinfo` 里的 `li` 也匹配进来（40 个而不是 10 个），每条被拆成 4 行。**采购人无独立字段**，用正则从标题开头抠（标题开头就是「中国邮政集团有限公司大连市分公司…」），覆盖率 82%。严格倒序，3 页跨约 2 个月。实测入库 121 条，截止时间 121/121、法律类 84/121 |
| **招商局集团电子招标采购交易网** cmhk | **央企·交通金融地产** | `http` | ✅ 可用（命中率高） | **官方平台**，Nuxt 3 SPA，首页 `_payload.json` 是空的，数据全在 **`POST /zsj-menhu-web/gonggao/pageQuery`**。⚠️ **接口有签名校验**（本站唯一的硬骨头）：`X-Timestamp` + `X-Authorization` + `domainInfo` + `loginflag` 四个头，签名不对一律 `sign.validation.failed`，且**与时间戳强绑定**——两分钟前抓的签名配新时间戳照样失败。算法藏在 `_nuxt/Bbq6RR9c.js`（base64 塞进 `fs` 变量后 eval，函数 `addSignForRequestHeader`），是 `md5(字符排序→反序→拼接(时间戳 + URL末段 + secret))`，secret 取自 `getTemplateConfigByDomain` 的 `data.templateConfig`。为此给采集器加了 **`sign_secret` / `sign_url_part`** 两个字段 + headers 里的 **`{now_ms}` / `{sign}`** 占位符（必须同一次生成，分两次会错位）。✅ **关键词搜索可用但参数叫 `keyWord`（大写 W）**——`ggName` / `keyword` / `title` / `searchKey` 传了不报错、total 也不变，极易误判成"不支持搜索"。命中：法律 773 / 法律顾问 313 / 律师 223 / 纠纷 168 / 诉讼 126 / 仲裁 21。⚠️ `gongGaoPageConfigDtoList` **必须传**，缺了回"系统错误"；`pageSize` **只能 12 或 15**（20/30/48 都回"非法请求参数"）。✅ 列表字段极全：`ggName` / `zbrName` **采购人** / `xmBH` / `jieZhiTime` **截止**（毫秒戳），所以 `fetch_detail: false` —— 详情页 `?ggGuid=` 是同一个 Nuxt 壳，正文要另一个同样带签名的接口。倒序，每页约跨 2 个月。实测入库 193 条：**采购人 193/193（100%，所有源里最高）**、法律类 180/193（93%）、截止 146/193。独有类型：物业费催缴、应收账款催收、仲裁案专家论证。刻意不收「合规」（126 条全是 IT 合规系统） |
| **华润守正电子招标平台·招标公告** szecp | **央企·综合** | `http` | ✅ 可用 | **官方平台** www.szecp.com.cn。⚠️ 注意别用 `szecp-uat.crc.com.cn`——那是 **UAT 测试环境**（500 条里 59% 标题带「测试」字样，法律类命中 **0**，最新数据停在 2026-06-15），且走老 TLS 要 `tls_legacy` 才连得上；`szecp.crc.com.cn`（JS 里硬编码的搜索域）当前 502。✅ 无反爬、无签名、裸 httpx 直连，接口 `GET /rcms-external-rest/content/getSZExtData`，关键词参数叫 **`title`**。⚠️ **`format=json` 参数不能省**：Spring MVC 开了 `favorParameter`，不带它时**同一组参数一会儿 JSON 一会儿 XML**（返回 XML 时解析出 0 条，伪装成"选择器失效"），改 Accept 头无效。⚠️ `channelIds` **不支持逗号传多个**（传 `26909,26915` 只返回 26909），所以招标/采购拆成 **szecp / szecpcg 两个配置**。✅ 列表字段全（title / publishDate / **deadline 14位** / number / purchaseType），`fetch_detail: false`。**采购人无独立字段**（`businessUnit` 只是集团级码），从标题开头抠，覆盖率 96%，为此新增了 **`json_field_regex`** 字段。入库 19 条：采购人 19/19、截止 19/19、**法律类 19/19（100%）** |
| **华润守正电子招标平台·采购公告** szecpcg | **央企·综合** | `http` | ✅ 可用（命中率高） | 同上站点的 **26915 频道**，接口同构。**法律类命中是招标频道的 5~10 倍**：法律 185 / 法律顾问 77 / 诉讼 77 / 律师 39 / 纠纷 35 / 仲裁 9（招标频道只有 16/4/2/1/4/1）。品类更贴法律服务：常年法律顾问、诉讼委托代理、追偿权纠纷执行代理、诉讼（仲裁）及非诉法律服务。总量 30 万条但关键词过滤后「法律」仅 185 条，pageSize=100 → 2 页兜住。入库 252 条：采购人 244/252（97%）、截止 245/252、**法律类 252/252（100%，所有源里最高）** |
| 中国招标投标公共服务平台 ztbpub | 转载源 | `browser` | ❌ 暂不接 | 启用了**瑞数动态防护**（返回 `function M(){var GH=[...]}` 混淆脚本），需逆向签名参数，本项目不在反爬上做对抗 |

#### 军队采购网的两个特别之处

1. **采购单位是匿名的。** 公告正文只写「我部」「某单位」，联系方式是「刘助理 17664059608」这种，
   从来不披露单位全称——这是军队采购的固有做法，不是抽取失败。所以 plap 来源的数据
   `purchaser` 字段会是空的，识别靠标题里的地域暗示（「上海某单位…」）和已填好的省份。
   想联系得点原文去看正文尾部的「采购单位联系方式」。

2. **详情页是登录墙。** `fetch()` 拿到 HTTP 200，但正文是「你好，欢迎来到军队采购网！【请登录】」。
   好在列表接口已经把全文给回来了，`fetch_detail: false` 完全够用——
   顺带也因为这个源踩出来一个真 bug，见下面「详情正文覆盖」那节。

#### 接口类来源的三个坑（zycg 踩出来的，同类站点通用）

zycg 的内容全在一个 REST 接口里，接它的过程把「接口类来源」会遇到的坑全踩了一遍，
结论记在这里，下次遇到类似站点可以直接照抄：

1. **裸 HTTP 打不通。** curl / httpx 把 `Referer`、`X-Requested-With`、`Content-Type`、
   `Accept` 试了个遍，一律 `{"code":"-1"}`。换成 Chromium 网络栈（`context.request.get`）
   立刻正常。这类站点认的是握手特征，不是请求头。
2. **`page.goto` 也不行。** 导航请求不带 `Referer`，接口照样拒绝。必须走
   `context.request.get(url, headers=...)` —— 既带得上自定义头，又是浏览器握手特征，
   还不用渲染页面（比开页面快一个数量级）。对应采集器里的 `raw_json` 分支。
3. **每个会话的第一个请求必然失败。** 与 URL、关键词、cookie 都无关，只跟
   「是不是本 context 的第一次请求」有关，第二次起就正常。所以 `raw_json` 路径
   内置了一次重试，不用为每个站点猜它要不要预热。

顺带两个配置字段是为此加的：

```yaml
headers:                       # 列表请求额外带的头（接口校验来源页时用）
  Referer: https://.../cggg/index.html
  X-Requested-With: XMLHttpRequest
raw_json: 由 response_type: json + mode: browser 自动推导，不用手写
```

#### 数据藏在 JS 变量里怎么办（json_extract，flzb 踩出来的）

有一类站点页面是服务端渲染、看着像正经 HTML，但**列表数据根本不在 DOM 里**，
而是塞在某个 JS 变量里。典型的是法律招标网：

```html
window._STATIC_DATA_ = function () {
  return {
    listTotal: '118075',
    listData: '[{"title":"…","url":"/news-xxx/","updateTime":"2026-10-24",…}]'
  };
};
```

这种页面 `json.loads` 整页必然失败，用 CSS 选择器也抠不到东西——因为列表条目
压根没渲染成 DOM 节点（页面上的列表是 JS 拿这份数据现渲染的）。

用一个带**一个捕获组**的正则先把那坨 JSON 抠出来即可，其余照旧：

```yaml
response_type: json
json_extract: "listData:\\s*'(\\[.*?\\])'"   # 命中第一组 → 当 JSON 解析
json_items: ""                               # 抠出来就是数组本身，不用再取路径
json_fields:
  title: title
  url: url
```

判断方法：把响应存下来搜 `listData`、`__INITIAL_STATE__`、`window.__` 这类关键字，
比去猜 API 快得多。注意 YAML 双引号里反斜杠要写两遍（`\\s` 才是 `\s`）。

#### 配额制限流：加大 delay 是没用的（cfcpn 踩出来的）

遇到 403 第一反应通常是「放慢一点」，但 cfcpn 证明了这条路走不通：

| 一轮请求数 | 结果 |
|:---|:---|
| 21 次 | 前 3 个 200，之后 40 多个全 403，**7 分钟没恢复** |
| 12 次 | 前 6 个 200，第 7 个起 403 |
| 5 次 | 全部 200，`status: success` |

请求间隔本来就已经是 3~6 秒，照样被封——说明它限的是**总配额**而不是速率。
所以正确的应对是**减少请求数**，三个旋钮按这个顺序拧：

1. `keywords` 精简——多一个关键词就多一次列表请求，代价最容易被低估
2. `detail_limit: N`——只给最新 N 条补详情，其余靠列表字段入库；
   这是为此新增的配置项，对任何限流站点都适用
3. `schedule` 降频——`0 */2 * * *` 改成 `0 8,13,18 * * *`

判据很简单：跑完看 `status`。出现 `blocked` 说明撞线了，往下调一档。

#### 详情接口比列表接口更难伺候（ctsc 踩出来的）

列表只要一个 URL 就够了，详情往往要三样都对上才拿得到东西。ctsc 把这三种情况全占了：

```yaml
# ① 详情接口和列表接口不是一个地址 → detail_url
detail_url: https://.../portal/base/tenderannouncement/view

# ② 详情端点还按公告类型分 → detail_url_map（key 取行里 src_type 字段）
#    电信：比选/招标 → tenderannouncement，单一来源 → purchaseannounce
detail_url_key: src_type
detail_url_map:
  CompareSelect:          https://.../portal/base/tenderannouncement/view
  PurchaseAnnounceBasic:  https://.../portal/base/purchaseannounce/view

# ③ 详情响应是单个对象，不是列表那种数组 → detail_json_items
#    列表是 data.pageInfo.list，详情是 data 本身。
#    不给的话会**静默地一条都解析不出来**（复用 json_items 找不到路径）。
detail_json_items: data
detail_json_fields:
  # ④ 同一个语义在不同类型里字段名不一样 → 用 | 列候选，取第一个非空
  content: "context|announceContext"
```

同理，**给用户点的详情页链接也可能按类型分**：电信 `DeclareDetails` 靠 URL 里的
`type` 参数选视图（招标 1、单一来源 2、资格预审 6），写死 1 的话单一来源点进去是
「未获取到公告信息」。这种情况用 `url_template_map`（同一套 key）。

⑤ **请求体是裸标量**（国家电网踩出来的）：`getNoticeBid` 只吃一个 JSON 字符串
（公告 id），`detail_post_data` 是字典、必然包成 `{"id": "..."}`，服务端一律回
`resultHint: SYS001`。这类站点用 `detail_post_raw`（需同时开 `post_json: true`）：

```yaml
detail_url: https://sgccetp.com.cn/ecpwcmcore/index/getNoticeBid
detail_post_raw: "{src_id}"      # 占位符照样取自列表行字段
```

⑥ **详情接口只服务某一类公告**时用 `detail_skip_if`（与 `drop_if` 同构，字段名 → 正则）。
国网的 `getNoticeBid` 遇到中标结果类 `doci-win` 必失败，实测 40 次里 25 次是白打；
而这类公告本来就没有截止时间，跳了也不损失字段：

```yaml
detail_skip_if:
  doctype: "doci-win"            # 行照样入库，只是不再打详情请求
```

排查顺序：正文全空但 `status: success` → 先查 `detail_json_items` 对不对，
再看 `detail_json_fields` 的键名（**别信 `content` 这个名字**，电信叫 `context`，
单一来源又叫 `announceContext`）。

#### 探测过但没接成的（留档，免得下次重复踩）

这份清单比上面那张可用表更长，但同样有价值——知道哪些路走不通，才不会在第二轮又走一遍。

| 站点 | 结果 |
|:---|:---|
| `ggzy.sc.gov.cn` `www.sdggzyjy.gov.cn` `zfcg.czf.fujian.gov.cn` `ggzy.nanjing.gov.cn` `www.whggzy.com` `szggzy.suzhou.gov.cn` `ec.sgcc.com.cn` `ebid.cnpc.com.cn` `cscec-in-zb.cscec.com` `epr.cgnpc.com.cn` `e.minmetals.com.cn` `zhaobiao.cmhk.com` `zycg.ccgp.gov.cn` | **域名根本不存在**（阿里 DNS 报 Non-existent domain）。清单里的 URL 是过期的，不是被封 |
| 广州 `gzggzy.cn` | 子路径一律返回 1718 字节的「页面找不到」；深圳那套 CMS 接口在广州是 405 / 404 |
| 江西 `jxsggzy.cn` | Vue 站点，栏目页路径 `/jyxx/0020xx/.../subPage.html` 全部 404，无 ES 检索中间件 |
| 安徽 `ggzy.ah.gov.cn` | `/zfcg/list` 是空壳框架页，浏览器里也抓不到任何列表 XHR |
| 辽宁 `ggzy.ln.gov.cn` | 有 TRS `was5/web/search`（关键词参数 `searchword`），但只索引了首页 5 个栏目，**没有采购公告栏目**；静态栏目页 404 |
| 天津 `ggzy.zwfwb.tj.gov.cn` | 接口一律返回 `{code:404,"并发连接数过多，请稍后重试"}` |
| 联通 `cuecp.cn` | 首页 API 只有通知类，招标公告藏在 SPA 登录后的路径里，无公开列表接口 |
| 四川/山东/河南/湖南 政采云分站 | 政采云各省独立部署，`categoryCode` 不通用。四川 301、山东 405、河南与湖南域名不可达 |
| `pccz.court.gov.cn` | 能访问，但它是**法院破产案件信息公开**平台，不是招标公告平台，与雷达定位不符 |
| `rmfysszc.gov.cn` | 直接返回「您的访问出错了」 |
| 必联网 `ebnew` / 采招网 `bidcenter` / 千里马 `qianlima` | 搜索 URL 全部 404；三家都要登录才能看正文，与本项目「不做对抗」的原则冲突 |

**2026-10-31 这一轮新增的落选记录**（同一轮里探过 43 个候选，只成了 4 个）：

| 站点 | 结果 |
|:---|:---|
| 华能 `ec.chng.com.cn` 华电 `www.chdtp.com` | 一律 **412**，页面挂瑞数动态防护（`/45i5xfip730ih5uh/sgodapt2y.js` 这类挑战脚本），解铃要逆向签名——本项目红线 |
| 国家电投 `ebid.espic.com.cn` | 首页只有 560 字节，纯 SPA 空壳；公告详情页是静态页但**列表没有公开接口**（目录页要登录态的投标管家才给） |
| 中核 `one.cnncecp.com` 中国铁塔 `ebid.chinatowercom.cn` | 同上，Vue/JS 渲染，列表由 XHR 异步灌入，本轮没定位到可直连的接口 |
| 国铁采购平台 `cg.95306.cn` | 列表确实走 `/elasticSearch/queryDataToEs`，但同一份 JS 里紧挨着 `checkRequestNumValidateCode`——**超过请求数就弹滑块验证码**。属于对抗型防护，不接 |
| 中广核 `epr.cgnpc.com.cn` 中石油 `ebid.cnpc.com.cn` 中车 `pur.crrc.cn` 宝武 `bid.baowugroup.com` 中化 `ebuy.sinochem.com` 国机 `ebid.sinomach.com.cn` 有色 `ec.cnmc.com.cn` 船舶 `zb.cssc.net.cn` | **域名解析失败**（getaddrinfo failed）。这些平台要么换了域名，要么压根不叫这个名字——要接得先重新确认域名 |
| 五矿 `ec.minmetals.com.cn` | 302 直接跳 `/logonAction.do`，列表要登录 |
| 北京 `zfcg.bjcz.gov.cn` / `ggzy.beijing.gov.cn` 河南 `hngp.gov.cn` 黑龙江 `hljcg.gov.cn` 青海 `qhggzyjy.ggzy.gov.cn` | **域名不存在**。这几个省的公开采购入口另在别处，别照抄「ccgp-省份」的命名猜 |
| 贵州 `ggzy.guizhou.gov.cn` 广西 `gxggzy.gxzf.gov.cn` 内蒙古 `ggzyjy.nmg.gov.cn` 新疆 福建 山西 | 站点能开，但**没有 ES 检索中间件**（`/inteligentsearch/**` 一律 404）；云南是 405（nginx 不接受 POST），宁夏返回「审核中」占位页 |
| 甘肃 `ggzyjy.gansu.gov.cn` | 412，有防护 |
| ~~安徽 `ccgp-anhui.gov.cn` 湖南 `ccgp-hunan.gov.cn` 山西 `ccgp-shanxi.gov.cn`~~ | ~~只差一个 categoryCode~~ → **已解决并接入**，见上表 `ahzfcg` / `hnzfcg` / `sxzfcg`。解法：从各省首页 HTML 里正则抠出全部候选码（`ZcyAnnouncement\d+` / `[a-z]+Category\d+` / `\d{2,3}-\d{5,8}`），再逐个 POST `/portal/category` 试，看返回的 `pathName` 是「采购公告」还是「合同/中标」类。**同一个栏目码在不同省含义不同，没有捷径** |

#### 政采云各省接入的三个坑（2026-10-31 实测）

| 坑 | 现象 | 解法 |
|:---|:---|:---|
| **body 键名必须是 `categoryCode`** | 写成 `code` 时接口回 **HTTP 200 + `{"result":{"data":null}}`**，不报错、不失败，表现就是「这个源一条都抓不到」，极难排查 | 严格用 `categoryCode`；探测时把响应体打出来看 `data` 是否为 null |
| **`articleId` 是 Base64，含 `+` `=`** | 原样拼进 query，服务端把 `+` 解成空格，详情返回 200 但 `data` 为 null。且不能用 `parse_qsl`——它会先把 `+` 解成空格再编成 `%20`，反而永久破坏数据 | 管线里的 `_encode_query_values` 手动按 `&`/`=` 切分后逐个 `quote`，不解码 |
| **详情是 GET 不是 POST** | 老代码里 JSON 详情分支写死 POST，政采云 `/portal/detail` 只吃 GET | `SiteRule` 加了 `detail_method`（默认 `POST`，电信/国网/cfcpn 不受影响），配 `detail_method: GET` + `detail_url: "...?articleId={article_id}"` |

另外：**省份不要用 `districtName` 反推**。实测归一化会把「马鞍山市本级」误判成**辽宁**（匹配到「鞍山」），「蜀山区」「孝义市」则完全判不出来。统一在 `static_fields` 里写死省份最稳。

#### 关于转载/聚合源

统一结论：**聚合站全部没接**。它们要么接口已关（404），要么把正文藏在登录后，
接入就得逆向签名或伪造会话——这本项目的红线。

替代做法：聚合站的内容源头本身就是各级交易平台，直接接那些平台既更原始也更稳定，
上面这十二个在采的源就是按这个思路选的。若确实要接某个聚合站，正确路径是用
`python scripts/probe_site.py` 在浏览器里旁听 XHR，找到它的真实 JSON 接口，而不是抠 DOM。

### 一条公告要抓三样东西，缺一样就不好用

| 字段 | 来源 | 缺了会怎样 |
|:---|:---|:---|
| 标题 / 采购人 / 发布日期 | 列表页 | 列表空了，说明选择器失效 |
| 预算金额 / 截止时间 | **详情页** | 雷达上全是「未披露」，没法判断值不值得投 |
| 省份 | 列表行或站点常量 | 地图上没有这块颜色 |

所以 ccgp 与 csg 开了 `fetch_detail: true`。代价是每条新增公告多一次请求（ccgp 约 3 秒/条），
因此加了两条护栏：**先判重再取详情**（已入库的不再打目标站）、**详情只对新增条目取**。
历史数据字段空着的话，先用 `python scripts/backfill_detail.py` 补正文，
再用 `python scripts/reextract.py` 离线重算字段——后者不发任何请求，秒级完成。

### 关键词为什么要跑六组

「法律顾问」「诉讼代理」「司法辅助」这类词在标题里互不包含，只搜「法律服务」会漏掉大半。
每个源在 YAML 里配 `keywords` 列表，一轮采集依次跑完，结果交给去重层合并：

```yaml
keywords: [法律服务, 法律顾问, 诉讼代理, 司法辅助, 律师服务, 法律援助]
```

广东那种不支持关键词检索的站，用 `local_filter: true` 拉全量再本地筛标题。

### 为什么之前只跑了 demo

三个原因，都不是「代码写错了」：

1. **没验证过**。早期 `ccgp.yaml` 的选择器是照公开页面结构写的，但从未真正发过请求——没有测试的配置等于没有配置。
2. **只配了 ccgp 一个真实源**，其余来源当时根本没写适配器。
3. **ccgp 的 WAF 需要浏览器**。`mode: http` 时它返回 HTTP 200 + 一段「访问过于频繁」的 HTML，看起来像成功，实际是拦截页。

第 3 点最隐蔽。现在 `fetcher.detect_waf()` 会识别这类「200 但内容是拦截页」的情况，命中特征词（`访问过于频繁`、`var GH=[`、`Just a moment` 等）就直接判失败并切浏览器，不再假装成功。

### 两种截然不同的取数路径

**路径 A：HTML 站点 → 必须真浏览器（ccgp）**

```yaml
mode: browser
wait_selector: "ul.vT-srch-result-list-bid > li"   # 等这个元素出现再取 DOM
```

`fetcher.BrowserSession` 会优先复用**本机已装的 Chrome / Edge**（自动探测路径，直接 `executable_path` 启动，绕开 playwright 新版对 headless-shell 的查找逻辑），整个采集周期共用一个 context 和 cookie——所以「先开首页再翻页」更像真人，也不会每页重启浏览器。

容器里没有本机浏览器，`Dockerfile` 里已经 `playwright install --with-deps chromium`。

**路径 B：JSON 接口 → 纯 HTTP 就够（ggzy）**

```yaml
response_type: json
search_url: "https://www.ggzy.gov.cn/information/pubTradingInfo/getTradList"
json_items: "data.records"
json_fields:
  title: "title"
  province: "provinceText"        # 服务端已给中文，不用自己猜
  industry: "industryTypeText"
  notice_type: "informationTypeText"
post_data:
  DEAL_CLASSIFY: "02"             # 02 = 政府采购
  TIMEBEGIN: "{start}"
  TIMEEND: "{end}"
  PAGENUMBER: "{page}"
  FINDTXT: "{keyword}"            # 关键词参数就叫 FINDTXT，不是 keyword
```

优先找 JSON 接口：字段结构化、不用解析 DOM、不怕改版。找接口的方法见下一节。

### 怎么给一个新站找接口

别盲猜选择器，用 `scripts/probe_site.py`：

```bash
python scripts/probe_site.py "https://某个招标站点的公告列表页" 法律服务
```

它会用真浏览器打开页面、旁听所有 XHR/fetch 响应，把返回 JSON 的接口连同请求体一起打出来，同时列出候选列表容器（哪个 `div/ul` 下有最多同构的链接条目）。ggzy 那个 `getTradList` 接口就是这么找到的。

#### 探源三件套（2026-10-31 新增，`scripts/` 下）

接新源前的三步勘察，按顺序用，能省掉大量盲猜：

```bash
# 1) 批量探活：这一批域名到底存不存在、通不通
python scripts/probe_candidates.py

# 2) 勘察单站：候选 JSON 接口、表单参数、重复出现的列表容器
python scripts/inspect_site.py "https://某个招标站点的公告列表页"

# 3) 浏览器旁听 XHR：SPA 站点的真实接口与栏目码（需 playwright）
python scripts/sniff_xhr.py https://www.ccgp-anhui.gov.cn 采购公告
```

`sniff_xhr.py` 会打印首页与点击后新增的 JSON 响应，并从页面里正则抠出栏目码候选
（`ZcyAnnouncement\d+` / `[a-z]+Category\d+` / `\d{2,3}-\d{5,8}`）——
**政采云各省的 categoryCode 就是这么抓出来的**。浏览器路径可用
`TR_BROWSER_PATH` / `CHROME_PATH` 指定，找不到就回落到 playwright 自带的 Chromium。

> ⚠️ `probe_candidates.py` 里必须 `trust_env=False`（脚本内已设置）。
> 本机若配了 `http_proxy`，代理会对部分政务域名直接回 502 —— 那不是站点不通，
> 是代理不放行，照着「502」判死会误杀一批可用的源。

### 校准与自检

```bash
# 全部来源试跑第一页，不写库
python scripts/check_sources.py

# 只查某几个
python scripts/check_sources.py ccgp ggzy
```

部署到新服务器后建议先跑这个——政务站常按地域/运营商限流，本机通不代表服务器通。

---

## 技术选型：为什么不是 React + PostgreSQL + Redis

BidInsight 规格推荐 React + AntD + PostgreSQL + Redis + Celery。本项目做了三处有意偏离，理由都是**「方便部署到服务器」**这个前提：

**1. 前端不用 React，改零构建原生实现**
- 无 `node_modules`、无 `npm install`、无构建产物
- 界面能力（列表/筛选/详情/图表）原生 JS 完全够用
- 部署 = 拷静态文件，`git pull` 即生效
- 代价：组件复用性差，将来要长成复杂产品建议换 Vue/React

**2. 数据库默认 SQLite，可一行切 PostgreSQL**
- 万级公告数据 SQLite 完全够，且免运维
- 已开 WAL 模式，读写并发不阻塞
- 切换：`TR_DATABASE_URL=postgresql://...`

**3. 调度用 APScheduler 内置，不依赖 Redis/Celery**
- 单进程起服务即可，不用额外跑 worker 和 broker
- 量大时再换 Celery（规格里的可选项）
- 代价：多副本部署时调度会重复触发，需加分布式锁

---

## 快速开始

### 方式一：本机直接跑（推荐先这样验证）

```bash
cd tender-radar
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 装浏览器（只有这一条命令需要联网下东西）
# 本机已装 Chrome/Edge 的话，程序会自动复用，不装也能跑；
# 但服务器上一般没有桌面浏览器，装一次最稳：
playwright install chromium

# 先自检各来源在你这台机器上通不通（不写库）
python scripts/check_sources.py

# 起服务
python main.py                  # 等价于 uvicorn app.main:app --host 0.0.0.0 --port 8000
python main.py --reload         # 本地开发建议带上，改完代码自动重启
```

> 后端是 Python 进程，**改完代码不重启，跑的还是内存里的旧代码**；
> 前端文件每次请求现读磁盘，于是会出现「页面是新的、接口是旧的」这种最迷惑的故障
> （典型症状：登录报 Method Not Allowed / 404）。

打开 <http://localhost:8000>，看首页 → 点「登录 / 注册」→ 填邮箱 → 点「获取验证码」
→ 去邮箱抄 6 位码 → 登录。

登录靠**邮箱验证码**，所以要配 SMTP（写 `.env` 或环境变量）：

```bash
cp .env.example .env
# 至少填这三项，其余按 .env.example 里的注释来
TR_SMTP_HOST=smtp.exmail.qq.com      # 腾讯企业邮箱；QQ 用 smtp.qq.com，163 用 smtp.163.com
TR_SMTP_USER=you@company.com
TR_SMTP_PASS=                        # 客户端专用密码 / 授权码，不是邮箱登录密码
```

**没配 SMTP 会降级成开发模式**：验证码只写日志、并由接口回显给前端（`dev_code`），
谁都能登录 —— 启动时会有醒目告警。公网部署不允许停留在这个状态。

登录的邮箱如果出现在 `configs/app.yaml → auth.admin_emails` 里，就是管理员，
能看到「管理」标签；其余邮箱一律普通用户。

```yaml
# configs/app.yaml
auth:
  admin_emails:
    - you@company.com   # 换成你自己的邮箱
```

> 也可以用环境变量覆盖：`TR_ADMIN_EMAILS=a@x.com,b@y.com`（逗号分隔，小写）。
> ⚠️ **值后面不要跟 `#` 注释**——曾经因为 `TR_REQUIRE_LOGIN=true  # 关掉后…`
> 被连注释一起读进值里、解析失败静默 fallback，导致公网数据裸奔了一整晚。
> 现在解析失败会打告警，但写注释还是单独起一行最稳。

改完重启即可生效（启动时会把白名单里的人提升为管理员）。之后要加管理员，
直接在「管理 → 注册用户」里点「设为管理员」。

登录后在控制台点右上角「抓取」就会真的去网站上抓。

首次跑完之后，把历史条目的预算和截止时间补齐（列表页不给这两项，要去详情页取）：
```bash
python scripts/backfill_detail.py --limit 80
```

地图数据是预生成的静态文件（`web/china-map.js`），一般不用管；
需要更新边界时再跑：
```bash
curl -o data/_probe/china_full.json https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json
python scripts/build_china_map.py
```

### 方式二：部署到服务器（域名 + HTTPS 对外）

完整步骤见 **`DEPLOY.md`**，包含 Docker Compose 与裸机 systemd 两种方案、
Nginx 反代与 Let's Encrypt 证书、备份升级、上线前安全清单与故障排查。

上传服务器时**只传代码与配置**：`app/`、`web/`、`configs/`、`main.py`、`requirements.txt`，
外加可选的 `deploy/`、`scripts/`、`.env.example`。`.venv/`（约 180MB）和 `data/` 都是本机产物，
服务器重新生成即可，详见 `DEPLOY.md` 的「第 0 步 · 到底要传哪些文件」。
也可以直接 `bash scripts/pack.sh` 打出干净的部署包（约 200KB），脚本会自检。

> ⚠️ 部署前先读 `DEPLOY.md` 的「0. 部署前必读」两件事：
> ① 大陆服务器域名**必须备案**，否则 80/443 会被直接拦掉；
> ② 当前登录是「填邮箱直接进」，不校验凭证，**正式对外前必须收紧**。

---

## 界面与功能

两层结构：**首页**（`/`，产品介绍，公开）→ **控制台**（`/app.html`，需登录）。

```
/                     首页：介绍 + 功能 + 数据源 + 定价 + FAQ，带登录入口
/app.html             控制台：看板 · 雷达 · 来源 ·（管理员）管理
```

未登录时打开 `/app.html` 会被门禁挡住，引导回首页或就地登录；
所有数据接口在服务端校验登录态，前端藏入口只是体验层，**权限不靠前端**。

### 首页

纯静态的落地页，不依赖任何外部字体/图片/CDN，离线可用。五个区块：
Hero（带三个取自 `/api/health` 的实时数字）、核心功能六宫格、三步流程、
数据源分组、定价（免费版可用 + 两个「规划中」占位）、FAQ。

### 账号与登录

**邮箱 + 6 位验证码登录，不设密码。** 目前不限制注册：任意邮箱首次登录即自动建号。

```
填邮箱 → 点「获取验证码」→ 邮箱收码 → 填码登录（首次登录即注册）
```

几个已经做进去的细节：

- **先验码、后建号**。顺序不能反——否则任何人拿任意邮箱刷接口就能灌出一堆空账号。
- 验证码**只存哈希不存明文**，`used` 标记用过即废，同一邮箱 60 秒内只能发一次，
  单码最多试 5 次。
- 验证码有效期 10 分钟（`TR_CODE_TTL`）。
- 没配 SMTP 时降级为**开发模式**：验证码打日志并回显给前端，方便本地联调，
  但公网部署必须配好，否则等于门禁形同虚设。

发信失败会直接把真实原因抛给用户（「验证码发送失败：<原始错误>」），不会静默吞掉
——SMTP 的问题八成是授权码或端口，看到原话才好排查。

几个刻意的决定：

| 决定 | 为什么 |
|:---|:---|
| 不用密码 | 工具型产品低频使用，少一次「想密码/找回密码」的摩擦，也不承担密码泄露的责任 |
| 会话存库（非 JWT） | 后台要能看在线数、要能踢人；令牌是 32 字节随机串，**库里只存 sha256** |
| 管理员白名单「只升不降」 | 后台手动提权的人不该在下一次重启时被白名单静默撤销 |
| 站内跳转不算浏览来源 | 否则「来源 Top 1」永远是自己 |

### 管理员与后台

管理员身份 = **邮箱白名单**（`auth.admin_emails` 或 `TR_ADMIN_EMAILS`）**或**在后台被手动提升。
启动时只做「白名单 → 提升」，**不做降级**：否则后台手动提的人会在下次重启时被静默撤销。

顶栏的「管理」标签只有管理员可见，包含四块：

- **概览**：今日 PV / UV / 新增用户，区间 UV、人均浏览页数、注册用户总数
- **访问趋势**：PV / UV / 新注册三条线的按日折线（手写 SVG，与主题共用 CSS 变量）
- **浏览来源**：渠道分类（直接/搜索/社交/外链）+ 来源域名 Top + 被访问页面 Top
- **注册用户**：邮箱、角色、注册时间、最近登录、登录次数，可提权 / 取消管理员 / 停用

统计口径：**UV 用 cookie 里的匿名 visitor_id，不按 IP**（一家公司共用一个出口 IP，
按 IP 去重会把 UV 严重压低）；**IP 只存哈希**，流量统计不需要知道具体是谁。

### 上线必须配的几项

```bash
# 管理员邮箱（逗号分隔，小写）。其余邮箱一律普通用户。
TR_ADMIN_EMAILS=you@yourcompany.com

# 若已切换回「邮箱验证码登录」，这六项必须配齐，否则用户收不到码
TR_SMTP_HOST=smtp.qq.com
TR_SMTP_PORT=465
TR_SMTP_SSL=true          # 465 → true（SSL 直连）；587 → false（STARTTLS）
TR_SMTP_USER=you@qq.com
TR_SMTP_PASS=             # 邮箱「授权码」，不是登录密码
TR_MAIL_FROM=             # 留空则用 TR_SMTP_USER
```

QQ 邮箱 / 163 需要在网页端「设置 → 账户」里开启 SMTP 服务并生成授权码；
企业微信邮箱、阿里云邮箱同样可用。Gmail 在国内服务器上基本连不通，不建议。

发信失败不会静默：接口返回 502 并提示检查 SMTP 配置，日志里有原始异常。

### 收费预留

不接支付，但把位置占住，后续接支付时**不用改表结构和路由**：

| 预留点 | 现状 |
|:---|:---|
| `users.plan / quota_monthly / plan_expires_at` | 全部 `free` / 不限 |
| 后台「套餐与收入」面板 | 显示套餐分布 + 「未接入支付，暂无收入数据」 |
| `GET /api/billing/plans` | free / pro / team 三档，可在 `configs/app.yaml` 覆盖价格文案 |

刻意**没有**预建订单表与支付回调——那部分取决于最终选哪个渠道（微信/支付宝/Stripe），
提前建表多半会建错。

---

### 控制台：三个业务页面

顶栏从左往右是 **看板 · 雷达 · 来源**（管理员多一个**管理**），**默认打开看板**——
先看全国分布和即将截止，再进雷达逐条筛。

**雷达** — 公告列表，按截止时间升序，最近要截止的排最前
- **每条一行的关键信息**：预算金额、截止日期、剩余天数（≤3 天标红、≤7 天标橙），
  外加采购人、地区、来源、发布日期；不用再点开详情才知道值不值得投
- 左侧色条分级：≤3 天红色、≤7 天橙色、已截止灰化
- 搜索 / 地区 / 类型筛选 + **预算区间筛选**（下拉选常用档，或用两个输入框自定义元区间）
- 只看未截止 + 含结果公告
- 点击卡片右侧抽屉看正文、抽取溯源、同源镜像
- 滚动自动加载下一页

> 不设「行业」维度：这个雷达只盯法律服务采购，再按行业切分只会把同类线索拆散。

**看板** — 概览指标、**中国省份热力地图**、发布趋势、区域 Top 10、即将截止、来源分布
- 地图按省份在库线索数上色，颜色越深线索越多；**点省份直接跳到雷达并自动带上该省筛选**
- 区域 Top 10 的每一行同样可点击跳转
- 边界数据来自国家标准行政区划（含台湾省与南海诸岛插图），完全离线渲染，
  不依赖任何第三方地图服务或瓦片；重新生成用 `python scripts/build_china_map.py`
- 省份归属按可信度分级取：**站点自带地区字段 > 站点规则写死（省级平台）> 文本推断**。
  推断时先剔除「（北京时间）」这类干扰，再忽略中标公告里中标律所的所在地，
  最后补一层地级市→省份映射（`深圳供电局` 才能落到广东）。
  推断不出来的就留空，地图上显示为「暂无线索」——不为了好看而编一个省。
  旧数据重算：`python scripts/fix_province.py --dry-run`

**来源** — 每个来源的健康状态、失败次数、手动采集、启用停用、重载规则

### 主题：跟随系统 / 浅色 / 深色

右上角一个分段控件，三选一，**选择存在 localStorage（键 `tr-theme`），刷新保留**。
**默认是深色**——用户没手动选过之前一律 `dark`，不跟随系统：

| 选项 | 行为 |
|:---|:---|
| 深色（默认） | 未手动选择时的默认；手动选过之后也始终深色 |
| 跟随系统 | 读 `prefers-color-scheme`，并且**实时跟随**——系统切深色，页面不用刷新就跟着变 |
| 浅色 | 始终浅色 |

实现上有四个点值得记一笔：

1. **`<head>` 里有一段内联脚本**，在首屏渲染前就把 `data-theme` 定好（默认 `dark`）。
   没有它的话，深色偏好用户每次刷新都会先闪一下白屏。
2. **所有颜色都是 CSS 变量**（`--bg` / `--surface` / `--brand` …），
   深色只是覆盖同一批变量名，组件本身不写死任何色值。
   地图和趋势图这种内联 SVG 也不例外：它们用 `class="l3"`、`class="tbar"` 取色，
   切主题只换变量、**不需要重新请求也不需要重绘**。
3. 同时写了 `color-scheme`，让原生下拉框、滚动条、表单控件跟着变，
   否则浅色页面上会挂一个刺眼的白底 `select`。

对比度按 WCAG AA 校准过（正文 17:1、次要文字 6:1、弱化说明文字 4.7:1，
主按钮文字 6.2:1），两种主题都是。

### 页脚与政策文档

页脚结构与 [quote.law](https://quote.law/) 一致：**政策协议 → 用户协议 / 隐私政策**，
版权主体显示为深圳易诉达法律服务有限公司。

```
web/
├─ legal/
│  ├─ legal.css              # 政策页排版（复用主站设计令牌，主题跟着整体走）
│  ├─ user-agreement.html    # 用户服务协议
│  └─ privacy-policy.html    # 个人信息保护政策
```

两份文档的写法刻意贴合这个系统本身，而不是套模板：

- **用户协议**里讲清了数据来源边界：只采集来源网站主动公开的公告，
  不破解 / 不绕过技术保护措施、不抓需登录内容、遇反爬提示即停；
  并明确「预算、截止时间等字段是程序自动抽取，可能缺失或偏差，以原文为准」。
- **隐私政策**写的是本系统的真实情况：不设注册、不收集个人信息；
  浏览器只存一个 `tr-theme`（主题偏好）；不接第三方统计与 CDN。
  其中一条容易被忽略但必须写的：**招标公告原文里本身就有采购人联系人姓名与电话**，
  属于来源网站公开的业务信息，政策里说明了只原样展示、并提供删除与异议渠道。

> 上线前请替换：政策文档中的联系邮箱、地址，以及页脚的备案号（如需）。
> 文档里留了 `<!-- TODO -->` 标记，搜 `yesuda` 就能全部找到。

## 新增一个采集来源

**不用改代码**。在 `configs/sites/` 加一份 YAML：

```yaml
id: my_source
name: 某某公共资源交易中心
enabled: true
mode: http                 # http | browser

search_url: "https://example.gov.cn/search?page={page}&kw={keyword}&start={start}&end={end}"
default_keyword: 法律服务

list_container: "div.list > div.item"
list_fields:
  title:
    selectors: ["a", "h3 a"]      # 多个选择器按序回退
    extract: text
  url:
    selectors: ["a"]
    extract: "attr:href"
  publish_time:
    selectors: ["span.date"]
    extract: text
    regex: "\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}"

detail_container: "div.content"
detail_fields:
  content:
    selectors: ["div.content"]
    extract: html
```

存盘后在「来源」页点**重载规则**，或调 `POST /api/sources/reload`。

### 站点校准怎么做

政务站改版频繁，选择器失效是常态。流程：

1. 浏览器打开目标列表页，F12 找到单条记录的外层元素 → 填进 `list_container`
2. 在该元素上右键 Copy selector，逐字段填 `list_fields`
3. 用单站点调试（不写库，只看解析结果）：

```bash
python - <<'EOF'
from app.crawler.adapters import get_site, parse_list
from app.crawler.fetcher import fetch
r = get_site("ccgp")
res = fetch(r.build_page_url(page=1, keyword="法律服务"))
print(res.status_code, res.error)
for row in parse_list(r, res.html)[:3]:
    print(row)
EOF
```

4. 解析通了再去界面上点采集

> 调试脚本里用 `build_page_url` 而不是 `build_url`：第 1 页不一定在 `search_url` 上
> （见下面 `first_page_url`），用 `build_url(page=1)` 会打到错误的地址，
> 从而把好源误判成「解析不出东西」。

### 第 1 页不在 search_url 上时

Destoon 一类 CMS 的栏目首页和分页页不是同一个地址，**而且内容不等价**。
这时给一份 `first_page_url`，分页循环会在 `page == 1` 时改用它：

```yaml
search_url: "http://cqzdt.toobiao.com/zhaobiao/page-{page}.shtml"
first_page_url: "http://cqzdt.toobiao.com/zhaobiao/"
```

判据：分别抓 `栏目页`、`page-1.shtml`、`page-2.shtml`，比对条目集合。
中国铁建 crcc 的 `page-1.shtml` 会 301 跳到**站点首页**，首页是混合列表，
只含 20 条招标公告里的 16 条，少的 4 条**既不在首页也不在第 2、3 页** ——
不配 `first_page_url` 就永久漏采，而且不会报错。（中国中铁 crecg 的同名 URL
是 301 跳栏目页，所以那边不用配。同名 URL 行为不一致，每个站都得单独验。）

注意 `first_page_url` 只作用于列表分页。详情 URL 的回落逻辑走的是
`build_url(page=1)`，不受影响 —— 这是有意的：没配 `detail_url` 的站点
如果让第 1 页规则生效，会把栏目首页当成详情页去抓。

### `warmup_url`：WAF 站点要先真开一次页面

`mode: browser` + `response_type: json` 的组合走的是 Chromium 网络栈
（`context.request.get`），它**不执行 JS**。而阿里云 WAF 这类防护的通行票
（`acw_sc__v2`）恰恰是页面里的挑战脚本算出来的 —— 不先把页面真开一次，
这个 cookie 永远拿不到，接口请求会被全部拦掉，重试多少次都没用。

给一份 `warmup_url`，采集器会在本轮开始前先用 `page.goto` 打开它，
让挑战脚本跑完、cookie 落进共享的 context，之后本轮所有请求都自动带上：

```yaml
mode: browser
response_type: json
warmup_url: "https://tang.cdt-ec.com/notice/moreController/toMore?globleType=0"
```

实测（大唐 datang）：不预热，冷启动请求 100% 返回 17136 字节的挑战页；
预热后连打 5 次全部 200。cookie 在整个 context 生命周期内有效，
所以一轮采集只需预热一次，成本约 3 秒。

预热失败**不判死**，只记一条 error 继续跑 —— 有些站点的列表接口本身不受防护，
照常往下跑即可，真被拦会在取页那一步报 `blocked`。

---

### `sign_secret`：请求头里带时间戳签名的站点

有些站点的接口头里放一个**随时间变化的签名**（招商局 cmhk 就是），
签名不对一律 `sign.validation.failed`。这类签名没法写死在 `headers` 里，
因为它是 `时间戳 → 签名` 的单次函数。

配 `sign_secret` 后，`headers` 里可以用两个占位符，由 `build_headers()`
**在同一次调用里**一起算出来：

```yaml
sign_secret: "92d58439-e6aa-42ff-b06a-4d4379a4b36c-78"
sign_url_part: "pageQuery"      # 留空则取 search_url 路径末段

headers:
  X-Timestamp: "{now_ms}"       # 当前毫秒时间戳
  X-Authorization: "{sign}"     # 与上面同一次算出的签名
```

```python
# build_headers() 的算法（招商局门户 axios 拦截器里的那套）
cat  = f"{now_ms}{sign_url_part}{sign_secret}"
sign = md5("".join(sorted(cat)[::-1]).encode()).hexdigest()
```

**两个占位符一定要同一次生成**：签名里含时间戳，分两次取会错位，
服务端直接判失败 —— 表现是每次请求都 200 但 `errMessage` 是
`sign.validation.failed`，很容易误判成"签名算法猜错了"。

没配 `sign_secret` 的站点走原路径，行为完全不变。

签名如果哪天开始失败，先重新取一次
`GET /zsj-menhu-web/portalDomain/getTemplateConfigByDomain` 的
`data.templateConfig` 看它有没有变 —— 那个值就是 secret，是静态的但不是永久的。

---

### `json_field_regex`：JSON 接口取值后再过一道正则

`json_fields` 只是「入库字段名 → 响应里的键名」的直映射，
碰到**采购人得从标题里抠**的站点就不够用了 —— 华润守正 szecp 的接口
只给长标题，没有独立采购人字段，而 `rule_purchaser` 要认「采购人：」这类标签，
对纯标题覆盖率是 **0**。

```yaml
json_fields:
  title: title
  purchaser: title          # 先照搬标题
json_field_regex:
  # 再从标题开头抠出单位名，覆盖率 96%
  purchaser: '^([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{4,28}?(?:股份有限公司|有限责任公司|有限公司|集团公司|公司|医院|研究院|大学|中心|局|委员会))'
```

语义与 HTML 路径 `list_fields` 的 `regex` 一致：**有一个捕获组就取 `group(1)`，
否则取整段匹配；匹配不上该字段留空**（不留原值，避免把整条标题塞进采购人列）。

---

## 目录结构

```
tender-radar/
├─ app/
│  ├─ main.py              # FastAPI 入口（同时托管前端）
│  ├─ config.py            # 配置中心（yaml + env）
│  ├─ db.py                # 引擎/会话，SQLite 自动开 WAL
│  ├─ models.py            # Source / Tender / CrawlRun / User / LoginCode / AuthSession / PageView
│  ├─ auth.py              # 会话令牌、登录/管理员依赖（验证码与 SMTP 代码保留但停用）
│  ├─ schemas.py           # API 契约
│  ├─ notify.py            # webhook 告警与关键词推送
│  ├─ scheduler.py         # APScheduler 定时增量
│  ├─ state.py             # 采集互斥锁
│  ├─ api/                 # tenders / stats / system / auth / analytics / billing
│  └─ crawler/
│     ├─ fetcher.py        # 三层递进取页
│     ├─ adapters.py       # YAML 规则加载与执行
│     ├─ extract.py        # 规则优先 + LLM 兜底
│     ├─ normalize.py      # 清洗/时间/金额/截止校验
│     ├─ dedupe.py         # 三层去重 + 跨源折叠
│     └─ pipeline.py       # 编排、增量、落库
├─ configs/
│  ├─ app.yaml             # 全局配置
│  └─ sites/*.yaml         # 站点规则（加来源只改这里）
├─ web/                    # 零构建前端
│  ├─ index.html / home.js # 首页（产品介绍，公开）
│  ├─ app.html / app.js    # 控制台（看板·雷达·来源）
│  ├─ auth.js              # 登录弹窗 / 门禁 / 账号菜单 / 埋点（两页共用）
│  ├─ admin.js             # 管理后台（概览·流量·来源·用户·套餐）
│  ├─ style.css
│  ├─ china-map.js         # 预生成的地图边界（含台湾省与南海诸岛）
│  └─ legal/               # 页脚政策文档（用户协议、隐私政策）
├─ scripts/
│  ├─ smoke_auth.py        # 首页/登录/控制台/后台 的浏览器冒烟测试（含截图）
│  ├─ backfill_detail.py   # 在线补详情页正文（补 missing 的 content_text）
│  ├─ reextract.py         # 离线重算字段：正文已有就用新规则再抽一遍，零请求
│  ├─ dedupe_repair.py     # 去重键升级后，合并历史遗留的重复行
│  ├─ fix_province.py      # 省份归属纠正（省级平台写死优先，其余重算）
│  ├─ build_china_map.py   # 生成地图边界数据
│  └─ backup_db.sh         # 每日备份
├─ data/                   # 运行时：SQLite + 原文快照
├─ deploy/                 # 部署用配置：nginx.conf / systemd unit / 自检脚本
├─ Dockerfile
├─ docker-compose.yml
├─ README.md
└─ DEPLOY.md               # 服务器安装部署说明
```

---

## API

自文档：`http://<host>:8000/docs`

除 `/api/health`、`/api/auth/*`、`/api/analytics/track` 外，其余接口都需要
`Authorization: Bearer <token>`，未登录返回 401；管理员接口返回 403。

| 方法 | 路径 | 鉴权 | 说明 |
|:---|:---|:---|:---|
| GET | `/api/health` | 公开 | 健康检查（含库内总数、调度状态）。首页要用，不能要求先登录 |
| POST | `/api/auth/code` | 公开 | 发登录验证码到邮箱。配了 SMTP 返回 `channel=smtp`；否则 `dev` 且回显 `dev_code` |
| POST | `/api/auth/login` | 公开 | **验证码登录**，先验码再建号。新邮箱自动注册 |
| POST | `/api/auth/logout` | 登录 | 销毁当前会话 |
| GET / PATCH | `/api/auth/me` | 登录 | 当前用户 / 改昵称 |
| GET | `/api/auth/users` | 管理员 | 用户列表 |
| PATCH | `/api/auth/users/{id}` | 管理员 | 改角色 / 停用 / 改套餐（不能操作自己） |
| GET | `/api/auth/session-stats` | 管理员 | 近 30 分钟在线会话数 |
| POST | `/api/analytics/track` | 公开 | 埋点（首页也要记，失败不影响页面） |
| GET | `/api/analytics/overview?days=30` | 管理员 | 今日/区间 PV·UV、用户数 |
| GET | `/api/analytics/traffic?days=30` | 管理员 | 按日 PV / UV / 新注册 |
| GET | `/api/analytics/referrers?days=30` | 管理员 | 渠道分布 + 来源域名 + UTM |
| GET | `/api/analytics/pages?days=30` | 管理员 | 页面 Top |
| GET | `/api/billing/plans` | 登录 | 套餐档位 |
| GET | `/api/billing/summary` | 管理员 | 套餐分布 + 收入（现为占位） |
| GET | `/api/tenders` | 登录 | 列表。`q, province, industry, notice_type, source_id, min_budget, max_budget, only_active, include_result, sort, page, size` |
| GET | `/api/tenders/{id}` | 登录 | 详情（含正文、抽取溯源、同源镜像） |
| POST | `/api/tenders/export` | 登录 | 导出 CSV（带 BOM，Excel 不乱码） |
| GET | `/api/stats/overview` | 登录 | 概览指标 |
| GET | `/api/stats/trend?days=30` | 登录 | 发布趋势 |
| GET | `/api/stats/distribution?dim=province\|industry\|notice_type` | 登录 | 分布 |
| GET | `/api/stats/upcoming?days=7` | 登录 | 即将截止 |
| GET | `/api/sources` | 登录 | 来源列表与健康状态 |
| POST | `/api/sources/reload` | 管理员 | 重载 `configs/sites` |
| PATCH | `/api/sources/{id}` | 管理员 | 启用/停用 |
| POST | `/api/crawl` | 管理员 | 手动采集。`{site_id?, max_pages, keyword?, fetch_detail?}` |

采集是**串行且带互斥锁**的：同一时刻只允许一轮，重复请求返回 409。这是为了保护目标站，也避免把自己 IP 打黑。
手动采集收敛到管理员，原因同上：普通用户随手点几下就能把十几个政务站打到限流。

---

## 去重三层

```
L1  主键      sha256(normalize(title) + publish_date + purchaser)
              → 拦本源的重复抓取
L2  归一化    全角转半角、去空白、剥离【】前缀、标点统一
              → 让「（2026）苏 0213…」和「(2026)苏0213…」算同一条
L3  跨源折叠  SimHash(64bit) 汉明距离 ≤12 且 Jaccard ≥0.62
              → 同一个标转发到多个平台时归为一簇，对外只暴露最早一条
```

L3 只在 `is_primary=true` 的记录间比对，避免簇内自我膨胀。

---

## 合规边界（请务必读）

工具是公开信息聚合器，不是爬虫武器库。使用前请确认：

1. **只采公开内容** —— 不登录、不绕验证码、不碰需要授权的区域
2. **温和速率** —— 默认 2–5 秒随机间隔，单源最多 5 页。不要调低
3. **尊重 robots.txt 与站点条款** —— 有明确禁止的站点不要加进 `configs/sites`
4. **原文标注来源并保留链接** —— 不做全文商业再分发（`source_url` 字段任何时候都要保留）
5. **个人信息** —— 公告里的联系人、电话如需展示，请脱敏
6. **不要把「线索」做成「骚扰」** —— 这是法律行业最关键的一条：《律师执业管理办法》禁止以不正当方式承揽业务、禁止承诺办案结果。本工具输出的是**线索清单**，触达必须由律师人工完成并自行合规

已知的验证码型来源（如中国招标投标公共服务平台）默认 `enabled: false`，要接请自行评估风险，并保持「只取列表、详情单条按需」的原则。

---

## 已知限制

- **33 个来源已实测在采**（另有 `ztbpub` 因瑞数动态防护默认关闭）。**但接入不等于零维护**：政务站改版频繁，每个源的可用性以 `/api/health` 与应用内「来源」页的实时健康度为准。新环境首次部署建议先跑一遍 `python scripts/check_sources.py`，确认在你自己的网络下解析条数正常。
- **ccgp 的截止时间要靠深采**：列表页只给发布时间和采购人，截止时间/预算在详情页。用来源页的「深采」按钮（`fetch_detail=true`），或调度里开。
- **站点选择器会失效**：政务站改版频繁，改版后 `check_sources.py` 会显示「解析 0 条」，按「站点校准」调整 YAML 即可，不用改代码。
- **限流是硬约束**：ccgp 建议 ≥3 秒间隔。把间隔压到 1 秒不会更快，只会更快被封。
- **未内置鉴权**：公网部署请用 Nginx Basic Auth / OAuth2 Proxy 兜在前面。
- **详情抓取是串行的**：全量取详情很慢（每条约 2–5 秒），建议只对高意向条目单独取。
- **LLM 兜底默认关闭**：开启后对央企/国企自由排版公告的召回提升明显，但会产生 API 成本。
- **无分布式调度**：多副本部署时定时任务会重复触发。

---

## 路线图

- [x] v0.1 采集 + 标准化 + 三层去重 + 列表检索 + 看板
- [x] v0.2 来源管理、手动采集、关键词订阅推送、CSV 导出
- [x] v0.3 真实站点接入（ccgp 浏览器模式 + ggzy JSON 接口）、WAF 识别、站点探测工具
- [x] v0.4 产品首页、邮箱验证码登录、访问统计与管理后台、套餐字段预留
- [ ] v0.5 把「即将截止」推到企业微信/飞书（webhook 已就绪，缺卡片模板）
- [ ] v0.6 每人一套关键词订阅与推送渠道（用户体系已就绪，缺规则管理界面）
- [ ] v0.7 采购方画像（同一单位的历史采购频次与预算规模）
- [ ] v0.8 接入社媒信号源（评论区求助帖），扩展成「网络找案」多源雷达
- [ ] v0.9 收费注册：套餐限额、订单与支付回调（表字段与面板已预留）

---

## 许可

本项目代码可自由使用与修改。采集行为及其产出数据的合规责任由部署者承担。
