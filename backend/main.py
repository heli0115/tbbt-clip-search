"""FastAPI 服务层：台词搜索接口 + 整集视频静态服务（支持 HTTP Range）。

契约来源：`tests/test_api.py`（本项目遵循「先写测试再写实现」，见 AGENTS.md 第 6 节）。

视频为什么必须支持 Range
------------------------
前端用 Media Fragment URI 播放片段：`<video src="/v/S01E01.mp4#t=754.2,761.8">`。
浏览器的 seek 与 Media Fragment 都依赖 `206 Partial Content`；服务端若不返回 206，
播放会退化成全量下载（实测 `python -m http.server` 即如此，见 `tools/serve_range.py`）。
Starlette 的 `FileResponse` 原生支持 Range，这里直接复用，不自己造轮子。

数据库连接为什么每请求新建
--------------------------
FastAPI 的同步端点运行在线程池中，与创建连接的线程不同，而 sqlite3 默认
`check_same_thread=True` 会直接报错。这里选择最简单的策略：**每个请求各开一个
连接**。SQLite 打开本地库是微秒级开销，11 万条量级下查询为毫秒级，不值得为此
引入连接池与跨线程锁。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline.store import connect, count_clips, create_share, get_share, init_db, search

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "DEFAULT_VIDEO_DIR",
    "DEFAULT_VIDEO_BASE",
    "ENV_VIDEO_BASE",
    "create_app",
    "create_app_from_env",
    "main",
]

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DEFAULT_VIDEO_DIR = Path(r"D:\study\生活大爆炸\视频素材\web")

# video 字段的前缀。本地 `/v`（同源，走下面的 /v 路由）；线上设
# `TBBT_VIDEO_BASE=https://video.<域名>`，让浏览器**直连 R2**，不经 VPS 中转
# —— 这是 AGENTS.md 第 10 节的硬性约束。
ENV_VIDEO_BASE = "TBBT_VIDEO_BASE"
DEFAULT_VIDEO_BASE = "/v"


class ShareRequest(BaseModel):
    """创建分享的入参。

    只收 `(season, episode, cue_index)` 三元组，**不接受客户端直接提交台词与时间码**——
    内容一律由服务端回查 `clips` 表，避免分享内容被伪造成任意文本。
    """

    season: int
    episode: int
    cue_index: int


def _video_url(video_base: str, season: int, episode: int) -> str:
    """拼出该集视频的 URL（`rstrip` 防止 base 带尾斜杠时拼出 `//`）。"""
    return f"{video_base.rstrip('/')}/S{season:02d}E{episode:02d}.mp4"


def _clip_to_hit(row, video_base: str) -> dict:
    """把 clips 行转成前端可直接使用的 JSON。

    `video` + `start_ms`/`end_ms` 让前端无需额外请求即可拼出 Media Fragment：
        f"{hit['video']}#t={hit['start_ms'] / 1000},{hit['end_ms'] / 1000}"
    """
    season = int(row["season"])
    episode = int(row["episode"])
    return {
        "season": season,
        "episode": episode,
        "cue_index": int(row["cue_index"]),
        "start_ms": int(row["start_ms"]),
        "end_ms": int(row["end_ms"]),
        "zh": row["zh"],
        "en": row["en"],
        "video": _video_url(video_base, season, episode),
    }


def create_app(
    db_path: str | Path,
    video_dir: str | Path,
    video_base: str = DEFAULT_VIDEO_BASE,
) -> FastAPI:
    """构造 FastAPI 应用。

    参数全部显式传入（不读全局配置），这样测试可以用 tmp_path 隔离运行。

    `video_base` 决定 `video` 字段的前缀：默认 `/v`（本地同源）；线上传
    `https://video.<域名>`，让浏览器直连 R2，而不是让 VPS 反代视频流量
    （AGENTS.md 第 10 节的硬性约束）。
    """
    db_path = Path(db_path)
    video_dir = Path(video_dir)

    # 幂等建表：保证后续每个请求打开连接时 schema 一定存在
    boot = connect(db_path)
    try:
        init_db(boot)
    finally:
        boot.close()

    app = FastAPI(
        title="TBBT 台词搜索",
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
    )

    @app.get("/api/health")
    def health() -> dict:
        conn = connect(db_path)
        try:
            return {"status": "ok", "clips": count_clips(conn)}
        finally:
            conn.close()

    @app.get("/api/search")
    def api_search(q: str = "", limit: int = DEFAULT_LIMIT) -> dict:
        keyword = q.strip()
        if not keyword:
            return {"query": q, "count": 0, "results": []}

        # 不交给 Query(le=...) 校验：超限时要截断而不是报 422
        capped = max(1, min(limit, MAX_LIMIT))

        conn = connect(db_path)
        try:
            rows = search(conn, keyword, limit=capped)
        finally:
            conn.close()

        results = [_clip_to_hit(row, video_base) for row in rows]
        return {"query": q, "count": len(results), "results": results}

    @app.post("/api/share")
    def api_create_share(payload: ShareRequest) -> dict:
        """为一条台词创建分享链接。

        token 由 `secrets.token_urlsafe` 生成（见 store.create_share），不可枚举——
        这是分享链接「不被爬虫索引」的前提（AGENTS.md 第 7 节）。
        """
        conn = connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT season, episode, cue_index, start_ms, end_ms, zh, en
                FROM clips
                WHERE season = ? AND episode = ? AND cue_index = ?
                """,
                (payload.season, payload.episode, payload.cue_index),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="片段不存在")

            token = create_share(
                conn,
                season=int(row["season"]),
                episode=int(row["episode"]),
                cue_index=int(row["cue_index"]),
                start_ms=int(row["start_ms"]),
                end_ms=int(row["end_ms"]),
                zh=row["zh"],
                en=row["en"],
            )
        finally:
            conn.close()

        return {
            "token": token,
            "path": f"/s/{token}",
            "clip": _clip_to_hit(row, video_base),
        }

    @app.get("/api/share/{token}")
    def api_get_share(token: str) -> dict:
        conn = connect(db_path)
        try:
            row = get_share(conn, token)
        finally:
            conn.close()

        if row is None:
            raise HTTPException(status_code=404, detail="分享链接无效或已失效")

        return _clip_to_hit(row, video_base)

    @app.get("/v/{name}")
    def video(name: str) -> FileResponse:
        # 只接受裸文件名，挡住 ../ 与子路径形式的路径穿越
        if name != Path(name).name or name in {".", ".."}:
            raise HTTPException(status_code=404, detail="视频不存在")
        path = video_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="视频不存在")
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes"},
        )

    return app


