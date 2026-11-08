# 招标雷达 · 安装部署说明

分两个阶段，各走各的：

- **第一部分**：三步跑起来，用 `http://IP:端口` 访问。（15 分钟）
- **第二部分**：加一层 Nginx，用 `https://域名` 对外。（等你准备好域名再做）

---

# 第 0 步 · 到底要传哪些文件

结论先说：**`.venv` 和 `data` 都不用传**，服务器上重新生成即可。两者加起来约 190MB，是纯本地产物。

## 必须传（缺一不可）

| 文件 / 目录 | 说明 |
| --- | --- |
| `app/` | 全部后端代码（含 `api/`、`crawler/`、`models.py`、`auth.py`、`config.py`、`main.py`） |
| `web/` | 前端页面，零构建、可直接托管（`index.html` 首页、`app.html` 应用页、`*.js`、`style.css`、`legal/`） |
| `configs/` | **站点采集规则**，尤其 `configs/sites/*.yaml`（13 个源）和 `configs/app.yaml`（管理员邮箱白名单就在这里） |
| `main.py` | 启动入口 |
| `requirements.txt` | 依赖清单 |

> `configs/` 最容易漏。少了它，服务能起来但一个数据源都没有；少了 `app.yaml`，管理员邮箱白名单失效，任何人都进不了后台。

## 建议传（部署用）

| 文件 / 目录 | 说明 |
| --- | --- |
| `deploy/` | `nginx.conf`、`tender-radar.service`、`check.sh`，第二部分配域名和 systemd 时要用到 |
| `scripts/` | 运维脚本，`backup_db.sh`、`check_sources.py`、`smoke_auth.py` 等 |
| `.env.example` | 服务器上 `cp .env.example .env` 后按注释改，比手写省事 |
| `README.md` / `DEPLOY.md` | 文档 |

## 不要传

| 文件 / 目录 | 原因 |
| --- | --- |
| `.venv/`（约 180MB） | **跨平台一定不能用**。里面的 `pyvenv.cfg` 写死了本机 Python 绝对路径，脚本里也是本机盘符。服务器上用 conda 新建环境 + `pip install -r requirements.txt` 重建 |
| `data/`（约 10MB+） | 运行时产物。首次启动会自动创建 `data/` 和 `data/raw/`，并自动建表。上传本地库反而可能因为 SQLite WAL 文件不完整而损坏 |
| `data/tender.db` 及 `-wal` / `-shm` | 数据库本体。要迁移**历史数据**时才单独传，且必须先停服务、确认 WAL 已合并（见下） |
| `.env` | 含密钥等本机配置，服务器应单独建 |
| `__pycache__/`、`*.pyc` | 字节码缓存 |
| `scripts/shots/` | 我调试时截的图，约 4.4MB，纯垃圾 |
| `data/_probe/`、`data/raw/` | 探测站点时留下的原始 HTML 快照，调试用，非运行必需 |
| `.git/`、`node_modules/` | 前端无构建，不需要 |

## 一键打出干净的部署包

在项目根目录执行（Git Bash / Linux / macOS 均可）：

```bash
cd tender-radar
tar -czf tender-radar-deploy.tar.gz \
  --exclude='.venv' --exclude='.git' \
  --exclude='data' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='.env' \
  --exclude='scripts/shots' \
  app web configs deploy scripts main.py requirements.txt .env.example
```

Windows 下用自带 tar（Win10 1803+ 就有）：

```powershell
tar -czf tender-radar-deploy.tar.gz --exclude=.venv --exclude=.git --exclude=data --exclude=__pycache__ --exclude=.env --exclude=scripts/shots app web configs deploy scripts main.py requirements.txt .env.example
```

打出后自检一下，确认没混进垃圾：

```bash
tar -tzf tender-radar-deploy.tar.gz | grep -E "\.venv|/data/|\.env$|shots/" && echo "❌ 有垃圾" || echo "✅ 干净"
tar -tzf tender-radar-deploy.tar.gz | grep -cE "configs/sites/.*\.yaml"   # 应为 13
```

## 如果想把本地已有的招标数据一起搬过去

不是拷贝 `data/` 目录，而是导出再导入。原因是 SQLite 的 WAL 模式：数据可能还躺在 `tender.db-wal` 里没合并进主库，只拷 `tender.db` 会丢最近的数据。

