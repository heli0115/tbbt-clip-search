"""把转码后的 mp4 批量上传到 Cloudflare R2（S3 兼容 API）。

为什么需要它
------------
上线方案要求视频由浏览器**直连 R2**（AGENTS.md 第 10 节），所以 279 个 mp4
（约 64 GB）必须先躺在 R2 上。本脚本是这条管线的最后一步：

    下载 → 提取字幕 → 入库 → 转码 → **上传 R2**

设计要点
--------
- **可中断续跑**：64 GB 上传以小时计（10 Mbps 上行约需 14 小时），默认跳过
  「远端已存在且大小一致」的对象；中断后直接重跑即可，`--force` 才强制重传。
- **分片上传**：`upload_file` 对超阈值的大文件自动走 multipart，单文件 240 MB 也能续分片。
- **并发**：默认 4 个文件并行——单连接吃不满上行带宽。
- **长缓存**：文件名即内容标识（`S01E01.mp4` 的内容不会变），故设 `immutable`，
  让 Cloudflare 边缘长期缓存，回源更少。

用法
----
    set R2_ACCOUNT_ID=<账号 ID>
    set R2_ACCESS_KEY_ID=<Access Key ID>
    set R2_SECRET_ACCESS_KEY=<Secret Access Key>
    set R2_BUCKET=thebong          # 本项目的桶名（不是 tbbt-video）

    # 先干跑，确认清单与总量
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web" --dry-run

    # 先传一季试水（S01 共 17 集）
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web" --limit 17

    # 全量（可反复重跑，已传的会跳过）
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web"

    # 卡片缩略图与封面：jpg 分别传到 cues/ 与 covers/ 前缀
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web\\cues" \\
        --suffix .jpg --key-prefix cues
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web\\covers" \\
        --suffix .jpg --key-prefix covers

依赖：`pip install boto3`（仅本脚本需要，其余管线只用标准库）
"""

from __future__ import annotations

import argparse
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

__all__ = [
    "ENV_ACCOUNT_ID",
    "ENV_ACCESS_KEY",
    "ENV_SECRET_KEY",
    "ENV_BUCKET",
    "DEFAULT_BUCKET",
    "CACHE_CONTROL",
    "CONTENT_TYPE",
    "content_type_for",
    "object_key",
    "find_files",
    "find_videos",
    "needs_upload",
    "should_report",
    "PROGRESS_EVERY",
    "endpoint_url",
    "upload_one",
    "main",
]

ENV_ACCOUNT_ID = "R2_ACCOUNT_ID"
ENV_ACCESS_KEY = "R2_ACCESS_KEY_ID"
ENV_SECRET_KEY = "R2_SECRET_ACCESS_KEY"
ENV_BUCKET = "R2_BUCKET"

# 本项目的桶名（AGENTS.md 第 9 节：**thebong**，不是 tbbt-video）。
# 作为默认值写死，省得每次上传都要记得 --bucket；仍可用参数或环境变量覆盖。
DEFAULT_BUCKET = "thebong"

CONTENT_TYPE = "video/mp4"
CACHE_CONTROL = "public, max-age=31536000, immutable"

# 逐文件打印会给 11.8 万个小文件刷出十几万行日志，所以默认每这么多个报一次进度
PROGRESS_EVERY = 200

# 只上传**严格形如 S01E01.<ext> 或 S01E01_0001.jpg** 的文件，按扩展名分别定死形态：
# `mp4` 只可能是整集视频；`jpg` 既可能是每集封面（`S01E01.jpg`）也可能是台词缩略图
# （`S01E01_0001.jpg`，序号 4 位）。这样抽帧中途的候选文件（`S01E01.cand0.jpg`）、
# 转码残留（`S01E01.mp4.part`）都不会被误传，也与后端 `/v/` 的白名单正则一致。
_FILE_PATTERNS = {
    "mp4": re.compile(r"^S(?P<season>\d{2})E(?P<episode>\d{2})\.mp4$"),
    "jpg": re.compile(
        r"^S(?P<season>\d{2})E(?P<episode>\d{2})(?:_(?P<index>\d{4}))?\.jpg$"
    ),
}

