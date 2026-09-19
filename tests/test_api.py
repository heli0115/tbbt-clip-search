"""FastAPI 搜索接口与视频服务的测试。

视频那部分重点验证 **HTTP Range（206）**——Media Fragment 播放依赖它，
如果服务端不返回 206，浏览器 seek 会退化成全量下载。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app, create_app_from_env
from pipeline.parse_srt import Cue
from pipeline.store import connect, init_db, insert_cues

CUES = [
    Cue(
        1,
        2380,
        4840,
        "如果一个光子打向有两个狭缝的平面",
        "So if a photon is directed through a plane",
    ),
    Cue(
        2,
        4960,
        6530,
        "如果有一个狭缝可以观测到",
        "with two slits in it and either slit is observed,",
    ),
]


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_db(conn)
    insert_cues(conn, CUES, season=1, episode=1)
    conn.close()

    app = create_app(db_path=db_path, video_dir=tmp_path / "videos")
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------
def test_health_reports_clip_count(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["clips"] == 2


# ---------------------------------------------------------------------------
# /api/search
# ---------------------------------------------------------------------------
def test_search_returns_hit_with_timecodes(client) -> None:
    response = client.get("/api/search", params={"q": "photon"})
    assert response.status_code == 200
    body = response.json()

    assert body["query"] == "photon"
    assert body["count"] == 1

    hit = body["results"][0]
    assert hit["season"] == 1
    assert hit["episode"] == 1
    assert hit["cue_index"] == 1
    assert hit["start_ms"] == 2380
    assert hit["end_ms"] == 4840
    assert "光子" in hit["zh"]
    assert "photon" in hit["en"]


def test_search_includes_playable_video_path(client) -> None:
    """前端要能直接拿这个字段拼 <video src="...">。"""
    hit = client.get("/api/search", params={"q": "photon"}).json()["results"][0]
    assert hit["video"] == "/v/S01E01.mp4"


def test_search_short_chinese_query_falls_back_to_like(client) -> None:
    """「狭缝」只有 2 个汉字，trigram 匹配不到，必须靠 LIKE 兜底。"""
    response = client.get("/api/search", params={"q": "狭缝"})
    assert response.json()["count"] == 2


def test_search_empty_query_returns_empty(client) -> None:
    response = client.get("/api/search", params={"q": ""})
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_search_no_match(client) -> None:
    response = client.get("/api/search", params={"q": "bazinga"})
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_search_respects_limit(client) -> None:
    response = client.get("/api/search", params={"q": "狭缝", "limit": 1})
    assert response.json()["count"] == 1


def test_search_limit_is_capped(client) -> None:
    """防止一次拉太多数据把响应撑爆。"""
    response = client.get("/api/search", params={"q": "狭缝", "limit": 999999})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# video 字段的 base 可配置 —— 线上视频由浏览器**直连 R2**（AGENTS.md 第 10 节）
# ---------------------------------------------------------------------------
def _search_first_hit(tmp_path, **app_kwargs):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_db(conn)
    insert_cues(conn, CUES, season=1, episode=1)
    conn.close()

    app = create_app(db_path=db_path, video_dir=tmp_path / "videos", **app_kwargs)
    with TestClient(app) as test_client:
        body = test_client.get("/api/search", params={"q": "photon"}).json()
    return body["results"][0]


def test_search_video_url_uses_configured_base(tmp_path) -> None:
    hit = _search_first_hit(tmp_path, video_base="https://video.example.com")
    assert hit["video"] == "https://video.example.com/S01E01.mp4"


def test_search_video_base_trailing_slash_is_normalised(tmp_path) -> None:
    """不能拼出 https://video.example.com//S01E01.mp4。"""
    hit = _search_first_hit(tmp_path, video_base="https://video.example.com/")
    assert hit["video"] == "https://video.example.com/S01E01.mp4"