```bash
# ① 本机：先停掉服务，保证 WAL 已落盘
sqlite3 data/tender.db "PRAGMA wal_checkpoint(TRUNCATE);"

# ② 只拷主库这一个文件
scp data/tender.db user@server:/opt/tender-radar/data/tender.db

# ③ 服务器上确认可读
sqlite3 data/tender.db "SELECT COUNT(*) FROM tenders;"
```

没装 `sqlite3` 命令也能做，用 Python 一行：`python -c "import sqlite3;c=sqlite3.connect('data/tender.db');c.execute('PRAGMA wal_checkpoint(TRUNCATE)');print('ok')"`。

## 两个库：业务库与账号库是分开的

```
data/tender.db  业务库  sources / tenders / crawl_runs     ← 采集线程独占写
data/auth.db    账号库  users / login_codes / auth_sessions / page_views
```

**为什么要拆**：SQLite 的写锁是整库一把。采集一轮要写几百条、事务能开好几分钟，这期间它一直抱着业务库的写锁；而登录每个请求都要写 `auth_sessions`，两边撞在同一把锁上就是 `database is locked` → 登录接口 500。拆开后各有一把锁，采集再忙也碰不到登录。

- 新部署什么都不用做，启动时自动建两个库。
- **老库升级不用手动迁移**：启动时会自动把混在 `tender.db` 里的 `users / login_codes / auth_sessions / page_views` 复制到 `auth.db`，**核对行数一致后才删源表**（对不上就保留源表并打日志告警）。日志里会看到「旧账号表已迁出业务库」。
- 想换账号库位置：`TR_AUTH_DATABASE_URL=sqlite:////opt/tender-radar/data/auth.db`。切 PostgreSQL 时不用拆（行级锁没这问题），不配这个变量就会自动沿用主库。
- 备份用户数据只需一个文件：`cp data/auth.db auth-$(date +%F).db`。WAL 模式下记得先 `PRAGMA wal_checkpoint(TRUNCATE)`。

## 实测结论

我把**只有** `app/ web/ configs/ main.py requirements.txt` 五项的干净目录丢到临时路径启动，`python main.py` 一次跑通：

- 自动创建 `data/` 与 `data/raw/`
- 自动建表，日志显示「从 configs/sites 同步了 33 个新来源」
- `/api/health` 返回 `sources: 33`（33 个在采 + `ztbpub` 默认停用，共 34 份规则）
- 管理员邮箱登录 → `role: "admin"`；其他邮箱 → `role: "user"`
- `/` 与 `/app.html` 均 200

也就是说，服务器上一份空库 + 13 个站点规则，跑起来后自己抓数据，不需要你本地那条 5.4MB 的库。

---

# 第一部分：三步跑起来

## 第 1 步 · 安装 conda

推荐 **Miniconda**（只有 100MB 左右，Anaconda 带一堆用不上的东西）。

```bash
# 下载（国内服务器建议用清华镜像，快很多）
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
# 清华镜像备选：
# wget https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh

# 静默安装到 ~/miniconda3
bash Miniconda3-latest-Linux-x86_64.sh -b -p "$HOME/miniconda3"

# 写入 shell 配置，让 conda 命令可用
"$HOME/miniconda3/bin/conda" init bash
exec bash                      # 重新加载 shell（或断开重连）
```

验证：`conda --version` 能输出版本号就行。

> **国内 pip 加速**（可选，但能省不少时间）：
> ```bash
> pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
> ```

## 第 2 步 · 建环境 + 装依赖

```bash
cd /opt/tender-radar          # 换成你的代码目录

conda create -n tender-radar python=3.12 -y
conda activate tender-radar

pip install -r requirements.txt
```

**浏览器内核二选一**（中国政府采购网等站点有 WAF，纯 HTTP 请求一定被拦，必须要真浏览器）：

```bash
# 方案一（推荐）：装系统 Chrome，程序会自动复用，省掉 ~150MB 的 Chromium 下载
sudo apt install -y wget
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

# 方案二：用 playwright 自带的 Chromium
# playwright install --with-deps chromium
# 并在 .env 里加一行：TR_BROWSER_CHANNEL=bundled
```

> 装系统 Chrome 时若报依赖缺失，补一句 `sudo apt -f install -y`。

## 启动前：把 SMTP 配好（不配等于门禁形同虚设）

登录是**邮箱验证码**，不配 SMTP 会降级成开发模式：验证码只写日志、并由接口
回显给前端，谁都能登录。所以公网部署这一步不能跳。

