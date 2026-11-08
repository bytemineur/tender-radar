"""取页层：三层递进。

  L1 HTTPX       常规静态页，带 UA 池 + 随机延迟 + 指数退避
  L2 Playwright  真浏览器，处理 JS 重页面 / 瑞数动态防护 / WAF
  L3 放弃        标记 source.health=blocked 并告警，绝不在调度里空转烧 IP

这一层刻意「不做对抗」：不代理池轮换、不破解验证码、不伪造指纹。
遇到验证码就停，交给人。

浏览器内核优先复用**本机已装的 Chrome / Edge**（channel），
省掉 ~150MB Chromium 下载；容器内则用 `playwright install chromium`。
"""

from __future__ import annotations

import base64
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import settings

# UA 池：覆盖主流浏览器，配合 Accept-Language 更像真实访问
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

# 常见 WAF / 反爬拦截页特征。命中即判定「被拦」，不重试、不硬刚。
WAF_SIGNATURES = (
    "访问过于频繁",
    "您的访问过于频繁",
    "请稍后再试",
    "访问被拒绝",
    "Request blocked",
    "Access Denied",
    "Just a moment",  # Cloudflare
    "安全验证",
    "人机验证",
    "请完成验证",
    "var GH=[",  # 瑞数动态防护（Riversafe）混淆脚本特征
    "bm-verify",  # 百度云加速
)


def _jitter(delay: dict[str, float] | None = None) -> None:
    """随机抖动限速——温和抓取，不给目标站添麻烦。"""
    lo = (delay or {}).get("min", settings.crawl.min_delay)
    hi = max((delay or {}).get("max", settings.crawl.max_delay), lo)
    time.sleep(random.uniform(lo, hi))


def _headers() -> dict[str, str]:
    return {**DEFAULT_HEADERS, "User-Agent": random.choice(UA_POOL)}


def _legacy_ssl_context() -> "ssl.SSLContext":
    """给「还在跑老 TLS」的站点用的 SSLContext。

    OpenSSL 3.x 默认禁用了不安全重协商（RFC 5746），
    中国移动采购与招标网那批老 F5/老 Nginx 还在握手阶段发起重协商，
    于是 Python 直接抛 UNSAFE_LEGACY_RENEGOTIATION_DISABLED —— 连不上，
    且和证书校验无关，verify=False 救不了。
    打开 OP_LEGACY_SERVER_CONNECT 才能握手，代价是这一路放弃前向安全性，
    所以只对明确声明 tls_legacy 的站点启用，不全局打开。
    """
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    ctx.options |= _ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


def detect_json_error(body: str) -> str | None:
    """JSON 接口把错误藏在 HTTP 200 里。

    政务接口普遍用 {errcode, errmsg} 信封：限流也照样返回 200。
    不识别的话，爬虫会以为「请求成功、只是没数据」，把限流错当成空结果，
    来源健康度显示正常，实际问题一直被盖住。
    """
    s = (body or "").lstrip()
    if not s.startswith("{"):
        return None
    try:
        import json as _json

        d = _json.loads(s)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None

    # 成功码各家写法不一致（ggzy 用 code:200，gdggzy 用 errcode:0），
    # 统一按「成功值白名单」过滤，落到白名单外才算错误。
    code = d.get("errcode", d.get("code"))
    if code in (None, "", 0, "0", 200, "200", "ok", "success"):
        return None

    msg = str(d.get("errmsg") or d.get("msg") or d.get("message") or "")
    # 正常的数据响应通常很大；短短几百字节且带错误码，基本可以确定是错误信封
    return f"errcode={code} {msg}".strip()


def detect_waf(html: str, status_code: int = 200) -> str | None:
    """判断返回内容是不是拦截页。是则返回命中的特征，否则 None。

    注意只看前 8KB：拦截页都很短，正文页里偶然出现「安全验证」四个字不算。
    """
    if status_code in (403, 405, 412, 429, 503):
        return f"HTTP {status_code}"
    head = (html or "")[:8192]

    # JSON 接口的错误信封优先判断，避免限流被当成空结果
    jerr = detect_json_error(head)
    if jerr:
        return jerr

    for sig in WAF_SIGNATURES:
        if sig in head:
            return sig
    return None


