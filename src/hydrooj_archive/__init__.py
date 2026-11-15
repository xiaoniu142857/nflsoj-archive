"""Hydro OJ 版 NFLSOJ 命令行工具（与 nflsoj 同款前端）。

用法：
    python -m hydrooj_archive          # 等价于 python src/hydrooj_archive/hydrooj.py
    hydrooj --help                     # 安装后（uv run / pipx）可直接使用
"""

from .hydrooj import __version__, main

__all__ = ["main", "__version__"]
