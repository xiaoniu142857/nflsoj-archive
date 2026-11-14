"""NFLSOJ 命令行工具（nflsoj-archive）。

用法：
    python -m nflsoj_archive          # 等价于 python nflsoj.py
    nflsoj --help                     # 安装后（uv run / pipx）可直接使用
"""

from .nflsoj import __version__, main

__all__ = ["main", "__version__"]