```bash
cp .env.example .env
# 至少填这四项
TR_ADMIN_EMAILS=you@company.com      # 管理员邮箱，登录后才看得到「管理」后台
TR_SMTP_HOST=smtp.exmail.qq.com      # 腾讯企业邮箱
TR_SMTP_PORT=465
TR_SMTP_SSL=true                     # 465 → true；587 → false（STARTTLS）
TR_SMTP_USER=you@company.com
TR_SMTP_PASS=                        # 客户端专用密码 / 授权码，不是邮箱登录密码
```

两台最常见的坑：

1. **云服务器封出站 25 端口**（华为云/阿里云/腾讯云默认都封）。465/587 一般不受影响，
   所以别用 25。配完先跑一次真实发信验证，超时再怀疑端口。
2. **填了邮箱登录密码报 535**。腾讯企业邮箱开了「安全登录」后必须用
   **客户端专用密码**（设置 → 邮箱绑定 → 客户端专用密码）。

**验收标准只有一个：你真的收到了那封邮件。** 配完点登录界面上的「获取验证码」，
收不到就去看启动日志里 `发送登录验证码 → xxx（渠道 smtp）` 后面有没有报错——
发信失败会把原始错误直接抛出来，八成是上面两条之一。

## 第 3 步 · 启动

```bash
python main.py
```

会打印：

```
========================================================
  招标雷达已启动
  本机访问：  http://127.0.0.1:8000/
  内网访问：  http://172.31.8.161:8000/
  公网访问：  http://123.60.186.131:8000/
  控制台：    http://123.60.186.131:8000/app.html
  按 Ctrl+C 停止
========================================================
```

浏览器打开那个「公网访问」地址即可。首页是产品介绍，**点右上角「登录」→ 填邮箱 →
点「获取验证码」→ 去邮箱抄 6 位码**进入控制台。

### 为什么服务器上会同时出现「内网」和「公网」两个地址

不是重复，是两张不同的网。

云服务器（华为云 / 阿里云 / 腾讯云都一样）的机器上**只有一块网卡，它的地址是私网 IP**，比如 `172.31.8.161`、`10.0.0.12`、`192.168.1.20`。你在云控制台看到的 `123.60.186.131` 是云厂商的 NAT 网关映射出来的，**这个地址压根不存在于这台机器上**。

所以：

| 地址 | 谁访问得到 | 用途 |
| --- | --- | --- |
| `127.0.0.1` | 只有服务器自己 | 本机 curl 自测 |
| 私网 IP（`172.31.x` / `10.x` / `192.168.x`） | 同一个 VPC / 内网里的其他机器 | 内网压测、同机房的反代 |
| 公网 IP | 整个互联网 | 你自己和客户访问 |

程序的自检逻辑只能拿到「网卡地址」，所以它显示的必然是私网 IP——**这不是 bug，是物理事实**。公网 IP 只能去问外面，所以从这一版起启动时会自动探测一次（`ip.3322.net` → `ipify` → `ifconfig.me`，1.2 秒超时，失败就跳过，绝不拖慢启动）。

如果探测不准或者你有多出口 / 专线，直接写死：

```bash
# 方式一：临时指定
TR_PUBLIC_HOST=123.60.186.131 python main.py

# 方式二：写进 .env（推荐，长期生效）
echo 'TR_PUBLIC_HOST=123.60.186.131' >> .env

# 不想让程序往外发请求（内网离线环境）
echo 'TR_PUBLIC_IP_LOOKUP=false' >> .env
```

> banner 里的地址只是**给你看**的提示，不影响服务实际监听哪个地址。真正决定能不能从外面访问的是两件事：`--host` 是不是 `0.0.0.0`，以及云控制台的**安全组**有没有放行这个端口。

### 打不开时按顺序查这三样

```bash
# ① 服务在听吗？（应看到 0.0.0.0:8000）
ss -lntp | grep 8000

# ② 本机自己能通吗？
curl -s http://127.0.0.1:8000/api/health

# ③ 安全组放行了吗？
# 华为云：控制台 → 服务器详情 → 安全组 → 入方向 → 添加 TCP 8000
# 阿里云/腾讯云同理。这一步不通，外面永远连不上，跟代码无关。
```

### 常用启动参数

