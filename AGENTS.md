# AGENTS.md — 《生活大爆炸》台词素材搜索站

> **本文件是本项目的权威计划与约定。所有后续开发、回答、代码变更必须完全按本计划执行。**
> 计划若需变更：**先改本文件，再改代码**。不得在未更新本文件的情况下偏离架构、技术栈或路线图。

---

## 1. 项目定位

关键词搜索电视剧台词 → 返回对应素材片段 → 中英双语 → 分享选中的素材。
素材限定为美剧《生活大爆炸》（The Big Bang Theory，12 季 / 279 集）。

## 2. 已定决策（不可擅自更改）

| 决策项 | 结论 |
|---|---|
| 产品形态 | **公开网站：在线播放 + 链接分享** |
| 素材来源 | **百度网盘**（视频内嵌字幕，需先分离字幕轨） |
| 搜索精度 | **句子级**（SRT 时间轴直接定位，不做词级强制对齐） |

> 以上三项由用户明确选定。**任何一项要改变，必须先向用户确认。**

## 3. 环境约束（实测）

- OS：Windows；shell 为 bash（git bash）
- ✅ node v24.15.0 / npm 11.12.1 / pnpm 11.24.0
- ✅ Python 3.12.3 / pip 26.2.1
- ✅ **ffmpeg / ffprobe / ffplay 已就绪**：`D:\ffmpeg\bin`（`2026-09-17-git-7070fe638e-essentials_build`），已加入**用户 PATH**（新开终端生效）
- ✅ **NVIDIA NVENC 硬件编码可用**（`h264_nvenc` / `hevc_nvenc`，RTX 4060 Laptop）→ M4 转码走硬件加速，不必纯 CPU 慢转
- ❌ **`github.com` 不可达**（`curl` 返回 `000`）→ 任何走 GitHub Releases 的安装都会失败（如 `winget install Gyan.FFmpeg`）；Python 依赖请走国内镜像 `https://pypi.tuna.tsinghua.edu.cn/simple`
- ⚠️ **D 盘仅剩 174G**（总计 752G，已用 579G）→ 必须**按季流水线**处理：下载一季 → 提取字幕 + 转码压缩 → 删除源文件 → 下一季
- 工作区根：`D:\study\生活大爆炸\生活大爆炸`

### 路径处理纪律（硬性）

路径含中文。调用 ffmpeg / ffprobe 等外部程序时：

- **禁止** `subprocess.run(f"ffmpeg -i {path}", shell=True)`
- **必须** `subprocess.run(["ffmpeg", "-i", path], capture_output=True)`
- Windows 下读取子进程输出需显式指定编码（如 `encoding="utf-8", errors="replace"`），避免 GBK 解码崩溃

## 4. 架构

```
【数据管线 · Python 一次性脚本】
百度网盘 → 下载单季 → ffprobe 诊断 → 提取字幕轨 (ffmpeg -map 0:s:N -c:s srt)
                                  └→ remux/转码 H.264 8bit mp4 + -movflags +faststart
                                            ↓
                            clips 表 (season, episode, start_ms, end_ms, en, zh, speaker)
                                            ↓
                            SQLite FTS5 (trigram tokenizer，中英文均可子串搜)

【服务层 · FastAPI】backend/main.py（契约见 tests/test_api.py，先测试后实现）
  /api/health          健康检查：{status, clips}
  /api/search?q=&limit= 关键词 → 台词结果列表（每条含 video 字段供前端拼 Media Fragment）
  /v/{file}            整集视频静态服务，**必须支持 Range(206)**
  /clip/{ep}?t=a,b     （M4+）服务端切片播放
  /s/{token}           （M5）分享短链

【前端 · Vite + React + Tailwind】
  搜索框 → 结果列表（双语 / 剧集 / 时间码）→ 片段播放器 → 生成分享链接

【托管】视频走 Cloudflare R2（出网流量免费）；服务与前端走香港轻量 VPS（免备案，见第 10 节）
```

