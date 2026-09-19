"""SQLite 存储层：建表、写入 clips、关键词搜索。

索引策略（AGENTS.md 第 4 节）
----------------------------
- 正文存在 `clips` 表；FTS5 表 `clips_fts` 以 external content 方式引用它，
  不重复存文本；三个触发器保持两者同步。
- tokenizer 用 `trigram`：中文无需分词即可做子串匹配。

已知限制（实测 SQLite 3.45.1）
------------------------------
FTS5 的 trigram 只能匹配 **>= 3 个字符** 的查询：`ph`、`光子` 都搜不到。
因此长度 < 3 的查询自动降级为 `LIKE '%q%'` 全表扫描。11 万条量级下这是
毫秒级操作，但这条兜底绝不能省——否则中文两字词会成为搜索盲区。
"""

from __future__ import annotations

import argparse
import re
import secrets
import sqlite3
from pathlib import Path

from .parse_srt import Cue, parse_srt_file

__all__ = [
    "MIN_FTS_QUERY_LEN",
    "SCHEMA",
    "connect",
    "init_db",
    "insert_cues",
    "search",
    "count_clips",
    "create_share",
    "get_share",
    "parse_episode_code",
    "main",
]

# trigram tokenizer 能匹配的最短查询长度
MIN_FTS_QUERY_LEN = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id        INTEGER PRIMARY KEY,
    season    INTEGER NOT NULL,
    episode   INTEGER NOT NULL,
    cue_index INTEGER NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    zh        TEXT NOT NULL DEFAULT '',
    en        TEXT NOT NULL DEFAULT '',
    UNIQUE (season, episode, cue_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
    zh,
    en,
    content='clips',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS clips_ai AFTER INSERT ON clips BEGIN
    INSERT INTO clips_fts(rowid, zh, en) VALUES (new.id, new.zh, new.en);
END;

CREATE TRIGGER IF NOT EXISTS clips_ad AFTER DELETE ON clips BEGIN
    INSERT INTO clips_fts(clips_fts, rowid, zh, en)
    VALUES ('delete', old.id, old.zh, old.en);
END;

CREATE TRIGGER IF NOT EXISTS clips_au AFTER UPDATE ON clips BEGIN
    INSERT INTO clips_fts(clips_fts, rowid, zh, en)
    VALUES ('delete', old.id, old.zh, old.en);
    INSERT INTO clips_fts(rowid, zh, en) VALUES (new.id, new.zh, new.en);
END;

-- 分享链接（M5）：token → 被选中的片段。
-- 刻意冗余存下 zh/en：即使日后重导字幕导致 clips 变化，
-- 已经发出去的分享链接内容也不会漂移。
CREATE TABLE IF NOT EXISTS shares (
    token      TEXT PRIMARY KEY,
    season     INTEGER NOT NULL,
    episode    INTEGER NOT NULL,
    cue_index  INTEGER NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    zh         TEXT NOT NULL DEFAULT '',
    en         TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CLIP_COLUMNS = "c.season, c.episode, c.cue_index, c.start_ms, c.end_ms, c.zh, c.en"
_EPISODE_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,3})")


def connect(path: str | Path) -> sqlite3.Connection:
    """打开数据库连接，并把 row_factory 设为 sqlite3.Row。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表、建 FTS5 索引、建同步触发器（幂等，可重复调用）。"""
    conn.executescript(SCHEMA)
    conn.commit()


def insert_cues(
    conn: sqlite3.Connection,
    cues: list[Cue],
    *,
    season: int,
    episode: int,
) -> int:
    """写入一集的字幕，返回写入条数。

    以 (season, episode, cue_index) 为唯一键 upsert——重复导入同一集会
    覆盖旧内容而非产生重复行（UPDATE 触发器会同步刷新 FTS 索引）。
    """
    if not cues:
        return 0
    rows = [
        (season, episode, cue.index, cue.start_ms, cue.end_ms, cue.zh, cue.en)
        for cue in cues
    ]
    with conn:  # 事务：异常自动回滚
        conn.executemany(
            """
            INSERT INTO clips (season, episode, cue_index, start_ms, end_ms, zh, en)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (season, episode, cue_index) DO UPDATE SET
                start_ms = excluded.start_ms,
                end_ms   = excluded.end_ms,
                zh       = excluded.zh,
                en       = excluded.en
            """,
            rows,
        )
    return len(rows)