```bash
python main.py --port 8080              # 换端口
python main.py --host 127.0.0.1         # 只本机可访问（配完 Nginx 后用这个）
python main.py --reload                 # 改代码自动重启，仅开发用
PORT=8080 python main.py                # 环境变量也认
```

### 后台常驻（不想开着终端）

```bash
mkdir -p logs
nohup python main.py > logs/app.log 2>&1 &
echo $! > logs/app.pid          # 记下 PID

# 停止
kill "$(cat logs/app.pid)"
```

> 临时用 `nohup` 可以，长期跑建议配 systemd，见第三部分。

## 第 4 步 · 验证

```bash
curl http://127.0.0.1:8000/api/health
# 期望看到 {"status":"ok","db":"sqlite","total":...,...}

curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/tenders
# 期望 401（未登录不给数据，说明门禁生效）
```

浏览器自查：

- [ ] 首页三个数字有值（不是 `—`）
- [ ] 填管理员邮箱登录后，顶栏显示「管理员」+「管理」标签
- [ ] 换一个别的邮箱登录，**看不到**「管理」标签
- [ ] 「来源」页点一次「采集全部」，看是否有新增

---

# 第二部分：用域名对外服务（加 Nginx）

到这一步才需要域名。做完之后访问方式从 `http://IP:8000` 变成 `https://域名`。

## 2.1 两个前置条件（卡住的人最多）

### ① 域名必须备案（仅限大陆服务器）

服务器在阿里云 / 腾讯云 / 华为云等**中国大陆**机房，域名想通过 80/443 访问，**必须完成 ICP 备案**。

没备案的典型现象：

- 服务器本机 `curl` 一切正常；
- 外网访问就是超时，且**没有任何报错信息**；
- 换个端口（比如 8000）反而能通——因为运营商只拦 80/443。

备案在域名注册商那边提交，通常几天到二十天。**建议先提备案再买服务器**，否则机器空转计费。

> 服务器在中国香港 / 境外不需要备案，但访问国内政务站点时出口是境外 IP，被风控概率明显更高。做国内政采采集还是推荐大陆机器。

### ② DNS 解析

在域名解析商处加一条 A 记录：

```
类型      A
主机记录  tender
记录值    <你的服务器公网 IP>
TTL       600
```

生效后确认：

```bash
ping tender.example.com        # 应解析到你的服务器 IP
```

## 2.2 安装 Nginx 与 Certbot

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

## 2.3 先只放 80 端口配置（为了签发证书）

**这一步有顺序讲究**：证书文件此刻还不存在，如果直接套用带 443 的完整配置，
`nginx -t` 会报 `cannot load certificate ... No such file or directory`。

```bash
sudo tee /etc/nginx/conf.d/tender-radar.conf > /dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name tender.example.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type "text/plain";
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
EOF

# Debian/Ubuntu 的默认站点会抢占 80 端口
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t && sudo systemctl reload nginx
```

## 2.4 签发证书

```bash
sudo certbot certonly --webroot -w /var/www/html -d tender.example.com
```

成功会输出证书路径：

```
/etc/letsencrypt/live/tender.example.com/fullchain.pem
/etc/letsencrypt/live/tender.example.com/privkey.pem
```

## 2.5 换成完整 HTTPS 配置

项目里已经备好：

```bash
sudo cp deploy/nginx.conf /etc/nginx/conf.d/tender-radar.conf
sudo sed -i 's/tender\.example\.com/你的域名/g' /etc/nginx/conf.d/tender-radar.conf
sudo nginx -t && sudo systemctl reload nginx
```

> Nginx < 1.25.1 会报 `unknown directive "http2"`。
> 把 `http2 on;` 删掉，并把 `listen 443 ssl;` 改成 `listen 443 ssl http2;`。

## 2.6 把应用改成只监听本机（重要）

加完 Nginx 之后，**8000 端口一定要收回来**，否则别人可以绕过 Nginx 直连应用。

```bash
python main.py --host 127.0.0.1
```

然后防火墙只放行 80/443：