### 关键技术决策（不得随意替换）

1. **索引**：SQLite FTS5，**两个索引分工**（中英文的匹配语义本就不同）：
   - `clips_fts`（`tokenize=trigram`）：中文**子串**匹配（`狭缝` 命中「有两个狭缝的平面」），
     同时兼作英文词匹配无果时的兜底。
   - `clips_fts_en`（`tokenize=unicode61`）：英文**按词**匹配。搜 `hi` 只命中独立的 "hi"，
     不会因为 `this` 里含 `hi` 就被拉出来。
   - `search()` 分流：纯 ASCII 查询先走词匹配，**无结果再退回子串**（这样 `phot` 仍能找到 `photon`）。

   ⚠️ **两个已踩过的坑（务必别改回去）**：
   - **FTS5 的 `MATCH` 左操作数必须是表名本身，不能用别名**。写
     `FROM clips_fts f ... WHERE f MATCH ?` 会抛 `no such column: f`；早期代码里这个异常被
     `except OperationalError: pass` 吞掉，于是每次搜索都静默降级为 `LIKE '%q%'` 全表扫描
     —— **索引从第一天起就没生效过**（既慢，`ORDER BY rank` 也从未起作用）。
   - **不能用 `SELECT count(*) FROM clips_fts_en` 判断 external content 索引是否为空**：
     它返回的是 `clips` 的行数（空索引也会报 117842），据此判断会永远跳过 `rebuild`。
     改用 `meta` 表里的显式标记。

   ⚠️ 实测（SQLite 3.45.1）：trigram **只能匹配 >= 3 个字符**的查询——`光子`（2 汉字）、`ph` 都搜不到。
   → 因此长度 < 3 时**自动降级为 `LIKE '%q%'`**；这条兜底不可省，否则中文两字词成搜索盲区。
2. **播放不切片**：整集 mp4 + 前端定位，服务端不做任何切片。

   ✅ **桌面浏览器**确实原生遵守 Media Fragment（真实 Chrome，`tools/verify_media_fragment.py` 4/4 通过）：
   - 起点精确到毫秒：`#t=2.38` → `currentTime=2.380s`
   - **end 原生遵守**：`#t=2.38,4.84` 播到 4.84s 自动 `paused=true`
   - ⚠️ 前提是静态服务支持 **HTTP Range（206）**（R2 原生支持；`python -m http.server` 不支持）

   ❌ **但移动端不能依赖它** —— 实测反馈：Android 真机上「有的从头播、有的从台词播」。
   根因是**两套定位机制打架**：浏览器原生 seek 与 JS 设的 `currentTime` 时机不定，
   谁先谁后不可预测。现行做法（见 `ClipPlayer.tsx`）：
   - `src` 用**裸地址，不带 `#t=`**，定位**完全由 JS 控制** —— 单一机制才可预测
   - 在 `loadedmetadata` / `loadeddata` / `canplay` / `timeupdate` **四个时机重试**定位；
     `loadedmetadata` 那一刻 `seekable` 常常还没就绪，设 `currentTime` 会被**静默忽略**
     （这正是「有的从头播」的来源）
   - 自写 `timeupdate` → 到 `end_ms` 显式 `pause()`：**移动端没人替你遵守 end**
   - 用 `useLayoutEffect` 显式 `play()`，**不靠 `autoPlay` 属性**：后者要等加载完才触发，
     那时用户手势授权窗口已过期，会被自动播放策略拦下（Android 上尤其明显）
   - `playsInline` 必须加，否则 iOS Safari 一点播放就强制全屏
   - 复制分享链接：`navigator.clipboard` **只在安全上下文（HTTPS/localhost）可用**，
     http 下必须退到 `execCommand('copy')`，再不行就给可一键全选的输入框

   M2–M3 完全不做服务端切片；仅在需要「保护整集不被整段浏览」时（M4+）才为分享片段做切片托管。
