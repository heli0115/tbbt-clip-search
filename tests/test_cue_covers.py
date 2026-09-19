"""cue_covers 模块测试：按台词起点抽帧、片头区间跳过、批量与断点续跑。

方案来源：AGENTS.md 第 4 节决策 5（卡片缩略图 = 台词起点画面）。
真跑 ffmpeg 属于集成测试，这里 monkeypatch 掉 `pipeline.covers.subprocess.run`。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline import covers, cue_covers
from pipeline.cue_covers import (
    INTRO_RANGE,
    build_cue_command,
    frame_name,
    frame_path,
    frames_for_season,
    is_intro,
    load_cues,
    make_cue_frame,
)


def _fake_run(calls: list, *, returncode: int = 0, write_bytes: int = 1024):
    """`subprocess.run` 替身：记录调用并写出「产物」。"""

    def fake(args, **kwargs):
        calls.append(list(args))
        if returncode == 0 and write_bytes:
            Path(args[-1]).write_bytes(b"\xff\xd8\xff" + b"\x00" * write_bytes)
        return subprocess.CompletedProcess(args, returncode, "", "")

    return fake


# ---------------------------------------------------------------------------
# 命名与路径
# ---------------------------------------------------------------------------


def test_frame_name_pads_cue_index() -> None:
    assert frame_name(1, 1, 1) == "S01E01_0001"
    assert frame_name(12, 24, 422) == "S12E24_0422"


def test_frame_path_uses_out_dir() -> None:
    path = frame_path(Path("web/cues"), 2, 19, 146)
    assert path.name == "S02E19_0146.jpg"
    assert path.parent == Path("web/cues")


# ---------------------------------------------------------------------------
# 片头区间
# ---------------------------------------------------------------------------


def test_is_intro_is_off_by_default() -> None:
    """本项目决定「照抽」：片头演职员表那段也是真实画面，默认不跳过任何 cue。"""
    assert INTRO_RANGE is None
    assert is_intro(31_900) is False
    assert is_intro(38_000) is False


def test_is_intro_marks_given_window() -> None:
    """显式给出区间时才生效（S01E01 实测叠加点在 31.9s / 38s / 46s）。"""
    window = (31_000, 47_000)
    assert is_intro(31_900, intro_range=window) is True
    assert is_intro(38_000, intro_range=window) is True
    assert is_intro(46_000, intro_range=window) is True


def test_is_intro_excludes_normal_dialogue() -> None:
    window = (31_000, 47_000)
    assert is_intro(2_380, intro_range=window) is False  # 开场旁白
    assert is_intro(25_050, intro_range=window) is False  # 区间之前
    assert is_intro(56_150, intro_range=window) is False  # 区间之后
    assert is_intro(window[0] - 1, intro_range=window) is False
    assert is_intro(window[1], intro_range=window) is False  # 右开区间


def test_is_intro_accepts_custom_range() -> None:
    assert is_intro(15_000, intro_range=(10_000, 20_000)) is True
    assert is_intro(15_000, intro_range=(16_000, 20_000)) is False


# ---------------------------------------------------------------------------
# 命令与单帧抽取
# ---------------------------------------------------------------------------


def test_build_cue_command_seeks_before_input_and_uses_cue_start() -> None:
    cmd = build_cue_command(Path("S01E01.mp4"), Path("out.jpg"), start_ms=2_380)
    assert cmd[cmd.index("-ss") + 1] == "2.380"
    assert cmd.index("-ss") < cmd.index("-i")


def test_build_cue_command_uses_small_card_size() -> None:
    """卡片显示约 230×130 CSS px，320 宽已覆盖 2x 屏，不必用整集封面的 640。"""
    cmd = build_cue_command(Path("a.mp4"), Path("out.jpg"), start_ms=1_000)
    assert f"scale={cue_covers.FRAME_WIDTH}:-2" in cmd
    assert cmd[cmd.index("-q:v") + 1] == str(cue_covers.FRAME_QUALITY)


def test_build_cue_command_seeks_before_input() -> None:
    cmd = build_cue_command(Path("S01E01.mp4"), Path("out.jpg"), start_ms=123_456)
    assert cmd[cmd.index("-ss") + 1] == "123.456"
    assert cmd.index("-ss") < cmd.index("-i")


def test_make_cue_frame_writes_atomically(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls))

    out = make_cue_frame(tmp_path / "S01E01.mp4", tmp_path / "S01E01_0001.jpg", start_ms=2_380)

    assert out.exists()
    assert out.stat().st_size > 0
    # 临时文件（.part 在扩展名之前）必须已被重命名掉
    assert list(out.parent.glob("*.part.jpg")) == []


def test_make_cue_frame_propagates_failure(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(covers.subprocess, "run", _fake_run(calls, returncode=1))

    target = tmp_path / "S01E01_0001.jpg"
    with pytest.raises(RuntimeError, match="抽帧失败"):
        make_cue_frame(tmp_path / "S01E01.mp4", target, start_ms=2_380)

    assert not target.exists()
    assert list(target.parent.glob("*.part.jpg")) == []


# ---------------------------------------------------------------------------
# 从数据库读 cue
# ---------------------------------------------------------------------------


def test_load_cues_reads_episode_and_start(tmp_path) -> None:
    from pipeline.parse_srt import Cue
    from pipeline.store import connect, init_db, insert_cues

    db = tmp_path / "t.db"
    conn = connect(db)
    init_db(conn)
    insert_cues(
        conn,
        [Cue(1, 2_380, 4_840, "甲", "a"), Cue(2, 4_960, 6_530, "乙", "b")],
        season=1,
        episode=1,
    )
    insert_cues(conn, [Cue(1, 1_000, 2_000, "丙", "c")], season=2, episode=1)
    conn.close()

    rows = load_cues(db)
    assert [tuple(row) for row in rows] == [
        (1, 1, 1, 2_380),
        (1, 1, 2, 4_960),
        (2, 1, 1, 1_000),
    ]


def test_load_cues_can_filter_by_season(tmp_path) -> None:
    from pipeline.parse_srt import Cue
    from pipeline.store import connect, init_db, insert_cues

    db = tmp_path / "t.db"
    conn = connect(db)
    init_db(conn)
    insert_cues(conn, [Cue(1, 2_380, 4_840, "甲", "a")], season=1, episode=1)
    insert_cues(conn, [Cue(1, 1_000, 2_000, "丙", "c")], season=2, episode=1)
    conn.close()

    assert [tuple(row) for row in load_cues(db, season=2)] == [(2, 1, 1, 1_000)]


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------


def _fake_maker(calls: list, *, fail_on: tuple[int, int, int] | None = None):
    def fake(video, dst, *, start_ms, ffmpeg="ffmpeg"):
        calls.append((Path(video).name, Path(dst).name, start_ms, ffmpeg))
        if fail_on is not None and Path(dst).name == f"{frame_name(*fail_on)}.jpg":
            raise RuntimeError("模拟失败")
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\xff\xd8\xff")
        return Path(dst)

    return fake


def _make_videos(tmp_path, codes: list[tuple[int, int]]) -> Path:
    video_dir = tmp_path / "web"
    video_dir.mkdir(exist_ok=True)
    for season, episode in codes:
        (video_dir / f"S{season:02d}E{episode:02d}.mp4").touch()
    return video_dir


def test_frames_for_season_writes_one_frame_per_cue(tmp_path) -> None:
    video_dir = _make_videos(tmp_path, [(1, 1)])
    cues = [(1, 1, 1, 2_380), (1, 1, 2, 4_960)]
    made: list = []

    results = frames_for_season(
        video_dir, tmp_path / "cues", cues, maker=_fake_maker(made)
    )

    assert [r.output.name for r in results] == ["S01E01_0001.jpg", "S01E01_0002.jpg"]
    assert all(not r.skipped and not r.intro and r.error is None for r in results)
    assert [call[2] for call in made] == [2_380, 4_960]  # 用的是 cue 起点
    assert [call[0] for call in made] == ["S01E01.mp4", "S01E01.mp4"]


def test_frames_for_season_generates_intro_credits_by_default(tmp_path) -> None:
    """默认照抽：片头（演职员表叠加）那几条也要有自己的图。"""
    video_dir = _make_videos(tmp_path, [(1, 1)])
    cues = [(1, 1, 1, 2_380), (1, 1, 11, 31_100)]
    made: list = []

    results = frames_for_season(
        video_dir, tmp_path / "cues", cues, maker=_fake_maker(made)
    )

    assert [r.intro for r in results] == [False, False]
    assert len(made) == 2
    assert (tmp_path / "cues" / "S01E01_0011.jpg").exists()


def test_frames_for_season_can_skip_a_given_window(tmp_path) -> None:
    """`intro_range` 只是可选开关（默认关）：给出时，区间内的 cue 不生成图。"""
    video_dir = _make_videos(tmp_path, [(1, 1)])
    cues = [(1, 1, 1, 2_380), (1, 1, 11, 31_100), (1, 1, 12, 38_000)]
    made: list = []

    results = frames_for_season(
        video_dir,
        tmp_path / "cues",
        cues,
        maker=_fake_maker(made),
        intro_range=(31_000, 47_000),
    )

    assert [r.intro for r in results] == [False, True, True]
    assert [r.output for r in results[1:]] == [None, None]
    assert len(made) == 1  # 只抽了区间外那一条
    assert not (tmp_path / "cues" / "S01E01_0011.jpg").exists()


def test_frames_for_season_skips_existing(tmp_path) -> None:
    video_dir = _make_videos(tmp_path, [(1, 1)])
    out_dir = tmp_path / "cues"
    out_dir.mkdir()
    (out_dir / "S01E01_0001.jpg").write_bytes(b"\xff\xd8\xff")
    made: list = []

    results = frames_for_season(
        video_dir, out_dir, [(1, 1, 1, 2_380)], maker=_fake_maker(made)
    )

    assert [r.skipped for r in results] == [True]
    assert made == []


def test_frames_for_season_force_regenerates(tmp_path) -> None:
    video_dir = _make_videos(tmp_path, [(1, 1)])
    out_dir = tmp_path / "cues"
    out_dir.mkdir()
    (out_dir / "S01E01_0001.jpg").write_bytes(b"\xff\xd8\xff")
    made: list = []

    results = frames_for_season(
        video_dir, out_dir, [(1, 1, 1, 2_380)], maker=_fake_maker(made), force=True
    )

    assert [r.skipped for r in results] == [False]
    assert len(made) == 1


def test_frames_for_season_reports_progress(tmp_path) -> None:
    video_dir = _make_videos(tmp_path, [(1, 1)])
    seen: list = []
    frames_for_season(
        video_dir,
        tmp_path / "cues",
        [(1, 1, 1, 2_380), (1, 1, 2, 4_960)],
        maker=_fake_maker([]),
        on_progress=seen.append,
    )

    assert [r.cue_index for r in seen] == [1, 2]


def test_single_failure_does_not_abort_the_batch(tmp_path) -> None:
    """11.8 万条里有个别失败很正常，不能为此中断整批。"""
    video_dir = _make_videos(tmp_path, [(1, 1)])
    cues = [(1, 1, 1, 2_380), (1, 1, 2, 4_960)]

    results = frames_for_season(
        video_dir,
        tmp_path / "cues",
        cues,
        maker=_fake_maker([], fail_on=(1, 1, 2)),
    )

    assert results[0].error is None
    assert results[1].error is not None
    assert "模拟失败" in results[1].error


def test_frames_for_season_works_across_episodes(tmp_path) -> None:
    video_dir = _make_videos(tmp_path, [(1, 1), (1, 2)])
    cues = [(1, 1, 1, 2_380), (1, 2, 1, 3_000)]
    made: list = []

    frames_for_season(video_dir, tmp_path / "cues", cues, maker=_fake_maker(made))

    assert [call[0] for call in made] == ["S01E01.mp4", "S01E02.mp4"]
