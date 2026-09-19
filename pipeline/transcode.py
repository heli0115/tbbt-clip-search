"""按季批量转码：把 x265 10bit 的 mkv 转成浏览器可播的 720p H.264 mp4。

配方的依据见 AGENTS.md 第 4 节「转码配方」——由 VMAF 实测定案：
720p / H.264 High / 1.5 Mbps 视频 + 128 kbps AAC + faststart。

为什么必须转码：源是 HEVC 10bit（yuv420p10le），浏览器无法直接播放。

用法：
    python -m pipeline.transcode "D:/.../生活大爆炸S01..." \\
        -o "D:/.../视频素材/web" --ffmpeg "D:/ffmpeg/bin/ffmpeg.exe"
"""

from __future__ import annotations

import argparse
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .batch import find_episodes

__all__ = [
    "VIDEO_ARGS",
    "TranscodeResult",
    "build_command",
    "transcode_one",
    "transcode_season",
    "main",
]

# 视频编码参数（AGENTS.md 第 4 节定案）
VIDEO_ARGS = [
    "-vf", "scale=1280:-2",
    "-c:v", "h264_nvenc",
    "-preset", "p5",
    "-rc", "vbr",
    "-b:v", "1500k",
    "-maxrate", "2000k",
    "-bufsize", "4000k",
    "-pix_fmt", "yuv420p",
    "-profile:v", "high",
    "-level", "4.1",
]

AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]


@dataclass(frozen=True)
class TranscodeResult:
    """单集转码结果。"""

    season: int
    episode: int
    output: Path
    skipped: bool
    elapsed: float = 0.0


def build_command(
    src: str | Path,
    dst: str | Path,
    ffmpeg: str = "ffmpeg",
    *,
    hwaccel: bool = True,
    video_args: list[str] | None = None,
) -> list[str]:
    """构造转码命令，返回**参数列表**（路径含中文，禁止拼 shell 字符串）。"""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if hwaccel:
        command += ["-hwaccel", "cuda"]
    command += ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0"]
    command += list(video_args if video_args is not None else VIDEO_ARGS)
    command += AUDIO_ARGS
    command += ["-movflags", "+faststart"]
    if Path(dst).suffix.lower() != ".mp4":
        # 临时文件（形如 .mp4.part）无法从扩展名推断容器格式，必须显式指定
        command += ["-f", "mp4"]
    command += [str(dst)]
    return command


def transcode_one(
    src: str | Path,
    dst: str | Path,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """转码单集，失败抛 RuntimeError。

    **原子写入**：先写 `<目标>.part`，成功后重命名成最终文件。
    这样脚本被中断时只会留下 .part，而不会产生一个「存在但不完整」的
    mp4——否则 `transcode_season` 的存在性检查会把它误判为已完成而跳过。
    """
    out_path = Path(dst)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    tmp_path.unlink(missing_ok=True)

    proc = subprocess.run(
        build_command(src, tmp_path, ffmpeg),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"转码失败 ({Path(src).name}): {proc.stderr.strip()[:400]}"
        )
    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"转码产物为空: {out_path}")

    tmp_path.replace(out_path)
    return out_path


def transcode_season(
    video_dir: str | Path,
    out_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    transcoder=transcode_one,
    force: bool = False,
    on_progress=None,
) -> list[TranscodeResult]:
    """转码一整季。已存在的产物默认跳过（可断点续跑）；`force=True` 则重转。"""
    output_dir = Path(out_dir)
    results: list[TranscodeResult] = []

    for episode in find_episodes(video_dir):
        target = output_dir / f"S{episode.season:02d}E{episode.episode:02d}.mp4"

        if target.exists() and not force:
            result = TranscodeResult(
                season=episode.season,
                episode=episode.episode,
                output=target,
                skipped=True,
            )
        else:
            started = time.monotonic()
            transcoder(episode.path, target, ffmpeg=ffmpeg)
            result = TranscodeResult(
                season=episode.season,
                episode=episode.episode,
                output=target,
                skipped=False,
                elapsed=time.monotonic() - started,
            )

        results.append(result)
        if on_progress is not None:
            on_progress(result)

    return results


def main(argv: list[str] | None = None) -> int:
    """命令行入口：按季批量转码。"""
    parser = argparse.ArgumentParser(description="按季批量转码为 720p H.264 mp4")
    parser.add_argument("video_dir", help="剧集所在目录")
    parser.add_argument("-o", "--out-dir", required=True, help="输出目录")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--force", action="store_true", help="重新转码已存在产物")
    args = parser.parse_args(argv)

    def report(item: TranscodeResult) -> None:
        tag = "跳过" if item.skipped else f"完成 {item.elapsed:6.1f}s"
        size = item.output.stat().st_size / 1048576 if item.output.exists() else 0
        print(f"  S{item.season:02d}E{item.episode:02d}  {tag}  {size:7.1f} MB", flush=True)

    started = time.monotonic()
    results = transcode_season(
        args.video_dir,
        args.out_dir,
        ffmpeg=args.ffmpeg,
        force=args.force,
        on_progress=report,
    )

    done = sum(1 for r in results if not r.skipped)
    total = sum(r.output.stat().st_size for r in results if r.output.exists())
    print(
        f"\n共 {len(results)} 集（新转 {done}）；合计 {total / 1073741824:.2f} GB；"
        f"耗时 {time.monotonic() - started:.0f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