3. **时间单位统一为 `ms`**，数据库字段名必须为 `start_ms` / `end_ms`。
4. **双语**：优先依赖"中英同一条字幕轨"。**只有在确认是分两条轨时**，才实现按时间轴重叠度对齐的逻辑。

### 转码配方（已定案，有实测依据）

产物场景：**小窗口看片段**（学台词 / 找素材），非全屏观影。

```bash
ffmpeg -hwaccel cuda -i input.mkv   -map 0:v:0 -map 0:a:0   -vf scale=1280:-2   -c:v h264_nvenc -preset p5 -rc vbr -b:v 1500k -maxrate 2000k -bufsize 4000k   -pix_fmt yuv420p -profile:v high -level 4.1   -c:a aac -b:a 128k -ac 2   -movflags +faststart output.mp4
```

VMAF 选型实测（S01E01，前 60 秒，各自目标分辨率）：

| 参数 | 体积 | VMAF | 结论 |
|---|---|---|---|
| 源 HEVC 10bit 1080p | 600 MB | 参照物 | 浏览器播不了，必须转 |
| 1080p @ cq24（6.7 Mbps） | 1103 MB | 77.9 | **纯浪费：体积 4 倍，画质无差** |
| 1080p @ 2.5 Mbps | 418 MB | 75.7 | 全屏场景才需要 |
| 720p @ 2.5 Mbps | 420 MB | 79.9 | 体积 +58%，画质仅 +1.1，已饱和 |
| **720p @ 1.5 Mbps** | **266 MB** | **78.8** | ✅ **定案（拐点）** |
| 720p @ 1.2 Mbps AV1 10bit | 224 MB | 79.3 | 弃：仅 +0.5 分，不值兼容性风险 |
| 720p @ 1.2 Mbps AV1 8bit | 224 MB | 79.1 | 弃：同上 |

**核心结论：VMAF 天花板 ≈ 79，与码率、编码格式、色深均无关。** 因此 1.5 Mbps 已是拐点，继续加码率/换 AV1/上 10bit 全是无效投入。

- 全剧预估：266 MB × 279 集 ≈ **74 GB**
- 编码耗时：NVENC ≈ **100 秒/集**（22 分钟视频，约 15x 实时）
- 若未来要全屏体验：改用 `scale=1920:-2` + `-b:v 2500k`（418 MB/集）

### 已落地的脚本

| 脚本 | 用途 |
|---|---|
| `pipeline/parse_srt.py` | 双语 SRT → 结构化 Cue（含清洗规则） |
| `pipeline/store.py` | 写入 SQLite + FTS5 索引，提供 `search()` |
| `tools/serve_range.py` | 本地支持 Range(206) 的静态服务（`http.server` 做不到） |
| `tools/verify_media_fragment.py` | 真实浏览器验证 Media Fragment 播放 |
| `pipeline/batch.py` | 按季批量：提取字幕 → 解析 → 入库（支持断点续跑） |
| `pipeline/transcode.py` | 按季批量转码（720p/H.264/1.5Mbps 定案配方） |
| `backend/main.py` | FastAPI 服务层：`/api/health` · `/api/search` · `/v/{file}`（Range 206） |
| `frontend/` | Vite + React + Tailwind：搜索框 → 结果列表（双语 / 剧集 / 时间码）→ 片段播放器 |
| `pipeline/upload_r2.py` | 批量上传转码产物到 R2（断点续传 / 分片 / 并发 / 长缓存头） |
| `deploy/` | `nginx.conf` · `tbbt-api.service` · `DEPLOY.md`（境外轻量 VPS 部署配置与手册） |
| `tools/pack_deploy.py` | 打包部署产物（只含 VPS 运行时需要的 10 个文件，避免多传 / 漏传） |
| `requirements.txt` | VPS 运行时依赖（fastapi / uvicorn） |

