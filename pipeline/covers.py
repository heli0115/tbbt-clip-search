"""按集抽帧生成卡片封面。

为什么要抽帧：Netflix 风格的卡片网格里，缩略图承担了大部分观感，而本项目
**没有任何海报素材**。但本地已经有 279 个转码好的 mp4 —— 从每集里取一帧，
就是最贴合内容的封面（AGENTS.md 第 2 节「封面图来源」、第 4 节决策 5）。

为什么不是「固定取 40% 处一帧」就完事
------------------------------------
40% 处通常落在正片中间（片头片尾的黑场、台标、演职员表都集中在两端），
但**夜戏/暗场景会抽出黑乎乎的卡片**。实测 S12E24：

| 位置 | 40%（556.6s） | 25%（347.9s） | 55%（765.4s） | 70%（974.1s） |
|---|---|---|---|---|
| YAVG | **29.97**（太暗） | 69.46 | 56.27 | 112.24 |

所以按 `CANDIDATE_RATIOS` 依次试：抽到亮度达标（`>= LUMA_MIN`）就停，
一个都不达标时取最亮的那张。**抽帧与亮度测量合并成一次 ffmpeg 调用**
（`signalstats,metadata=print:file=-` 是 pass-through 滤镜），
平均每集只多花零点几秒。

用法：
    python -m pipeline.covers "D:/.../视频素材/web" -o "D:/.../视频素材/web/covers"
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .batch import find_episodes

__all__ = [
    "CANDIDATE_RATIOS",
    "COVER_RATIO",
    "COVER_WIDTH",
    "COVER_QUALITY",
    "LUMA_MIN",
    "CoverResult",
    "cover_position_seconds",
    "build_duration_command",
    "probe_duration",
    "build_command",
    "parse_luma",
    "render_frame",
    "make_cover",
    "covers_for_dir",
    "main",
]

# 候选抽帧位置（按时长比例），第一个首选、其余依次兜底
CANDIDATE_RATIOS = (0.4, 0.25, 0.55, 0.7)
# 兼容旧名：首选位置
COVER_RATIO = CANDIDATE_RATIOS[0]
# 封面宽度：卡片最宽约 320 CSS px，640 已能覆盖 2x 屏
COVER_WIDTH = 640
# ffmpeg 的 MJPEG 质量刻度：2 最好、31 最差
COVER_QUALITY = 4
# 亮度下限（signalstats 的 YAVG，0–255）。实测：暗夜戏约 30，正常正片 56–112
LUMA_MIN = 45.0

_LUMA_RE = re.compile(r"signalstats\.YAVG=([\d.]+)")


@dataclass(frozen=True)
class CoverResult:
    """单集封面结果。"""

    season: int
    episode: int
    output: Path
    skipped: bool
    elapsed: float = 0.0


def cover_position_seconds(duration_s: float, *, ratio: float = COVER_RATIO) -> float:
    """按比例算出抽帧时间点（秒）。"""
    return duration_s * ratio


def build_duration_command(video: str | Path, ffprobe: str = "ffprobe") -> list[str]:
    """构造探测时长的 ffprobe 命令。

    路径含中文，必须返回**参数列表**（AGENTS.md 第 3 节硬性纪律）。
    """
    return [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]


def probe_duration(video: str | Path, ffprobe: str = "ffprobe") -> float:
    """读出一集视频的时长（秒）；失败抛 RuntimeError。"""
    proc = subprocess.run(
        build_duration_command(video, ffprobe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"探测时长失败 ({Path(video).name}): {proc.stderr.strip()[:400]}"
        )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(
            f"无法解析时长 ({Path(video).name}): {proc.stdout.strip()!r}"
        ) from None


def build_command(
    video: str | Path,
    dst: str | Path,
    *,
    position_s: float,
    ffmpeg: str = "ffmpeg",
    width: int = COVER_WIDTH,
    quality: int = COVER_QUALITY,
    measure_luma: bool = False,
) -> list[str]:
    """构造抽帧命令（参数列表，禁止拼 shell 字符串）。

    `-ss` 放在 `-i` **之前**：输入前 seek 是快速定位（只解到关键帧），
    放在输入之后则要解码并丢弃前面的全部帧，慢得多。

    `measure_luma=True` 时追加 `signalstats,metadata=print:file=-`：这两个是
    pass-through 滤镜，不影响写出的 jpg，却能在**同一次调用**里把平均亮度
    打到 stdout（解析见 `parse_luma`）。

    不写 `-f image2`：目标文件名保留了 `.jpg` 扩展名，ffmpeg 自己就能推断格式。
    """
    filters = f"scale={width}:-2"
    if measure_luma:
        filters += ",signalstats,metadata=print:file=-"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{position_s:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        filters,
        "-q:v",
        str(quality),
        str(dst),
    ]


def parse_luma(stdout: str) -> float | None:
    """从 ffmpeg 的 metadata 输出里读出 YAVG；读不到返回 None。"""
    match = _LUMA_RE.search(stdout)
    return float(match.group(1)) if match else None


def _candidate_path(out_path: Path, index: int) -> Path:
    """候选帧的文件名：`S01E01.cand0.jpg`。

    候选只在 `make_cover` 内部短暂存在（抽完立刻择优并删掉落选者），
    唯一的真实产物始终是 `S01E01.jpg` —— 存在性检查只看它，
    所以半途中断留下的候选不会被误判为「已完成」。
    """
    return out_path.with_name(f"{out_path.stem}.cand{index}{out_path.suffix}")


def render_frame(
    video: str | Path,
    dst: str | Path,
    *,
    position_s: float,
    ffmpeg: str = "ffmpeg",
    width: int = COVER_WIDTH,
    quality: int = COVER_QUALITY,
    measure_luma: bool = False,
) -> float | None:
    """从 `position_s` 处抽一帧写入 `dst`，返回该帧亮度（未测则 None）。

    **原子写入**：先写 `<名字>.part.<扩展名>`，成功后重命名。
    ⚠️ `.part` 必须放在扩展名**之前**，否则 ffmpeg 无法从扩展名推断格式。

    这是「按集封面」(`make_cover`) 与「按台词封面」(`cue_covers`) 共用的唯一
    抽帧实现 —— 临时命名、空产物清理、失败不留半成品这些坑只处理一次。
    """
    out_path = Path(dst)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _temp_path(out_path)
    tmp_path.unlink(missing_ok=True)

    proc = subprocess.run(
        build_command(
            video,
            tmp_path,
            position_s=position_s,
            ffmpeg=ffmpeg,
            width=width,
            quality=quality,
            measure_luma=measure_luma,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"抽帧失败 ({Path(video).name}): {proc.stderr.strip()[:400]}")
    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"封面产物为空: {out_path}")

    tmp_path.replace(out_path)
    return parse_luma(proc.stdout) if measure_luma else None


def _temp_path(out_path: Path) -> Path:
    """单文件版本的临时名（`.part` 放在扩展名之前）。"""
    return out_path.with_name(f"{out_path.stem}.part{out_path.suffix}")


def make_cover(
    video: str | Path,
    dst: str | Path,
    *,
    duration_s: float | None = None,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    ratios: tuple[float, ...] = CANDIDATE_RATIOS,
    luma_min: float = LUMA_MIN,
) -> Path:
    """抽一帧存成封面，失败抛 RuntimeError。

    依次尝试 `ratios` 里的位置，抽到亮度达标的就停；全都不达标时取最亮的一张。
    读不到亮度（老 ffmpeg / 滤镜异常）时按现状采用，不反复抽帧。

    **原子写入**：所有候选帧先写 `.part`，胜出者重命名成最终文件，其余删除。
    中途失败不会留下「存在但不完整」的封面——否则 `covers_for_dir` 的存在性
    检查会把它当作已完成而跳过。
    """
    out_path = Path(dst)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if duration_s is None:
        duration_s = probe_duration(video, ffprobe)

    rendered: list[tuple[float | None, Path]] = []
    try:
        for index, ratio in enumerate(ratios):
            candidate = _candidate_path(out_path, index)
            candidate.unlink(missing_ok=True)
            luma = render_frame(
                video,
                candidate,
                position_s=cover_position_seconds(duration_s, ratio=ratio),
                ffmpeg=ffmpeg,
                measure_luma=True,
            )
            rendered.append((luma, candidate))
            if luma is None or luma >= luma_min:
                break
    except BaseException:
        for _, candidate in rendered:
            candidate.unlink(missing_ok=True)
        raise

    best_luma, best_path = rendered[0]
    for luma, candidate in rendered:
        if luma is not None and (best_luma is None or luma > best_luma):
            best_luma, best_path = luma, candidate

    for _, candidate in rendered:
        if candidate != best_path:
            candidate.unlink(missing_ok=True)

    best_path.replace(out_path)
    return out_path


def covers_for_dir(
    video_dir: str | Path,
    out_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    prober=probe_duration,
    maker=make_cover,
    force: bool = False,
    on_progress=None,
) -> list[CoverResult]:
    """为一个目录下的所有剧集生成封面。

    已存在的封面默认跳过（可断点续跑）；`force=True` 则重新生成。
    时长只探测一次并传给 `maker`，避免重复调 ffprobe。
    """
    output_dir = Path(out_dir)
    results: list[CoverResult] = []

    for episode in find_episodes(video_dir):
        target = output_dir / f"S{episode.season:02d}E{episode.episode:02d}.jpg"

        if target.exists() and not force:
            result = CoverResult(
                season=episode.season,
                episode=episode.episode,
                output=target,
                skipped=True,
            )
        else:
            started = time.monotonic()
            duration = prober(episode.path, ffprobe)
            maker(episode.path, target, duration_s=duration, ffmpeg=ffmpeg)
            result = CoverResult(
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
    """命令行入口：为一个目录（通常是一季或全剧）批量生成封面。"""
    parser = argparse.ArgumentParser(description="按集抽帧生成卡片封面")
    parser.add_argument("video_dir", help="剧集（mp4）所在目录")
    parser.add_argument("-o", "--out-dir", required=True, help="封面输出目录")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe 可执行文件路径")
    parser.add_argument("--force", action="store_true", help="重新生成已存在的封面")
    args = parser.parse_args(argv)

    def report(item: CoverResult) -> None:
        tag = "跳过" if item.skipped else f"完成 {item.elapsed:5.2f}s"
        size = item.output.stat().st_size / 1024 if item.output.exists() else 0
        print(
            f"  S{item.season:02d}E{item.episode:02d}  {tag}  {size:7.1f} KB",
            flush=True,
        )

    started = time.monotonic()
    results = covers_for_dir(
        args.video_dir,
        args.out_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        force=args.force,
        on_progress=report,
    )

    made = sum(1 for r in results if not r.skipped)
    total = sum(r.output.stat().st_size for r in results if r.output.exists())
    print(
        f"\n共 {len(results)} 集（新生成 {made}）；合计 {total / 1048576:.1f} MB；"
        f"耗时 {time.monotonic() - started:.0f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
