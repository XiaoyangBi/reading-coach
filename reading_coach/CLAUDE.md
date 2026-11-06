# L2 模块地图

reading_coach/
L2 | 父级: ../CLAUDE.md

`config` 与 `credentials` 不依赖任何包内模块。领域模块可以依赖它们，彼此不互相依赖。
`session` 编排领域模块。入口层装配 `session`，不重写教练逻辑。
`credentials` 是 BYOK 信任边界。`cli` 与 `web` 消费它产出的 `Provider`，不读密钥环境变量。
`web` 与 `scheduler` 是两个进程，共写 `coach.db`。两者都经 `config.connect` 取连接。
`test.py` 单向引用全部模块。没有模块引用 `test.py`。

## 成员清单

__init__.py: 包根。声明 `__version__` 与四层边界。不导入业务模块。
config.py: 运行契约。把环境变量翻译成路径、端口、主机、令牌、白名单与日志级别。导出 `connect` 与 `init_db`。不导入业务模块。
credentials.py: BYOK 凭证契约。唯一把环境变量翻译成模型与数据源凭证的位置。产出不可变 `Provider`，`api_key` 不参与 repr。不含第三方 SDK。
book.py: 书籍解析。把 epub/txt/md/pdf 折成章节与段落锚点，写入 `books/paragraphs`。重导入先预览锚点漂移，再交给 confirm 回调。
tools.py: 工具运行时。`Registry` 产出 schema，`run_tool` 把异常转成模型可读文本。每次调用先输出一句旁白。
loop.py: Agent 内层循环。推进消息与工具调用，用四个接缝把门禁留给 `session`。
context.py: 上下文预算。只打桩书籍原文类工具结果，不动问答记录。不做有损摘要。
recovery.py: 瞬时错误重试。只处理可重试状态码。不切换模型，不吞业务异常。
models.py: OpenAI 兼容适配层。翻译函数是纯函数，离线可测。厂商私有参数由凭证能力位决定，不写死厂商名。
coach.py: 教练 prompt、工具 schema 与代码边。计划先行、题量预算、费曼规则、深挖授权由代码裁决。
judge.py: 裁判子代理。每次用全新 messages，用 `tool_choice` 强制结构化判分。教练看不到裁判依据。
ledger.py: 掌握度账本与间隔重复状态机。教练视图与裁判记录分离。`scheduler` 只读它挑到期概念。
notes.py: 原话笔记写入器。按书名分文件，只追加不覆盖。撤销时由 `session` 按路径回滚。
tracelog.py: 事件日志与脱敏。按进程里全部凭证环境变量的实际值脱敏，覆盖任意形状的 BYOK 密钥。
highlights.py: 划线来源适配。微信读书网关失败就降级本地 txt，并返回来源标记。不中断陪练。
shelf.py: 书架模式适配。按书名解析 bookId，把用户划线折成 `Book`。
session.py: 编排层。把注册表、模型、账本、笔记、撤销栈接成一局陪练。控制词优先于教练意图。
cli.py: 终端入口。把 BYOK 凭证对象分发到 SDK 适配层，再装配 Session。`web` 复用它的 `make_models`，它不读密钥环境变量。
web.py: 浏览器入口。把 `say/ask` 映射成 SSE 事件与等待回复；Host/Origin 白名单加可选 Basic 令牌构成访问边界；页面只收到 BYOK 非敏感模型描述。
scheduler.py: 每日推送与反馈台账。跑完即退。选概念走规则不调模型。`web` 复用它的查询接口。
test.py: 唯一离线验收入口。用脚本化假模型与临时库跑端到端会话，并检查 BYOK 凭证契约。CI 与 console_scripts 都调用它。
static/index.html: 单页工作台。内联 CSS 与 JS。用 SSE 接事件，用 fetch 回答案，页脚显示当前 BYOK 模型。不加载外部资源。

# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
