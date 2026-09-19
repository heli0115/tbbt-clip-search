"""最小的支持 HTTP Range 的静态文件服务。

用途：本地验证 Media Fragment URI（`S01E01.mp4#t=start,end`）的片段播放。

为什么不能直接用 `python -m http.server`
-----------------------------------------
实测（Python 3.12.3）：它**不支持 Range 请求**，收到 `Range: bytes=0-1023`
仍返回 `200` 并附完整文件长度，且不带 `Accept-Ranges` 头。而浏览器的 seek
与 Media Fragment 全都依赖 `206 Partial Content`，因此必须自行补上这一层。

用法：
    python tools/serve_range.py <目录> [--port 8765]
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
from pathlib import Path

_RANGE_RE = re.compile(r"bytes=(?P<start>\d*)-(?P<end>\d*)")


class _BoundedReader:
    """把文件对象包装成「只再读 N 字节」，供 copyfile 使用。"""

    def __init__(self, fileobj, remaining: int) -> None:
        self._fileobj = fileobj
        self._remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        chunk = self._fileobj.read(size)
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self._fileobj.close()


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """在 SimpleHTTPRequestHandler 之上补上 Range / 206 支持。"""

    def send_head(self):  # type: ignore[override]
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        try:
            fileobj = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(fileobj.fileno()).st_size
        content_type = self.guess_type(path)

        match = _RANGE_RE.fullmatch(self.headers.get("Range", "").strip())
        if match is None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return fileobj

        start = int(match["start"]) if match["start"] else 0
        end = int(match["end"]) if match["end"] else size - 1
        if start >= size or start > end:
            fileobj.close()
            self.send_error(416, "Requested Range Not Satisfiable")
            return None
        end = min(end, size - 1)

        fileobj.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return _BoundedReader(fileobj, end - start + 1)

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        # 验证时日志噪音太大，只在显式开启时打印
        if os.environ.get("SERVE_RANGE_VERBOSE"):
            super().log_message(fmt, *args)


def build_server(directory: str, port: int = 8765, bind: str = "127.0.0.1"):
    """构造一个支持 Range 的 HTTP 服务实例（供测试直接调用）。"""
    handler = functools.partial(
        RangeRequestHandler, directory=str(Path(directory).resolve())
    )
    return http.server.ThreadingHTTPServer((bind, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="支持 Range 的静态文件服务")
    parser.add_argument("directory", nargs="?", default=".", help="要服务的目录")
    parser.add_argument("-p", "--port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args(argv)

    httpd = build_server(args.directory, args.port, args.bind)
    print(f"服务中: http://{args.bind}:{args.port}/  ->  {Path(args.directory).resolve()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
