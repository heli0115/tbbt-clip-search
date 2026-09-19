"""`pipeline/upload_r2.py` 的纯逻辑测试。

不联网、不依赖 boto3 —— boto3 只在真正上传时延迟导入，
所以这里能直接测「哪些文件要传、怎么判定」这部分规则。
"""

from __future__ import annotations

from pathlib import Path

from pipeline.upload_r2 import (
    CACHE_CONTROL,
    find_videos,
    needs_upload,
    object_key,
    upload_one,
)

# ---------------------------------------------------------------------------
# object_key / find_videos
# ---------------------------------------------------------------------------
def test_object_key_is_bare_filename() -> None:
    """R2 里必须是裸文件名——前端按 https://video.<域名>/S01E01.mp4 取。"""
    assert object_key(Path(r"D:\videos\S01E01.mp4")) == "S01E01.mp4"


def test_find_videos_filters_non_episodes(tmp_path) -> None:
    for name in ("S01E01.mp4", "notes.txt", "cover.jpg", "S01E01.mp4.part"):
        (tmp_path / name).write_bytes(b"x")

    found = [p.name for p in find_videos(tmp_path)]
    assert found == ["S01E01.mp4"]


def test_find_videos_uses_natural_order(tmp_path) -> None:
    """字典序会把 S01E10 排在 S01E02 前，必须按 (季, 集) 数字排序。"""
    for name in ("S01E10.mp4", "S01E02.mp4", "S02E01.mp4", "S01E01.mp4"):
        (tmp_path / name).write_bytes(b"x")

    found = [p.name for p in find_videos(tmp_path)]
    assert found == ["S01E01.mp4", "S01E02.mp4", "S01E10.mp4", "S02E01.mp4"]


# ---------------------------------------------------------------------------
# needs_upload —— 断点续传的判定核心
# ---------------------------------------------------------------------------
def test_needs_upload_when_remote_missing() -> None:
    need, reason = needs_upload(None, 100)
    assert need is True
    assert "不存在" in reason


def test_needs_upload_when_size_differs() -> None:
    """远端存在但大小不符 —— 多半是上次传了一半，必须重传。"""
    need, reason = needs_upload(99, 100)
    assert need is True
    assert "不一致" in reason


def test_no_upload_when_size_matches() -> None:
    need, reason = needs_upload(100, 100)
    assert need is False
    assert "一致" in reason


# ---------------------------------------------------------------------------
# upload_one —— 用假 client，不联网
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, remote_sizes: dict[str, int] | None = None) -> None:
        self.remote_sizes = remote_sizes or {}
        self.uploads: list[tuple[str, dict]] = []

    def head_object(self, Bucket: str, Key: str):  # noqa: N803 - 模拟 boto3 签名
        if Key not in self.remote_sizes:
            raise RuntimeError("404 Not Found")
        return {"ContentLength": self.remote_sizes[Key]}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):  # noqa: N803
        self.uploads.append((Key, ExtraArgs))


def _make_video(tmp_path: Path, name: str, size: int) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return path


def test_upload_one_skips_matching_object(tmp_path) -> None:
    video = _make_video(tmp_path, "S01E01.mp4", 100)
    client = _FakeClient({"S01E01.mp4": 100})

    status, key, size, _ = upload_one(client, "bucket", video)

    assert status == "skip"
    assert key == "S01E01.mp4"
    assert size == 100
    assert client.uploads == []


def test_upload_one_uploads_missing_object_with_media_headers(tmp_path) -> None:
    video = _make_video(tmp_path, "S01E01.mp4", 100)
    client = _FakeClient({})

    status, key, _, note = upload_one(client, "bucket", video)

    assert status == "up"
    assert key == "S01E01.mp4"
    assert "不存在" in note
    assert len(client.uploads) == 1
    _, extra = client.uploads[0]
    assert extra["ContentType"] == "video/mp4"
    assert extra["CacheControl"] == CACHE_CONTROL


def test_upload_one_force_reuploads_existing(tmp_path) -> None:
    video = _make_video(tmp_path, "S01E01.mp4", 100)
    client = _FakeClient({"S01E01.mp4": 100})

    status, _, _, note = upload_one(client, "bucket", video, force=True)

    assert status == "up"
    assert "强制重传" in note
    assert len(client.uploads) == 1


def test_upload_one_dry_run_touches_nothing(tmp_path) -> None:
    video = _make_video(tmp_path, "S01E01.mp4", 100)
    client = _FakeClient({})

    status, _, size, _ = upload_one(client, "bucket", video, dry_run=True)

    assert status == "dry"
    assert size == 100
    assert client.uploads == []
