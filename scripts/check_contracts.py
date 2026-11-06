#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GEB 契约守护：让 L3 文件契约与 L2 模块地图始终同构。

    python scripts/check_contracts.py          # 只检查，退出码非 0 即不一致
    python scripts/check_contracts.py --fix   # 为缺少契约的文件播种头部

CI 在每个 Python 版本上运行检查。新增业务文件却没有 L3 头部，构建就会失败。
播种用的契约文本是本文件唯一的模板来源；播种后以各文件头部为准。
"""
# - [INPUT]: 依赖标准库 argparse / pathlib / sys；不依赖项目包。
# - [OUTPUT]: 对外提供 GEB 契约检查与 --fix 播种命令。
# - [POS]: scripts/ 的文档守护器。CI 在离线检查之后调用它，阻止孤立代码变更。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "reading_coach"
PROTOCOL = "# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md"
REQUIRED = ("[INPUT]:", "[OUTPUT]:", "[POS]:", "[PROTOCOL]:")

# 每项 = (依赖了什么, 对外提供什么, 它在这个局部结构里是谁、为何存在)
CONTRACTS = {
    "book.py": (
        "标准库 zipfile / html.parser / re / sqlite3 / pathlib，可选 pypdf；不依赖包内模块",
        "Book、Library、parse_book、parse_epub、parse_text、parse_pdf、anchor_label",
        "领域层唯一书籍解析器。把 epub/txt/md/pdf 折成章节与段落锚点并写入 books/paragraphs；"
        "重导入会重排锚点，因此先预览差异，再把替换决定交给 cli 传入的 confirm 回调，自己不询问用户",
    ),
    "cli.py": (
        "config 与 book/coach/judge/ledger/notes/recovery/session/tracelog；惰性依赖 models、shelf、anthropic、python-dotenv",
        "main、entry、load_env、has_model_key、make_models、confirm、pick_book、SHELF_NOTE",
        "终端入口，也是唯一按 key 选择 provider 的位置。web 复用它的 make_models 与 SHELF_NOTE；"
        "它只装配 Session 并驱动一轮，不实现教练逻辑",
    ),
    "coach.py": (
        "book.anchor_label 与 tools.Registry / Tool",
        "SYSTEM、WHY、build_registry、opening",
        "教练的 prompt、工具 schema 与代码边。把计划先行、题量预算、费曼规则、深挖授权写成门禁，"
        "让规则由代码裁决而不是靠模型自觉",
    ),
    "context.py": (
        "标准库；不依赖包内模块",
        "manage、size 与 BULKY / MAX_RESULT_CHARS / KEEP_RECENT / STUB 常量",
        "上下文预算层。只压缩书籍原文类工具结果，问答记录不动；用打桩替代有损摘要，"
        "session 在每轮调用模型前使用它",
    ),
    "highlights.py": (
        "标准库 urllib；可选 WEREAD_API_KEY；调用方注入 call 以便离线测试",
        "weread_call、from_weread、from_local、get_highlights",
        "划线来源适配层。先试微信读书网关，再退到数据目录里的本地 txt；任何失败都返回来源标记而不中断陪练；"
        "shelf 复用同一个 weread_call",
    ),
    "judge.py": (
        "标准库 json；无包内依赖",
        "JudgeError、build_prompt、judge、SYSTEM、VERDICT_TOOL",
        "裁判子代理。每次构造全新 messages，用 tool_choice 强制结构化判分；"
        "教练看不到历史答案、历史等级与裁判依据",
    ),
    "ledger.py": (
        "标准库 sqlite3 / time；无包内依赖",
        "Ledger、next_state",
        "掌握度账本与间隔重复状态机。教练视图与裁判记录分离；scheduler 只读它挑选到期概念",
    ),
    "loop.py": (
        "tools.run_tool",
        "run_turn、final_text",
        "Agent 内层循环。用 before_call / pre_tool / post_tool / should_stop 四个接缝把门禁留给 session，"
        "自己只负责工具调用与消息推进",
    ),
    "models.py": (
        "标准库；惰性依赖 openai SDK 与 recovery.with_retry",
        "to_openai_messages、to_openai_tools、from_openai_response、make_deepseek",
        "OpenAI 兼容适配层。翻译函数是纯函数，离线可测；cli 按 key 决定是否启用它，loop 与门禁一行不改",
    ),
    "notes.py": (
        "标准库 pathlib / re；无包内依赖",
        "Notes、slug、frontmatter",
        "用户原话笔记写入器。按书名分文件、只追加不覆盖；session 在撤销时按路径回滚它",
    ),
    "recovery.py": (
        "标准库 time；无包内依赖",
        "with_retry、should_retry、retry_delay",
        "瞬时错误重试。只处理可重试状态码，不切换模型、不吞掉业务异常，因此失败状态能被 session 如实报告",
    ),
    "scheduler.py": (
        "config 与 book.Library、ledger.Ledger、tracelog.Trace；macOS 通知依赖 osascript / terminal-notifier",
        "plan_today、run、push_history、add_feedback、undo_feedback、ensure_schema、main、entry、FEEDBACK",
        "每日复习推送与反馈台账。跑完即退不常驻；账本即信箱，选概念走规则不调模型；"
        "web 复用它的查询与反馈接口，两者共写同一个 coach.db",
    ),
    "session.py": (
        "config、context、book、coach、highlights、judge、loop",
        "Session、LEVEL_LABEL、SOURCE_LABEL 等控制词表",
        "编排层。把工具注册表、模型、账本、笔记、撤销栈接成一局陪练；ask_user 是最重的工具，"
        "控制词优先于教练意图。cli 与 web 共用它，不重写教练逻辑",
    ),
    "shelf.py": (
        "book.Book 与 highlights.weread_call；可选 WEREAD_API_KEY",
        "resolve、build_book",
        "书架模式适配层。按书名解析 bookId，把用户划线折成 Book；cli 与 web 由此进入以划线为原文的陪练",
    ),
    "test.py": (
        "包内全部领域模块（含惰性导入 models / shelf / scheduler / web）；标准库 tempfile / sqlite3 / http.client；无外网、无真实 key",
        "main、entry 与 17 组离线检查",
        "唯一离线验收入口。用脚本化假模型与临时库跑端到端会话；CI 与 console_scripts 都调用它",
    ),
    "tools.py": (
        "标准库 json；无包内依赖",
        "Tool、Registry、run_tool、NO_INPUT",
        "工具运行时。Registry 产出 schema，run_tool 统一把异常转成模型可读文本；narrate 让每次调用先说一句人话",
    ),
    "tracelog.py": (
        "标准库 sqlite3 / re / json / os；无包内依赖",
        "Trace、scrub、SECRET_ENV、SECRET_RE",
        "事件日志与脱敏。写入 SQLite 的 trace 表；按环境变量值与密钥形状脱敏，web 推流前对文本再 scrub 一次",
    ),
    "web.py": (
        "config、scheduler、book/coach/ledger/notes/session/tracelog，函数内复用 cli 的 make_models 与 shelf.build_book；标准库 http.server / threading",
        "make_server、main、entry、App、Handler、Live、classify、books_view、concepts_view、runtime_view",
        "浏览器工作台入口。把同一个 Session 的 say/ask 映射成 SSE 事件与等待回复；"
        "Host/Origin 白名单加可选 Basic 令牌构成访问边界，页面永远拿不到 key",
    ),
}


def render(name: str) -> str:
    dep, out, pos = CONTRACTS[name]
    return "\n".join((
        f"# - [INPUT]: 依赖 {dep}。",
        f"# - [OUTPUT]: 对外提供 {out}。",
        f"# - [POS]: {pos}。",
        PROTOCOL,
    )) + "\n"


def has_contract(text: str) -> bool:
    head = "\n".join(text.splitlines()[:60])
    return all(mark in head for mark in REQUIRED)


def plant(path: Path, name: str) -> bool:
    """把契约插在 from __future__ 之后。找不到时插在模块 docstring 之后。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    future = next((i for i, ln in enumerate(lines) if ln.startswith("from __future__ import")), None)
    if future is not None:
        at = future + 1
    else:
        end = next((i for i, ln in enumerate(lines[1:], 1) if ln.rstrip().endswith('"""')), 0)
        at = end + 1
    lines.insert(at, "\n" + render(name) + "\n")
    path.write_text("".join(lines), encoding="utf-8")
    return True


