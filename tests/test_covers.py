"""covers 模块测试：抽帧命令构造与按集批量生成封面。

方案来源：AGENTS.md 第 2 节「封面图来源」与第 4 节决策 5。
真跑 ffmpeg 属于集成测试，单元测试用 monkeypatch 替掉 `subprocess.run`。

亮度择优为什么必要（实测）：S12E24 在时长 40% 处的 YAVG 只有 **29.97**
（夜戏），同一集在 25% 处 69.5、70% 处 112.2 —— 固定单点会抽出「黑乎乎」的卡片。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline import covers
from pipeline.covers import (
    CANDIDATE_RATIOS,
    build_command,
    build_duration_command,
    cover_position_seconds,
    covers_for_dir,
    make_cover,
    parse_luma,
    probe_duration,
)


def _fake_run(
    calls: list,
    *,
    returncode: int = 0,
    stdout: str = "",
    write_bytes: int = 2048,
    luma_seq: list[float | None] | None = None,
):
    """`subprocess.run` 的替身：记录调用、写出「产物」，并可按次返回不同亮度。"""
    state = {"renders": 0}

    def fake(args, **kwargs):
        calls.append((list(args), kwargs))
        if "ffprobe" in Path(args[0]).name:
            return subprocess.CompletedProcess(args, returncode, stdout, "")

        # 抽帧调用
        if returncode == 0 and write_bytes:
            Path(args[-1]).write_bytes(b"\xff\xd8\xff" + b"\x00" * write_bytes)

        out = ""
        if luma_seq is not None and returncode == 0:
            value = luma_seq[min(state["renders"], len(luma_seq) - 1)]
            state["renders"] += 1
            if value is not None:
                out = f"lavfi.signalstats.YAVG={value}\n"
        return subprocess.CompletedProcess(args, returncode, out, "")

    return fake


# ---------------------------------------------------------------------------
# 时间点选择
# ---------------------------------------------------------------------------


def test_cover_position_uses_ratio() -> None:
    # S01E01 实测时长 1377.984s；首个候选点仍是 40%
    assert cover_position_seconds(1377.984) == pytest.approx(551.1936)


def test_first_candidate_ratio_is_stable() -> None:
    """默认候选点必须仍是 40%：它在绝大多数集数上都抽到干净正片画面。"""
    assert CANDIDATE_RATIOS[0] == pytest.approx(0.4)


def test_candidate_ratios_are_distinct_and_in_range() -> None:
    assert len(set(CANDIDATE_RATIOS)) == len(CANDIDATE_RATIOS)
    assert all(0.0 < ratio < 1.0 for ratio in CANDIDATE_RATIOS)


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------


def test_build_duration_command_returns_arg_list() -> None:
    cmd = build_duration_command(Path("a.mp4"))
    assert isinstance(cmd, list)
    assert all(isinstance(part, str) for part in cmd)
    assert "format=duration" in cmd


def test_commands_keep_chinese_paths_as_single_args() -> None:
    video = Path("D:/study/生活大爆炸/视频素材/web/S01E01.mp4")
    assert str(video) in build_duration_command(video)
    assert str(video) in build_command(video, Path("out.jpg"), position_s=10.0)


def test_build_command_seeks_before_input() -> None:
    """`-ss` 必须在 `-i` 之前：输入前 seek 是快速定位，之后是解码丢弃（慢得多）。"""
    cmd = build_command(Path("a.mp4"), Path("out.jpg"), position_s=551.194)
    assert cmd.index("-ss") < cmd.index("-i")


def test_build_command_seek_keeps_millisecond_precision() -> None:
    cmd = build_command(Path("a.mp4"), Path("out.jpg"), position_s=551.1936)
    assert cmd[cmd.index("-ss") + 1] == "551.194"


def test_build_command_requests_single_frame() -> None:
    cmd = build_command(Path("a.mp4"), Path("out.jpg"), position_s=1.0)
    assert cmd[cmd.index("-frames:v") + 1] == "1"


def test_build_command_scales_to_cover_width() -> None:
    cmd = build_command(Path("a.mp4"), Path("out.jpg"), position_s=1.0)
    filters = cmd[cmd.index("-vf") + 1]
    assert filters.startswith(f"scale={covers.COVER_WIDTH}:-2")


def test_build_command_sets_jpeg_quality() -> None:
    cmd = build_command(Path("a.mp4"), Path("out.jpg"), position_s=1.0)
    assert cmd[cmd.index("-q:v") + 1] == str(covers.COVER_QUALITY)


def test_build_command_never_forces_container_format() -> None:
    """不能加 `-f image2`：临时文件名保留了 .jpg 结尾，让 ffmpeg 自己推断即可。"""
    cmd = build_command(Path("a.mp4"), Path("S01E01.part.jpg"), position_s=1.0)
    assert "-f" not in cmd


def test_build_command_can_measure_luma_in_one_pass() -> None:
    """抽帧与亮度测量合并成一次调用，省掉一半 ffmpeg 进程。"""
    cmd = build_command(Path("a.mp4"), Path("out.jpg"), position_s=1.0, measure_luma=True)
    filters = cmd[cmd.index("-vf") + 1]
    assert "signalstats" in filters
    assert "metadata=print:file=-" in filters


def test_build_command_omits_luma_filters_by_default() -> None:
    cmd = build_command(Path("a.mp4"), Path("out.jpg"), position_s=1.0)
    assert "signalstats" not in cmd[cmd.index("-vf") + 1]


# ---------------------------------------------------------------------------
# 亮度解析
# ---------------------------------------------------------------------------


def test_parse_luma_reads_yavg() -> None:
    out = "frame:0    pts:0       pts_time:0\nlavfi.signalstats.YAVG=29.9747\n"
    assert parse_luma(out) == pytest.approx(29.9747)


def test_parse_luma_returns_none_when_missing() -> None:
    assert parse_luma("nothing here") is None


# ---------------------------------------------------------------------------
# 临时文件命名
# ---------------------------------------------------------------------------


def test_candidate_path_keeps_jpg_extension() -> None:
    """候选文件名必须保留 .jpg 结尾。

    踩过的坑（同类问题在 transcode 上真实发生过）：写成 `.jpg.part` 后 ffmpeg
    无法从扩展名推断格式，会直接报错——所以临时用的 `.part` 要放在扩展名**之前**
    （由 `render_frame` 负责），候选最终名则保持干净。
    """
    candidate = covers._candidate_path(Path("covers/S01E01.jpg"), 0)
    assert candidate.name == "S01E01.cand0.jpg"
    assert candidate.suffix == ".jpg"


def test_temp_path_puts_part_before_extension() -> None:
    assert covers._temp_path(Path("covers/S01E01.jpg")).name == "S01E01.part.jpg"


def test_candidate_paths_are_distinct_per_candidate() -> None:
    first = covers._candidate_path(Path("covers/S01E01.jpg"), 0)
    second = covers._candidate_path(Path("covers/S01E01.jpg"), 1)
    assert first != second


# ---------------------------------------------------------------------------
# 单集抽帧
# ---------------------------------------------------------------------------


def test_make_cover_writes_atomically(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls))

    target = tmp_path / "covers" / "S01E01.jpg"
    out = make_cover(tmp_path / "S01E01.mp4", target, duration_s=1000.0)

    assert out == target
    assert target.exists()
    assert target.stat().st_size > 0
    # 临时文件必须已经被重命名掉，不能留在磁盘上
    assert list(target.parent.glob("*.part.jpg")) == []
    assert target.parent.is_dir()


def test_make_cover_uses_duration_to_pick_position(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls))

    make_cover(tmp_path / "S01E01.mp4", tmp_path / "S01E01.jpg", duration_s=1000.0)

    args = calls[-1][0]
    assert args[args.index("-ss") + 1] == "400.000"


def test_make_cover_probes_duration_when_not_given(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, stdout="1377.984\n"))

    make_cover(tmp_path / "S01E01.mp4", tmp_path / "S01E01.jpg")

    # 第一次调用是 ffprobe，之后才是抽帧
    assert len(calls) == 2
    assert "-show_entries" in calls[0][0]
    args = calls[-1][0]
    assert args[args.index("-ss") + 1] == "551.194"


def test_make_cover_stops_at_first_bright_candidate(monkeypatch, tmp_path) -> None:
    """首个候选点亮度达标 → 不浪费后续抽帧。"""
    calls: list = []
    monkeypatch.setattr(
        covers.subprocess, "run", _fake_run(calls, luma_seq=[85.0, 90.0, 95.0, 99.0])
    )

    make_cover(tmp_path / "S01E01.mp4", tmp_path / "S01E01.jpg", duration_s=1000.0)

    assert len(calls) == 1


def test_make_cover_retries_after_dark_candidate(monkeypatch, tmp_path) -> None:
    """S12E24 的真实情况：40% 处 YAVG=29.97（暗），25% 处 69.5（可用）。"""
    calls: list = []
    monkeypatch.setattr(
        covers.subprocess, "run", _fake_run(calls, luma_seq=[29.97, 69.5, 56.3, 112.2])
    )

    make_cover(tmp_path / "S01E01.mp4", tmp_path / "S01E01.jpg", duration_s=1000.0)

    assert len(calls) == 2
    second = calls[-1][0]
    assert second[second.index("-ss") + 1] == f"{1000.0 * CANDIDATE_RATIOS[1]:.3f}"


def test_make_cover_keeps_brightest_when_all_candidates_dark(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(
        covers.subprocess, "run", _fake_run(calls, luma_seq=[30.0, 20.0, 44.0, 41.0])
    )

    target = tmp_path / "S01E01.jpg"
    make_cover(tmp_path / "S01E01.mp4", target, duration_s=1000.0)

    # 4 个候选点全部试过（阈值 45 一个都没过）
    assert len(calls) == len(CANDIDATE_RATIOS)
    assert target.exists()
    assert list(target.parent.glob("*.part.jpg")) == []


def test_make_cover_cleans_up_losing_candidates(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(
        covers.subprocess, "run", _fake_run(calls, luma_seq=[10.0, 200.0, None, None])
    )

    target = tmp_path / "S01E01.jpg"
    make_cover(tmp_path / "S01E01.mp4", target, duration_s=1000.0)

    assert [p.name for p in target.parent.iterdir()] == ["S01E01.jpg"]


def test_make_cover_does_not_retry_when_luma_unavailable(monkeypatch, tmp_path) -> None:
    """读不到 YAVG（老版本 ffmpeg / 滤镜异常）时按现状采用，不反复抽帧。"""
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, luma_seq=[None]))

    make_cover(tmp_path / "S01E01.mp4", tmp_path / "S01E01.jpg", duration_s=1000.0)

    assert len(calls) == 1


def test_make_cover_removes_partial_output_on_failure(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, returncode=1))

    target = tmp_path / "S01E01.jpg"
    with pytest.raises(RuntimeError, match="抽帧失败"):
        make_cover(tmp_path / "S01E01.mp4", target, duration_s=1000.0)

    assert not target.exists()
    assert list(target.parent.glob("*.part.jpg")) == []


def test_make_cover_rejects_empty_output(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, write_bytes=0))

    target = tmp_path / "S01E01.jpg"
    with pytest.raises(RuntimeError, match="封面产物为空"):
        make_cover(tmp_path / "S01E01.mp4", target, duration_s=1000.0)

    assert not target.exists()
    assert list(target.parent.glob("*.part.jpg")) == []


def test_probe_duration_parses_ffprobe_output(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, stdout="1377.984000\n"))

    assert probe_duration(Path("S01E01.mp4")) == pytest.approx(1377.984)


def test_probe_duration_raises_on_garbage(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, stdout="N/A\n"))

    with pytest.raises(RuntimeError, match="无法解析时长"):
        probe_duration(Path("S01E01.mp4"))


def test_probe_duration_raises_on_nonzero_exit(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, returncode=1))

    with pytest.raises(RuntimeError, match="探测时长失败"):
        probe_duration(Path("S01E01.mp4"))


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------


def _fake_prober(calls: list):
    def fake(video, ffprobe="ffprobe") -> float:
        calls.append(Path(video))
        return 1000.0

    return fake


def _fake_maker(calls: list):
    def fake(video, dst, *, duration_s=None, ffprobe="ffprobe", ffmpeg="ffmpeg"):
        calls.append((Path(video), Path(dst), duration_s))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\xff\xd8\xff")
        return Path(dst)

    return fake


def test_covers_for_dir_generates_one_cover_per_episode(tmp_path) -> None:
    video_dir = tmp_path / "web"
    out_dir = tmp_path / "web" / "covers"
    video_dir.mkdir()
    for name in ["S01E01.mp4", "S01E02.mp4", "S01E10.mp4"]:
        (video_dir / name).touch()

    made: list = []
    results = covers_for_dir(
        video_dir, out_dir, prober=_fake_prober([]), maker=_fake_maker(made)
    )

    assert [r.output.name for r in results] == [
        "S01E01.jpg",
        "S01E02.jpg",
        "S01E10.jpg",
    ]
    assert all(not r.skipped for r in results)
    assert all(Path(r.output).exists() for r in results)


def test_covers_for_dir_skips_existing_covers(tmp_path) -> None:
    video_dir = tmp_path / "web"
    out_dir = tmp_path / "web" / "covers"
    video_dir.mkdir()
    out_dir.mkdir(parents=True)
    (video_dir / "S01E01.mp4").touch()
    (out_dir / "S01E01.jpg").write_bytes(b"\xff\xd8\xff")

    made: list = []
    results = covers_for_dir(
        video_dir, out_dir, prober=_fake_prober([]), maker=_fake_maker(made)
    )

    assert [r.skipped for r in results] == [True]
    assert made == []


def test_covers_for_dir_force_regenerates(tmp_path) -> None:
    video_dir = tmp_path / "web"
    out_dir = tmp_path / "covers"
    video_dir.mkdir()
    out_dir.mkdir(parents=True)
    (video_dir / "S01E01.mp4").touch()
    (out_dir / "S01E01.jpg").write_bytes(b"\xff\xd8\xff")

    made: list = []
    results = covers_for_dir(
        video_dir,
        out_dir,
        prober=_fake_prober([]),
        maker=_fake_maker(made),
        force=True,
    )

    assert [r.skipped for r in results] == [False]
    assert len(made) == 1


def test_covers_for_dir_reports_progress(tmp_path) -> None:
    video_dir = tmp_path / "web"
    video_dir.mkdir()
    (video_dir / "S01E01.mp4").touch()
    (video_dir / "S01E02.mp4").touch()

    seen: list = []
    covers_for_dir(
        video_dir,
        tmp_path / "covers",
        prober=_fake_prober([]),
        maker=_fake_maker([]),
        on_progress=seen.append,
    )

    assert [r.episode for r in seen] == [1, 2]


def test_covers_for_dir_passes_probed_duration_to_maker(tmp_path) -> None:
    video_dir = tmp_path / "web"
    video_dir.mkdir()
    (video_dir / "S01E01.mp4").touch()

    made: list = []
    covers_for_dir(
        video_dir, tmp_path / "covers", prober=_fake_prober([]), maker=_fake_maker(made)
    )

    assert made[0][2] == 1000.0
