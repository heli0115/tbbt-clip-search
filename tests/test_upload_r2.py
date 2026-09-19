"""`pipeline/upload_r2.py` 的纯逻辑测试。

不联网、不依赖 boto3 —— boto3 只在真正上传时延迟导入，
所以这里能直接测「哪些文件要传、怎么判定」这部分规则。
"""

from __future__ import annotations

from pathlib import Path

from pipeline import upload_r2
from pipeline.upload_r2 import (
    CACHE_CONTROL,
    DEFAULT_BUCKET,
    ENV_ACCESS_KEY,
    ENV_ACCOUNT_ID,
    ENV_BUCKET,
    ENV_SECRET_KEY,
    content_type_for,
    find_files,
    find_videos,
    needs_upload,
    object_key,
    should_report,
    upload_one,
)


def test_default_bucket_is_project_bucket() -> None:
    """桶名写死成默认值，省得每次上传都要记得 `--bucket`（本项目是 thebong）。"""
    assert DEFAULT_BUCKET == "thebong"


def test_should_report_every_n() -> None:
    """上传 11.8 万个缩略图时不能逐文件打印（否则刷出十几万行）。"""
    assert should_report(200, 1000) is True
    assert should_report(199, 1000) is False
    assert should_report(1000, 1000) is True  # 最后一个必报
    assert should_report(7, 7) is True  # 总数少于 every 时也要报


def test_should_report_accepts_custom_interval() -> None:
    assert should_report(50, 1000, every=50) is True
    assert should_report(50, 1000, every=100) is False


def test_main_reports_missing_credentials(tmp_path, monkeypatch, capsys) -> None:
    """凭据是会话级的（换终端就没了）：缺什么、怎么设，都要直接打出来。"""
    for name in (ENV_ACCOUNT_ID, ENV_ACCESS_KEY, ENV_SECRET_KEY, ENV_BUCKET):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "S01E01.jpg").write_bytes(b"x")

    code = upload_r2.main([str(tmp_path), "--suffix", ".jpg"])

    assert code == 2
    out = capsys.readouterr().out
    assert ENV_ACCESS_KEY in out  # 列出了缺哪几个
    assert "$env:R2_ACCESS_KEY_ID" in out  # 并给出可照抄的设置命令
    assert DEFAULT_BUCKET in out  # 提醒桶名已有默认值、不必设

# ---------------------------------------------------------------------------
# object_key / find_videos
# ---------------------------------------------------------------------------
def test_object_key_is_bare_filename() -> None:
    """R2 里必须是裸文件名——前端按 https://video.<域名>/S01E01.mp4 取。"""
    assert object_key(Path(r"D:\videos\S01E01.mp4")) == "S01E01.mp4"


def test_object_key_applies_key_prefix() -> None:
    """封面走 covers/ 前缀，与后端拼出的 URL 一致：/covers/S01E01.jpg。"""
    assert object_key(Path(r"D:\web\covers\S01E01.jpg"), "covers/") == "covers/S01E01.jpg"


def test_object_key_normalises_missing_slash() -> None:
    assert object_key(Path("S01E01.jpg"), "covers") == "covers/S01E01.jpg"
    assert object_key(Path("S01E01.jpg"), "/covers/") == "covers/S01E01.jpg"


def test_content_type_by_suffix() -> None:
    assert content_type_for(Path("S01E01.mp4")) == "video/mp4"
    assert content_type_for(Path("S01E01.jpg")) == "image/jpeg"


def test_find_files_filters_by_suffix(tmp_path) -> None:
    for name in ("S01E01.jpg", "S01E10.jpg", "S01E01.mp4", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")

    assert [p.name for p in find_files(tmp_path, ".jpg")] == ["S01E01.jpg", "S01E10.jpg"]
    assert [p.name for p in find_files(tmp_path, "mp4")] == ["S01E01.mp4"]


def test_find_files_ignores_partial_candidates(tmp_path) -> None:
    """抽帧中途的候选临时文件（S01E01.cand0.part.jpg）绝不能上传。"""
    for name in ("S01E01.jpg", "S01E01.cand0.part.jpg", "S01E01.part.jpg"):
        (tmp_path / name).write_bytes(b"x")

    assert [p.name for p in find_files(tmp_path, ".jpg")] == ["S01E01.jpg"]


def test_find_files_accepts_cue_frame_names(tmp_path) -> None:
    """台词缩略图是 S01E01_0001.jpg 形态，序号按数字排序（不是字典序）。"""
    for name in ("S01E01_0010.jpg", "S01E01_0002.jpg", "S01E01_0001.jpg", "S02E01_0001.jpg"):
        (tmp_path / name).write_bytes(b"x")

    assert [p.name for p in find_files(tmp_path, ".jpg")] == [
        "S01E01_0001.jpg",
        "S01E01_0002.jpg",
        "S01E01_0010.jpg",
        "S02E01_0001.jpg",
    ]


def test_find_videos_still_only_matches_episode_videos(tmp_path) -> None:
    for name in ("S01E01.mp4", "S01E01_0001.jpg", "S01E01_0001.mp4"):
        (tmp_path / name).write_bytes(b"x")

    # 五位数序号 / 非 4 位序号都不是我们的命名，不该被上传
    assert [p.name for p in find_files(tmp_path, ".mp4")] == ["S01E01.mp4"]


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
        self.head_calls = 0

    def head_object(self, Bucket: str, Key: str):  # noqa: N803 - 模拟 boto3 签名
        self.head_calls += 1
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


def test_upload_one_uses_prefix_and_jpeg_content_type(tmp_path) -> None:
    cover = _make_video(tmp_path, "S01E01.jpg", 100)
    client = _FakeClient({})

    status, key, _, _ = upload_one(client, "bucket", cover, key_prefix="covers")

    assert status == "up"
    assert key == "covers/S01E01.jpg"
    _, extra = client.uploads[0]
    assert extra["ContentType"] == "image/jpeg"


def test_upload_one_prefix_is_used_for_remote_lookup(tmp_path) -> None:
    """断点续传的远端比对必须按带前缀的 key 查，否则每次都会重传。"""
    cover = _make_video(tmp_path, "S01E01.jpg", 100)
    client = _FakeClient({"covers/S01E01.jpg": 100})

    status, _, _, note = upload_one(client, "bucket", cover, key_prefix="covers")

    assert status == "skip"
    assert "一致" in note
    assert client.uploads == []


def test_upload_one_checks_remote_by_default(tmp_path) -> None:
    cover = _make_video(tmp_path, "S01E01.jpg", 100)
    client = _FakeClient({"covers/S01E01.jpg": 100})

    upload_one(client, "bucket", cover, key_prefix="covers")

    assert client.head_calls == 1


def test_upload_one_can_skip_remote_check(tmp_path) -> None:
    """首次全量上传用它省掉一半请求 —— 11.8 万个 7 KB 小文件时，耗时≈请求数。"""
    cover = _make_video(tmp_path, "S01E01.jpg", 100)
    client = _FakeClient({"covers/S01E01.jpg": 100})  # 远端已有且大小一致

    status, _, _, note = upload_one(client, "bucket", cover, check_remote=False)

    assert status == "up"  # 不查远端 → 直接传（幂等）
    assert client.head_calls == 0
    assert "未查远端" in note
    assert len(client.uploads) == 1
