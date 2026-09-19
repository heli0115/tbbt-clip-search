"""store 模块测试：建表、入库、搜索，以及 trigram 短查询降级。

关键约束（实测 SQLite 3.45.1）：
FTS5 的 trigram tokenizer 只能匹配 **>= 3 个字符** 的查询。
「ph」（2 字母）、「光子」（2 汉字）都匹配不到，因此长度 < 3 的查询
必须降级为 LIKE 子串扫描——否则中文两字词会成为搜索盲区。
"""

from __future__ import annotations

import pytest

from pipeline.parse_srt import Cue
from pipeline.store import (
    connect,
    count_clips,
    create_share,
    get_share,
    init_db,
    insert_cues,
    parse_episode_code,
    search,
)


@pytest.fixture
def conn():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


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
    Cue(
        3,
        6530,
        7530,
        "那它没有同时通过两个狭缝",
        "it will not go through both slits.",
    ),
]


# ---------------------------------------------------------------------------
# 建表与写入
# ---------------------------------------------------------------------------
def test_init_db_is_idempotent() -> None:
    connection = connect(":memory:")
    init_db(connection)
    init_db(connection)  # 重复初始化不应抛异常
    assert count_clips(connection) == 0
    connection.close()


def test_insert_and_count(conn) -> None:
    assert insert_cues(conn, CUES, season=1, episode=1) == 3
    assert count_clips(conn) == 3


def test_insert_is_upsert_by_episode_and_cue(conn) -> None:
    """同一集的同一条 cue 重导时应覆盖，而不是产生重复。"""
    insert_cues(conn, CUES, season=1, episode=1)
    insert_cues(conn, [Cue(1, 100, 200, "改写后的中文", "rewritten")], season=1, episode=1)
    assert count_clips(conn) == 3

    rows = search(conn, "改写后的")
    assert len(rows) == 1
    assert rows[0]["start_ms"] == 100


def test_insert_keeps_episodes_separate(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    insert_cues(conn, CUES, season=1, episode=2)
    assert count_clips(conn) == 6


# ---------------------------------------------------------------------------
# 搜索：FTS5 主路径
# ---------------------------------------------------------------------------
def test_search_english_full_word(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    rows = search(conn, "photon")
    assert len(rows) == 1
    assert rows[0]["zh"].startswith("如果一个光子")


def test_search_english_plural(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    # cue 2 与 cue 3 都含 "slits"
    assert len(search(conn, "slits")) == 2


def test_search_chinese_three_chars_uses_trigram(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    assert len(search(conn, "光子打")) == 1


# ---------------------------------------------------------------------------
# 搜索：短查询降级（trigram 的死角）
# ---------------------------------------------------------------------------
def test_two_letter_query_falls_back_to_like(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    assert len(search(conn, "ph")) == 1  # trigram 不支持，靠 LIKE 兜住


def test_two_char_chinese_query_falls_back_to_like(conn) -> None:
    """「狭缝」只有 2 个汉字——中文两字词绝不能成为搜索盲区。"""
    insert_cues(conn, CUES, season=1, episode=1)
    assert len(search(conn, "狭缝")) == 3


# ---------------------------------------------------------------------------
# 搜索：返回内容与边界
# ---------------------------------------------------------------------------
def test_search_returns_full_row(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    row = search(conn, "photon")[0]
    assert row["season"] == 1
    assert row["episode"] == 1
    assert row["cue_index"] == 1
    assert row["start_ms"] == 2380
    assert row["end_ms"] == 4840
    assert row["en"] == "So if a photon is directed through a plane"


def test_search_empty_query_returns_nothing(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    assert search(conn, "") == []
    assert search(conn, "   ") == []


def test_search_no_match(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    assert search(conn, "bazinga") == []


def test_search_respects_limit(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    assert len(search(conn, "狭缝", limit=2)) == 2


def test_search_spans_episodes(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)
    insert_cues(conn, CUES, season=1, episode=2)
    rows = search(conn, "狭缝")
    assert len(rows) == 6
    assert {r["episode"] for r in rows} == {1, 2}


def test_search_survives_fts_syntax_characters(conn) -> None:
    """用户输入带引号/星号时不应抛异常。"""
    insert_cues(conn, CUES, season=1, episode=1)
    for weird in ['"photon', "photon*", "a-b", "^x"]:
        search(conn, weird)  # 不抛异常即通过


# ---------------------------------------------------------------------------
# 季集号解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("S01E01.bilingual.srt", (1, 1)),
        ("The.Big.Bang.Theory.S01E17.2007.1080p.mkv", (1, 17)),
        ("s12e24.srt", (12, 24)),
        ("The.Big.Bang.Theory.S01E01.2007.1080p.Blu-ray.x265.10bit.AC3￡cXcY@FRDS.srt", (1, 1)),
    ],
)
def test_parse_episode_code(text: str, expected: tuple[int, int]) -> None:
    assert parse_episode_code(text) == expected


def test_parse_episode_code_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_episode_code("no-episode-here.srt")


# ---------------------------------------------------------------------------
# 分享链接（M5）—— shares 表
# ---------------------------------------------------------------------------
def _make_share(conn, **overrides):
    payload = dict(
        season=1,
        episode=1,
        cue_index=1,
        start_ms=2380,
        end_ms=4840,
        zh="如果一个光子打向有两个狭缝的平面",
        en="So if a photon is directed through a plane",
    )
    payload.update(overrides)
    return create_share(conn, **payload)


def test_create_share_returns_retrievable_token(conn) -> None:
    insert_cues(conn, CUES, season=1, episode=1)

    token = _make_share(conn)

    assert isinstance(token, str)
    assert len(token) >= 8

    row = get_share(conn, token)
    assert row["season"] == 1
    assert row["episode"] == 1
    assert row["cue_index"] == 1
    assert row["start_ms"] == 2380
    assert row["end_ms"] == 4840
    assert "光子" in row["zh"]


def test_share_tokens_are_unique_and_unguessable_length(conn) -> None:
    """token 不可枚举是分享链接「不被爬虫索引」的前提（AGENTS.md 第 7 节）。"""
    tokens = {_make_share(conn, cue_index=i) for i in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 8 for t in tokens)


def test_get_share_returns_none_for_unknown_token(conn) -> None:
    assert get_share(conn, "no-such-token") is None
