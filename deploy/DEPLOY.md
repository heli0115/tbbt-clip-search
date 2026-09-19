# 部署手册（方案 ③：腾讯云轻量 · 境外 + Cloudflare R2）

> 决策与硬性约束见 `AGENTS.md` 第 10 节。**部署不需要改任何代码**，只改环境变量。

## 0. 总览

```
浏览器
  ├─ / （Vite 产物）                        → 腾讯云轻量（境外 · 东京）：nginx 直接 serve
  ├─ /api/search · /api/health              → 同一台 VPS：nginx → uvicorn(FastAPI) → tbbt.db
  └─ https://video.helilab.space/S01E01.mp4  → Cloudflare R2
```

前端与 API **同源**，所以没有 CORS 配置；视频**直连 R2**，不经过 VPS。

## 1. 先做的事（脚本代替不了）

| 步骤 | 要点 |
|---|---|
| 买境外轻量 VPS | 腾讯云/阿里云，**2核2G / 40GB SSD 起**，支付宝可付。**必须选境外地域**（免备案）——本项目实际用腾讯云轻量「境外通用套餐」**东京**节点 |
| 注册域名 | Cloudflare Registrar / Namecheap，约 $10/年。本项目：`helilab.space` |
| 注册 Cloudflare | 需国际信用卡（R2 用） |

## 2. R2：建桶 + 传视频

### 2.1 建桶并开放访问

Dashboard → R2 → Create bucket（本项目用的是 `thebong`）。

**不要用 `r2.dev` 子域对外**（有限速、仅供测试）。绑定自定义域名：

R2 → 桶 → Settings → Custom Domains → Connect Domain → `video.helilab.space`

### 2.2 取 S3 凭据

R2 → Manage R2 API Tokens → Create API token → 权限 **Object Read & Write**，记下
Account ID / Access Key ID / Secret Access Key。

### 2.3 上传 279 个 mp4

**凭据是会话级的**：`$env:...` 只对当前终端有效，**新开一个终端就没了**（本项目真实踩过：
新开终端跑上传，报「缺少桶名 / 缺少凭据环境变量」）。

```powershell
pip install boto3

# 三个值都从 Cloudflare → R2 → Manage R2 API Tokens 取（Secret 只在创建时显示一次）
$env:R2_ACCOUNT_ID        = "<Account ID>"
$env:R2_ACCESS_KEY_ID     = "<Access Key ID>"
$env:R2_SECRET_ACCESS_KEY = "<Secret Access Key>"
# 桶名不用设：upload_r2.DEFAULT_BUCKET 已是 thebong

# 干跑，确认清单与总量
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web" --dry-run

# 先传 S01 试水（17 集）
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web" --limit 17

# 全量 64 GB（10 Mbps 上行约 14 小时；中断后重跑即可续传）
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web"
```

### 2.4 验证 R2 侧

打开 `https://video.helilab.space/S01E01.mp4` 能播，并确认 **Range 生效**：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -r 0-1023 https://video.helilab.space/S01E01.mp4
# 期望 206 —— 前端 seek 到任意位置全靠它
```

> Media Fragment（`...#t=2.38,4.84`）在桌面 Chrome 里也能直接验，**但站点前端并不用它**：
> 移动端实测不可靠，定位改由 JS 控制（见 AGENTS.md 第 4 节）。所以这里只需看 **Range 是否 206**。

### 2.5 生成并上传卡片缩略图

卡片缩略图 = **每条台词起点那一帧**（AGENTS.md 第 4 节决策 5）；另有每集封面作兜底：

