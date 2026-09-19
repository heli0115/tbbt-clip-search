"""batch 模块测试：剧集扫描、ffmpeg 命令构造、按季处理。

注意 ffmpeg 调用用「注入的假提取器」测试——真跑 ffmpeg 属于集成测试，
不该进单元测试（AGENTS.md 第 3 节：路径含中文，命令必须用参数列表）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.batch import (
    EpisodeFile,
    build_extract_command,
    find_episodes,
    process_season,
)
from pipeline.store import connect, count_clips, init_db, search

BILINGUAL_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "如果一个光子打向有两个狭缝的平面\n"
    '<font face="Calibri Italic">So if a photon is directed</font>\n'
    "\n"
    "2\n"
    "00:00:03,000 --> 00:00:04,000\n"
    "谢尔顿说话了\n"
    '<font face="Calibri Italic">Sheldon is talking</font>\n'
)


def _write_fake_extractor(calls: list, payload: str = BILINGUAL_SRT):
    """返回一个假提取器：记录调用、写出固定字幕，不碰 ffmpeg。"""

    def fake(mkv, out_srt, subtitle_stream=0, ffmpeg="ffmpeg"):
        calls.append((Path(mkv), Path(out_srt), subtitle_stream))
        Path(out_srt).parent.mkdir(parents=True, exist_ok=True)
        Path(out_srt).write_text(payload, encoding="utf-8")
        return Path(out_srt)

    return fake


# ---------------------------------------------------------------------------
# ffmpeg 命令构造
# ---------------------------------------------------------------------------
def test_build_extract_command_returns_arg_list() -> None:
    cmd = build_extract_command(Path("a.mkv"), Path("a.srt"))
    assert isinstance(cmd, list)
    assert all(isinstance(part, str) for part in cmd)


def test_build_extract_command_maps_subtitle_stream() -> None:
    cmd = build_extract_command(Path("a.mkv"), Path("a.srt"), subtitle_stream=0)
    assert "-map" in cmd
    assert "0:s:0" in cmd
    assert "-c:s" in cmd
    assert "srt" in cmd


def test_build_extract_command_honours_stream_index() -> None:
    cmd = build_extract_command(Path("a.mkv"), Path("a.srt"), subtitle_stream=2)
    assert "0:s:2" in cmd


def test_build_extract_command_keeps_paths_as_single_args() -> None:
    """含中文与空格的路径必须是独立参数，绝不能被拼进 shell 字符串。"""
    tricky = Path("D:/study/生活大爆炸/视频素材/Show S01E01.mkv")
    cmd = build_extract_command(tricky, Path("out.srt"))
    assert str(tricky) in cmd


# ---------------------------------------------------------------------------
# 剧集扫描
# ---------------------------------------------------------------------------
def test_find_episodes_sorts_by_season_and_episode(tmp_path) -> None:
    for name in [
        "The.Big.Bang.Theory.S01E03.1080p.mkv",
        "The.Big.Bang.Theory.S01E01.1080p.mkv",
        "The.Big.Bang.Theory.S01E02.1080p.mkv",
    ]:
        (tmp_path / name).touch()
    episodes = find_episodes(tmp_path)
    assert [e.episode for e in episodes] == [1, 2, 3]
    assert all(isinstance(e, EpisodeFile) for e in episodes)
    assert all(e.season == 1 for e in episodes)


def test_find_episodes_ignores_non_video_files(tmp_path) -> None:
    (tmp_path / "The.Big.Bang.Theory.S01E01.1080p.mkv").touch()
    (tmp_path / "The.Big.Bang.Theory_文档.txt").touch()
    (tmp_path / "readme.md").touch()
    assert len(find_episodes(tmp_path)) == 1


def test_find_episodes_raises_on_unparsable_video(tmp_path) -> None:
    (tmp_path / "random_clip.mkv").touch()
    with pytest.raises(ValueError, match="random_clip"):
        find_episodes(tmp_path)


def test_find_episodes_handles_real_frds_filename(tmp_path) -> None:
    name = (
        "The.Big.Bang.Theory.S01E01.2007.1080p.Blu-ray.x265.10bit."
        "AC3￡cXcY@FRDS.mkv"
    )
    (tmp_path / name).touch()
    episodes = find_episodes(tmp_path)
    assert [(e.season, e.episode) for e in episodes] == [(1, 1)]


# ---------------------------------------------------------------------------
# 按季处理
# ---------------------------------------------------------------------------
def test_process_season_extracts_parses_and_stores(tmp_path) -> None:
    video_dir = tmp_path / "videos"
    srt_dir = tmp_path / "subs"
    video_dir.mkdir()
    for name in ["Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv"]:
        (video_dir / name).touch()

    calls: list = []
    conn = connect(":memory:")
    init_db(conn)

    results = process_season(
        conn, video_dir, srt_dir, extractor=_write_fake_extractor(calls)
    )

    assert len(results) == 2
    assert len(calls) == 2
    assert count_clips(conn) == 4  # 每集 2 条
    assert len(search(conn, "photon")) == 2
    assert {r.episode for r in results} == {1, 2}
    assert all(r.cues == 2 for r in results)
    conn.close()


def test_process_season_reuses_existing_srt(tmp_path) -> None:
    """第二次运行不应重复提取——按季流水线要能断点续跑。"""
    video_dir = tmp_path / "videos"
    srt_dir = tmp_path / "subs"
    video_dir.mkdir()
    (video_dir / "Show.S01E01.1080p.mkv").touch()

    calls: list = []
    conn = connect(":memory:")
    init_db(conn)

    process_season(conn, video_dir, srt_dir, extractor=_write_fake_extractor(calls))
    process_season(conn, video_dir, srt_dir, extractor=_write_fake_extractor(calls))

    assert len(calls) == 1, "第二次应当复用已存在的字幕"
    assert count_clips(conn) == 2
    conn.close()


def test_process_season_force_reextracts(tmp_path) -> None:
    video_dir = tmp_path / "videos"
    srt_dir = tmp_path / "subs"
    video_dir.mkdir()
    (video_dir / "Show.S01E01.1080p.mkv").touch()

    calls: list = []
    conn = connect(":memory:")
    init_db(conn)

    process_season(conn, video_dir, srt_dir, extractor=_write_fake_extractor(calls))
    process_season(
        conn, video_dir, srt_dir, extractor=_write_fake_extractor(calls), force=True
    )

    assert len(calls) == 2
    assert count_clips(conn) == 2  # upsert，不重复
    conn.close()


def test_process_season_writes_expected_srt_names(tmp_path) -> None:
    video_dir = tmp_path / "videos"
    srt_dir = tmp_path / "subs"
    video_dir.mkdir()
    (video_dir / "Show.S01E07.1080p.mkv").touch()

    calls: list = []
    conn = connect(":memory:")
    init_db(conn)
    process_season(conn, video_dir, srt_dir, extractor=_write_fake_extractor(calls))

    assert (srt_dir / "S01E07.bilingual.srt").exists()
    conn.close()
