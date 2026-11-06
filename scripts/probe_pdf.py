#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 解析探针。检查 pypdf 是否可用，以及逐页抽取有没有卡住的页。

    python scripts/probe_pdf.py /path/to/book.pdf
"""
# - [INPUT]: 依赖标准库 sys / time 与可选 pypdf 的 PdfReader。
# - [OUTPUT]: 对外提供逐页抽取耗时与总字数统计的命令行探针。
# - [POS]: scripts/ 的诊断脚本。只读命令行传入的一个 PDF，不写数据库，不导入项目包。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md

from __future__ import annotations

import sys
import time


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python scripts/probe_pdf.py <book.pdf>")
        return 2
    from pypdf import PdfReader
    reader = PdfReader(sys.argv[1])
    total = 0
    for i, page in enumerate(reader.pages, 1):
        t0 = time.time()
        try:
            text = page.extract_text() or ""
        except Exception as e:
            print(f"page {i}: EXTRACT FAIL {e}", flush=True)
            continue
        dt = time.time() - t0
        total += len(text)
        if dt > 1.0:
            print(f"page {i}: slow {dt:.1f}s, {len(text)} chars", flush=True)
    print("页数:", len(reader.pages), "总字数:", total, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