```bash
sudo ufw allow 22/tcp          # 别把自己锁在外面
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

> **云服务器还要在厂商控制台的安全组里放行 80/443**。
> 只配 ufw 不放安全组，是「配完了还是访问不了」最常见的原因。

## 2.7 证书自动续期

```bash
systemctl status certbot.timer        # 应显示 active (waiting)
sudo certbot renew --dry-run          # 干跑一遍确认能续
```

---

# 第三部分：长期运行（可选但建议）

`nohup` 跑着，服务器重启就没了。正式用建议交给 systemd。

## 3.1 建运行用户

```bash
sudo useradd -r -s /usr/sbin/nologin tender
sudo mkdir -p /opt/tender-radar/data/raw
sudo chown -R tender:tender /opt/tender-radar
```

> 如果用 conda，把 `deploy/tender-radar.service` 里的 `ExecStart`
> 改成 `/home/<你的用户>/miniconda3/envs/tender-radar/bin/python main.py`，
> 并把 `User=` 改成你自己的用户名（conda 装在个人目录下，root 或别的用户拿不到）。

## 3.2 装服务单元

```bash
sudo cp deploy/tender-radar.service /etc/systemd/system/
sudo chmod 644 /etc/systemd/system/tender-radar.service
sudo systemctl daemon-reload
sudo systemctl enable --now tender-radar
sudo systemctl status tender-radar          # 期望 active (running)
```

常用命令：

```bash
sudo journalctl -u tender-radar -f                  # 实时日志
sudo systemctl restart tender-radar                 # 改完代码/配置必须重启
sudo systemctl stop tender-radar
```

> ⚠️ **改了 Python 代码或 `configs/*.yaml` 必须重启**。前端 HTML/CSS/JS 是每次请求
> 现读磁盘所以立即生效，Python 是进程启动时加载进内存的——这个差异常让人误以为改坏了。

## 3.3 备份

```bash
chmod +x scripts/backup_db.sh
crontab -e
# 每天凌晨 3:30 备份
30 3 * * * /opt/tender-radar/scripts/backup_db.sh >> /var/log/tender-backup.log 2>&1
```

**恢复**：停服务 → 备份文件覆盖 `data/tender.db` → 删除同目录 `tender.db-wal` 和 `tender.db-shm` → 启服务。

> `data/` 不要放 NFS 等网络文件系统：SQLite 的 WAL 依赖可靠文件锁，网络盘会损坏数据库。

## 3.4 升级

```bash
cd /opt/tender-radar
cp data/tender.db "data/tender.db.bak.$(date +%F)"

git pull
conda activate tender-radar
pip install -q -r requirements.txt

sudo systemctl restart tender-radar
sudo journalctl -u tender-radar -n 50
```

---

# 附录

## A. 第一次部署别忘了配管理员

```bash
cp .env.example .env
chmod 600 .env
```

确认这行是你的邮箱，且**全小写**：

```bash
TR_ADMIN_EMAILS=zhanghuihua868@outlook.com
```

不在这个名单里的邮箱，登录进去一律是普通用户，看不到「管理」。

> `.env` 是本次新加的能力。以前代码只读系统环境变量，`.env` 文件其实不生效；
> 现在会在启动时自动加载（真实环境变量优先级更高）。

## B. 环境变量

| 变量 | 默认 | 说明 |
|:---|:---|:---|
| `TR_ADMIN_EMAILS` | — | 管理员邮箱，逗号分隔，**全小写**。漏配则无人可进后台 |
| `TR_REQUIRE_LOGIN` | `true` | 关掉 = 数据接口完全公开，**生产绝不能关** |
| `TR_HOST` / `TR_PORT` / `PORT` | `0.0.0.0` / `8000` | 监听地址与端口 |
| `TR_SCHEDULER_ENABLED` | `true` | 关掉后只提供 API，不自动采集 |
| `TR_SCHEDULER_INTERVAL_MIN` | `120` | 增量采集间隔（分钟） |
| `TR_MIN_DELAY` / `TR_MAX_DELAY` | `2.0` / `5.0` | 请求间隔随机范围（秒），**不要低于 1** |
| `TR_TIMEOUT` | `25` | 单请求超时（秒） |
| `TR_MAX_PAGES` | `5` | 单源单次最多翻页数 |
| `TR_BROWSER_CHANNEL` | `auto` | `auto`=复用系统 Chrome；`bundled`=playwright 自带；`none`=禁用浏览器 |
| `TR_CORS_ORIGINS` | 空=不启用 | 前后端分离时才填，**不要用 `*`** |
| `TR_PUBLIC_HOST` | 空=自动探测 | 启动 banner 里显示的对外地址，写死可跳过探测 |
| `TR_PUBLIC_IP_LOOKUP` | `true` | 关掉则不联网探测公网 IP（离线/内网环境用） |
| `TR_NOTIFY_ENABLED` / `TR_NOTIFY_WEBHOOK` | `false` / 空 | 采集失败推送（企业微信/飞书/Slack） |
| `TZ` | `Asia/Shanghai` | 影响截止日期计算，别乱改 |

优先级：**真实环境变量 > `.env` > `configs/app.yaml`**。

## C. 故障排查

### 端口被占用

```
ERROR: [Errno 98] Address already in use
```

换端口：`python main.py --port 8080`。
或找出占用者：`sudo lsof -i :8000`。

### 外网打不开，本机 curl 正常

按顺序查：

1. **云厂商安全组**没放行端口（最常见）；
2. 系统防火墙：`sudo ufw status`；
3. 应用没监听 `0.0.0.0`：`ss -lntp | grep 8000`，看到 `127.0.0.1:8000` 就是只绑了本机，
   启动时去掉 `--host 127.0.0.1`（或显式写 `--host 0.0.0.0`）。

### HTTPS 超时但 HTTP 的 8000 端口能通

**没备案**（大陆机器）。见 2.1。

### 改了代码没生效

重启服务。见 3.2 的提示框。

### 点「抓取」转圈后 504

长任务被 Nginx 掐断。确认 `deploy/nginx.conf` 里 `proxy_read_timeout 600s;` 没被改掉。

### 某个源一直抓不到

云机房 IP 被目标站限流的典型表现。用浏览器能打开、程序抓不到就是这回事。
**不要提高频率**，只会封更久；先在「来源」页停用该源。

### 登录后看不到「管理」标签

1. 邮箱是否**全小写**且写进了 `TR_ADMIN_EMAILS`；
2. 环境变量是否真注入到进程：
   `sudo cat /proc/$(pgrep -f uvicorn | head -1)/environ | tr '\0' '\n' | grep TR_ADMIN`
3. 改完重启服务；
4. Ctrl+Shift+R 硬刷新浏览器。

### 不登录也能看到数据（门禁失效）

先用一行命令确认：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/tenders
# 期望 401。返回 200 就是门禁没生效
```

返回 200 说明 `TR_REQUIRE_LOGIN` 实际是 false。检查 `.env`：

```bash
grep TR_REQUIRE_LOGIN .env
```

**最常见的原因是行尾注释**：写成 `TR_REQUIRE_LOGIN=true      # 只本地调试用`，
老版本解析器会把整行当值（`"true      # 只本地调试用"`），认不出来就当 false，
门禁被静默关掉——服务照常启动，日志一句都不提。

修法两种，改完都要重启：

```bash
# ① 临时：把注释挪到上一行，值单独一行
sed -i 's/^TR_REQUIRE_LOGIN=.*/TR_REQUIRE_LOGIN=true/' .env

