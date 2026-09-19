"""把部署所需的最小文件集打成 tar.gz，方便一次性传到 VPS。

为什么需要它
------------
部署到 VPS 其实只需要三样东西（AGENTS.md 第 10 节）：

    backend/      服务层代码
    pipeline/     只有 store.py + parse_srt.py 是运行时依赖（其余是数据管线，一次性）
    tbbt.db       11.8 万条台词的索引（约 54 MB）

**视频不上服务器**（全在 R2），所以 `视频素材/`、`frontend/`、`tests/`、`node_modules/`
一个都不用传。手工 scp 很容易漏文件或多传几百 MB，所以这里把「该传什么」写死成代码。

用法
----
    python -m tools.pack_deploy                 # 生成 deploy-bundle.tar.gz
    python -m tools.pack_deploy -o D:\\bundle.tar.gz

传到 VPS 后解压到 /opt/tbbt 即可，见 deploy/DEPLOY.md。
"""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path

__all__ = [
    "RUNTIME_FILES",
    "REQUIRED_FILES",
    "collect_files",
    "missing_required",
    "pack",
    "main",
]

# 运行时真正需要的代码/配置（相对项目根）
RUNTIME_FILES = [
    "backend/__init__.py",
    "backend/main.py",
    "pipeline/__init__.py",
    "pipeline/parse_srt.py",  # store.py 依赖它的 Cue
    "pipeline/store.py",
    "requirements.txt",
    "deploy/nginx.conf",
    "deploy/tbbt-api.service",
    "deploy/DEPLOY.md",
]

# 缺了就跑不起来的文件
REQUIRED_FILES = ["backend/main.py", "pipeline/store.py", "pipeline/parse_srt.py", "tbbt.db"]


def collect_files(root: Path) -> list[Path]:
    """返回实际存在、应当打包的文件（按相对路径排序）。"""
    found = []
    for rel in RUNTIME_FILES + ["tbbt.db"]:
        path = root / rel
        if path.is_file():
            found.append(path)
    return sorted(found)


def missing_required(root: Path) -> list[str]:
    """列出缺失的必需文件（打包前校验，避免传出个残缺包）。"""
    return [rel for rel in REQUIRED_FILES if not (root / rel).is_file()]


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def pack(root: Path, out: Path) -> tuple[int, int]:
    """打包，返回 (文件数, 产物字节数)。"""
    files = collect_files(root)
    with tarfile.open(out, "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=str(path.relative_to(root)))
    return len(files), out.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包部署产物（只含 VPS 运行时需要的文件）")
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument(
        "-o", "--output", default="deploy-bundle.tar.gz", help="输出路径"
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    out = Path(args.output)

    missing = missing_required(root)
    if missing:
        print("缺少必需文件，先补齐再打包：")
        for rel in missing:
            print(f"  - {rel}")
        return 1

    file_count, size = pack(root, out)

    print(f"已打包 {file_count} 个文件 → {out.resolve()}")
    print(f"产物大小：{_human(size)}")
    print()
    print("清单：")
    for path in collect_files(root):
        rel = path.relative_to(root)
        print(f"  {rel}  ({_human(path.stat().st_size)})")
    print()
    print("下一步（见 deploy/DEPLOY.md）：")
    print(f"  scp {out.name} <user>@<vps-ip>:/tmp/")
    print("  # VPS 上：sudo mkdir -p /opt/tbbt && sudo tar -xzf /tmp/… -C /opt/tbbt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