```bash
# ① 每条台词的缩略图 —— 先跑一季试水（S01 约 1 分钟）
python -m pipeline.cue_covers "D:\study\生活大爆炸\视频素材\web" -o "D:\study\生活大爆炸\视频素材\web\cues" --season 1

# ② 全剧 117842 条（约 95 分钟 / 826 MB；中断后重跑即可续）
python -m pipeline.cue_covers "D:\study\生活大爆炸\视频素材\web" -o "D:\study\生活大爆炸\视频素材\web\cues"

# ③ 每集封面（兜底用，279 张约 2 分钟）
python -m pipeline.covers "D:\study\生活大爆炸\视频素材\web" -o "D:\study\生活大爆炸\视频素材\web\covers"

# ④ 上传：分别走 cues/ 与 covers/ 前缀
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web\cues" --suffix .jpg --key-prefix cues
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web\covers" --suffix .jpg --key-prefix covers
```

全部是断点续跑：中断后重跑即可，已生成的会自动跳过。
**只配一个 `TBBT_VIDEO_BASE`**：后端据此同时拼出视频、缩略图与兜底封面的 URL。

## 3. VPS：部署服务

### 3.1 装系统依赖

```bash
sudo apt update && sudo apt install -y python3-venv nginx
sudo useradd -r -s /usr/sbin/nologin tbbt
sudo mkdir -p /opt/tbbt /var/www/tbbt
sudo chown -R tbbt:tbbt /opt/tbbt
```

### 3.2 拷代码与数据库

**只拷两样：代码 + `tbbt.db`（约 56 MB）。视频不上服务器**（AGENTS.md 第 10 节约束 5）。

```bash
# 在本机项目根目录执行
rsync -av --exclude='__pycache__' --exclude='.pytest_cache' \
      backend pipeline conftest.py user@<vps-ip>:/tmp/tbbt-src/
scp tbbt.db user@<vps-ip>:/tmp/tbbt-src/

# 在 VPS 上
sudo cp -r /tmp/tbbt-src/* /opt/tbbt/
sudo chown -R tbbt:tbbt /opt/tbbt

cd /opt/tbbt
sudo -u tbbt python3 -m venv .venv
sudo -u tbbt .venv/bin/pip install fastapi "uvicorn[standard]"
```

### 3.3 配 systemd

```bash
sudo cp deploy/tbbt-api.service /etc/systemd/system/
sudo nano /etc/systemd/system/tbbt-api.service     # 本项目：TBBT_VIDEO_BASE=https://video.helilab.space
sudo systemctl daemon-reload
sudo systemctl enable --now tbbt-api

systemctl status tbbt-api
curl -s localhost:8000/api/health                  # {"status":"ok","clips":117842}
```

### 3.4 配 nginx

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/tbbt
sudo nano /etc/nginx/sites-available/tbbt          # 本项目：server_name helilab.space;
sudo ln -s /etc/nginx/sites-available/tbbt /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 4. 前端：构建并发布

Vite 产物是纯静态，**在本机构建**（VPS 上不需要 Node）：

```bash
cd frontend
npm run build
scp -r dist/* user@<vps-ip>:/tmp/tbbt-dist/

# 在 VPS 上
sudo cp -r /tmp/tbbt-dist/* /var/www/tbbt/
sudo chown -R www-data:www-data /var/www/tbbt
```

### 4.1 只更新代码（增量发布）

**改了源码 ≠ 线上生效**：`/var/www/tbbt` 是构建产物、`/opt/tbbt` 是另一份代码拷贝，
两者都不在仓库里。本项目真实踩过：前端改完、R2 缩略图也传好了，刷新线上页面**还是旧界面**
（跨域看着像没生效，其实只是没部署）。

后端本次只动了 **`backend/main.py`**（新增 `cover` / `poster` 字段与 `/v/cues/`），
`pipeline/`、`requirements.txt`、`tbbt.db` 都没变 —— 所以不必重传 54 MB 的部署包。

```powershell
# 本地：构建前端 + 只传变化的文件（<vps-ip> 换成实际地址）
Set-Location "D:\study\生活大爆炸\生活大爆炸\frontend"
npm run build

Set-Location "D:\study\生活大爆炸\生活大爆炸"
scp -r frontend\dist\* ubuntu@<vps-ip>:/tmp/tbbt-dist/
scp backend\main.py     ubuntu@<vps-ip>:/tmp/
```

