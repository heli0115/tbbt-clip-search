"""transcode 模块测试：转码命令构造与按季批量转码。

配方来源：AGENTS.md 第 4 节「转码配方」（720p / H.264 / 1.5Mbps，VMAF 实测定案）。
真跑 ffmpeg 属于集成测试，单元测试用注入的假转码器。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.transcode import build_command, transcode_season


def test_build_command_returns_arg_list() -> None:
    cmd = build_command(Path("a.mkv"), Path("a.mp4"))
    assert isinstance(cmd, list)
    assert all(isinstance(part, str) for part in cmd)


def test_build_command_uses_nvenc_h264() -> None:
    cmd = build_command(Path("a.mkv"), Path("a.mp4"))
    assert "h264_nvenc" in cmd


def test_build_command_scales_to_720p() -> None:
    cmd = build_command(Path("a.mkv"), Path("a.mp4"))
    assert "scale=1280:-2" in cmd


def test_build_command_sets_bitrate() -> None:
    cmd = build_command(Path("a.mkv"), Path("a.mp4"))
    assert "1500k" in cmd


def test_build_command_enables_faststart() -> None:
    cmd = build_command(Path("a.mkv"), Path("a.mp4"))
    assert "+faststart" in cmd


def test_build_command_keeps_paths_as_single_args() -> None:
    src = Path("D:/study/生活大爆炸/视频素材/Show S01E01.mkv")
    cmd = build_command(src, Path("out.mp4"))
    assert str(src) in cmd


def _fake_transcoder(calls: list):
    def fake(src, dst, ffmpeg="ffmpeg"):
        calls.append((Path(src), Path(dst)))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\x00" * 2048)
        return Path(dst)

    return fake


def test_transcode_season_processes_all_episodes(tmp_path) -> None:
    video_dir = tmp_path / "videos"
    out_dir = tmp_path / "web"
    video_dir.mkdir()
    for name in ["Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv"]:
        (video_dir / name).touch()

    calls: list = []
    results = transcode_season(
        video_dir, out_dir, transcoder=_fake_transcoder(calls)
    )

    assert len(results) == 2
    assert len(calls) == 2
    assert (out_dir / "S01E01.mp4").exists()
    assert (out_dir / "S01E02.mp4").exists()


def test_transcode_season_skips_existing_outputs(tmp_path) -> None:
    """已转码的集应跳过——批量任务要能断点续跑。"""
    video_dir = tmp_path / "videos"
    out_dir = tmp_path / "web"
    video_dir.mkdir()
    out_dir.mkdir()
    (video_dir / "Show.S01E01.1080p.mkv").touch()
    (out_dir / "S01E01.mp4").write_bytes(b"existing")

    calls: list = []
    results = transcode_season(
        video_dir, out_dir, transcoder=_fake_transcoder(calls)
    )

    assert calls == []
    assert results[0].skipped is True


def test_transcode_season_force_reencodes(tmp_path) -> None:
    video_dir = tmp_path / "videos"
    out_dir = tmp_path / "web"
    video_dir.mkdir()
    out_dir.mkdir()
    (video_dir / "Show.S01E01.1080p.mkv").touch()
    (out_dir / "S01E01.mp4").write_bytes(b"existing")

    calls: list = []
    transcode_season(
        video_dir, out_dir, transcoder=_fake_transcoder(calls), force=True
    )
    assert len(calls) == 1


def test_transcode_season_reports_progress(tmp_path) -> None:
    video_dir = tmp_path / "videos"
    out_dir = tmp_path / "web"
    video_dir.mkdir()
    (video_dir / "Show.S01E03.1080p.mkv").touch()

    seen: list = []
    transcode_season(
        video_dir,
        out_dir,
        transcoder=_fake_transcoder([]),
        on_progress=seen.append,
    )
    assert seen and seen[0].episode == 3


# ---------------------------------------------------------------------------
# 中断安全性：原子写入
# ---------------------------------------------------------------------------
def test_transcode_one_removes_partial_output_on_failure(tmp_path, monkeypatch) -> None:
    """转码失败/中断时不得留下最终产物，否则下次运行会误判为已完成而跳过。"""
    from pipeline import transcode as tr

    src = tmp_path / "Show.S01E01.mkv"
    src.touch()
    dst = tmp_path / "S01E01.mp4"

    class FakeProc:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: FakeProc())

    with pytest.raises(RuntimeError):
        tr.transcode_one(src, dst)

    assert not dst.exists(), "失败时不应留下最终产物"
    assert not (tmp_path / "S01E01.mp4.part").exists(), "临时文件应被清理"


def test_transcode_one_writes_atomically(tmp_path, monkeypatch) -> None:
    """成功路径：先写 .part，完成后原子重命名成最终文件。"""
    from pipeline import transcode as tr

    src = tmp_path / "Show.S01E01.mkv"
    src.touch()
    dst = tmp_path / "S01E01.mp4"

    def fake_run(cmd, **kwargs):
        out = Path(cmd[-1])
        assert out.name.endswith(".part"), "应当先写临时文件"
        out.write_bytes(b"x" * 512)

        class P:
            returncode = 0
            stderr = ""
        return P()

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    result = tr.transcode_one(src, dst)

    assert result == dst
    assert dst.exists()
    assert not (tmp_path / "S01E01.mp4.part").exists()


def test_build_command_specifies_format_for_temp_files() -> None:
    """临时文件 .mp4.part 无法从扩展名推断容器格式，必须显式 -f mp4。"""
    cmd = build_command(Path("a.mkv"), Path("a.mp4.part"))
    assert "-f" in cmd
    assert "mp4" in cmd


def test_build_command_omits_format_for_plain_mp4() -> None:
    cmd = build_command(Path("a.mkv"), Path("a.mp4"))
    assert "-f" not in cmd