## 5. 路线图（按风险排序，非功能排序）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M0** | ✅ **已全部完成**：ffmpeg 就绪 + S01 样片到位 + ffprobe 诊断完毕 + SRT 提取验证通过（详见第 8 节） | ✅ 字幕轨信息已获取；✅ `samples/S01E01.bilingual.srt`（422 条）提取成功 |
| **M1** | ✅ **已完成**：解析 + 清洗（`pipeline/parse_srt.py`）、入库 + FTS5 索引（`pipeline/store.py`），共 46 个测试通过；S01E01 已入库 422 条 | ✅ 搜 `photon` / `谢尔顿` 命中且时间码正确（`bazinga` 属后期季，S01E01 无此词） |
| **M2** | ✅ **已完成**：`backend/main.py`（3 端点，Range 已实测）+ `frontend/`（搜索 / 列表 / 播放器）。**用户实机验证通过**：搜 `狭缝` → 点开 → 2.38s 起播、4.84s 自动暂停 | ✅ 已达成 |
| **M3** | ✅ **已完成**（随 M2 一并交付，未引入任何对齐逻辑） | ✅ 中英对照显示正确 |
| **M4** | 🔄 批量处理 + 转码 ✅（12 季 279 集 / 64.2 GB）；上传托管**代码** ✅（见第 10 节），⏳ 待执行 | 12 季全部可搜可播 |
| **M5** | 🔄 分享链接 ✅（token + `/s/{token}` 分享页）；卡片图 ⏳；部署上线 ⏳ | 一条链接发给朋友可直接看片段 |

**范围纪律**：M0–M2 全程本地，不碰服务器。不得提前实现 M3+ 的功能。

## 6. 编码纪律

- **先写测试再写实现**：数据管线每个脚本都要有 pytest，验证时间码单调递增等不变量。
- **任务粒度必须有验收标准**。"做个台词搜索"是坏任务；"写 `parse_srt.py`，输入 srt 输出 JSON 数组含 `start_ms/end_ms/text`，并附 pytest"是好任务。
- **字幕清洗规则统一**（已实现在 `pipeline/parse_srt.py`，不得各处重复实现）：
  - 剥离 HTML 标签：`<font ...>` / `</font>` / `<i>` / `</i>`
  - 剥离 ASS 覆盖标签：`{\an8}` / `{\pos(...)}` / `{\i1}`
  - ASS 硬换行 `\N` 转成真正的换行
  - **保留圆括号与方括号内容** —— 实测 S01 的圆括号是台词内容（如「(克林贡是外星人)」），方括号未出现；**宁保留噪声，不删真内容**
  - 按字符集拆分中英（含 CJK → `zh`，其余 → `en`），不依赖行序
  - 跳过空块、时间码非法块、时间区间倒置块
- **时间码解析**必须兼容 `HH:MM:SS,mmm`（SRT）与 `HH:MM:SS.mmm`（VTT/ASS）。

## 7. 版权缓解措施（用户已知悉风险并选择公开站）

- 站点定位为**英语学习 / 教学**用途
- `robots.txt` 禁止爬虫索引；分享链接使用**不可猜测的 token**
- 准备 DMCA 下架响应流程（收到投诉即撤对应剧集）
- M4+ 分享仅托管**被选中的片段**，不开放整集自由浏览

## 8. M0 诊断结论（✅ 已完成）

