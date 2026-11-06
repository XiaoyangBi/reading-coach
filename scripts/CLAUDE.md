# L2 模块地图

scripts/
L2 | 父级: ../CLAUDE.md

本目录的脚本只用标准库。它们不导入 `reading_coach`，因此在依赖装齐之前也能运行。

## 成员清单

probe_pdf.py: PDF 解析探针。对命令行传入的文件逐页计时并统计字数。不写数据库，不含写死路径。
check_contracts.py: GEB 契约守护。检查 L3 头部与 L2 成员清单。`--fix` 为缺头部的模块播种契约。CI 每次构建都运行它。

# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
