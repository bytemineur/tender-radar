# 招标雷达 — 生产镜像
# 单进程同时提供 API + Web，最小化运维面。
FROM python:3.12-slim

# 容器里没有本机 Chrome/Edge，用 playwright 自带的 Chromium
# （注释必须独立成行：写在 ENV 的续行里会被当成值的一部分或被判为非法指令）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    TR_BROWSER_CHANNEL=bundled \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 系统依赖：
#   libxml2/libxslt  — lxml 运行时
#   chromium 依赖库  — playwright 装的 Chromium 需要这批 .so
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 ca-certificates \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 装 Chromium 及其系统依赖。这一步比较大（~400MB），但 ccgp 这类
# 站点有 WAF，纯 HTTP 一定拿不到数据，没有浏览器等于没有来源。
# 注意：不要给 Playwright 设 npmmirror 镜像，它没有新版 Chrome for Testing
# 的目录布局，会 404（CaseBoard 踩过这个坑）。
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY web ./web
COPY configs ./configs
COPY scripts ./scripts

# 数据目录（SQLite + 原文快照）。生产建议挂载为 volume。
RUN mkdir -p /app/data/raw

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