# ---------------------------------------------------------------------------
# create_app_from_env —— 线上由 systemd 用环境变量拉起，**部署不改代码**
# ---------------------------------------------------------------------------
def test_create_app_from_env_reads_configuration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TBBT_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("TBBT_VIDEO_DIR", str(tmp_path / "videos"))
    monkeypatch.setenv("TBBT_VIDEO_BASE", "https://video.example.com")

    conn = connect(tmp_path / "env.db")
    init_db(conn)
    insert_cues(conn, CUES, season=1, episode=1)
    conn.close()

    app = create_app_from_env()
    with TestClient(app) as client:
        body = client.get("/api/search", params={"q": "photon"}).json()

    assert body["results"][0]["video"] == "https://video.example.com/S01E01.mp4"


# ---------------------------------------------------------------------------
# /api/share —— 分享链接（M5）
# ---------------------------------------------------------------------------
def test_create_share_returns_path(client) -> None:
    response = client.post(
        "/api/share", json={"season": 1, "episode": 1, "cue_index": 1}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == f"/s/{body['token']}"
    assert len(body["token"]) >= 8


def test_share_roundtrip_returns_playable_clip(client) -> None:
    token = client.post(
        "/api/share", json={"season": 1, "episode": 1, "cue_index": 1}
    ).json()["token"]

    response = client.get(f"/api/share/{token}")
    assert response.status_code == 200

    body = response.json()
    assert body["season"] == 1
    assert body["episode"] == 1
    assert body["start_ms"] == 2380
    assert body["end_ms"] == 4840
    assert "光子" in body["zh"]
    assert "photon" in body["en"]
    assert body["video"] == "/v/S01E01.mp4"


def test_share_content_comes_from_server_not_client(client) -> None:
    """客户端只能给 (季,集,cue)，不能伪造台词/时间码。"""
    response = client.post(
        "/api/share",
        json={
            "season": 1,
            "episode": 1,
            "cue_index": 1,
            "zh": "伪造的台词",
            "start_ms": 999999,
        },
    )

    body = client.get(f"/api/share/{response.json()['token']}").json()
    assert body["zh"] != "伪造的台词"
    assert body["start_ms"] == 2380


def test_share_unknown_token_returns_404(client) -> None:
    assert client.get("/api/share/deadbeef").status_code == 404


def test_share_nonexistent_clip_returns_404(client) -> None:
    response = client.post(
        "/api/share", json={"season": 9, "episode": 9, "cue_index": 99}
    )
    assert response.status_code == 404


def test_share_video_url_follows_configured_base(tmp_path) -> None:
    """线上分享页的视频同样必须直连 R2，而不是 VPS。"""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_db(conn)
    insert_cues(conn, CUES, season=1, episode=1)
    conn.close()

    app = create_app(
        db_path=db_path,
        video_dir=tmp_path / "videos",
        video_base="https://video.example.com",
    )
    with TestClient(app) as client:
        token = client.post(
            "/api/share", json={"season": 1, "episode": 1, "cue_index": 1}
        ).json()["token"]
        body = client.get(f"/api/share/{token}").json()

    assert body["video"] == "https://video.example.com/S01E01.mp4"


# ---------------------------------------------------------------------------
# /v/{file} —— 视频必须支持 Range
# ---------------------------------------------------------------------------
def test_video_serves_partial_content(tmp_path) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "S01E01.mp4").write_bytes(b"x" * 1000)

    app = create_app(db_path=tmp_path / "empty.db", video_dir=videos)
    with TestClient(app) as test_client:
        response = test_client.get(
            "/v/S01E01.mp4", headers={"Range": "bytes=0-99"}
        )

    assert response.status_code == 206, "必须返回 206，否则浏览器 seek 会退化为全量下载"
    assert response.headers["content-range"] == "bytes 0-99/1000"
    assert len(response.content) == 100


def test_video_full_request_returns_200(tmp_path) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "S01E01.mp4").write_bytes(b"x" * 1000)

    app = create_app(db_path=tmp_path / "empty.db", video_dir=videos)
    with TestClient(app) as test_client:
        response = test_client.get("/v/S01E01.mp4")

    assert response.status_code == 200
    assert response.headers.get("accept-ranges") == "bytes"


def test_video_missing_returns_404(tmp_path) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()

    app = create_app(db_path=tmp_path / "empty.db", video_dir=videos)
    with TestClient(app) as test_client:
        response = test_client.get("/v/NOPE.mp4")

    assert response.status_code == 404