_CONTENT_TYPES = {
    ".mp4": CONTENT_TYPE,
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def content_type_for(path: Path) -> str:
    """按扩展名给出 ContentType（封面是 jpg，浏览器要按图片处理）。"""
    return _CONTENT_TYPES.get(path.suffix.lower(), CONTENT_TYPE)


def object_key(path: Path, key_prefix: str = "") -> str:
    """R2 中的对象名。

    视频是**裸文件名**（前端按 `https://video.<域名>/S01E01.mp4` 取）；
    封面加 `covers/` 前缀（对应 `https://video.<域名>/covers/S01E01.jpg`，
    与后端 `_cover_url()` 拼出的 URL 一致）。
    """
    prefix = key_prefix.strip("/")
    return f"{prefix}/{path.name}" if prefix else path.name


def find_files(directory: Path, suffix: str = ".mp4") -> list[Path]:
    """列出待上传的文件，按 (季, 集, 序号) 自然排序。

    接受两种命名：`S01E01.mp4`（整集视频 / 每集封面）与
    `S01E01_0001.jpg`（台词缩略图，序号 4 位）。未知扩展名直接返回空 —— 不猜。
    """
    pattern = _FILE_PATTERNS.get(suffix.lower().lstrip("."))
    if pattern is None:
        return []

    found: list[tuple[int, int, int, Path]] = []
    for path in Path(directory).iterdir():
        if not path.is_file():
            continue
        match = pattern.match(path.name)
        if match is None:
            continue
        groups = match.groupdict()
        index = int(groups["index"]) if groups.get("index") else 0
        found.append((int(groups["season"]), int(groups["episode"]), index, path))
    return [path for _, _, _, path in sorted(found)]


def find_videos(directory: Path) -> list[Path]:
    """待上传的整集视频（`S01E01.mp4`）。"""
    return find_files(directory, ".mp4")


def should_report(done: int, total: int, every: int = PROGRESS_EVERY) -> bool:
    """是否该打印一行进度。

    默认**每 `every` 个报一次**（最后一个必报）—— 上传 11.8 万个缩略图时，
    逐文件打印会刷出十几万行，既看不清进度也拖慢终端。
    """
    return done == total or done % every == 0


def needs_upload(remote_size: int | None, local_size: int) -> tuple[bool, str]:
    """纯函数：远端状态 → 是否需要上传。

    `remote_size` 为 None 表示远端不存在。
    """
    if remote_size is None:
        return True, "远端不存在"
    if remote_size != local_size:
        return True, f"大小不一致（远端 {remote_size} / 本地 {local_size}）"
    return False, "已存在且一致"


def endpoint_url(account_id: str) -> str:
    """R2 的 S3 兼容端点。"""
    return f"https://{account_id}.r2.cloudflarestorage.com"


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def _build_client(
    account_id: str, access_key: str, secret_key: str, *, max_pool_connections: int = 64
):
    import boto3  # 延迟导入：只有真正上传时才需要 boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url(account_id),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        # 默认连接池只有 10 条：并发一高就 "Connection pool is full" 并退化成串行。
        # 缩略图是 11.8 万个 7 KB 的小文件，瓶颈是**请求延迟**而不是带宽，所以池要大。
        config=Config(
            max_pool_connections=max_pool_connections,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def _build_transfer_config():
    from boto3.s3.transfer import TransferConfig

    mb = 1024 * 1024
    return TransferConfig(
        multipart_threshold=16 * mb,
        multipart_chunksize=16 * mb,
        max_concurrency=4,
    )


def _remote_size(client, bucket: str, key: str) -> int | None:
    """远端对象大小；不存在或不可读时返回 None。"""
    try:
        return int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
    except Exception:
        return None


def upload_one(
    client,
    bucket: str,
    path: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    key_prefix: str = "",
    transfer_config=None,
    check_remote: bool = True,
) -> tuple[str, str, int, str]:
    """上传单个文件，返回 `(状态, key, 字节数, 备注)`。

    状态取值：`up` 已上传 / `skip` 跳过 / `dry` 待上传（dry-run 不查远端）。

    `check_remote=False` 跳过 `head_object`：**首次全量上传**用它可省掉一半请求 ——
    11.8 万个 7 KB 的小文件，耗时几乎就等于请求数。代价是重跑时已传的会被重传一遍
    （幂等，只是多花时间）。
    """
    key = object_key(path, key_prefix)
    size = path.stat().st_size

    if dry_run:
        return "dry", key, size, "dry-run，未查询远端"

    if check_remote:
        remote = _remote_size(client, bucket, key)
        need, reason = needs_upload(remote, size)
        if not need and not force:
            return "skip", key, size, reason
    else:
        need, reason = True, "未查远端（--no-remote-check）"

    extra = {"ContentType": content_type_for(path), "CacheControl": CACHE_CONTROL}
    started = time.monotonic()
    if transfer_config is None:
        client.upload_file(str(path), bucket, key, ExtraArgs=extra)
    else:
        client.upload_file(
            str(path), bucket, key, ExtraArgs=extra, Config=transfer_config
        )
    elapsed = max(time.monotonic() - started, 1e-6)

    note = "强制重传" if (check_remote and force and not need) else reason
    return "up", key, size, f"{note} · {_human(size / elapsed)}/s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量上传转码产物到 Cloudflare R2")
    parser.add_argument("directory", help="转码产物目录（如 视频素材/web）")
    parser.add_argument(
        "--bucket",
        default=os.environ.get(ENV_BUCKET) or DEFAULT_BUCKET,
        help=f"桶名（默认 {DEFAULT_BUCKET}，也可用环境变量 {ENV_BUCKET}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不上传也不查远端")
    parser.add_argument("--force", action="store_true", help="强制重传已存在的对象")
    parser.add_argument("--workers", type=int, default=4, help="并行文件数（默认 4）")
    parser.add_argument("--limit", type=int, help="只处理前 N 个（试跑用）")
    parser.add_argument(
        "--suffix", default=".mp4", help="只上传该扩展名（默认 .mp4；封面用 .jpg）"
    )
    parser.add_argument(
        "--key-prefix", default="", help="对象名前缀（封面用 covers，与后端 URL 对应）"
    )
    parser.add_argument(
        "--no-remote-check",
        action="store_true",
        help="跳过「远端是否已存在」查询直接上传（首次全量上传用它可省一半请求，明显更快）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=f"逐个文件打印（默认每 {PROGRESS_EVERY} 个打印一行进度）",
    )
    args = parser.parse_args(argv)

    directory = Path(args.directory)
    videos = find_files(directory, args.suffix)
    if args.limit:
        videos = videos[: args.limit]
    if not videos:
        print(f"在 {directory} 没找到 S*E*{args.suffix}", flush=True)
        return 1

    total_bytes = sum(p.stat().st_size for p in videos)
    print(
        f"待处理 {len(videos)} 个文件 / 合计 {_human(total_bytes)}",
        flush=True,
    )

    if args.dry_run:
        client = None
        transfer_config = None
        print("（dry-run：只列清单，不上传）\n", flush=True)
    else:
        if not args.bucket:
            print(f"缺少桶名：用 --bucket 或环境变量 {ENV_BUCKET}", flush=True)
            return 2

        account_id = os.environ.get(ENV_ACCOUNT_ID, "")
        access_key = os.environ.get(ENV_ACCESS_KEY, "")
        secret_key = os.environ.get(ENV_SECRET_KEY, "")
        missing = [
            name
            for name, value in (
                (ENV_ACCOUNT_ID, account_id),
                (ENV_ACCESS_KEY, access_key),
                (ENV_SECRET_KEY, secret_key),
            )
            if not value
        ]
        if missing:
            # 凭据是会话级的：换一个终端就没了，所以把「怎么设」直接打出来
            print("缺少凭据环境变量：", flush=True)
            for name in missing:
                print(f"  - {name}", flush=True)
            print(
                "\n在 PowerShell 里设置（值从 Cloudflare → R2 → Manage R2 API Tokens 取）：",
                flush=True,
            )
            print(f'  $env:{ENV_ACCOUNT_ID} = "<Account ID>"', flush=True)
            print(f'  $env:{ENV_ACCESS_KEY} = "<Access Key ID>"', flush=True)
            print(f'  $env:{ENV_SECRET_KEY} = "<Secret Access Key>"', flush=True)
            print(f'  # 桶名默认已是 {DEFAULT_BUCKET}，不用设', flush=True)
            return 2

        client = _build_client(
            account_id,
            access_key,
            secret_key,
            max_pool_connections=max(64, args.workers * 2),
        )
        transfer_config = _build_transfer_config()

        # 预检：凭据与桶权限有问题就该立刻失败，而不是传到一半才发现
        try:
            client.head_bucket(Bucket=args.bucket)
        except Exception as exc:  # noqa: BLE001 - 想原样展示给用户
            print(f"无法访问桶 {args.bucket}：{exc}", flush=True)
            return 3

        print(f"目标桶：{args.bucket}  （中断后重跑即可续传）\n", flush=True)

    stats = {"up": 0, "skip": 0, "dry": 0, "fail": 0}
    done = 0
    width = len(str(len(videos)))
    started = time.monotonic()
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                upload_one,
                client,
                args.bucket,
                path,
                force=args.force,
                dry_run=args.dry_run,
                key_prefix=args.key_prefix,
                transfer_config=transfer_config,
                check_remote=not args.no_remote_check,
            ): path
            for path in videos
        }
        for future in as_completed(futures):
            done += 1
            path = futures[future]
            try:
                status, key, size, note = future.result()
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不该中断整批
                stats["fail"] += 1
                failures.append(f"{path.name}: {str(exc)[:120]}")
                if args.verbose or should_report(done, len(videos)):
                    print(
                        f"[{done:>{width}}/{len(videos)}] {path.name}  失败：{str(exc)[:120]}",
                        flush=True,
                    )
                continue

            stats[status] = stats.get(status, 0) + 1
            if args.verbose:
                label = {"up": "上传", "skip": "跳过", "dry": "待传"}[status]
                print(
                    f"[{done:>{width}}/{len(videos)}] {key}  {label}  "
                    f"{size / 1048576:.1f} MB  {note}",
                    flush=True,
                )
            elif should_report(done, len(videos)):
                rate = done / max(time.monotonic() - started, 1e-6)
                remain = (len(videos) - done) / rate / 60 if rate > 0 else 0
                print(
                    f"[{done:>{width}}/{len(videos)}]  上传 {stats['up']}  跳过 {stats['skip']}  "
                    f"失败 {stats['fail']}  ·  {rate:.1f} 文件/s  剩余约 {remain:.0f} 分钟",
                    flush=True,
                )

    print(
        f"\n完成：上传 {stats['up']}，跳过 {stats['skip']}，失败 {stats['fail']}"
        f"；耗时 {(time.monotonic() - started) / 60:.1f} 分钟",
        flush=True,
    )
    for item in failures[:20]:
        print(f"  失败：{item}", flush=True)
    if stats["fail"]:
        print("失败项可重跑本命令补齐（已传的会自动跳过）", flush=True)
    return 0 if stats["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