```bash
# VPS 上：落位 + 重启（前端产物换属主为 www-data，后端代码换属主为 tbbt）
sudo cp -r /tmp/tbbt-dist/* /var/www/tbbt/ && sudo chown -R www-data:www-data /var/www/tbbt
sudo cp /tmp/main.py /opt/tbbt/backend/main.py && sudo chown tbbt:tbbt /opt/tbbt/backend/main.py
sudo systemctl restart tbbt-api && systemctl status tbbt-api --no-pager | head -5
```

验证（**R2 那条必须带浏览器 UA**，否则 Cloudflare 会返回 `403 error code: 1010`，
见 AGENTS.md 第 9 节）：

```bash
curl -s https://helilab.space/api/search?q=photon\&limit=1 | head -c 300
#   → 应包含 "cover": "https://video.helilab.space/cues/S01E01_0001.jpg"
#           与 "poster": "https://video.helilab.space/covers/S01E01.jpg"

curl -s -A "Mozilla/5.0" -o /dev/null -w "%{http_code}\n" https://video.helilab.space/cues/S01E01_0001.jpg   # 200
```

改完浏览器要 **Ctrl+F5 强刷**（`index.html` 设了 no-cache，但浏览器里的 `dist/assets/*.js` 名字带哈希，一般会自动换新）。

## 5. HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d helilab.space
```

certbot 会自动补上 443 段与 HTTP 跳转。

## 6. 验收清单

> 对应 2026-09-19 的上线（域名 `helilab.space`）。**已复验**：R2 的 `Range → 206`、`http://` 一律 **301 → `https://`**。
> 带 https 的项未能从开发机复验——该机 curl 的出站 TLS 被 sandbox 拦掉（连 `https://www.baidu.com` 都返回 `000`）。

- [ ] `curl https://helilab.space/api/health` → `{"status":"ok","clips":117842}`
- [ ] 打开首页搜「狭缝」→ **4 条**
- [ ] 点第一条 → 播放器从 **2.38s 播到 4.84s 自动暂停**
- [ ] 开发者工具 Network：视频请求打到 **`video.helilab.space`（R2）**，不是 `helilab.space`
- [ ] `curl -H "Range: bytes=0-99" -I https://video.helilab.space/S01E01.mp4` → **206**
- [ ] `curl -I https://video.helilab.space/cues/S01E01_0001.jpg` → **200** + `content-type: image/jpeg`
- [ ] `curl -I https://video.helilab.space/covers/S01E01.jpg` → **200**（兜底封面）
- [ ] 首页结果卡片显示**各自不同的**缩略图（不是同集共用一张、也不是灰块）
- [ ] `https://helilab.space/robots.txt` → `Disallow: /`
- [ ] `https://helilab.space/v/S01E01.mp4` → **404**（证明 VPS 没在代理视频）

## 7. 常见坑

| 现象 | 原因 |
|---|---|
| 播放器转圈不出画面 | `TBBT_VIDEO_BASE` 没设或写错 → video 字段指向 VPS 的 `/v/`（被 nginx 404） |
| 搜到但点了没反应 | 后端没起：`journalctl -u tbbt-api -f` |
| 视频偶尔卡顿 | 走 Cloudflare 线路的固有代价；后续可考虑国内 CDN 回源（但要备案） |
| 担心被刷流量 | R2 出网免费，不必担心；只有 Class B 请求费 $0.36/百万次 |
| `scp/rsync` 报中文路径错 | Windows 上用引号包裹路径；或改用 WinSCP |

## 8. 版权缓解（AGENTS.md 第 7 节）

- 站点定位为**英语学习 / 教学**
- `robots.txt` 禁止爬虫索引（已随前端产物提供）
- 分享链接使用不可猜测的 token（M5 实现）
- 准备 DMCA 下架流程：收到投诉即撤对应剧集
