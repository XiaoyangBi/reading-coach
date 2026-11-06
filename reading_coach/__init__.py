# -*- coding: utf-8 -*-
"""reading-coach: 本地优先的阅读陪练 Agent。

包内模块按依赖方向分层：
    config     运行契约，不依赖任何业务模块
    credentials  BYOK 凭证契约，不依赖任何业务模块，不导入第三方 SDK
    book/ledger/notes/tracelog/tools/context/loop/recovery/models/judge/coach
               领域与机制层，可独立测试
    session    编排层，把上面各层接成一局陪练
    cli/web/scheduler  入口层，只做适配，不重写教练逻辑

版本号是发布契约的唯一来源。pyproject.toml 与 __version__ 必须一致。
"""
from __future__ import annotations

__version__ = "3.2.0"

# - [INPUT]: 不依赖包内模块。
# - [OUTPUT]: 对外提供 __version__ 与 reading_coach 包标识。
# - [POS]: 包的根。定义分层边界与发布版本，不承载业务逻辑。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
