"""parse_srt 的单元测试与真实样本回归测试。

用例依据 `samples/S01E01.bilingual.srt` 的实况统计（422 个 cue）：

  - 418 个 cue 是「1 行中文 + 1 行英文」，2 个纯中文，1 个纯英文，1 个单行中文
  - 英文行被三层嵌套 <font> 标签包裹
  - 方括号 SDH 标记**未出现**
  - 圆括号是**有效注释**（如「(生物分类)」「(克林贡是外星人)」），绝不能清洗掉
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.parse_srt import (
    parse_srt,
    parse_srt_file,
    parse_timestamp,
    split_bilingual,
    strip_tags,
)

REAL_SRT = Path(__file__).resolve().parents[1] / "samples" / "S01E01.bilingual.srt"
requires_sample = pytest.mark.skipif(
    not REAL_SRT.exists(), reason=f"样本文件缺失: {REAL_SRT}"
)


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("00:00:00,000", 0),
        ("00:00:02,380", 2380),
        ("00:00:02.380", 2380),  # 点号分隔（VTT / ASS）
        ("00:15:10,540", 910540),
        ("01:02:03,004", 3723004),
    ],
)
def test_parse_timestamp(text: str, expected: int) -> None:
    assert parse_timestamp(text) == expected


def test_parse_timestamp_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_timestamp("这不是时间码")


# ---------------------------------------------------------------------------
# strip_tags
# ---------------------------------------------------------------------------
def test_strip_nested_font_tags() -> None:
    raw = (
        '<font face="Calibri Italic"><font size="14">'
        '<font color="#eba862">So if a photon</font></font></font>'
    )
    assert strip_tags(raw) == "So if a photon"


def test_strip_ass_override_tags_and_hard_newline() -> None:
    raw = r"{\an8}top line\Nbottom line"
    assert strip_tags(raw) == "top line\nbottom line"


def test_strip_simple_html_tags() -> None:
    assert strip_tags("<i>hello</i>") == "hello"


def test_parenthetical_notes_are_preserved() -> None:
    """防回归：本剧圆括号是台词内容，不是 SDH 标记。"""
    for text in ("(生物分类)", "谢尔顿(克林贡是外星人)"):
        assert strip_tags(text) == text


# ---------------------------------------------------------------------------
# split_bilingual
# ---------------------------------------------------------------------------
def test_split_standard_bilingual_cue() -> None:
    zh, en = split_bilingual(
        [
            "如果一个光子打向有两个狭缝的平面",
            '<font face="Calibri Italic">So if a photon is directed through a plane</font>',
        ]
    )
    assert zh == "如果一个光子打向有两个狭缝的平面"
    assert en == "So if a photon is directed through a plane"


def test_split_is_order_independent() -> None:
    """依据字符集判断，而非行序。"""
    zh, en = split_bilingual(["Hello there", "你好"])
    assert zh == "你好"
    assert en == "Hello there"


def test_split_chinese_only() -> None:
    assert split_bilingual(["洗个痛快澡"]) == ("洗个痛快澡", "")


def test_split_english_only() -> None:
    zh, en = split_bilingual(["<i>Yeah!</i>", "<i>Oh no.</i>"])
    assert zh == ""
    assert en == "Yeah! Oh no."


def test_split_joins_multiple_chinese_lines_without_space() -> None:
    zh, _ = split_bilingual(["如果一个光子打向", "有两个狭缝的平面"])
    assert zh == "如果一个光子打向有两个狭缝的平面"


def test_split_ignores_blank_lines() -> None:
    assert split_bilingual(["", "  ", "你好"]) == ("你好", "")


# ---------------------------------------------------------------------------
# parse_srt
# ---------------------------------------------------------------------------
SIMPLE_SRT = """1
00:00:02,380 --> 00:00:04,840
如果一个光子打向有两个狭缝的平面
<font face="Calibri Italic"><font size="14"><font color="#eba862">So if a photon is directed through a plane</font></font></font>

2
00:00:04,960 --> 00:00:06,530
如果有一个狭缝可以观测到
<font face="Calibri Italic">with two slits in it</font>
"""


def test_parse_srt_basic_fields() -> None:
    cues = parse_srt(SIMPLE_SRT)
    assert len(cues) == 2
    first = cues[0]
    assert first.index == 1
    assert first.start_ms == 2380
    assert first.end_ms == 4840
    assert first.zh == "如果一个光子打向有两个狭缝的平面"
    assert first.en == "So if a photon is directed through a plane"
    assert cues[1].start_ms == 4960


def test_parse_srt_handles_crlf() -> None:
    assert len(parse_srt(SIMPLE_SRT.replace("\n", "\r\n"))) == 2


def test_parse_srt_handles_utf8_bom() -> None:
    assert len(parse_srt("\ufeff" + SIMPLE_SRT)) == 2


def test_parse_srt_skips_blank_cues() -> None:
    text = """1
00:00:01,000 --> 00:00:02,000


2
00:00:03,000 --> 00:00:04,000
你好
"""
    cues = parse_srt(text)
    assert len(cues) == 1
    assert cues[0].zh == "你好"


def test_parse_srt_drops_reversed_time_range() -> None:
    assert parse_srt("1\n00:00:05,000 --> 00:00:02,000\n你好\n") == []


# ---------------------------------------------------------------------------
# 真实样本回归（AGENTS.md 第 6 节要求的不变量）
# ---------------------------------------------------------------------------
@requires_sample
def test_sample_cue_count() -> None:
    assert len(parse_srt_file(REAL_SRT)) == 422


@requires_sample
def test_sample_timestamps_are_monotonic_and_positive() -> None:
    cues = parse_srt_file(REAL_SRT)
    assert cues, "样本不应解析为空"
    for cue in cues:
        assert cue.end_ms > cue.start_ms
    for prev, cur in zip(cues, cues[1:]):
        assert cur.start_ms >= prev.start_ms


@requires_sample
def test_sample_has_no_leftover_markup() -> None:
    open_brace = "{" + chr(92)  # "{<backslash>"，匹配残留的 ASS 标签
    for cue in parse_srt_file(REAL_SRT):
        for field in (cue.zh, cue.en):
            lowered = field.lower()
            assert "<font" not in lowered
            assert "</font>" not in lowered
            assert open_brace not in field


@requires_sample
def test_sample_is_mostly_bilingual() -> None:
    cues = parse_srt_file(REAL_SRT)
    bilingual = sum(1 for c in cues if c.zh and c.en)
    assert bilingual / len(cues) > 0.95


@requires_sample
def test_sample_preserves_parenthetical_notes() -> None:
    """防回归：圆括号注释不得被清洗掉。"""
    joined = " ".join(c.zh + c.en for c in parse_srt_file(REAL_SRT))
    assert "(生物分类)" in joined
