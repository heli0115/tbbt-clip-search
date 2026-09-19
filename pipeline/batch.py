"""批量处理：从剧集文件提取字幕轨 → 解析 → 入库。

对应 AGENTS.md 的「按季流水线」：一次处理一季而不是全 12 季，避免 D 盘空间爆掉。

典型用法：
    python -m pipeline.batch "D:/study/生活大爆炸/视频素材/生活大爆炸S01..." \\
        -d tbbt.db --ffmpeg "D:/ffmpeg/bin/ffmpeg.exe"
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .parse_srt import parse_srt_file
from .store import connect, count_clips, init_db, insert_cues, parse_episode_code

__all__ = [
    "EpisodeFile",
    "EpisodeResult",
    "find_episodes",
    "build_extract_command",
    "extract_subtitle",
    "process_season",
    "main",
]

_VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}


@dataclass(frozen=True)
class EpisodeFile:
    """一个可识别的剧集文件。"""

    path: Path
    season: int
    episode: int


@dataclass(frozen=True)
class EpisodeResult:
    """单集处理结果。"""

    season: int
    episode: int
    cues: int
    srt_path: Path
    extracted: bool


def find_episodes(video_dir: str | Path) -> list[EpisodeFile]:
    """扫描目录，返回按 (季, 集) 排序的剧集文件。

    无法从文件名解析出季集号的视频文件会**直接报错**而不是被静默跳过——
    漏掉一集的代价远大于中途失败的代价。
    """
    directory = Path(video_dir)
    if not directory.is_dir():
        raise NotADirectoryError(f"不是目录: {directory}")

    episodes: list[EpisodeFile] = []
    unknown: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        try:
            season, episode = parse_episode_code(path.name)
        except ValueError:
            unknown.append(path.name)
            continue
        episodes.append(EpisodeFile(path=path, season=season, episode=episode))

    if unknown:
        raise ValueError(f"以下文件无法解析出季集号: {unknown}")
    return sorted(episodes, key=lambda item: (item.season, item.episode))


def build_extract_command(
    mkv: str | Path,
    out_srt: str | Path,
    subtitle_stream: int = 0,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """构造提取字幕的 ffmpeg 命令，返回**参数列表**。

    硬性纪律（AGENTS.md 第 3 节）：素材路径含中文，用 shell 字符串拼接必然出事。
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(mkv),
        "-map",
        f"0:s:{subtitle_stream}",
        "-c:s",
        "srt",
        str(out_srt),
    ]


def extract_subtitle(
    mkv: str | Path,
    out_srt: str | Path,
    subtitle_stream: int = 0,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """调用 ffmpeg 把第 N 条字幕流提取成 SRT 文件。"""
    out_path = Path(out_srt)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    command = build_extract_command(mkv, out_path, subtitle_stream, ffmpeg)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"提取字幕失败 ({Path(mkv).name}): {proc.stderr.strip()[:400]}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"提取出的字幕为空: {out_path}")
    return out_path


def process_season(
    conn,
    video_dir: str | Path,
    srt_dir: str | Path,
    *,
    subtitle_stream: int = 0,
    extractor=extract_subtitle,
    force: bool = False,
    ffmpeg: str = "ffmpeg",
) -> list[EpisodeResult]:
    """处理一季：逐集「提取字幕 → 解析 → 入库」。

    已存在的 SRT 默认复用（按季流水线要能断点续跑）；`force=True` 则重新提取。
    入库走 upsert，重复运行不会产生重复行。
    """
    results: list[EpisodeResult] = []
    for episode in find_episodes(video_dir):
        srt_path = Path(srt_dir) / f"S{episode.season:02d}E{episode.episode:02d}.bilingual.srt"

        extracted = False
        if force or not srt_path.exists():
            extractor(episode.path, srt_path, subtitle_stream, ffmpeg)
            extracted = True

        cues = parse_srt_file(srt_path)
        insert_cues(
            conn, cues, season=episode.season, episode=episode.episode
        )
        results.append(
            EpisodeResult(
                season=episode.season,
                episode=episode.episode,
                cues=len(cues),
                srt_path=srt_path,
                extracted=extracted,
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    """命令行入口：把一整季的剧集字幕提取、解析并写入索引。"""
    parser = argparse.ArgumentParser(description="批量提取剧集字幕并入库")
    parser.add_argument("video_dir", help="剧集所在目录")
    parser.add_argument("-d", "--db", default="tbbt.db", help="数据库路径")
    parser.add_argument("--srt-dir", default="subtitles", help="字幕输出目录")
    parser.add_argument(
        "--stream", type=int, default=0, help="字幕流序号（TBBT 为 0 = 中上英下）"
    )
    parser.add_argument("--force", action="store_true", help="重新提取已存在的字幕")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)
    before = count_clips(conn)

    results = process_season(
        conn,
        args.video_dir,
        args.srt_dir,
        subtitle_stream=args.stream,
        force=args.force,
        ffmpeg=args.ffmpeg,
    )

    for item in results:
        mark = "提取" if item.extracted else "复用"
        print(f"  S{item.season:02d}E{item.episode:02d}  {mark}  {item.cues:>4} 条")

    after = count_clips(conn)
    print(f"\n处理 {len(results)} 集；库内 {before} -> {after} 条（净增 {after - before}）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
