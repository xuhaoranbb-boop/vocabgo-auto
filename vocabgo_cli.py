# -*- coding: utf-8 -*-
"""命令行启动入口（给打包出的命令行版 exe 用）。"""

from __future__ import annotations

import sys

from vocabgo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