**素材**：`D:\study\生活大爆炸\视频素材\` 下 **S01 全 17 集（12G）**，命名 `The.Big.Bang.Theory.S01E01.2007.1080p.Blu-ray.x265.10bit.AC3￡cXcY@FRDS.mkv`（压制组 FRDS，字幕来源 YYeTs/Zimuzu）。

**S01E01 实测流结构**：

| # | 类型 | codec | 语言 | 标题 |
|---|---|---|---|---|
| 0 | video | `hevc` 1920×1080 **`yuv420p10le`** | — | **10bit，浏览器无法直接播放** |
| 1 | audio | `ac3` 6ch | eng | Eng-DD5.1 |
| 2 | subtitle | **`ass`** | chi | **中上英下-YYeTs ← 采用这条** |
| 3 | subtitle | `ass` | chi | 简体中文-官方 |
| 4 | subtitle | `subrip` | chi | 简体中文-YYeTs |

**结论**：

- ✅ 全部为**文本字幕轨**（`ass` / `subrip`），**无 PGS、无硬字幕** → 一条 ffmpeg 命令即可提取
- ✅✅ **流 #2 中英同轨**（同一 cue 内中文行在上、英文行在下）→ **M3 对齐工作量 ≈ 0**，按行拆分即可
- ⚠️ 英文行被 `<font face="Calibri Italic"><font size="14"><font color="#eba862">…` 包裹，**清洗必须剥离**（第 6 节规则已覆盖）
- ⚠️ 视频 **10bit HEVC** → 浏览器不能播，M4 用 `h264_nvenc` 转 H.264 8bit
- 📊 单集 **422 条 / 1378 秒** → 全剧预计 **≈ 11 万条 clips**

**M1 解析规则**：每个 cue = 2 行 → 第 1 行 = 中文，第 2 行 = 英文（剥离 `<font>` 标签后）。

**已验证的提取命令**：

```bash
ffmpeg -i "S01E01….mkv" -map 0:s:0 -c:s srt "S01E01.bilingual.srt"
```

以下为参考（原阻塞项已全部解决）：

诊断命令：

```bash
ffprobe -v error -select_streams s \
  -show_entries stream=index,codec_name:stream_tags=language,title \
  -of default=noprint_wrappers=1 "样片.mkv"
```

字幕轨类型对照：

| `codec_name` | 类型 | 处理方式 |
|---|---|---|
| `subrip` / `ass` / `mov_text` | 文本软字幕 | ✅ `ffmpeg -map 0:s:N -c:s srt` |
| `hdmv_pgs_subtitle` / `dvd_subtitle` | 图形软字幕 | ⚠️ 先导出 `.sup` 再 OCR |
| 无字幕轨但画面有字幕 | 硬字幕 | ❌ 逐帧 OCR 或 ASR 转写 |

## 9. 进度快照（2026-09-18）

| 阶段 | 状态 |
|---|---|
| M0 环境准备 + 字幕轨诊断 | ✅ |
| M1 字幕解析 + 清洗 + 入库 + FTS5 | ✅ **105 个测试通过** |
| 全剧入库 | ✅ **12 季 279 集 / 117,842 条**（S01 7029 … S12 10214） |
| 全剧转码 | ✅ **279 集 / 64.22 GB**，输出至 `视频素材/web/` |
| M2 服务层（`backend/main.py`） | ✅ `/api/health` · `/api/search` · `/v/{file}`，Range 206 已用真实 HTTP 服务实测 |
| M2 React 播放器（`frontend/`） | ✅ 完成，**用户实机验证通过**（搜 `狭缝` → 点开 → 2.38s 起播、4.84s 自动暂停） |
| M4 部署上线 | ✅ **已上线**：腾讯云轻量（境外）`<你的 VPS 公网 IP>`（Ubuntu 26.04 + Python 3.14）；nginx → uvicorn(2 workers) → `tbbt.db`；`http://<你的 VPS 公网 IP>/` 搜索与播放全通，`/v/` 返回 404（证明确实没反代视频） |
| M5 分享链接 | ✅ 代码完成：`shares` 表 + `POST /api/share` + `GET /api/share/{token}` + 前端「分享这条台词」与 `/s/{token}` 分享页；⏳ 待实机验证 |
| M5 卡片图 | ⏸ **明确延后**（用户决定：M5 的验收标准已由分享链接满足，上线后按实际分享场景再决定要不要） |
| 搜索质量修复 | ✅ 英文改为**按词匹配**（搜 `hi` 不再匹到 `this`）；修复 **FTS5 别名 bug**（索引此前从未真正生效）；结果上限 30 → 100 |
| 移动端体验 | ✅ 播放器吸顶（`deploy` 见 App.tsx 注释）、`playsInline` 修 iOS 强制全屏、输入框 16px 修 iOS 聚焦缩放、安全区适配、回到顶部按钮 |
| M4 上传托管（视频） | ✅ **279 个 mp4 已全部上传到 R2 桶 `thebong`**（64.2 GB，上传 278 + 跳过 1，失败 0），逐文件大小比对一致；`r2.dev` 已实测 `Range → 206`。⚠️ 桶名是 `thebong`，不是 `tbbt-video`；endpoint 为 `https://<account_id>.r2.cloudflarestorage.com` |

