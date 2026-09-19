"""按台词（cue）抽帧：让每张卡片显示「这句台词开始那一刻」的画面。

为什么要有它
------------
`covers.py` 是**按集**抽一张（同一集的所有台词共用一张图）。搜索结果里十条
台词常常来自同一集，于是十张卡片长得一模一样 —— 这正是 AGENTS.md 第 4 节
决策 5 里说的「卡片不够 Netflix」的根因。本模块改成**按 cue 抽**：
`cues/S01E01_0001.jpg` 对应 S01E01 第 1 条台词的起点画面。

实测（S01E01，422 条）：0.11 s/帧、320×180/q6 单张 6.5 KB
→ 全剧 117,842 条约 3.5 小时（单进程）、约 0.73 GB。

字幕与画面同步吗？——**同步**。抽样核对过：cue 19「Is this the high-iq sperm
bank?」(56.15s) 抽到的正是精子库前台，cue 20「If you have to ask…」(60.97s)
是前台医生，与台词内容吻合。

片头（演职员表）怎么处理
------------------------
部分集数的片头是**演职员表叠加在正片画面上**（不是独立片段），那几秒的 cue 会
抽出「starring Jim Parsons」这种字卡（实测 S01E01 的叠加点在 31.9s / 38s / 46s）。
**本项目默认照抽**（用户 2026-09-19 决定：字卡也是真实画面，不该人为过滤）。
若日后想跳过某段，用 `--intro-start 31000 --intro-end 47000`（两个都给出才生效）；
被跳过的 cue 不生成文件，由前端回退到该集封面（`poster` 字段）。

用法：
    python -m pipeline.cue_covers "D:/.../视频素材/web" -o "D:/.../视频素材/web/cues" --season 1
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .covers import render_frame
from .covers import build_command as _build_cover_command
from .store import connect

__all__ = [
    "FRAME_WIDTH",
    "FRAME_QUALITY",
    "INTRO_RANGE",
    "CueFrameResult",
    "frame_name",
    "frame_path",
    "is_intro",
    "build_cue_command",
    "make_cue_frame",
    "load_cues",
    "frames_for_season",
    "main",
]

# 卡片显示约 230×130 CSS px：320 宽已覆盖 2x 屏，比整集封面（640）小一半，
# 于是单张只有 ~6.5 KB（整集封面 ~25 KB）
FRAME_WIDTH = 320
FRAME_QUALITY = 6

# 可选的「跳过区间」（毫秒，右开）——**默认不跳过任何 cue**。
# 背景：部分集数的片头是演职员表**叠加在正片画面上**（不是独立片段），那几秒的 cue
# 会抽出「starring Jim Parsons」这种字卡（实测 S01E01 的叠加点在 31.9s / 38s / 46s）。
# 用户 2026-09-19 明确决定：**照抽**，字卡也是真实画面。
# 若日后想跳过，用 `--intro-start 31000 --intro-end 47000`（两个都要给）即可。
INTRO_RANGE: tuple[int, int] | None = None


@dataclass(frozen=True)
class CueFrameResult:
    """一条台词的抽帧结果。

    - `output is None` 表示落在片头区间、**故意不生成**（前端回退到该集封面）
    - `skipped` 表示产物已存在而跳过（可断点续跑）
    - `error` 非空表示这一条失败（不中断整批）
    """

    season: int
    episode: int
    cue_index: int
    output: Path | None
    skipped: bool
    intro: bool
    elapsed: float = 0.0
    error: str | None = None


def frame_name(season: int, episode: int, cue_index: int) -> str:
    """产物文件名（不含扩展名）：`S01E01_0001`。

    序号补零到 4 位：同一集的 cue 按字典序排序时就等于按时间排序
    （每集最多几百条，4 位足够）。
    """
    return f"S{season:02d}E{episode:02d}_{cue_index:04d}"


def frame_path(out_dir: str | Path, season: int, episode: int, cue_index: int) -> Path:
    """产物完整路径：`<out_dir>/S01E01_0001.jpg`。"""
    return Path(out_dir) / f"{frame_name(season, episode, cue_index)}.jpg"


def is_intro(start_ms: int, *, intro_range: tuple[int, int] | None = None) -> bool:
    """该起点是否落在「跳过区间」内（右开）。

    `intro_range=None`（默认）表示**不跳过任何 cue** —— 片头演职员表那段也照抽。
    """
    if intro_range is None:
        return False
    return intro_range[0] <= start_ms < intro_range[1]


def build_cue_command(
    video: str | Path,
    dst: str | Path,
    *,
    start_ms: int,
    ffmpeg: str = "ffmpeg",
    width: int = FRAME_WIDTH,
    quality: int = FRAME_QUALITY,
) -> list[str]:
    """构造抽帧命令（参数列表，禁止拼 shell 字符串）。"""
    return _build_cover_command(
        video,
        dst,
        position_s=start_ms / 1000,
        ffmpeg=ffmpeg,
        width=width,
        quality=quality,
    )


def make_cue_frame(
    video: str | Path,
    dst: str | Path,
    *,
    start_ms: int,
    ffmpeg: str = "ffmpeg",
    width: int = FRAME_WIDTH,
    quality: int = FRAME_QUALITY,
) -> Path:
    """抽「这条台词起点」那一帧。

    **不做亮度择优**（`covers.make_cover` 会）：画面必须对应这句台词，
    换到别的时刻就不叫「台词开头的画面」了 —— 即使那一帧偏暗。
    原子写入与失败清理统一由 `covers.render_frame` 负责。
    """
    render_frame(
        video,
        dst,
        position_s=start_ms / 1000,
        ffmpeg=ffmpeg,
        width=width,
        quality=quality,
    )
    return Path(dst)


def load_cues(db_path: str | Path, season: int | None = None) -> list[tuple[int, int, int, int]]:
    """从 clips 表读出 `(season, episode, cue_index, start_ms)`，按季/集/时间排序。"""
    sql = "SELECT season, episode, cue_index, start_ms FROM clips"
    params: tuple = ()
    if season is not None:
        sql += " WHERE season = ?"
        params = (season,)
    sql += " ORDER BY season, episode, start_ms"

    conn = connect(db_path)
    try:
        return [
            (int(row[0]), int(row[1]), int(row[2]), int(row[3]))
            for row in conn.execute(sql, params)
        ]
    finally:
        conn.close()


def frames_for_season(
    video_dir: str | Path,
    out_dir: str | Path,
    cues: list[tuple[int, int, int, int]],
    *,
    ffmpeg: str = "ffmpeg",
    force: bool = False,
    intro_range: tuple[int, int] | None = None,
    workers: int = 1,
    maker=make_cue_frame,
    on_progress=None,
) -> list[CueFrameResult]:
    """批量抽帧。

    - 已存在的产物默认跳过（可断点续跑），`force=True` 重新生成
    - 传入 `intro_range` 时，落在该区间的 cue 返回 `intro=True` 且不生成文件；
      **默认为 None（不跳过任何 cue）**
    - 单条失败只记 `error`，**不中断整批**（11.8 万条里个别失败很正常）
    - `workers > 1` 时多进程并行（抽帧是外部进程，并行能显著缩短总时长）
    """
    video_root = Path(video_dir)
    output_dir = Path(out_dir)

    def run(item: tuple[int, int, int, int]) -> CueFrameResult:
        season, episode, cue_index, start_ms = item

        if is_intro(start_ms, intro_range=intro_range):
            return CueFrameResult(season, episode, cue_index, None, False, True)

        target = frame_path(output_dir, season, episode, cue_index)
        if target.exists() and not force:
            return CueFrameResult(season, episode, cue_index, target, True, False)

        video = video_root / f"S{season:02d}E{episode:02d}.mp4"
        started = time.monotonic()
        try:
            maker(video, target, start_ms=start_ms, ffmpeg=ffmpeg)
        except Exception as exc:  # noqa: BLE001 - 单条失败不该中断整批
            return CueFrameResult(
                season,
                episode,
                cue_index,
                target,
                False,
                False,
                elapsed=time.monotonic() - started,
                error=str(exc),
            )
        return CueFrameResult(
            season,
            episode,
            cue_index,
            target,
            False,
            False,
            elapsed=time.monotonic() - started,
        )

    results: list[CueFrameResult] = []
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, item) for item in cues]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                if on_progress is not None:
                    on_progress(result)
        results.sort(key=lambda r: (r.season, r.episode, r.cue_index))
    else:
        for item in cues:
            result = run(item)
            results.append(result)
            if on_progress is not None:
                on_progress(result)

    return results


def main(argv: list[str] | None = None) -> int:
    """命令行入口：为（某一季的）全部台词抽起点帧。"""
    parser = argparse.ArgumentParser(description="按台词起点抽帧生成卡片缩略图")
    parser.add_argument("video_dir", help="剧集（mp4）所在目录")
    parser.add_argument("-o", "--out-dir", required=True, help="缩略图输出目录")
    parser.add_argument("-d", "--db", default="tbbt.db", help="数据库路径（默认 tbbt.db）")
    parser.add_argument("--season", type=int, help="只处理某一季（试水用）")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--workers", type=int, default=4, help="并行进程数（默认 4）")
    parser.add_argument("--force", action="store_true", help="重新生成已存在的缩略图")
    parser.add_argument(
        "--intro-start",
        type=int,
        help="可选：跳过区间起点（毫秒）。与 --intro-end 同时给出才生效，默认不跳过任何 cue",
    )
    parser.add_argument(
        "--intro-end", type=int, help="可选：跳过区间终点（毫秒，右开）"
    )
    args = parser.parse_args(argv)

    intro_range = (
        (args.intro_start, args.intro_end)
        if args.intro_start is not None and args.intro_end is not None
        else None
    )
    cues = load_cues(args.db, season=args.season)
    if not cues:
        print("没有读到任何 cue（检查 --db 与 --season）", flush=True)
        return 1

    scope = f"第 {args.season} 季" if args.season else "全剧"
    skip_note = (
        f"跳过区间 {intro_range[0] / 1000:.0f}s–{intro_range[1] / 1000:.0f}s"
        if intro_range is not None
        else "不跳过任何 cue（片头演职员表也照抽）"
    )
    print(f"{scope}：{len(cues)} 条台词；{skip_note}；并行 {args.workers}", flush=True)

    def report(item: CueFrameResult) -> None:
        code = f"S{item.season:02d}E{item.episode:02d}_{item.cue_index:04d}"
        if item.error is not None:
            print(f"  {code}  失败：{item.error[:120]}", flush=True)
        elif item.intro:
            print(f"  {code}  区间跳过", flush=True)
        elif item.skipped:
            print(f"  {code}  跳过", flush=True)
        else:
            size = item.output.stat().st_size / 1024 if item.output else 0
            print(f"  {code}  完成 {item.elapsed:5.2f}s  {size:6.1f} KB", flush=True)

    started = time.monotonic()
    results = frames_for_season(
        args.video_dir,
        args.out_dir,
        cues,
        ffmpeg=args.ffmpeg,
        force=args.force,
        intro_range=intro_range,
        workers=args.workers,
        on_progress=report,
    )

    made = sum(1 for r in results if r.output is not None and not r.skipped and r.error is None)
    skipped = sum(1 for r in results if r.skipped)
    intro = sum(1 for r in results if r.intro)
    failed = sum(1 for r in results if r.error is not None)
    total = sum(r.output.stat().st_size for r in results if r.output and r.output.exists())

    print(
        f"\n共 {len(results)} 条：生成 {made}，跳过 {skipped}，片头跳过 {intro}，失败 {failed}；"
        f"合计 {total / 1048576:.1f} MB；耗时 {time.monotonic() - started:.0f}s",
        flush=True,
    )
    if failed:
        print("失败项可重跑本命令补齐（已生成的会自动跳过）", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
