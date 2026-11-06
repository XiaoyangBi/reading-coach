# reading-coach

本地优先的阅读陪练 Agent。书你自己选，理解你自己完成。Agent 只做三件事：提问、记录、到点提醒。

它不是一个聊天机器人，也不是云端 SaaS。你的书、划线、回答、笔记和模型密钥都留在自己的机器上。

## 它做什么

- 导入 `epub` / `txt` / `md` / `pdf`，或按书名取回你在微信读书里的划线。
- 教练读目录、账本、划线和原文，定档位，先出计划，再一题一问。
- 独立裁判对照原文判分。账本更新，通过的回答以你的原话追加进笔记。
- 间隔重复：到期概念优先复习。每日 20:00 推送一条，反馈可标记也可撤销。
- 浏览器工作台四块：今日、陪练、书架与账本、推送台账。

## 五分钟跑起来

要求 Python 3.9+。

```bash
git clone https://github.com/XiaoyangBi/reading-coach.git
cd reading-coach
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[pdf]"                 # 不需要 PDF 就写 pip install -e .
cp .env.example .env                    # 填 DEEPSEEK_API_KEY 或 ANTHROPIC_API_KEY

reading-coach-test                      # 17 组离线检查，无 key、无网络
reading-coach samples/边际之书.epub     # 用自带小样书试一次
reading-coach-web                       # 打开 http://127.0.0.1:8765/
```

答题只要记住四个词：`跳过` / `深挖` / `撤销` / `结束`。

## 三个入口

| 命令 | 作用 |
|---|---|
| `reading-coach [书.epub 或 书名]` | 终端陪练。不带参数时选书，到期复习排前面 |
| `reading-coach-web [--no-open]` | 浏览器工作台。`--no-open` 给常驻服务用 |
| `reading-coach-scheduler` | 跑一次每日推送后退出，由系统定时器调用 |

也可用 `python -m reading_coach.cli` / `python -m reading_coach.web` / `python -m reading_coach.scheduler`。

## 数据与隐私

全部数据写在 `READING_COACH_HOME`（默认项目根下的 `data/`）：

- `coach.db`：书、段落、账本、trace、推送台账
- `notes/`：你的原话笔记
- `highlights/`：本地划线导出
- `*.log`：调度器与服务日志

密钥不进数据库。`tracelog.scrub()` 在写入 trace 前按密钥值与形状脱敏。页面只拿到 `key_ready` 布尔值。
`data/`、`books/`、`.env` 都在 `.gitignore` 里。CI 会扫描误入仓库的密钥与个人数据。

## 部署

### A. 本地常驻（macOS / Linux）

```bash
python deploy/install_service.py        # 生成到 deploy/generated/
```

按脚本打印的命令在**系统终端**安装。macOS 用 launchd，Linux 用 systemd user unit。
模板不含绝对路径、用户名或写死的解释器。后台 shell 调 `launchctl` 会失败，所以脚本只生成文件。

健康检查：`curl -s http://127.0.0.1:8765/api/health`。

### B. Docker（长期运行推荐）

```bash
cp .env.example .env                    # 填 key
docker compose up -d
```

镜像以非 root 用户运行。数据落在命名卷 `coach-data`。端口只绑宿主机 `127.0.0.1:8765`。容器带 HEALTHCHECK。

### 公网访问（谨慎）

默认形态是本机工具：只有 Host/Origin 白名单，没有会话级授权。**不要**把 `READING_COACH_BIND` 改成 `0.0.0.0` 后直接暴露。

确实要远程访问时：

1. 设一个强 `READING_COACH_TOKEN`。
2. 设 `READING_COACH_ALLOWED_HOSTS=your.domain`。
3. 用带 TLS 的反向代理转发。浏览器会弹 HTTP Basic 登录框，用户名任意，密码填 token。
4. `config.require_token()` 会在非回环绑定且无 token 时拒绝启动。

`READING_COACH_TRUST_PROXY=1` 只适用于「容器内监听 `0.0.0.0` + 宿主机只映射回环端口」。公网部署必须删掉它。

## 配置

配置只从环境变量读取。完整清单与默认值见 `.env.example`。

| 变量 | 默认 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` | 空 | 二选一，前者优先 |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | `api.deepseek.com` / `deepseek-chat` | 任何 OpenAI 兼容端点 |
| `WEREAD_API_KEY` | 空 | 微信读书划线，缺失则降级本地 txt |
| `READING_COACH_HOME` | 项目根 `data/` | 数据根目录 |
| `READING_COACH_PORT` | `8765` | 工作台端口 |
| `READING_COACH_BIND` | `127.0.0.1` | 监听地址 |
| `READING_COACH_TOKEN` | 空 | HTTP Basic 密码，非回环绑定必填 |
| `READING_COACH_ALLOWED_HOSTS` | 空 | 追加 `host:port`，逗号分隔 |
| `READING_COACH_LOG_LEVEL` | `INFO` | 日志级别 |
| `PROACTIVE_RUNTIME_STORE` | 空 | 只读展示外部运行时的影子决策 |

`.env` 不覆盖进程里已有的环境变量。容器与 systemd 直接注入真实环境变量即可。

## 架构与扩展

依赖方向：`config` ← 领域层 ← `session` ← 入口层。领域模块之间不互相依赖。`cli` 与 `web` 共用同一个 `Session`，不重写教练逻辑。

| 想加什么 | 改哪里 |
|---|---|
| 新的 OpenAI 兼容模型 | 只改 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_MODEL`，代码不动 |
| 新的 provider | 在 `cli.make_models` 加分支，返回 `(coach_model, judge_model)` |
| 新的工具 | 在 `coach.build_registry` 注册，schema 与代码边都写在那里 |
| 新的配置项 | 只改 `reading_coach/config.py` |
| 新的通知渠道 | 给 `scheduler.run` 换一个 `notify` 参数 |
| 新的 API 路由 | 改 `web.Handler`，并在 `test_web` 补用例 |

`CLAUDE.md` 是 L1 宪法，各目录 `CLAUDE.md` 是 L2 模块地图，每个 `.py` 文件头部是 L3 契约。
改代码后按 L3 → L2 → L1 回环检查。`scripts/check_contracts.py` 会挡住漏掉契约或漏列成员的提交。

## 验收

```bash
reading-coach-test                 # 离线，无 key、无网络
python scripts/check_contracts.py # GEB 三层同构
python -m build && pip install dist/*.whl
```

CI 在 Python 3.9 / 3.10 / 3.11 / 3.12 跑离线检查、契约检查、wheel 安装与密钥卫生扫描。

## 已知限制

- 撤销只在当前会话内有效，退出后不能回滚。
- 微信读书网关是非公开接口，不可用时降级到本地划线文件。
- 工作台一次只允许一局陪练。
- 没有用户系统。这是单机工具，不是多租户服务。

## License

MIT。见 `LICENSE`。