**已验证的事实（避免重复试错）**：

- `bazinga` **不在 S01**（全季 0 命中）——M1 验收词改用 `photon` / `谢尔顿` 是正确的
- 跨集搜索正常：`physics` 同时命中 S01E01 与 S01E03
- FTS5 trigram 的 <3 字符死角由 LIKE 降级兜住，中文两字词（`狭缝`）可搜
- 单季 17 集的字幕提取 + 入库仅需 **11 秒**（转码才是耗时项，约 100 秒/集）
- **转码必须用原子写入**（`transcode_one` 先写 `.mp4.part` 再重命名）：
  否则任务被中断会留下「存在但不完整」的 mp4，而 `transcode_season` 的存在性检查
  会把它误判为已完成并跳过（本项目真实踩过：S01E12 曾因此损坏）
- ffmpeg 无法从 `.part` 扩展名推断容器格式，写临时文件时**必须显式 `-f mp4`**

**前端依赖版本已定案（勿随意升级）**：

- ❌ `vite@8`（rolldown 化）与 `@tailwindcss/vite` 不兼容：rolldown 打包 `vite.config.ts` 时无法处理 oxide 的 `.node`（`UNLOADABLE_DEPENDENCY ... stream did not contain valid UTF-8`），退化到 `--configLoader runner` 同样失败
- ❌ `@vitejs/plugin-react@6` 的 peer 是 `vite: ^8.0.0` —— 降级 Vite 时必须**两者一起降**
- ✅ 定案组合：`vite@^7.3.6` + `@vitejs/plugin-react@^5.2.0` + `tailwindcss@^4.3.3`（`@tailwindcss/vite` 插件）
- npm 装包若报 `EPERM`（spawn），加 `--ignore-scripts` 即可（oxide / esbuild 都是预编译二进制，不需要构建脚本）
- npm 官方源在国内极慢（4 分钟未完），改用 `--registry=https://registry.npmmirror.com` 后 **14 秒**装完

**已评估并否决的托管平台（勿重复踩）**：

- ❌ **Netlify / Cloudflare Pages 纯静态路线**：Netlify Functions 只支持 Node.js / Go，**跑不了 FastAPI**；
  且 serverless 是无状态 + 临时文件系统，放不下 54 MB 的 `tbbt.db`。要走纯静态就必须把 11.8 万条台词
  打包下发前端（gzip 约 1.5–2.5 MB）改做浏览器端搜索，代价是**丢掉服务端能力**（分享 token、限流、鉴权），
  且 Netlify/Cloudflare 在大陆无节点，国内速度反而不如香港轻量。
  **用户已明确选择保持香港轻量 → 方案 ③ 不变。**

**⚠️ 环境限制：agent 的 sandbox 拦截 node 的 `child_process`**

实测（2026-09-18）：`python subprocess` 能正常执行 `net use`（rc=0），但 `node` 的 `spawnSync('net')` 与 `spawnSync('cmd.exe')` **一律 EPERM**。
后果：**Vite 无法在 agent 会话内运行**——`windowsSafeRealPathSync` 需要 `exec('net use')`、esbuild 需要 spawn 服务进程。
因此 `npm run build` / `npm run dev` 必须在**普通终端**里由人执行；纯 JS 工具（如 `tsc`）可以用 `node node_modules/typescript/bin/tsc -b` 绕过。

