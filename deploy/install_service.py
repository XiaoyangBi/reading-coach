#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成常驻服务定义。不执行 launchctl / systemctl。

    python deploy/install_service.py                  # 按当前平台生成到 deploy/generated/
    python deploy/install_service.py --platform systemd

模板里没有写死用户名、项目路径或解释器路径。全部由本脚本在目标机器上推导。
后台 shell 调用 launchctl 会失败，因此脚本只产出文件并打印需要在系统终端执行的命令。
"""
# - [INPUT]: 依赖标准库 argparse / os / platform / pathlib / string / sys；不依赖项目包。
# - [OUTPUT]: 对外提供 launchd 与 systemd 服务定义的生成命令。
# - [POS]: deploy/ 的安装前端。把模板里的机器相关值替换为当前机器的真实值，不改动任何系统状态。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md

from __future__ import annotations

import argparse
import os
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "deploy"
OUT = TEMPLATES / "generated"

# 模板名 -> 安装后的单元名。Label 与打印的安装命令都以此为准。
TARGETS = {
    "launchd.web.plist.tmpl": "com.readingcoach.web.plist",
    "launchd.scheduler.plist.tmpl": "com.readingcoach.scheduler.plist",
    "systemd.web.service.tmpl": "reading-coach-web.service",
    "systemd.scheduler.service.tmpl": "reading-coach-scheduler.service",
    "systemd.scheduler.timer.tmpl": "reading-coach-scheduler.timer",
}


def values() -> dict:
    return {
        "PYTHON": sys.executable,
        "ROOT": str(ROOT),
        "DATA": os.environ.get("READING_COACH_HOME", str(ROOT / "data")),
        "ENV_FILE": str(ROOT / ".env"),
        "PORT": os.environ.get("READING_COACH_PORT", "8765"),
        "HOUR": "20",
        "MINUTE": "00",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 reading-coach 常驻服务定义")
    ap.add_argument("--platform", choices=("auto", "launchd", "systemd"), default="auto")
    args = ap.parse_args()
    platform = args.platform
    if platform == "auto":
        platform = "launchd" if sys.platform == "darwin" else "systemd"

    names = (["launchd.web.plist.tmpl", "launchd.scheduler.plist.tmpl"] if platform == "launchd"
             else ["systemd.web.service.tmpl", "systemd.scheduler.service.tmpl",
                   "systemd.scheduler.timer.tmpl"])
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        rendered = string.Template(text).safe_substitute(values())
        target = OUT / TARGETS[name]
        target.write_text(rendered, encoding="utf-8")
        print(f"generated {target.relative_to(ROOT)}")

    home = Path.home()
    if platform == "launchd":
        dest = home / "Library" / "LaunchAgents"
        print("\n在系统终端执行（不要从后台 shell 执行 launchctl）：")
        print(f"  cp {OUT}/*.plist {dest}/")
        print(f"  launchctl bootstrap gui/$(id -u) {dest}/com.readingcoach.web.plist")
        print(f"  launchctl bootstrap gui/$(id -u) {dest}/com.readingcoach.scheduler.plist")
    else:
        dest = home / ".config" / "systemd" / "user"
        print("\n执行：")
        print(f"  mkdir -p {dest} && cp {OUT}/*.service {OUT}/*.timer {dest}/")
        print("  systemctl --user daemon-reload")
        print("  systemctl --user enable --now reading-coach-web.service")
        print("  systemctl --user enable --now reading-coach-scheduler.timer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
