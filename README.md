# TBBT 台词搜索

> 关键词搜台词 → 中英双语结果 → 精确播放对应片段 → 一键分享

把《生活大爆炸》全 12 季 279 集变成**可搜索、可精确播放、可分享**的英语学习素材库。

🔗 **在线演示**：https://helilab.space

---

## 它解决什么问题

想用美剧学英语时，最想要的是这样一句话：「帮我找出所有说 *bazinga* 的地方，我要听原声怎么念的。」

现有工具要么只能给文本（搜不到声音），要么要求你先把整集下下来自己拖进度条。这个项目把中间的活全干了：

- **搜**：输入任意关键词（中文、英文、两字词都行），命中 11.8 万条台词
- **看**：中英对照，标注季集号与时间码
- **听**：点开即在浏览器里**精确播放那一句**（起止精确到毫秒），不用下整集
- **分享**：生成一条不可猜测的链接，发给朋友直接就能看那个片段

---

## 技术亮点

### 1. 播片段不用切片 —— 靠 Media Fragment URI

传统做法是每次播放都调 ffmpeg 实时切片，或者预生成成千上万个小文件。这里都不用：

```html
<video src="https://video.example.com/S01E01.mp4#t=754.2,761.8">
```

`#t=start,end` 是浏览器**原生支持**的媒体片段语法。实测结论（真实 Chrome）：

- 起点定位精确到毫秒（`#t=2.38` → `currentTime=2.380s`）
- **end 边界浏览器原生遵守**：播到 4.84s 自动 `paused=true`，前端**不需要写任何暂停逻辑**

代价只有一个：静态服务必须支持 **HTTP Range（206）**。这一点在 Cloudflare R2 上原生满足，而 Python 自带的 `http.server` 不支持（所以仓库里有 `tools/serve_range.py` 用于本地验证）。

### 2. 中文搜索：FTS5 trigram + LIKE 降级

索引用 SQLite FTS5 配 `trigram` tokenizer —— 中文无需分词即可做子串匹配，零运维、单文件。

但实测（SQLite 3.45.1）发现一个死角：**trigram 只能匹配 ≥ 3 个字符的查询**，`光子`（2 汉字）、`ph` 都搜不到。

所以 `search()` 在查询长度 < 3 时**自动降级为 `LIKE '%q%'`**：

```python
if len(keyword) >= MIN_FTS_QUERY_LEN:
    # FTS5 trigram，按相关度排序
    ...
# 兜底：中文两字词绝不能成为搜索盲区
pattern = f"%{keyword}%"
```

这条兜底不可省 —— 否则「狭缝」「谢尔顿」这类**两字词全部搜不到**。

### 3. 视频出网流量 $0

视频托管在 Cloudflare R2（**出网流量免费**），并且**由浏览器直连**，不经过自己的服务器：

```
浏览器 ──┬─ /             → 小 VPS：nginx → uvicorn → SQLite
         └─ video.<域名>  → Cloudflare R2（64 GB 视频，出网 $0）
```

这样做的收益是双重的：**流量费恒为 0**，且**并发数不受服务器带宽限制**（2 核 2G 的小机器也能撑住）。

实现上只靠一个环境变量：

```bash
TBBT_VIDEO_BASE=https://video.example.com   # 后端据此拼出 video 字段
```

本地开发时默认 `/v`（同源，走 FastAPI 自己的 Range 服务），线上切到 R2 域名 —— **同一份代码，两种形态**。

### 4. 关键选型都有实测数据支撑

**转码配方**（VMAF 实测，S01E01 前 60 秒）：

| 参数 | 体积 | VMAF | 结论 |
|---|---|---|---|
| 源 HEVC 10bit 1080p | 600 MB | 参照 | 浏览器播不了，必须转 |
| 1080p @ cq24 | 1103 MB | 77.9 | ❌ 体积 4 倍，画质无差 |
| 1080p @ 2.5 Mbps | 418 MB | 75.7 | 全屏场景才需要 |
| 720p @ 2.5 Mbps | 420 MB | 79.9 | 已饱和（+58% 体积换 +1.1 分） |
| **720p @ 1.5 Mbps** | **266 MB** | **78.8** | ✅ **定案（拐点）** |