### 启动方式（本地开发）

> ⚠️ **cmd.exe 跨盘符必须用 `cd /d`**：本工作区在 D 盘，而 cmd 的 `cd "D:\..."` 只改 D 盘自己的记录、
> **不切换当前盘**（提示符仍是 `C:\...`），于是 `python -m backend.main` 报
> `ModuleNotFoundError: No module named 'backend'`。PowerShell 的 `Set-Location` 没有这个问题。

```bash
# 终端 1：后端（项目根目录）
cd /d "D:\study\生活大爆炸\生活大爆炸"       # PowerShell 直接 cd "D:\study\..."
python -m backend.main --port 8000

# 终端 2：前端
cd /d "D:\study\生活大爆炸\生活大爆炸\frontend"
npm run dev          # http://localhost:5173，已配 proxy → 127.0.0.1:8000
```

## 10. 托管与部署方案（M4/M5 · 已定案）

**决策**（用户选定）：**方案 ③ —— 服务与前端放境外轻量 VPS（东京/新加坡/香港均可），视频走 Cloudflare R2。**

选它而不是国内大陆方案的理由：**国内大陆服务器 + 域名对外服务必须 ICP 备案（7–20 工作日 + 实名）**，
而实名会把版权风险直接落到个人（见下方「被否决的方案」）。境外节点 → **免备案**，且可用支付宝/微信付款。

> **选型实测**：腾讯云轻量的「境外通用套餐」（新加坡/东京/硅谷…）折扣后约 **30–33 元/月**，
> 其中**东京**到国内延迟最低（约 50–80ms）。**不必执着香港**——视频在 R2 上，
> 这台机器只扛 API（每次响应几 KB）+ 前端静态文件（约 200 KB），对带宽几乎无要求；
> 实测其月流量消耗约 6 GB，而套餐含 0.5TB 流量包，用不到 1%。
> ⚠️ 买前要按**续费价**（而非首单折扣价）评估。

### 架构

```
浏览器
  ├─ / （Vite 产物）                        → 境外轻量 VPS：nginx 直接 serve
  ├─ /api/search · /api/health              → 同一台 VPS：nginx → uvicorn(FastAPI) → tbbt.db
  └─ https://video.<域名>/S01E01.mp4#t=a,b   → Cloudflare R2（出网流量免费）
```

**前端与 API 同源**（同一域名同一端口）→ **不需要任何 CORS 配置**，
与本地开发时 Vite proxy 的同源形态完全一致，少一处上线惊喜。

### 硬性约束（违反即失去方案的主要收益）

1. **视频必须由浏览器直连 R2，禁止让 VPS 反代 `/v/*`**——否则香港轻量的 30 Mbps 峰值带宽
   立刻成为瓶颈，也白白浪费「R2 出网免费」。`deploy/nginx.conf` 里显式把 `/v/` 404 掉。
2. `/api/search` 的 `video` 字段必须是**可配置的绝对 URL**：
   环境变量 `TBBT_VIDEO_BASE`（默认 `/v`，保持本地开发不变）。
   本地 `→ /v/S01E01.mp4`；线上 `→ https://video.<域名>/S01E01.mp4`。
3. `<video>` 跨源加载**不需要 CORS**（浏览器以 no-cors 模式发 Range 请求，不触发预检）。
   但 R2 桶需开启公共访问：优先绑定自定义域名，`r2.dev` 子域有限速、仅供测试。
4. 媒体片段播放依赖 **Range(206)**：R2 原生支持，无需额外配置。
5. 香港轻量**只放 `tbbt.db`（54 MB），不放视频**——磁盘与带宽都留给服务本身，
   因此 40GB SSD 套餐足够（这正是选 ③ 而不是「香港轻量全包」的关键）。

### 成本

