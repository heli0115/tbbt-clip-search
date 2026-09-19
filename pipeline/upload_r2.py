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
    set R2_BUCKET=tbbt-video

    # 先干跑，确认清单与总量
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web" --dry-run

    # 先传一季试水（S01 共 17 集）
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web" --limit 17

    # 全量（可反复重跑，已传的会跳过）
    python -m pipeline.upload_r2 "D:\\study\\生活大爆炸\\视频素材\\web"

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
    "CACHE_CONTROL",
    "CONTENT_TYPE",
    "object_key",
    "find_videos",
    "needs_upload",
    "endpoint_url",
    "upload_one",
    "main",
]

ENV_ACCOUNT_ID = "R2_ACCOUNT_ID"
ENV_ACCESS_KEY = "R2_ACCESS_KEY_ID"
ENV_SECRET_KEY = "R2_SECRET_ACCESS_KEY"
ENV_BUCKET = "R2_BUCKET"

CONTENT_TYPE = "video/mp4"
CACHE_CONTROL = "public, max-age=31536000, immutable"

_EPISODE_RE = re.compile(r"S(\d+)E(\d+)")


def object_key(path: Path) -> str:
    """R2 中的对象名。必须是裸文件名——前端按 `https://video.<域名>/{key}` 取。"""
    return path.name


def find_videos(directory: Path) -> list[Path]:
    """列出待上传的剧集文件，按 (季, 集) 自然排序（避免 S01E10 排在 S01E02 前）。"""

    def sort_key(path: Path) -> tuple[int, int]:
        match = _EPISODE_RE.search(path.name)
        return (int(match.group(1)), int(match.group(2))) if match else (9999, 9999)

    return sorted(directory.glob("S*E*.mp4"), key=sort_key)


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


def _build_client(account_id: str, access_key: str, secret_key: str):
    import boto3  # 延迟导入：只有真正上传时才需要 boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url(account_id),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
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
    transfer_config=None,
) -> tuple[str, str, int, str]:
    """上传单个文件，返回 `(状态, key, 字节数, 备注)`。

    状态取值：`up` 已上传 / `skip` 跳过 / `dry` 待上传（dry-run 不查远端）。
    """
    key = object_key(path)
    size = path.stat().st_size

    if dry_run:
        return "dry", key, size, "dry-run，未查询远端"

    remote = _remote_size(client, bucket, key)
    need, reason = needs_upload(remote, size)
    if not need and not force:
        return "skip", key, size, reason

    extra = {"ContentType": CONTENT_TYPE, "CacheControl": CACHE_CONTROL}
    started = time.monotonic()
    if transfer_config is None:
        client.upload_file(str(path), bucket, key, ExtraArgs=extra)
    else:
        client.upload_file(
            str(path), bucket, key, ExtraArgs=extra, Config=transfer_config
        )
    elapsed = max(time.monotonic() - started, 1e-6)

    note = "强制重传" if (force and not need) else reason
    return "up", key, size, f"{note} · {_human(size / elapsed)}/s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量上传转码产物到 Cloudflare R2")
    parser.add_argument("directory", help="转码产物目录（如 视频素材/web）")
    parser.add_argument("--bucket", default=os.environ.get(ENV_BUCKET, ""))
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不上传也不查远端")
    parser.add_argument("--force", action="store_true", help="强制重传已存在的对象")
    parser.add_argument("--workers", type=int, default=4, help="并行文件数（默认 4）")
    parser.add_argument("--limit", type=int, help="只处理前 N 个（试跑用）")
    args = parser.parse_args(argv)

    directory = Path(args.directory)
    videos = find_videos(directory)
    if args.limit:
        videos = videos[: args.limit]
    if not videos:
        print(f"在 {directory} 没找到 S*E*.mp4", flush=True)
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
            print("缺少凭据环境变量：" + ", ".join(missing), flush=True)
            return 2

        client = _build_client(account_id, access_key, secret_key)
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

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                upload_one,
                client,
                args.bucket,
                path,
                force=args.force,
                dry_run=args.dry_run,
                transfer_config=transfer_config,
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
                print(f"[{done:>{width}}/{len(videos)}] {key}  失败：{exc}", flush=True)
                continue

            stats[status] = stats.get(status, 0) + 1
            label = {"up": "上传", "skip": "跳过", "dry": "待传"}[status]
            print(
                f"[{done:>{width}}/{len(videos)}] {key}  {label}  "
                f"{size / 1048576:.1f} MB  {note}",
                flush=True,
            )

    print(
        f"\n完成：上传 {stats['up']}，跳过 {stats['skip']}，失败 {stats['fail']}",
        flush=True,
    )
    if stats["fail"]:
        print("失败项可重跑本命令补齐（已传的会自动跳过）", flush=True)
    return 0 if stats["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