def check() -> list[str]:
    errors = []
    l1 = ROOT / "CLAUDE.md"
    if not l1.is_file() or PROTOCOL not in l1.read_text(encoding="utf-8"):
        errors.append("L1 缺失或缺少 [PROTOCOL]：CLAUDE.md")
    for path in sorted(PACKAGE.glob("*.py")):
        if not has_contract(path.read_text(encoding="utf-8")):
            errors.append(f"L3 缺失：{path.relative_to(ROOT)}")
    for directory in ("reading_coach", "scripts", "deploy", ".github"):
        folder = ROOT / directory
        if not folder.is_dir():
            continue
        l2 = folder / "CLAUDE.md"
        if not l2.is_file():
            errors.append(f"L2 缺失：{directory}/CLAUDE.md")
            continue
        body = l2.read_text(encoding="utf-8")
        for name in [p.name for p in folder.glob("*.py")]:
            if name not in body:
                errors.append(f"L2 未列成员：{directory}/{name}")
        if PROTOCOL not in body:
            errors.append(f"L2 缺少 [PROTOCOL]：{l2.relative_to(ROOT)}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description="检查或播种 GEB L3 契约")
    ap.add_argument("--fix", action="store_true", help="为缺少契约的文件写入头部")
    args = ap.parse_args()

    if args.fix:
        for name in CONTRACTS:
            path = PACKAGE / name
            if path.is_file() and not has_contract(path.read_text(encoding="utf-8")):
                plant(path, name)
                print(f"seeded {path.relative_to(ROOT)}")

    errors = check()
    for line in errors:
        print(line, file=sys.stderr)
    if errors:
        return 1
    print(f"GEB contracts: ok ({len(CONTRACTS)} seeded modules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