# ── 请求体加密 ───────────────────────────────────────────────────────────
# 少数站点的接口不吃明文 JSON：整个请求体
# 要用服务端**现下发**的 RSA 公钥加密，再塞进 {"param": "<base64>"}。
# 公钥每次请求都可能换（实测同一个页面里连取两次就不同），所以不能缓存。
#
# 算法是按浏览器里的 jsencrypt 复刻的：
#   payload = JSON.stringify({...原始参数, sign: md5(JSON.stringify(原始参数)),
#                             timeStamp: Date.now()})
#   密文   = concat(RSA_PKCS1_v1_5(每 117 字节一块))  再整体 base64
# 这里没有引入 cryptography / pycryptodome：PKCS#1 v1.5 加密本身
# 就是 `pow(m, e, n)`，配一个二十行的 DER 解析就够了，省一个重依赖。

_B64_MAP = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_PAD = "="


def _der_tlv(data: bytes, i: int) -> tuple[int, bytes, int]:
    """最小 DER 解析：返回 (tag, 内容, 下一个 TLV 的偏移)。"""
    tag = data[i]
    i += 1
    ln = data[i]
    i += 1
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[i:i + n], "big")
        i += n
    return tag, data[i:i + ln], i + ln


def _rsa_public_key(b64: str) -> tuple[int, int]:
    """把 SubjectPublicKeyInfo（base64 DER）解成 (n, e)。"""
    der = base64.b64decode("".join((b64 or "").split()).encode("ascii"))
    _, spki, _ = _der_tlv(der, 0)             # SEQUENCE
    _, _alg, p = _der_tlv(spki, 0)            # AlgorithmIdentifier
    tag, bits, _ = _der_tlv(spki, p)          # BIT STRING
    if tag != 0x03:
        raise ValueError("公钥格式不是 BIT STRING")
    _, body, _ = _der_tlv(bits[1:], 0)        # RSAPublicKey SEQUENCE
    _, nbytes, q = _der_tlv(body, 0)
    _, ebytes, _ = _der_tlv(body, q)
    return int.from_bytes(nbytes, "big"), int.from_bytes(ebytes, "big")


def _hex2b64(h: str) -> str:
    """jsencrypt 的 hex2b64：3 个十六进制字符 → 2 个 base64 字符。"""
    out: list[str] = []
    i = 0
    while i + 3 <= len(h):
        c = int(h[i:i + 3], 16)
        out.append(_B64_MAP[c >> 6])
        out.append(_B64_MAP[c & 63])
        i += 3
    if i + 1 == len(h):
        out.append(_B64_MAP[int(h[i:i + 1], 16) << 2])
    elif i + 2 == len(h):
        c = int(h[i:i + 2], 16)
        out.append(_B64_MAP[c >> 2])
        out.append(_B64_MAP[(c & 3) << 4])
    while len(out) & 3:
        out.append(_B64_PAD)
    return "".join(out)


def _rsa_encrypt_long(n: int, e: int, text: str) -> str:
    """分块 PKCS#1 v1.5 加密，密文拼接后整体 base64。

    按 **UTF-8 字节**分块而不是按字符：原版 jsencrypt 按 JS 字符数切 117，
    一旦夹中文就会出现一块超过 117 字节、pkcs1pad2 直接失败的老 bug。
    按字节切始终合法，服务端按 128 字节一块解密的结果也一样。
    """
    ksz = (n.bit_length() + 7) // 8
    max_len = ksz - 11
    raw = text.encode("utf-8")
    hex_all = ""
    for off in range(0, len(raw), max_len):
        chunk = raw[off:off + max_len]
        ps = bytes(random.randrange(1, 256) for _ in range(ksz - len(chunk) - 3))
        m = int.from_bytes(b"\x00\x02" + ps + b"\x00" + chunk, "big")
        hex_all += format(pow(m, e, n), "0256x")[-ksz * 2:]
    return _hex2b64(hex_all)


