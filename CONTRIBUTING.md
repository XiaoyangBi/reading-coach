# 贡献指南

## 四条不可让渡的原则

1. 本地优先。不把书、划线、回答或笔记送到用户机器之外。
2. 密钥不落盘。不把 API key 写进数据库、日志、页面或测试断言。
3. 验收离线可跑。`reading-coach-test` 不需要 key，不需要网络。
4. BYOK 边界不可绕过。模型与数据源凭证只能来自 `credentials.py`。其余模块不得直接读取 `*_API_KEY`。`config.py` 只管运行参数与 `READING_COACH_TOKEN` 这类本地访问口令。

## 开发流程

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[pdf]"
reading-coach-test
python scripts/check_contracts.py
```

改代码前先读目标目录的 `CLAUDE.md`，再读目标文件的 L3 头部。
改完代码按 L3 → L2 → L1 回环检查。

## 提交前门禁

- `reading-coach-test` 全绿。
- `python scripts/check_contracts.py` 退出码为 0。
- `python -m compileall -q reading_coach scripts deploy` 无错误。
- 新增配置只写进 `reading_coach/config.py`。
- 新增环境变量同步写进 `.env.example` 与 README 配置表。
- 不提交 `.env`、`data/`、`books/`、`deploy/generated/`。

## 新增文件要做的三件事

1. 写 L3 头部：`[INPUT]` 说清依赖什么，`[OUTPUT]` 说清提供什么，`[POS]` 说清自己是谁。用架构师视角写职责边界，不要罗列变量名。
2. 在所属目录的 L2 `CLAUDE.md` 成员清单里补一行。
3. 若新增顶层模块或改变依赖方向，更新根 `CLAUDE.md`。

`python scripts/check_contracts.py --fix` 可以播种 L3 头部。契约文本在脚本里的 `CONTRACTS` 表，新增模块要补一条。

## 扩展约束

- 教练与裁判逻辑只住在 `session.py` 及其下游。`web.py` 与 `cli.py` 只做适配。
- 门禁用代码裁决，不靠提示词自觉。计划先行、题量预算、费曼规则、深挖授权都写在 `coach.build_registry` 的代码边里。
- 新工具必须带 `narrate`，让用户看得见 Agent 在做什么。
- 新增模型端点先在 `credentials.resolve()` 声明凭证来源与能力位，再在 `cli.make_models` 分发。`models.py` 不写厂商名。
- 新写操作必须可撤销，或明确说明为何不可撤销。
- 单文件超过 800 行就拆分。

## 提交信息

用一句中文或英文说清变更对象与动作。例：`修复 web 会话线程未走统一数据库连接`。
不写「优化」「调整」这类无信息量的词。
