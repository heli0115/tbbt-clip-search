# 部署手册（方案 ③：香港轻量 VPS + Cloudflare R2）

> 决策与硬性约束见 `AGENTS.md` 第 10 节。**部署不需要改任何代码**，只改环境变量。

## 0. 总览

```
浏览器
  ├─ / （Vite 产物）                        → 香港轻量 VPS：nginx 直接 serve
  ├─ /api/search · /api/health              → 同一台 VPS：uvicorn(FastAPI) → tbbt.db
  └─ https://video.<域名>/S01E01.mp4#t=a,b   → Cloudflare R2
```

前端与 API **同源**，所以没有 CORS 配置；视频**直连 R2**，不经过 VPS。

## 1. 先做的事（脚本代替不了）

| 步骤 | 要点 |
|---|---|
| 买香港轻量 VPS | 腾讯云/阿里云，**2核2G / 40GB SSD 起**，支付宝可付。务必选**香港地域**（免备案） |
| 注册域名 | Cloudflare Registrar / Namecheap，约 $10/年 |
| 注册 Cloudflare | 需国际信用卡（R2 用） |

## 2. R2：建桶 + 传视频

### 2.1 建桶并开放访问

Dashboard → R2 → Create bucket（如 `tbbt-video`）。

**不要用 `r2.dev` 子域对外**（有限速、仅供测试）。绑定自定义域名：

R2 → 桶 → Settings → Custom Domains → Connect Domain → `video.<你的域名>`

### 2.2 取 S3 凭据

R2 → Manage R2 API Tokens → Create API token → 权限 **Object Read & Write**，记下
Account ID / Access Key ID / Secret Access Key。

### 2.3 上传 279 个 mp4

```bash
pip install boto3

set R2_ACCOUNT_ID=<Account ID>
set R2_ACCESS_KEY_ID=<Access Key ID>
set R2_SECRET_ACCESS_KEY=<Secret Access Key>
set R2_BUCKET=tbbt-video

# 干跑，确认清单与总量
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web" --dry-run

# 先传 S01 试水（17 集）
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web" --limit 17

# 全量 64 GB（10 Mbps 上行约 14 小时；中断后重跑即可续传）
python -m pipeline.upload_r2 "D:\study\生活大爆炸\视频素材\web"
```

### 2.4 验证 R2 侧

打开 `https://video.<你的域名>/S01E01.mp4` 能播；再验 Media Fragment：

```
https://video.<你的域名>/S01E01.mp4#t=2.38,4.84
```

应**自动从 2.38s 播到 4.84s 后暂停**。这一步过了，说明 R2 的 Range 与 Media Fragment 都正常。

## 3. VPS：部署服务

### 3.1 装系统依赖

```bash
sudo apt update && sudo apt install -y python3-venv nginx
sudo useradd -r -s /usr/sbin/nologin tbbt
sudo mkdir -p /opt/tbbt /var/www/tbbt
sudo chown -R tbbt:tbbt /opt/tbbt
```

### 3.2 拷代码与数据库

**只拷两样：代码 + `tbbt.db`（54 MB）。视频不上服务器**（AGENTS.md 第 10 节约束 5）。

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
sudo nano /etc/systemd/system/tbbt-api.service     # 改 TBBT_VIDEO_BASE 为你的 R2 域名
sudo systemctl daemon-reload
sudo systemctl enable --now tbbt-api

systemctl status tbbt-api
curl -s localhost:8000/api/health                  # {"status":"ok","clips":117842}
```

### 3.4 配 nginx

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/tbbt
sudo nano /etc/nginx/sites-available/tbbt          # 改 server_name
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

## 5. HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d tbbt.<你的域名>
```

certbot 会自动补上 443 段与 HTTP 跳转。

## 6. 验收清单

- [ ] `curl https://tbbt.<域名>/api/health` → `{"status":"ok","clips":117842}`
- [ ] 打开首页搜「狭缝」→ **4 条**
- [ ] 点第一条 → 播放器从 **2.38s 播到 4.84s 自动暂停**
- [ ] 开发者工具 Network：视频请求打到 **`video.<域名>`（R2）**，不是 `tbbt.<域名>`
- [ ] `curl -H "Range: bytes=0-99" -I https://video.<域名>/S01E01.mp4` → **206**
- [ ] `https://tbbt.<域名>/robots.txt` → `Disallow: /`
- [ ] `https://tbbt.<域名>/v/S01E01.mp4` → **404**（证明 VPS 没在代理视频）

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