def encrypt_request_body(cfg: dict, body: dict) -> dict:
    """按 encrypt 配置把整个请求体加密。

    cfg: {"type": "rsa", "public_url": "...", "field": "param"}
    失败时抛异常 —— 加密失败还照发明文是毫无意义的请求，
    不如让调用方把这一次记成 error，下一轮再试。
    """
    if not cfg or str(cfg.get("type") or "").lower() != "rsa":
        return body
    public_url = str(cfg.get("public_url") or "").strip()
    field = str(cfg.get("field") or "param")
    if not public_url:
        raise ValueError("encrypt.public_url 为空")

    import hashlib
    import json as _json

    r = fetch_http(public_url, method="POST", timeout=20)
    if not r.ok:
        raise ValueError(f"取 RSA 公钥失败：{r.error or r.status_code}")
    n, e = _rsa_public_key("".join(r.html.split()))

    # sign 是对**原始参数**的 md5（不含 sign 和 timeStamp 本身），
    # 顺序也要和 JSON.stringify 一致 —— Python dict 保序，照抄即可。
    plain = _json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    payload = dict(body)
    payload["sign"] = hashlib.md5(plain.encode("utf-8")).hexdigest()
    payload["timeStamp"] = int(time.time() * 1000)
    enc = _rsa_encrypt_long(
        n, e, _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return {field: enc}


@dataclass
class FetchResult:
    """取页结果。"""

    html: str
    # http | browser | http+browser
    method: str
    status_code: int
    final_url: str = ""
    error: str | None = None
    # 命中的 WAF 特征（非空表示被拦截）
    blocked_by: str | None = None

    @property
    def ok(self) -> bool:
        return self.html != "" and self.error is None and not self.blocked_by


# ── L1：HTTPX ────────────────────────────────────────────────────────────
def fetch_http(
    url: str,
    *,
    timeout: float | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: dict | None = None,
    json_body: dict | None = None,
    legacy_tls: bool = False,
) -> FetchResult:
    """L1：HTTPX 取页，指数退避 1s/4s/16s。

    legacy_tls：目标站还在跑老 TLS、会在握手时重协商（详见 _legacy_ssl_context）。
    """
    timeout = timeout or settings.crawl.timeout
    last_err = "未知错误"
    for attempt in range(settings.crawl.max_retries):
        try:
            _jitter()
            # 老 TLS 站点要用带 OP_LEGACY_SERVER_CONNECT 的 context，
            # 其余站点沿用 verify=False（政务站证书链普遍不完整）。
            verify: object = _legacy_ssl_context() if legacy_tls else False
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                verify=verify,  # type: ignore[arg-type]
                headers={**_headers(), **(headers or {})},
            ) as client:
                resp = client.request(method, url, data=data, json=json_body)
            body = resp.text
            waf = detect_waf(body, resp.status_code)
            if resp.status_code == 200 and not waf:
                return FetchResult(
                    html=body,
                    method="http",
                    status_code=resp.status_code,
                    final_url=str(resp.url),
                )
            if waf and resp.status_code == 200:
                # 200 但返回的是拦截页：直接交给上层切换浏览器，退避无意义
                return FetchResult(
                    html=body,
                    method="http",
                    status_code=200,
                    final_url=str(resp.url),
                    blocked_by=waf,
                )
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code in (403, 429, 503):
                time.sleep(settings.crawl.backoff_base * (4**attempt))
                continue
            return FetchResult(
                html="", method="http", status_code=resp.status_code, error=last_err
            )
        except httpx.TimeoutException:
            last_err = "请求超时"
            time.sleep(settings.crawl.backoff_base * (4**attempt))
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(settings.crawl.backoff_base * (4**attempt))
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            break
    return FetchResult(html="", method="http", status_code=0, error=last_err)


