"""解析《生活大爆炸》双语 SRT，输出结构化数据。

数据来源
--------
`ffmpeg -i <mkv> -map 0:s:0 -c:s srt out.srt`
从「中上英下-YYeTs字幕」ASS 轨转出的 SRT（见 AGENTS.md 第 8 节）。

实况（samples/S01E01.bilingual.srt，422 个 cue）
-----------------------------------------------
- 418 个 cue：1 行中文 + 1 行英文
- 2 个 cue：2 行中文（无英文翻译）
- 1 个 cue：2 行英文（无中文）
- 1 个 cue：仅 1 行中文
- 每个英文行被三层嵌套 font 标签包裹
- 圆括号是台词内容（如「(生物分类)」），必须保留
- 方括号 SDH 标记在本集未出现

输出
----
[{"index": int, "start_ms": int, "end_ms": int, "zh": str, "en": str}, ...]
时间单位统一为毫秒（AGENTS.md 第 4 节）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = [
    "Cue",
    "parse_timestamp",
    "strip_tags",
    "split_bilingual",
    "parse_srt",
    "parse_srt_file",
    "main",
]

# HH:MM:SS,mmm 或 HH:MM:SS.mmm（毫秒位 1-3 位，兼容 VTT/ASS 的厘秒写法）
_TIMESTAMP = re.compile(r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})")

# 时间轴行：起点 --> 终点（端点后可能跟 SRT 位置参数，此处忽略）
_TIMELINE = re.compile(r"^(?P<start>\S+)\s*-->\s*(?P<end>\S+)")

# 样式标签
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")  # <font ...> </font> <i> </i>
_ASS_TAG = re.compile(r"\{[^}]*\}")  # ASS 覆盖标签：an8 定位、pos、i1 等
_ASS_NEWLINE = re.compile(r"\\[Nnh]")  # ASS 硬换行

# CJK 判定：扩展 A + 统一表意文字 + 兼容表意文字
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True)
class Cue:
    """一条字幕：时间区间 + 中英文本。"""

    index: int
    start_ms: int
    end_ms: int
    zh: str
    en: str


def parse_timestamp(text: str) -> int:
    """把 HH:MM:SS,mmm 或 HH:MM:SS.mmm 解析成毫秒。

    >>> parse_timestamp("00:00:02,380")
    2380
    >>> parse_timestamp("00:15:10.540")
    910540
    """
    match = _TIMESTAMP.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"无法解析时间码: {text!r}")
    # 毫秒位 1 位=分秒、2 位=厘秒、3 位=毫秒，统一右补零到毫秒
    milliseconds = int(match["ms"].ljust(3, "0"))
    seconds = int(match["h"]) * 3600 + int(match["m"]) * 60 + int(match["s"])
    return seconds * 1000 + milliseconds


def strip_tags(text: str) -> str:
    """剥离样式标签，返回纯文本。

    会移除：嵌套 font / i 等 HTML 标签、ASS 覆盖标签、ASS 硬换行（转成真正的换行）。

    **不会**触碰圆括号与方括号内容——本剧中它们是台词的一部分
    （如「(生物分类)」），删掉即破坏数据。
    """
    text = _ASS_NEWLINE.sub("\n", text)
    text = _ASS_TAG.sub("", text)
    text = _HTML_TAG.sub("", text)
    return text.strip()


def split_bilingual(lines: list[str]) -> tuple[str, str]:
    """把若干原始文本行拆成 (中文, 英文)。

    依据字符集判断而非行序：含 CJK 的行归中文，其余归英文。
    这样即使字幕组换了行序（英文在上）也不会拆错。
    中文行直接拼接（中文无需空格分隔），英文行以空格拼接。
    """
    zh_parts: list[str] = []
    en_parts: list[str] = []
    for line in lines:
        cleaned = strip_tags(line)
        if not cleaned:
            continue
        if _CJK.search(cleaned):
            zh_parts.append(cleaned)
        else:
            en_parts.append(cleaned)
    return "".join(zh_parts), " ".join(en_parts)


def parse_srt(text: str) -> list[Cue]:
    """解析 SRT 文本为 Cue 列表。

    容错：跳过空块、无时间轴的块、时间码非法的块、时间区间倒置的块，
    以及清洗后无任何文本的块。不抛异常。
    兼容 UTF-8 BOM 与 CRLF / LF 换行。
    """
    text = text.lstrip("\ufeff")
    blocks = re.split(r"\r?\n[ \t]*\r?\n", text.strip())

    cues: list[Cue] = []
    for block in blocks:
        if not block.strip():
            continue
        lines = block.splitlines()

        timeline_at = next(
            (i for i, line in enumerate(lines) if _TIMELINE.match(line.strip())),
            None,
        )
        if timeline_at is None:
            continue

        timeline = _TIMELINE.match(lines[timeline_at].strip())
        assert timeline is not None  # 由上面的 next() 保证
        try:
            start_ms = parse_timestamp(timeline["start"])
            end_ms = parse_timestamp(timeline["end"])
        except ValueError:
            continue
        if end_ms <= start_ms:
            continue

        zh, en = split_bilingual(lines[timeline_at + 1 :])
        if not zh and not en:
            continue

        index = len(cues) + 1
        if timeline_at > 0 and lines[timeline_at - 1].strip().isdigit():
            index = int(lines[timeline_at - 1].strip())

        cues.append(Cue(index=index, start_ms=start_ms, end_ms=end_ms, zh=zh, en=en))
    return cues


def parse_srt_file(path: str | Path) -> list[Cue]:
    """读取并解析 SRT 文件，自动尝试 UTF-8 / GB18030 编码。"""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return parse_srt(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解码字幕文件: {path}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口：解析 SRT，输出 JSON 或统计信息。"""
    parser = argparse.ArgumentParser(description="解析双语 SRT 字幕，输出 JSON")
    parser.add_argument("srt", help="输入 SRT 文件路径")
    parser.add_argument("-o", "--output", help="输出 JSON 路径（默认打印到 stdout）")
    parser.add_argument("--stats", action="store_true", help="只打印统计信息")
    args = parser.parse_args(argv)

    cues = parse_srt_file(args.srt)

    if args.stats:
        bilingual = sum(1 for c in cues if c.zh and c.en)
        print(f"cue 总数 : {len(cues)}")
        print(f"双语     : {bilingual}")
        print(f"仅中文   : {sum(1 for c in cues if c.zh and not c.en)}")
        print(f"仅英文   : {sum(1 for c in cues if c.en and not c.zh)}")
        if cues:
            print(f"时长     : {cues[-1].end_ms / 1000:.1f}s")
        return 0

    payload = json.dumps([asdict(c) for c in cues], ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"已写出 {len(cues)} 条 -> {args.output}")
    else:
        try:
            print(payload)
        except BrokenPipeError:
            # 下游提前关闭管道（例如 `| head`、`| grep -m1`），静默退出
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