**核心结论：VMAF 天花板 ≈ 79，与码率、编码格式、色深均无关。** 所以 1.5 Mbps 就是拐点，继续加码率或换 AV1 都是无效投入。全剧 279 集共 64.22 GB。

**云存储成本对比**（每月）：

| | Cloudflare R2 | 国内 OSS/COS + CDN |
|---|---|---|
| 存储 64 GB | **≈ $0.8** | ≈ ¥8 |
| 出网流量 | **$0** | ¥0.5/GB（直连）/ ¥0.21–0.24（CDN） |
| 备案 | 不需要 | **必须 ICP 备案** |

对一个「读多写少 + 出网敏感」的视频站，R2 的免费出网是压倒性的。

---

## 架构

```
【数据管线 · Python 一次性脚本】
视频(mkv) ─┬─→ ffmpeg 提取字幕轨 → SRT
           └─→ NVENC 转码 720p H.264 + faststart → mp4
                        ↓
              parse_srt.py（清洗 / 中英拆分 / 时间码解析）
                        ↓
              clips 表 (season, episode, cue_index, start_ms, end_ms, zh, en)
                        ↓
              SQLite FTS5 (trigram) ──→ search()

【服务层 · FastAPI】
  /api/health           健康检查
  /api/search?q=&limit= 关键词 → 台词列表（含 video 字段）
  /api/share           生成分享 token
  /api/share/{token}   按 token 取片段
  /v/{file}            本地开发用，整集视频 + Range(206)

【前端 · Vite + React + Tailwind】
  搜索框 → 结果列表（双语/剧集/时间码）→ 片段播放器 → 分享链接

【托管】
  前端 + API → 境外轻量 VPS（nginx 同源托管，无 CORS）
  视频       → Cloudflare R2（浏览器直连）
```

---

## 目录结构

```
backend/            FastAPI 服务层（搜索 / 分享 / 视频 Range）
frontend/           Vite + React + Tailwind 前端
pipeline/           数据管线
  parse_srt.py        双语 SRT → 结构化 Cue（含清洗规则）
  store.py            SQLite + FTS5 索引，提供 search() / create_share()
  batch.py            按季批量：提取字幕 → 解析 → 入库（断点续跑）
  transcode.py        按季批量转码（720p/H.264/1.5Mbps 定案配方）
  upload_r2.py        批量上传到 R2（断点续传 / 分片 / 并发）
tools/
  serve_range.py      本地 Range(206) 静态服务
  verify_media_fragment.py  真实浏览器验证 Media Fragment 行为
  pack_deploy.py      打包部署产物（只含 VPS 运行时需要的文件）
deploy/             nginx.conf · tbbt-api.service · DEPLOY.md
tests/              pytest（105 个测试）
AGENTS.md           项目计划与所有关键决策的权威记录
```

---

## 本地开发

需要 Python 3.10+ 与 Node 18+。

```bash
# 1) 后端（项目根目录）
python -m backend.main --port 8000

# 2) 前端（另开终端）
cd frontend
npm install
npm run dev          # http://localhost:5173，已配 proxy → 127.0.0.1:8000
```

前端 `/api`、`/v` 都通过 Vite proxy 代理到后端，**保持同源** —— 与生产形态一致，所以不需要任何 CORS 配置。

> ⚠️ Windows 的 cmd 用户注意：`cd` 跨盘符要用 `cd /d "D:\..."`，否则当前盘不变、命令会找不到文件。

## 测试

```bash
python -m pytest -q          # 105 passed
```

覆盖：字幕清洗规则、FTS5 与短查询降级、时间码不变量、API 契约、HTTP Range、分享 token 唯一性、上传续传判定。

## 部署

见 [`deploy/DEPLOY.md`](deploy/DEPLOY.md)。核心三步：传部署包 → systemd 起服务 → nginx + certbot。

---

## 版权声明

**本项目仅供学习与教学用途，不提供、不托管任何影视素材。**

- 仓库中**不包含**任何视频文件或字幕文件（详见 `.gitignore`）
- 使用者需自行准备影音素材，并自行承担相应的合规责任
- 站点定位为英语学习工具；`robots.txt` 已禁止搜索引擎索引
- 收到权利方投诉将立即下架对应内容