def count_clips(conn: sqlite3.Connection) -> int:
    """库内 clips 总数。"""
    return int(conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0])


def _as_fts_phrase(query: str) -> str:
    """把用户输入包成 FTS5 短语字面量，内部引号按 FTS5 规则双写。"""
    return '"' + query.replace('"', '""') + '"'


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """关键词搜索台词，返回 clips 行。

    - 长度 >= 3：走 FTS5 trigram 索引（按相关度排序）
    - 长度 < 3：trigram 匹配不到，降级为 LIKE 子串扫描
    """
    keyword = query.strip()
    if not keyword:
        return []

    if len(keyword) >= MIN_FTS_QUERY_LEN:
        fts_sql = f"""
            SELECT {_CLIP_COLUMNS}
            FROM clips_fts f
            JOIN clips c ON c.id = f.rowid
            WHERE f MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        try:
            return conn.execute(fts_sql, (_as_fts_phrase(keyword), limit)).fetchall()
        except sqlite3.OperationalError:
            pass  # FTS5 查询语法异常时落到 LIKE 兜底

    pattern = f"%{keyword}%"
    like_sql = f"""
        SELECT {_CLIP_COLUMNS}
        FROM clips c
        WHERE c.zh LIKE ? OR c.en LIKE ?
        ORDER BY c.season, c.episode, c.start_ms
        LIMIT ?
    """
    return conn.execute(like_sql, (pattern, pattern, limit)).fetchall()


def create_share(
    conn: sqlite3.Connection,
    *,
    season: int,
    episode: int,
    cue_index: int,
    start_ms: int,
    end_ms: int,
    zh: str = "",
    en: str = "",
    token_bytes: int = 8,
) -> str:
    """为片段生成分享 token 并落库，返回 token。

    `secrets.token_urlsafe(8)` ≈ 64 bit 熵、11 个字符 —— 不可枚举，
    这是分享链接「不被爬虫索引」的前提（AGENTS.md 第 7 节）。
    """
    token = secrets.token_urlsafe(token_bytes)
    with conn:
        conn.execute(
            """
            INSERT INTO shares
                (token, season, episode, cue_index, start_ms, end_ms, zh, en)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token, season, episode, cue_index, start_ms, end_ms, zh, en),
        )
    return token


def get_share(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """按 token 取分享；不存在返回 None。"""
    return conn.execute(
        """
        SELECT season, episode, cue_index, start_ms, end_ms, zh, en
        FROM shares WHERE token = ?
        """,
        (token,),
    ).fetchone()


def parse_episode_code(text: str) -> tuple[int, int]:
    """从文件名或路径里提取 (season, episode)。

    >>> parse_episode_code("S01E01.bilingual.srt")
    (1, 1)
    """
    match = _EPISODE_RE.search(text)
    if match is None:
        raise ValueError(f"无法从 {text!r} 解析出季集号")
    return int(match.group(1)), int(match.group(2))


def main(argv: list[str] | None = None) -> int:
    """命令行入口：把 SRT 建入索引，可选立即搜索验证。"""
    parser = argparse.ArgumentParser(description="把双语 SRT 建入 SQLite FTS5 索引")
    parser.add_argument(
        "srt", nargs="+", help="SRT 文件（文件名需含 S01E01 形式的季集号）"
    )
    parser.add_argument("-d", "--db", default="tbbt.db", help="数据库路径（默认 tbbt.db）")
    parser.add_argument("-s", "--search", help="入库后搜索该关键词做验证")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)

    written = 0
    for raw_path in args.srt:
        path = Path(raw_path)
        season, episode = parse_episode_code(path.name)
        cues = parse_srt_file(path)
        written += insert_cues(conn, cues, season=season, episode=episode)
        print(f"S{season:02d}E{episode:02d}: 解析 {len(cues):>4} 条")

    print(f"本次写入 {written} 条；库内共 {count_clips(conn)} 条")

    if args.search:
        rows = search(conn, args.search)
        print(f"\n搜索 {args.search!r}：命中 {len(rows)} 条")
        for row in rows:
            head = f"S{row['season']:02d}E{row['episode']:02d} {row['start_ms'] / 1000:8.2f}s"
            print(f"  [{head}] {row['zh']}")
            print(f"                 {row['en']}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
