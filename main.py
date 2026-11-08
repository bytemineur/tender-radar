#!/usr/bin/env python
"""招标雷达 — 一键启动入口。

    python main.py                          # 监听 0.0.0.0:8000
    python main.py --port 8080              # 换端口
    python main.py --host 127.0.0.1         # 只本机可访问
    python main.py --reload                 # 改代码自动重启（仅开发用）
    PORT=8080 python main.py                # 也能用环境变量
    TR_PUBLIC_HOST=1.2.3.4 python main.py   # 启动 banner 里显示的对外地址

等价于：
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

这个入口存在的唯一理由是「少记一条命令」。真正的启动逻辑仍在
app/main.py（FastAPI 实例、生命周期、路由挂载），这里只负责把它跑起来。
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import urllib.request


def _default_host() -> str:
    return os.getenv("TR_HOST", "0.0.0.0")


def _default_port() -> int:
    raw = os.getenv("TR_PORT") or os.getenv("PORT") or "8000"
    try:
        return int(str(raw).strip())
    except ValueError:
        return 8000


def _outbound_ip() -> str:
    """拿本机出口网卡的地址。

    原理是往公网发一个 UDP 包（并不真的发出去），看内核选了哪块网卡。
    注意：这拿到的是**网卡地址**。云服务器上永远是 172.16/10/192.168 这类
    私网 IP——公网 IP 是云厂商 NAT 映射出来的，压根不存在于这台机器上。
    取不到就退回 127.0.0.1。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))  # 阿里 DNS，仅用于选路
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _is_private(ip: str) -> bool:
    """判断是不是内网/保留地址。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr.is_private or addr.is_loopback or addr.is_link_local


# 按顺序试，前面失败就换下一个。都是纯文本或 JSON 的轻量接口。
_PUBLIC_IP_SOURCES = (
    "https://ip.3322.net",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)


def _public_ip(timeout: float = 1.2) -> str:
    """探测本机出口的公网 IP，拿不到返回空串。

    云服务器的公网 IP 只存在于云厂商的 NAT 网关里，本机网卡上没有，socket
    查不出来，只能问外部服务。宁可少显示一行，也别显示一个错的地址让人
    照着调半天。

    - 设 TR_PUBLIC_HOST 就直接用那个值，跳过探测（最稳，也适合专线/多出口）
    - 设 TR_PUBLIC_IP_LOOKUP=false 可完全关掉探测（不想让本机往外发请求时）
    """
    forced = os.getenv("TR_PUBLIC_HOST", "").strip()
    if forced:
        return forced
    if os.getenv("TR_PUBLIC_IP_LOOKUP", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return ""

    # 空 ProxyHandler：绕开 http_proxy/https_proxy。服务器上常设了代理，
    # 走代理查到的可能是代理机自己的 IP，那就误导人了。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in _PUBLIC_IP_SOURCES:
        try:
            with opener.open(url, timeout=timeout) as resp:
                text = resp.read(128).decode("utf-8", "ignore").strip()
        except Exception:  # noqa: BLE001  探测失败不该影响启动
            continue
        if not text:
            continue
        # 有的源回 JSON 或带说明文字，取第一个像 IP 的片段
        for token in text.replace('"', " ").replace(",", " ").split():
            token = token.strip(" :{}[]")
            try:
                ipaddress.ip_address(token)
            except ValueError:
                continue
            if not _is_private(token):
                return token
    return ""


def _print_banner(host: str, port: int) -> None:
    """把访问地址打到终端。

    分开显示「内网」和「公网」：云服务器上这道区别最关键——网卡只有私网 IP，
    公网 IP 是云厂商 NAT 出去的，两者都能访问但适用场景完全不同（内网给同
    VPC 的机器用，公网要安全组放行）。

    统一走一次 print 并 flush=True：重定向到日志文件时 stdout 是全缓冲的，
    逐行 print 的横幅会卡在缓冲区里，进程被 kill 时整段丢失（日志里只剩
    uvicorn 的 INFO，看不到这几行地址）。
    """
    lines = ["", "=" * 56, "  招标雷达已启动", f"  本机访问：  http://127.0.0.1:{port}/"]
    bind_all = host in ("0.0.0.0", "", "::")

    if not bind_all:
        lines.append("  外部访问：  未开放（只监听本机，请用 Nginx 反代出去）")
    else:
        lan = _outbound_ip()
        wan = _public_ip()
        if lan and lan != "127.0.0.1":
            label = "内网访问" if _is_private(lan) else "外部访问"
            lines.append(f"  {label}：  http://{lan}:{port}/")
        if wan:
            lines.append(f"  公网访问：  http://{wan}:{port}/")
            lines.append(f"  控制台：    http://{wan}:{port}/app.html")
        else:
            lines.append("  公网访问：  未探测到")
            lines.append("              云服务器请用控制台的「弹性公网 IP / 公网 IP」；")
            lines.append("              或设 TR_PUBLIC_HOST=你的公网IP 后重启")
            if lan and lan != "127.0.0.1":
                lines.append(f"  控制台：    http://{lan}:{port}/app.html")
    lines += ["  按 Ctrl+C 停止", "=" * 56, ""]

    print("\n".join(lines), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="main.py", description="招标雷达 Tender Radar"
    )
    parser.add_argument(
        "--host",
        default=_default_host(),
        help="监听地址。默认 0.0.0.0（外部可访问）；调试用 127.0.0.1",
    )
    parser.add_argument(
        "--port", type=int, default=_default_port(), help="监听端口，默认 8000"
    )
    parser.add_argument(
        "--reload", action="store_true", help="改代码自动重启，仅开发用"
    )
    parser.add_argument(
        "--forwarded-allow-ips",
        default=os.getenv("TR_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        help="信任哪些上游代理的 X-Forwarded-* 头。默认只信本机的 Nginx；"
             "Nginx 在别的机器上时填它的内网 IP，别图省事填 * "
             "——那样任何人都能伪造来源 IP",
    )
    args = parser.parse_args()

    import uvicorn  # noqa: PLC0415  延迟导入：--help 时不必等依赖加载

    from app.main import app  # noqa: PLC0415

    _print_banner(args.host, args.port)

    # 直接传 app 对象，不传字符串：这样不依赖当前工作目录在 sys.path 里。
    # 需要 --reload 时才退回字符串形式（uvicorn 要求 reload 必须给导入路径）。
    target = "app.main:app" if args.reload else app

    uvicorn.run(
        target,
        host=args.host,
        port=args.port,
        # 单进程是硬要求：APScheduler 跑在应用进程里，多 worker 会重复触发；
        # 且 SQLite 写入全局排他，多进程容易 "database is locked"。
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