| 项目 | 量 | 费用 |
|---|---|---|
| 香港轻量 VPS | 2核2G / 40GB SSD / 30Mbps / 1TB-月 | 新用户 ≈¥24–60/月（原价约 2–3 倍）|
| R2 存储 | 64.22 GB × $0.015/GB-月 | ≈ **$0.8/月** |
| R2 出网流量 | 任意 | **$0** |
| 域名 | .com 约 $10/年 | ≈ ¥6/月 |

⚠️ 国内/香港云的共同特点：**新用户首年折扣极大，续费常翻 2–3 倍**，下单前按原价核算。

### 被否决的方案（决策留痕）

- **国内大陆全程**（国内轻量 + OSS/COS + CDN）：国内速度最快，但**必须 ICP 备案 7–20 工作日 + 实名**。
  实名会把这套版权风险直接落到个人——国内云商收到侵权投诉是**直接关停 + 保留证据移交**，
  而境外服务商通常只按 DMCA 下架对应内容。**服务器放哪，决定风险落在谁身上。**
- **香港轻量全包**（视频也放机器上）：64 GB 视频装不进 40GB 套餐，得上 80GB 档；
  且并发被 30 Mbps 峰值带宽卡在 ≈8–10 人。视频放 R2 则流量永不计费、并发不受服务器带宽限制。
- **纯境外**（Vultr/DO + R2）：最便宜，但国内访问视频最一般。

### 上线待办

- [x] 购买境外轻量 VPS（腾讯云轻量 · 境外通用套餐）
- [x] 注册 Cloudflare 账号
- [x] 建 R2 桶 `thebong` + 上传 279 个 mp4（64.2 GB，逐文件比对一致）
- [x] VPS 部署：`deploy/nginx.conf` + `deploy/tbbt-api.service`，只拷 `tbbt.db`
- [x] 前端 `npm run build`，产物放到 VPS 的 `/var/www/tbbt`
- [x] `robots.txt` 禁止索引（已随前端产物提供）
- [ ] **注册域名**（Cloudflare Registrar，约 $10/年）
- [ ] R2 桶绑定自定义域名 `video.<域名>`（替换 `r2.dev` —— 后者有限速，仅供测试）
- [ ] 设置 `TBBT_VIDEO_BASE=https://video.<域名>` 并重启 `tbbt-api`
- [ ] nginx `server_name` 改成域名
- [ ] `certbot --nginx -d <域名>` 配 HTTPS

### 部署实录（2026-09-19 已上线）

| 项 | 值 |
|---|---|
| VPS | 腾讯云轻量 · **境外** · `<你的 VPS 公网 IP>`（Ubuntu 26.04 LTS / Python 3.14.4 / 2核2G / 40GB）|
| 部署路径 | `/opt/tbbt`，服务账号 `tbbt`，systemd 单元 `tbbt-api` |
| 前端产物 | `/var/www/tbbt`（`www-data`），来源：本地 `npm run build` → `dist/` |
| 传输方式 | `tools/pack_deploy.py` 生成 `deploy-bundle.tar.gz`（10 文件 / 28.9 MB）+ 前端 dist 包 |
| 当前访问 | `http://<你的 VPS 公网 IP>/`（**尚未配域名与 HTTPS**）|

**这次实际踩到的坑**：

1. **`sudo tar` 解压后目录归 root** → 非 root 用户无法在其中建 venv，报
   `Permission denied: '/opt/tbbt/.venv'`。
   解法：先 `sudo chown -R ubuntu:ubuntu /opt/tbbt` 装依赖，最后再 `chown -R tbbt:tbbt`。
2. **cmd 的 `cd` 跨盘符不切换当前盘**（本项目在 D 盘）→ 在错误目录跑 `npm run build`，
   报 `Missing script: "build"`。用 `cd /d "D:\..."`，或干脆用 PowerShell。
3. **nginx `reload` 后立刻 curl 可能命中还持有旧配置的 worker**，拿到 404；重跑即正常。
4. 腾讯云轻量的 Ubuntu 镜像**默认用户是 `ubuntu` 而非 root**，全程需要 `sudo`。