def create_app_from_env() -> FastAPI:
    """无参工厂，供 ASGI 服务器直接引用（配置全部来自环境变量）。

    线上由 systemd 拉起：
        uvicorn backend.main:create_app_from_env --factory --host 127.0.0.1 --port 8000

    好处是**部署不改代码**，只需要改环境变量（见 `deploy/tbbt-api.service`）。
    """
    return create_app(
        db_path=os.environ.get("TBBT_DB", "tbbt.db"),
        video_dir=os.environ.get("TBBT_VIDEO_DIR", str(DEFAULT_VIDEO_DIR)),
        video_base=os.environ.get(ENV_VIDEO_BASE, DEFAULT_VIDEO_BASE),
    )


def main(argv: list[str] | None = None) -> int:
    """命令行入口：启动服务（`python -m backend.main`）。"""
    import uvicorn

    parser = argparse.ArgumentParser(description="启动 TBBT 台词搜索服务")
    parser.add_argument("-d", "--db", default="tbbt.db", help="数据库路径（默认 tbbt.db）")
    parser.add_argument(
        "--video-dir", default=str(DEFAULT_VIDEO_DIR), help="转码后 mp4 所在目录"
    )
    parser.add_argument(
        "--video-base",
        default=os.environ.get(ENV_VIDEO_BASE, DEFAULT_VIDEO_BASE),
        help=f"video 字段前缀；线上设为 R2 域名（也可用环境变量 {ENV_VIDEO_BASE}）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    app = create_app(args.db, args.video_dir, video_base=args.video_base)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