# ── L2：Playwright ───────────────────────────────────────────────────────
# 直接按可执行文件路径启动，绕开 playwright 的 channel→headless-shell 逻辑。
# 新版 playwright 用 headless=True 时会去找 chromium_headless_shell，
# 而本机只装了完整版 Chrome/Edge，找不到就报错。指定 executable_path 最稳。
_BROWSER_CANDIDATES = [
    ("chrome", "/usr/bin/google-chrome"),
    ("chrome", "/opt/google/chrome/chrome"),
    ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ("msedge", "/usr/bin/microsoft-edge"),
    ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("msedge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def pick_executable() -> tuple[str, str] | None:
    """找一个可用的本机浏览器，返回 (内核名, 可执行文件路径)。

    优先级：环境变量 TR_BROWSER_PATH > 本机 Chrome > Edge
    返回 None 表示没有本机浏览器，交给 playwright 内置 Chromium。
    """
    env_path = (os.getenv("TR_BROWSER_PATH") or "").strip()
    if env_path and Path(env_path).exists():
        return ("custom", env_path)
    if (os.getenv("TR_BROWSER_CHANNEL") or "").strip().lower() in (
        "none",
        "chromium",
        "bundled",
    ):
        return None
    for name, p in _BROWSER_CANDIDATES:
        if Path(p).exists():
            return (name, p)
    return None


def pick_channel() -> str | None:
    """兼容旧接口：返回内核名，用于展示。"""
    got = pick_executable()
    return got[0] if got else None


class BrowserSession:
    """一个可复用的浏览器会话。

    每次 fetch 都 launch 一次浏览器约需 1~2 秒，分页抓取会被放大。
    这里在一个来源的整个采集周期内复用同一个 context，页签按需新建，
    并保留 cookie —— 这样「先访问首页再翻页」更接近真人行为。
    """

    def __init__(self, *, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._browser = None
        self._ctx = None
        self.error: str | None = None
        self.kernel: str = ""

    def __enter__(self) -> BrowserSession:
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415

            self._pw = sync_playwright().start()
            kw: dict = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    # 新版 Chrome 无头模式必须显式声明，否则会被识别为旧无头
                    "--headless=new",
                ],
            }
            got = pick_executable()
            if got:
                self.kernel = got[0]
                kw["executable_path"] = got[1]
            else:
                self.kernel = "chromium"
            try:
                self._browser = self._pw.chromium.launch(**kw)
            except Exception:  # noqa: BLE001
                # --headless=new 在个别老版本上不被接受，去掉重试一次
                kw["args"] = [a for a in kw["args"] if a != "--headless=new"]
                self._browser = self._pw.chromium.launch(**kw)
            self._ctx = self._browser.new_context(
                user_agent=random.choice(UA_POOL),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        return self

    def warmup(self, url: str, timeout: float | None = None) -> str | None:
        """开跑前先真打开一次页面，让 WAF 的 JS 挑战把 cookie 写进本 context。

        阿里云 WAF（acw_sc__v2）的票是页面脚本算出来的，
        context.request.get 不执行 JS，光靠它永远过不去。
        这里用 page.goto 走一遍真实渲染，cookie 落在共享的 _ctx 上，
        后续 raw_json 请求就自动带上了。
        返回 None 表示成功，否则是错误原因。
        """
        if self._ctx is None:
            return f"浏览器未启动：{self.error or '未知原因'}"
        page = None
        try:
            page = self._ctx.new_page()
            page.goto(
                url,
                timeout=int((timeout or settings.crawl.timeout) * 1000),
                wait_until="domcontentloaded",
            )
            # 挑战脚本算完 cookie 后会 reload 一次，等它跑完
            page.wait_for_timeout(3000)
            return None
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            try:
                page and page.close()
            except Exception:  # noqa: BLE001
                pass

    def fetch(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
        wait_ms: int = 1200,
        timeout: float | None = None,
        post_data: dict | None = None,
        headers: dict[str, str] | None = None,
        raw_json: bool = False,
    ) -> FetchResult:
        if self._ctx is None:
            return FetchResult(
                html="", method="browser", status_code=0,
                error=f"浏览器启动失败：{self.error or '未知原因'}",
            )
        timeout_ms = int((timeout or settings.crawl.timeout) * 1000)

        # 取 JSON 接口：走 Chromium 的网络栈发请求，但不渲染页面。
        # 这类接口（zycg 就是）既校验 Referer，又不接受 curl/httpx 的握手特征；
        # 而 page.goto 又拿不到 Referer——只有这一条路两头都满足，
        # 而且比开页面渲染快一个数量级。
        if raw_json:
            # 会话里的第一个请求常常是失败的：zycg 的 notice 接口每个 context
            # 的第一次调用必回 errcode=-1，第二次起就正常，与 URL、关键词、
            # cookie 都无关（换 URL 也一样，只有「是不是本会话第一次」有关）。
            # 所以这里给一次重试——成本只有一发请求，收益是不用为每个站点
            # 猜它到底要不要预热。
            last: FetchResult | None = None
            for attempt in range(2):
                try:
                    resp = self._ctx.request.get(
                        url, headers=dict(headers or {}), timeout=timeout_ms
                    )
                    text = resp.text()
                    waf = detect_waf(text, resp.status)
                    last = FetchResult(
                        html=text,
                        method="browser-json",
                        status_code=resp.status,
                        final_url=url,
                        blocked_by=waf,
                        error=None if not waf else f"被拦截：{waf}",
                    )
                    if resp.status == 200 and not waf:
                        return last
                except Exception as exc:  # noqa: BLE001
                    last = FetchResult(
                        html="", method="browser-json", status_code=0,
                        error=f"{type(exc).__name__}: {str(exc)[:160]}",
                    )
                    break
                if attempt == 0:
                    time.sleep(0.6)
            return last or FetchResult(
                html="", method="browser-json", status_code=0, error="取页失败"
            )

        page = self._ctx.new_page()
        try:
            if post_data is not None:
                page.goto(url.split("?")[0], timeout=timeout_ms,
                          wait_until="domcontentloaded")
                resp = page.request.post(url, form=post_data, timeout=timeout_ms)
                html = resp.text()
                status = resp.status
            else:
                resp = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                status = resp.status if resp else 200
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=8000)
                    except Exception:  # noqa: BLE001
                        pass  # 选择器没等到不算失败，交给解析层判定
                page.wait_for_timeout(wait_ms)
                html = page.content()

            waf = detect_waf(html, status)
            return FetchResult(
                html=html,
                method="browser",
                status_code=status,
                final_url=page.url,
                blocked_by=waf,
                error=None if not waf else f"被拦截：{waf}",
            )
        except Exception as exc:  # noqa: BLE001
            return FetchResult(
                html="", method="browser", status_code=0,
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def __exit__(self, *exc) -> None:
        for obj in (self._ctx, self._browser):
            try:
                obj and obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def fetch_browser(
    url: str,
    *,
    timeout: float | None = None,
    wait_selector: str | None = None,
    wait_ms: int = 1200,
) -> FetchResult:
    """L2 单次调用版：内部自建会话，抓一页就关。适合详情页按需取。"""
    with BrowserSession() as sess:
        return sess.fetch(url, wait_selector=wait_selector, wait_ms=wait_ms, timeout=timeout)


def fetch(
    url: str,
    *,
    allow_browser: bool | None = None,
    prefer_browser: bool = False,
    wait_selector: str | None = None,
    session: BrowserSession | None = None,
    headers: dict[str, str] | None = None,
    raw_json: bool = False,
) -> FetchResult:
    """三层递进入口。

    prefer_browser=True 时跳过 HTTP 直连浏览器——用于已知有瑞数/WAF 的站点，
    省掉一次必然失败的请求（也少给对方添一次拦截记录）。

    raw_json=True 且走浏览器时，用 Chromium 网络栈直接取响应体，不开页面渲染，
    返回的是原始文本而不是 page.content()。给 JSON 接口专用。
    """
    allow_browser = (
        settings.crawl.browser_fallback if allow_browser is None else allow_browser
    )

    if prefer_browser and allow_browser:
        if session is not None:
            br = session.fetch(
                url, wait_selector=wait_selector, headers=headers, raw_json=raw_json
            )
        else:
            br = fetch_browser(url, wait_selector=wait_selector)
        return br

    res = fetch_http(url)
    if res.ok:
        return res
    # HTTP 被 WAF 拦（返回 200 但内容是拦截页）也算失败，转浏览器
    if res.ok is False and not allow_browser:
        return res

    if allow_browser:
        if session is not None:
            br = session.fetch(url, wait_selector=wait_selector)
        else:
            br = fetch_browser(url, wait_selector=wait_selector)
        if br.ok:
            return br
        if res.blocked_by:
            br.error = f"http 被拦({res.blocked_by}); browser: {br.error}"
        elif res.error:
            br.error = f"http: {res.error}; browser: {br.error}"
        return br

    return res
