#!/usr/bin/env python3
"""青少年读物需求与印次决策应用入口。

用法示例：
  python3 teenpress-cli.py init-db --as-of 2026-10-01
  python3 teenpress-cli.py forecast --book B1
  python3 teenpress-cli.py compare --book B1 --plan S1:F2:400 --plan S2:F2:600
完整说明见 使用说明.md。
"""
from teenpress.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
