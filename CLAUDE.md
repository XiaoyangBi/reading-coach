# L1 项目宪法

reading-coach - 本地优先的阅读陪练 Agent。用户选书，用户完成理解。Agent 提问、记录、到点提醒。

Python 3.9+ + 标准库 http.server + SQLite + OpenAI 兼容 SDK + python-dotenv

## 产品边界

- 数据落在用户机器。`coach.db`、笔记、划线、日志都写进 `READING_COACH_HOME`。
- 模型能力是 BYOK。使用者自带密钥，项目不提供、不代管、不转发模型服务。`credentials.py` 是唯一密钥读取点，产出不可变 `Provider`；页面只收到非敏感模型描述。
- 默认只监听 `127.0.0.1`。Host 与 Origin 白名单拦截 DNS rebinding。
- 绑定非回环地址必须设置 `READING_COACH_TOKEN`。`config.require_token()` 执行这条门禁。
- 教练与裁判共用 `session.Session`。`cli` 与 `web` 是两个适配层，不重写教练逻辑。
- 调度器跑完即退。它由 launchd 或 systemd timer 每日唤醒，不常驻。

<directory>
reading_coach/ - 产品包。配置层、凭证层、领域层、编排层、三个入口。(21 个 .py + static/index.html)
deploy/ - 服务定义模板与生成器。生成物落在 deploy/generated/ 且不入库。(5 模板 + 1 脚本)
scripts/ - 离线探针与 GEB 契约守护。(2 脚本)
.github/ - CI 与平台配置。四版本测试矩阵、wheel 构建、卫生检查。3.9 钉住 ubuntu-22.04。(1 workflow + L2 地图)
samples/ - 自带小样书。离线验收用它，不需要网络。(1 epub)
</directory>

<config>
pyproject.toml - 打包元数据、依赖区间、四个 console_scripts。
docker-compose.yml - 单机容器部署。端口只绑宿主机回环。
.env.example - 全部环境变量与默认值。
LICENSE - MIT。
scripts/check_contracts.py - 检查 L3 契约与 L2 清单；--fix 播种 L3。
</config>

## 变更法则

1. 改代码先读目标目录 `CLAUDE.md`，再读目标文件 L3 头部。
2. 改完代码按 L3 → L2 → L1 顺序回环检查。
3. 新增业务文件先写 L3 头部，再补 L2 成员清单。
4. 新增运行配置只写进 `reading_coach/config.py`；新增模型凭证配置只写进 `reading_coach/credentials.py`。其余模块不得直接读密钥环境变量。
5. 提交前跑 `reading-coach-test` 与 `python scripts/check_contracts.py`。
6. 单文件超过 800 行即拆分。现在最大的文件是 `test.py`。

# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