# ② 彻底：更新到已修复解析器的版本（会把行尾注释正确剥掉）
```

顺便说一句：判断一个部署有没有这个毛病，最快的办法是看启动日志里有没有这句
`数据接口门禁已关闭（TR_REQUIRE_LOGIN=false）…`。新版会在门禁关闭时明确警告。

## D. 安全提醒（正式对外前必读）

| 项 | 状态 | 要做的事 |
|:---|:---|:---|
| 登录方式 | ⚠️ **填邮箱直接进，不校验凭证** | 任何人知道管理员邮箱就能进后台。收紧方法：`app/api/auth.py` 的 `login` 里加回 `consume_code(...)`，SMTP 代码整段保留着，前端把验证码输入框加回来即可 |
| 管理员邮箱 | ⚠️ 默认值 | 换成你自己的，全小写 |
| `TR_REQUIRE_LOGIN` | ✅ `true` | 保持，别关 |
| `.env` 权限 | ⚠️ | `chmod 600`，且别提交进 git |
| 8000 端口 | ⚠️ 配 Nginx 后 | 改成只绑 `127.0.0.1`，只放行 80/443 |
| 备份 | ⚠️ | 配 cron，并**验证过一次恢复流程** |
| `--workers` | ✅ 1 | 不能调高（APScheduler 会重复触发 + SQLite 锁冲突） |
| 政策文档 | ⚠️ | 《隐私政策》需与当前实现一致（已同步过一次） |

**用 IP + 端口裸跑适合内网测试。正式对外务必先做完上表，再加 Nginx。**
